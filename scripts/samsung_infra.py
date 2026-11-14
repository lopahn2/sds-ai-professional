#!/usr/bin/env python3
"""기존 기반과 비밀을 변경하지 않는 삼성 실험 기반 생성기."""
import base64
import hashlib
import json
import os
import secrets
import subprocess
import uuid
from pathlib import Path
import re
import shlex

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "deploy/samsung/infra-template.json"
CATALOG = ROOT / "deploy/samsung/system-catalog.json"
ENVS = ("dev", "stg", "prd")
SERVICES = {"business": "business-api", "trade": "trade-member", "delivery": "delivery-member"}
MESSAGING = "liveprobe-messaging"
DATA = "liveprobe-data"
BROKER = "kafka.liveprobe-messaging.svc:9092"


class InfraError(Exception):
    """비밀값 없는 공개 오류만 전달한다."""


def obj(kind, name, namespace=None, **fields):
    meta = {"name": name, "labels": {"liveprobe.io/managed": "true", "liveprobe.io/infra": "samsung"}}
    if namespace:
        meta["namespace"] = namespace
    return {"apiVersion": {"StatefulSet": "apps/v1", "NetworkPolicy": "networking.k8s.io/v1"}.get(kind, "v1"),
            "kind": kind, "metadata": meta, **fields}


def secret_refs():
    refs = [{"namespace": MESSAGING, "name": "samsung-kafka-bootstrap", "keys": ["broker.p12", "store-password", "kafka-jaas.conf", "adminclient.properties", "ca.crt", "cluster-id"]}]
    for system in SERVICES:
        refs.append({"namespace": DATA, "name": f"samsung-{system}-bootstrap", "keys": ["admin-password", "bootstrap.sql", "bootstrap.contract", "10-init.sh"]})
    for env in ENVS:
        for service in SERVICES.values():
            for suffix, keys in (("db", ["dsn"]), ("kafka", ["client.json", "ca.crt"]),
                                 ("api", ["credentials.json"] + ([] if service == "business-api" else ["reader-token"])),
                                 ("tls", ["ca.crt", "tls.crt", "tls.key"])):
                refs.append({"namespace": env, "name": f"{service}-{suffix}", "keys": keys})
        refs.append({"namespace": env, "name": "samsung-inventory-db", "keys": ["password"]})
    return refs


def topics_and_acls(catalog):
    topics, acls = [], []
    for env in ENVS:
        for target in catalog["kafka"]["targets"]:
            topic = catalog["kafka"]["topic_template"].format(environment=env, target=target)
            topics.append({"name": topic, "partitions": 1, "replication_factor": 1,
                           "config": {"cleanup.policy": "compact,delete", "retention.bytes": 67108864,
                                      "retention.ms": 259200000, "max.message.bytes": 65536,
                                      "segment.bytes": 16777216, "segment.ms": 3600000,
                                      "min.cleanable.dirty.ratio": 0.2, "min.insync.replicas": 1}})
            acls.extend([
                {"principal": f"User:business-{env}", "type": "topic", "resource": topic, "operations": ["Write", "Describe"], "pattern": "literal"},
                {"principal": f"User:{target}-{env}", "type": "topic", "resource": topic, "operations": ["Read", "Describe"], "pattern": "literal"},
                {"principal": f"User:{target}-{env}", "type": "group", "resource": f"{env}.samsung-{target}.master.v1", "operations": ["Read"], "pattern": "literal"},
            ])
    return topics, acls


def bootstrap_script(topics, acls):
    lines = ["#!/bin/sh", "set -eu", "export KAFKA_HEAP_OPTS='-Xms32m -Xmx64m'",
             "export KAFKA_OPTS=''", "export KAFKA_JMX_OPTS='-Dcom.sun.management.jmxremote=false'"]
    common = ["--bootstrap-server", BROKER, "--command-config", "/etc/kafka/secrets/adminclient.properties"]
    for topic in topics:
        args = ["/opt/kafka/bin/kafka-topics.sh", *common, "--create", "--if-not-exists", "--topic", topic["name"],
                "--partitions", "1", "--replication-factor", "1"]
        for key, value in topic["config"].items():
            args += ["--config", f"{key}={value}"]
        lines.append(shlex.join(args))
    for acl in acls:
        args = ["/opt/kafka/bin/kafka-acls.sh", *common, "--add", "--allow-principal", acl["principal"],
                "--resource-pattern-type", "literal", "--" + acl["type"], acl["resource"]]
        for op in acl["operations"]:
            args += ["--operation", op]
        lines.append(shlex.join(args))
    return "\n".join(lines) + "\n"


def pvc(size, storage_class):
    return {"metadata": {"name": "data"}, "spec": {"accessModes": ["ReadWriteOnce"], "storageClassName": storage_class,
            "resources": {"requests": {"storage": size}}}}


def dns_rule():
    return {"to": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "kube-system"}},
                    "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}],
            "ports": [{"protocol": p, "port": 53} for p in ("UDP", "TCP")]}


def peer(namespace, app):
    return {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": namespace}},
            "podSelector": {"matchLabels": {"app": app}}}


def policy(name, namespace, app, ingress=None, egress=None):
    spec = {"podSelector": {"matchLabels": {"app": app}}, "policyTypes": []}
    for key, value in (("ingress", ingress), ("egress", egress)):
        if value is not None:
            spec["policyTypes"].append(key.capitalize())
            spec[key] = value
    return obj("NetworkPolicy", name, namespace, spec=spec)


def kafka_objects(config, topics, acls):
    env = {
        "KAFKA_PROCESS_ROLES": "broker,controller", "KAFKA_NODE_ID": "1",
        "KAFKA_LISTENERS": "SASL_SSL://0.0.0.0:9092,CONTROLLER://127.0.0.1:9093",
        "KAFKA_ADVERTISED_LISTENERS": "SASL_SSL://" + BROKER,
        "KAFKA_LISTENER_SECURITY_PROTOCOL_MAP": "SASL_SSL:SASL_SSL,CONTROLLER:SASL_SSL",
        "KAFKA_CONTROLLER_LISTENER_NAMES": "CONTROLLER", "KAFKA_CONTROLLER_QUORUM_VOTERS": "1@127.0.0.1:9093",
        "KAFKA_INTER_BROKER_LISTENER_NAME": "SASL_SSL", "KAFKA_LOG_DIRS": "/var/lib/kafka/data/logs",
        "KAFKA_SASL_ENABLED_MECHANISMS": "PLAIN", "KAFKA_SASL_MECHANISM_INTER_BROKER_PROTOCOL": "PLAIN",
        "KAFKA_SASL_MECHANISM_CONTROLLER_PROTOCOL": "PLAIN", "KAFKA_AUTHORIZER_CLASS_NAME": "org.apache.kafka.metadata.authorizer.StandardAuthorizer",
        "KAFKA_SUPER_USERS": "User:bootstrap-admin", "KAFKA_ALLOW_EVERYONE_IF_NO_ACL_FOUND": "false",
        "KAFKA_AUTO_CREATE_TOPICS_ENABLE": "false", "KAFKA_NUM_PARTITIONS": "1",
        "KAFKA_DEFAULT_REPLICATION_FACTOR": "1", "KAFKA_MIN_INSYNC_REPLICAS": "1",
        "KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR": "1", "KAFKA_OFFSETS_TOPIC_NUM_PARTITIONS": "1",
        "KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR": "1", "KAFKA_TRANSACTION_STATE_LOG_MIN_ISR": "1",
        "KAFKA_TRANSACTION_STATE_LOG_NUM_PARTITIONS": "1", "KAFKA_GROUP_INITIAL_REBALANCE_DELAY_MS": "0",
        "KAFKA_LOG_CLEANER_THREADS": "1", "KAFKA_LOG_CLEANER_DEDUPE_BUFFER_SIZE": "16777216",
        "KAFKA_LOG_CLEANER_IO_BUFFER_SIZE": "131072", "KAFKA_NUM_NETWORK_THREADS": "2",
        "KAFKA_NUM_IO_THREADS": "2", "KAFKA_NUM_REPLICA_FETCHERS": "1", "KAFKA_BACKGROUND_THREADS": "2",
        "KAFKA_MESSAGE_MAX_BYTES": "65536", "KAFKA_LOG_SEGMENT_BYTES": "16777216",
        "KAFKA_LOG_RETENTION_BYTES": "67108864", "KAFKA_LOG_RETENTION_HOURS": "72",
        "KAFKA_SOCKET_REQUEST_MAX_BYTES": "1048576", "KAFKA_REPLICA_FETCH_MAX_BYTES": "1048576",
        "KAFKA_SSL_KEYSTORE_TYPE": "PKCS12", "KAFKA_SSL_KEYSTORE_FILENAME": "broker.p12",
        "KAFKA_SSL_KEY_CREDENTIALS": "store-password", "KAFKA_SSL_KEYSTORE_CREDENTIALS": "store-password",
        "KAFKA_SSL_TRUSTSTORE_TYPE": "PEM", "KAFKA_SSL_TRUSTSTORE_LOCATION": "/etc/kafka/secrets/ca.crt",
        "KAFKA_SSL_CLIENT_AUTH": "none", "KAFKA_SSL_ENDPOINT_IDENTIFICATION_ALGORITHM": "HTTPS",
        "KAFKA_HEAP_OPTS": "-Xms128m -Xmx256m", "KAFKA_JMX_OPTS": "-Dcom.sun.management.jmxremote=false",
        "KAFKA_OPTS": "-Djava.security.auth.login.config=/etc/kafka/secrets/kafka-jaas.conf",
    }
    # 공식 진입점의 표준출력에는 설정 값이 섞일 수 있어 브로커·초기화 출력을 폐기한다.
    entry = """set -eu
export CLUSTER_ID="$(cat /etc/kafka/secrets/cluster-id)"
/etc/kafka/docker/run >/dev/null 2>&1 &
pid=$!
trap 'kill -TERM "$pid" 2>/dev/null || true; wait "$pid" || true' TERM INT
n=0
until (KAFKA_HEAP_OPTS='-Xms32m -Xmx64m' KAFKA_OPTS='' /opt/kafka/bin/kafka-topics.sh --bootstrap-server kafka.liveprobe-messaging.svc:9092 --command-config /etc/kafka/secrets/adminclient.properties --list) >/dev/null 2>&1; do
  kill -0 "$pid" || exit 1
  n=$((n+1)); [ "$n" -lt 90 ] || exit 1
  sleep 2
done
/bin/sh /opt/samsung/bootstrap.sh >/dev/null 2>&1 || { kill -TERM "$pid"; exit 1; }
touch /tmp/samsung-ready
wait "$pid"
"""
    labels = {"app": "kafka", "liveprobe.io/infra": "samsung"}
    container = {"name": "kafka", "image": config["images"]["kafka"], "imagePullPolicy": "IfNotPresent",
                 "command": ["/bin/sh", "-c", entry], "env": [{"name": k, "value": v} for k, v in env.items()],
                 "ports": [{"name": "sasl-ssl", "containerPort": 9092}],
                 "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"cpu": "500m", "memory": config["kafka_memory_limit"]}},
                 "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
                 "volumeMounts": [{"name": "data", "mountPath": "/var/lib/kafka/data"}, {"name": "credentials", "mountPath": "/etc/kafka/secrets", "readOnly": True},
                                  {"name": "bootstrap", "mountPath": "/opt/samsung", "readOnly": True}],
                 "startupProbe": {"exec": {"command": ["/bin/sh", "-c", "test -f /tmp/samsung-ready"]}, "periodSeconds": 10, "failureThreshold": 120},
                 "readinessProbe": {"exec": {"command": ["/bin/sh", "-c", "test -f /tmp/samsung-ready"]}, "periodSeconds": 10},
                 "livenessProbe": {"tcpSocket": {"port": 9092}, "periodSeconds": 20}}
    sts = obj("StatefulSet", "kafka", MESSAGING, spec={"replicas": 1, "serviceName": "kafka-headless", "selector": {"matchLabels": labels},
              "template": {"metadata": {"labels": labels}, "spec": {"automountServiceAccountToken": False, "terminationGracePeriodSeconds": 60,
                 "securityContext": {"runAsNonRoot": True, "runAsUser": 1000, "runAsGroup": 1000, "fsGroup": 1000},
                 "containers": [container], "volumes": [{"name": "credentials", "secret": {"secretName": "samsung-kafka-bootstrap", "defaultMode": 288}},
                 {"name": "bootstrap", "configMap": {"name": "samsung-kafka-bootstrap"}}]}},
              "volumeClaimTemplates": [pvc("4Gi", config["storage_class"])]})
    objects = [obj("ConfigMap", "samsung-kafka-bootstrap", MESSAGING, data={"bootstrap.sh": bootstrap_script(topics, acls)}), sts]
    for name in ("kafka", "kafka-headless"):
        spec = {"selector": labels, "ports": [{"name": "sasl-ssl", "port": 9092, "targetPort": 9092}]}
        if name.endswith("headless"):
            spec.update(clusterIP="None", publishNotReadyAddresses=True)
        objects.append(obj("Service", name, MESSAGING, spec=spec))
    # 브로커 자신의 서비스 주소도 초기 부트스트랩 중 해석·접속되어야 한다.
    objects[-2]["spec"]["publishNotReadyAddresses"] = True
    allowed = [peer(env_name, service) for env_name in ENVS for service in SERVICES.values()]
    objects.append(policy("samsung-kafka-boundary", MESSAGING, "kafka",
                          ingress=[{"from": allowed + [peer(MESSAGING, "kafka")], "ports": [{"protocol": "TCP", "port": 9092}]}],
                          egress=[dns_rule(), {"to": [peer(MESSAGING, "kafka")], "ports": [{"protocol": "TCP", "port": 9092}]}]))
    return objects


def postgres_ready_command():
    """TCP 수락과 영속 초기화 계약의 일치를 함께 요구한다."""
    script = '''set -eu
bootstrap_dir="${SAMSUNG_BOOTSTRAP_DIR:-/var/run/samsung/bootstrap}"
test -f "$PGDATA/.samsung-bootstrap-complete"
cmp -s "$PGDATA/.samsung-bootstrap-complete" "$bootstrap_dir/bootstrap.contract"
exec pg_isready "$@"
'''
    return ["/bin/sh", "-ec", script, "samsung-postgres-ready", "-h", "127.0.0.1", "-U", "postgres"]


def postgres_objects(config):
    objects = []
    for system, service in SERVICES.items():
        name, secret = f"{system}-db", f"samsung-{system}-bootstrap"
        labels = {"app": name, "liveprobe.io/infra": "samsung"}
        container = {"name": "postgres", "image": config["images"]["postgres"], "imagePullPolicy": "IfNotPresent",
                     "args": ["postgres", "-c", "shared_buffers=16MB", "-c", "max_connections=40", "-c", "password_encryption=scram-sha-256",
                              "-c", "log_statement=none", "-c", "log_min_error_statement=panic"],
                     "env": [{"name": "POSTGRES_PASSWORD_FILE", "value": "/var/run/samsung/bootstrap/admin-password"},
                             {"name": "PGDATA", "value": "/var/lib/postgresql/data/pgdata"},
                             {"name": "POSTGRES_INITDB_ARGS", "value": "--auth-host=scram-sha-256 --auth-local=trust"}],
                     "ports": [{"name": "postgres", "containerPort": 5432}],
                     "resources": {"requests": {"cpu": "50m", "memory": "128Mi"}, "limits": {"cpu": "500m", "memory": config["postgres_memory_limit"]}},
                     "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
                     "volumeMounts": [{"name": "data", "mountPath": "/var/lib/postgresql/data"},
                                      {"name": "bootstrap", "mountPath": "/var/run/samsung/bootstrap", "readOnly": True},
                                      {"name": "init", "mountPath": "/docker-entrypoint-initdb.d", "readOnly": True}],
                     "startupProbe": {"exec": {"command": postgres_ready_command()}, "periodSeconds": 5, "timeoutSeconds": 5, "failureThreshold": 60},
                     "readinessProbe": {"exec": {"command": postgres_ready_command()}, "periodSeconds": 10, "timeoutSeconds": 5},
                     "livenessProbe": {"exec": {"command": ["pg_isready", "-h", "127.0.0.1", "-U", "postgres"]}, "periodSeconds": 20, "timeoutSeconds": 5}}
        objects += [obj("Service", name, DATA, spec={"selector": labels, "ports": [{"name": "postgres", "port": 5432, "targetPort": 5432}]}),
                    obj("StatefulSet", name, DATA, spec={"replicas": 1, "serviceName": name, "selector": {"matchLabels": labels},
                        "template": {"metadata": {"labels": labels}, "spec": {"automountServiceAccountToken": False,
                            "securityContext": {"runAsNonRoot": True, "runAsUser": 70, "runAsGroup": 70, "fsGroup": 70},
                            "containers": [container], "volumes": [{"name": "bootstrap", "secret": {"secretName": secret, "defaultMode": 288}},
                             {"name": "init", "secret": {"secretName": secret, "defaultMode": 288, "items": [{"key": "10-init.sh", "path": "10-init.sh"}]}}]}},
                        "volumeClaimTemplates": [pvc("2Gi", config["storage_class"])]})]
        allowed = [peer(e, service) for e in ENVS]
        if system == "trade":
            allowed += [peer(e, "inventory") for e in ENVS]
        objects.append(policy(f"samsung-{name}-boundary", DATA, name,
                              ingress=[{"from": allowed, "ports": [{"protocol": "TCP", "port": 5432}]}], egress=[dns_rule()]))
    for env in ENVS:
        for system, service in SERVICES.items():
            egress = [dns_rule(), {"to": [peer(DATA, f"{system}-db")], "ports": [{"protocol": "TCP", "port": 5432}]},
                      {"to": [peer(MESSAGING, "kafka")], "ports": [{"protocol": "TCP", "port": 9092}]}]
            if system != "business":
                egress += [{"to": [peer(env, "business-api")], "ports": [{"protocol": "TCP", "port": 8443}]}]
            objects.append(policy(f"samsung-{service}-infra", env, service, egress=egress))
        inventory_policy = policy("samsung-inventory-db", env, "inventory",
                                  egress=[dns_rule(), {"to": [peer(DATA, "trade-db")], "ports": [{"protocol": "TCP", "port": 5432}]}])
        # 이관 전 원본 inventory가 새 DB 전용 정책으로 격리되지 않게 한다.
        # 부모가 전환 완료한 새 Pod에만 이 label을 추가한다.
        inventory_policy["spec"]["podSelector"]["matchLabels"]["liveprobe.io/database"] = "samsung-trade"
        objects.append(inventory_policy)
    return objects


def render():
    config = json.loads(TEMPLATE.read_text())
    catalog = json.loads(CATALOG.read_text())
    if tuple(catalog["environments"]) != ENVS or catalog["domain_modes"] != {v: k for k, v in SERVICES.items()}:
        raise InfraError("카탈로그 환경·서비스 계약이 일치하지 않는다")
    if catalog["kafka"]["bootstrap"] != BROKER or set(catalog["kafka"]["targets"]) != {"trade", "delivery"}:
        raise InfraError("카탈로그 Kafka 계약이 일치하지 않는다")
    for image in config["images"].values():
        if not re.fullmatch(r"(?:apache/kafka|postgres)@sha256:[0-9a-f]{64}", image):
            raise InfraError("실제 이미지 RepoDigest 고정이 필요하다")
    if config["kafka_memory_limit"] not in ("512Mi", "768Mi") or config["postgres_memory_limit"] not in ("256Mi", "384Mi"):
        raise InfraError("승인된 메모리 한도를 벗어났다")
    topics, acls = topics_and_acls(catalog)
    objects = [obj("Namespace", ns) for ns in (MESSAGING, DATA)]
    objects += kafka_objects(config, topics, acls) + postgres_objects(config)
    return {"apiVersion": "liveprobe.samsung/infra-v1", "manifest": {"apiVersion": "v1", "kind": "List", "items": objects},
            "topics": topics, "acls": acls, "secret_refs": secret_refs(),
            "inputs": {"catalog_sha256": hashlib.sha256(CATALOG.read_bytes()).hexdigest(),
                       "template_sha256": hashlib.sha256(TEMPLATE.read_bytes()).hexdigest()},
            "execution": "새 리소스만 create한다. 기존 이름·비공개 상태가 존재하면 거절한다. 실제 이관·앱 배포는 별도다."}


def run_quiet(args, payload=None, timeout=120):
    """외부 도구 출력·예외는 값이 있을 수 있어 절대 재출력하지 않는다."""
    try:
        result = subprocess.run(args, input=payload, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        raise InfraError("외부 명령을 실행하지 못했거나 제한시간이 지났다") from None
    if result.returncode:
        raise InfraError(f"외부 명령 실패: 종료코드 {result.returncode}")
    return result.stdout


def private_file(path, value):
    data = value.encode() if isinstance(value, str) else value
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)


def checked_new_directory(path):
    path = Path(path).absolute()
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            raise InfraError("비공개 경로의 심볼릭 링크는 허용하지 않는다")
    if path.exists():
        raise InfraError("비공개 상태가 이미 존재한다. 기존 자격을 읽거나 회전하지 않고 중단한다")
    if not path.parent.is_dir():
        raise InfraError("비공개 상위 디렉터리를 먼저 안전하게 준비해야 한다")
    path.mkdir(mode=0o700)
    return path


def generate_pki(directory, days):
    directory.mkdir(mode=0o700)
    ca_config = """[req]
prompt=no
distinguished_name=dn
x509_extensions=ca
[dn]
CN=Samsung Local Experiment CA
[ca]
basicConstraints=critical,CA:TRUE,pathlen:0
keyUsage=critical,keyCertSign,cRLSign
subjectKeyIdentifier=hash
"""
    private_file(directory / "ca.cnf", ca_config)
    run_quiet(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", str(days),
               "-keyout", str(directory / "ca.key"), "-out", str(directory / "ca.crt"), "-config", str(directory / "ca.cnf")])
    names = {"broker": ["kafka", "kafka.liveprobe-messaging", "kafka.liveprobe-messaging.svc",
                        "kafka.liveprobe-messaging.svc.cluster.local", "kafka-0.kafka-headless.liveprobe-messaging.svc",
                        "kafka-0.kafka-headless.liveprobe-messaging.svc.cluster.local", "localhost"]}
    for env in ENVS:
        for service in SERVICES.values():
            names[f"{service}-{env}"] = [service, f"{service}.{env}", f"{service}.{env}.svc", f"{service}.{env}.svc.cluster.local"]
    for name, dns_names in names.items():
        key, csr, cert, ext = (directory / (name + suffix) for suffix in (".key", ".csr", ".crt", ".cnf"))
        san = ",".join("DNS:" + dns for dns in dns_names)
        if name == "broker":
            san += ",IP:127.0.0.1"
        private_file(ext, "basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\nsubjectAltName=" + san + "\n")
        run_quiet(["openssl", "req", "-new", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                   "-out", str(csr), "-subj", "/CN=" + dns_names[0]])
        run_quiet(["openssl", "x509", "-req", "-in", str(csr), "-CA", str(directory / "ca.crt"),
                   "-CAkey", str(directory / "ca.key"), "-CAcreateserial", "-days", str(days),
                   "-out", str(cert), "-extfile", str(ext), "-sha256"])
        run_quiet(["openssl", "verify", "-CAfile", str(directory / "ca.crt"), str(cert)])
    private_file(directory / "store-password", secrets.token_hex(24))
    run_quiet(["openssl", "pkcs12", "-export", "-in", str(directory / "broker.crt"), "-inkey", str(directory / "broker.key"),
               "-certfile", str(directory / "ca.crt"), "-out", str(directory / "broker.p12"), "-name", "broker",
               "-passout", "file:" + str(directory / "store-password"), "-keypbe", "AES-256-CBC", "-certpbe", "AES-256-CBC", "-macalg", "SHA256"])


def postgres_init_script():
    """전체 SQL 성공 후에만 현재 계약을 원자적으로 영속화한다."""
    return '''#!/bin/sh
set -eu
umask 077
bootstrap_dir="${SAMSUNG_BOOTSTRAP_DIR:-/var/run/samsung/bootstrap}"
marker="$PGDATA/.samsung-bootstrap-complete"
test ! -e "$marker"
test -f "$bootstrap_dir/bootstrap.contract"
psql -v ON_ERROR_STOP=1 --username postgres --dbname postgres -f "$bootstrap_dir/bootstrap.sql" >/dev/null 2>&1
temporary="$marker.tmp.$$"
trap 'rm -f "$temporary"' EXIT
cp "$bootstrap_dir/bootstrap.contract" "$temporary"
chmod 600 "$temporary"
mv "$temporary" "$marker"
'''


def sql_bootstrap(system, passwords):
    lines = ["\\set ON_ERROR_STOP on", "REVOKE CONNECT ON DATABASE postgres, template0, template1 FROM PUBLIC;"]
    for name, password in passwords.items():
        if not re.fullmatch(r"(?:trade|business|delivery|inventory)_(?:dev|stg|prd)", name) or not re.fullmatch(r"[0-9a-f]{48}", password):
            raise InfraError("DB 초기화 내부 형식이 일치하지 않는다")
        limit = 4 if name.startswith("inventory_") else 6
        lines += [f"CREATE ROLE {name} LOGIN PASSWORD '{password}' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS CONNECTION LIMIT {limit};",
                  f"CREATE DATABASE {name} OWNER {name} TEMPLATE template0;", f"REVOKE ALL ON DATABASE {name} FROM PUBLIC;",
                  f"GRANT CONNECT, TEMPORARY ON DATABASE {name} TO {name};", f"ALTER ROLE {name} SET idle_in_transaction_session_timeout='60s';",
                  f"\\connect {name}", "REVOKE ALL ON SCHEMA public FROM PUBLIC;", f"ALTER SCHEMA public OWNER TO {name};",
                  f"GRANT USAGE, CREATE ON SCHEMA public TO {name};", "\\connect postgres"]
    return "\n".join(lines) + "\n"


def prepare_private(path, bundle):
    """신규 상태만 만든다. 이미 존재하는 상태의 내용은 읽지 않는다."""
    path = checked_new_directory(path)
    old_umask = os.umask(0o077)
    try:
        config = json.loads(TEMPLATE.read_text())
        pki = path / "pki"
        generate_pki(pki, config["certificate_days"])
        ca = (pki / "ca.crt").read_bytes()
        items = []

        def add(namespace, name, values):
            data = {key: base64.b64encode(value.encode() if isinstance(value, str) else value).decode() for key, value in values.items()}
            items.append(obj("Secret", name, namespace, type="Opaque", immutable=True, data=data))

        kafka_passwords = {f"{system}-{env}": secrets.token_hex(24) for env in ENVS for system in SERVICES}
        admin = secrets.token_hex(24)
        jaas = 'KafkaServer { org.apache.kafka.common.security.plain.PlainLoginModule required\n username="bootstrap-admin"\n password="' + admin + '"\n user_bootstrap-admin="' + admin + '"\n'
        jaas += "\n".join(f' user_{name}="{password}"' for name, password in kafka_passwords.items()) + ";\n};\n"
        client = 'security.protocol=SASL_SSL\nsasl.mechanism=PLAIN\nssl.truststore.type=PEM\nssl.truststore.location=/etc/kafka/secrets/ca.crt\nssl.endpoint.identification.algorithm=https\nsasl.jaas.config=org.apache.kafka.common.security.plain.PlainLoginModule required username="bootstrap-admin" password="' + admin + '";\n'
        add(MESSAGING, "samsung-kafka-bootstrap", {"broker.p12": (pki / "broker.p12").read_bytes(),
            "store-password": (pki / "store-password").read_bytes(), "ca.crt": ca, "kafka-jaas.conf": jaas,
            "adminclient.properties": client, "cluster-id": base64.urlsafe_b64encode(uuid.uuid4().bytes).decode().rstrip("=")})
        db_passwords = {}
        for system in SERVICES:
            names = [f"{system}_{env}" for env in ENVS]
            if system == "trade":
                names += [f"inventory_{env}" for env in ENVS]
            passwords = {name: secrets.token_hex(24) for name in names}
            db_passwords.update(passwords)
            sql = sql_bootstrap(system, passwords)
            contract = "samsung-postgres-bootstrap-v1:" + hashlib.sha256(sql.encode()).hexdigest() + "\n"
            add(DATA, f"samsung-{system}-bootstrap", {"admin-password": secrets.token_hex(24),
                "bootstrap.sql": sql, "bootstrap.contract": contract,
                "10-init.sh": postgres_init_script()})
        for env in ENVS:
            principals = [{"token": secrets.token_hex(32), "role": "business-admin", "environment": env, "target_system": None}]
            principals += [{"token": secrets.token_hex(32), "role": target + "-reader", "environment": env, "target_system": "samsung-" + target} for target in ("trade", "delivery")]
            for system, service in SERVICES.items():
                role = f"{system}_{env}"
                add(env, service + "-db", {"dsn": f"postgresql://{role}:{db_passwords[role]}@{system}-db.liveprobe-data.svc:5432/{role}"})
                kafka_client = {"bootstrap.servers": BROKER, "security.protocol": "SASL_SSL", "sasl.mechanism": "PLAIN",
                                "sasl.username": f"{system}-{env}", "sasl.password": kafka_passwords[f"{system}-{env}"],
                                "ssl.ca.location": "/var/run/samsung/kafka/ca.crt", "ssl.endpoint.identification.algorithm": "https"}
                add(env, service + "-kafka", {"client.json": json.dumps(kafka_client), "ca.crt": ca})
                own = principals if system == "business" else [p for p in principals if p["role"] == system + "-reader"]
                api = {"credentials.json": json.dumps({"principals": own})}
                if system != "business":
                    api["reader-token"] = own[0]["token"]
                add(env, service + "-api", api)
                add(env, service + "-tls", {"ca.crt": ca, "tls.crt": (pki / f"{service}-{env}.crt").read_bytes(),
                                           "tls.key": (pki / f"{service}-{env}.key").read_bytes()})
            add(env, "samsung-inventory-db", {"password": db_passwords[f"inventory_{env}"]})
        actual = {(i["metadata"]["namespace"], i["metadata"]["name"]): sorted(i["data"]) for i in items}
        expected = {(r["namespace"], r["name"]): sorted(r["keys"]) for r in bundle["secret_refs"]}
        if actual != expected:
            raise InfraError("생성한 비밀 참조가 공개 계약과 일치하지 않는다")
        private_file(path / "secrets.json", json.dumps({"apiVersion": "v1", "kind": "List", "items": items}))
        private_file(path / "metadata.json", json.dumps({"inputs": bundle["inputs"], "secret_refs": bundle["secret_refs"], "state": "generated"}, ensure_ascii=False, indent=2))
    finally:
        os.umask(old_umask)


def execute(bundle, context, private_dir, runner=run_quiet):
    """새 이름만 create하고 정확한 대상의 이름과 rollout을 다시 확인한다."""
    private_dir = Path(private_dir).absolute()
    if private_dir.exists() or private_dir.is_symlink():
        raise InfraError("비공개 상태가 이미 존재한다. 기존 자격을 읽거나 회전하지 않고 중단한다")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:@/-]{0,127}", context):
        raise InfraError("명시적인 유효 context가 필요하다")
    prefix = ["kubectl", "--context", context]

    def get_name(kind, name, namespace=None):
        args = prefix + ["get", kind.lower() + "/" + name]
        if namespace:
            args += ["-n", namespace]
        return runner(args + ["--ignore-not-found=true", "-o", "name"]).strip()

    # 이미지 ID나 임의 지문이 아니라 로컬 엔진의 실제 RepoDigest를 대조한다.
    images = {c["image"] for o in bundle["manifest"]["items"] if o["kind"] == "StatefulSet"
              for c in o["spec"]["template"]["spec"]["containers"]}
    for image in sorted(images):
        try:
            digests = json.loads(runner(["docker", "image", "inspect", image, "--format", "{{json .RepoDigests}}"] ))
        except (ValueError, TypeError):
            raise InfraError("로컬 이미지 RepoDigest 조회 형식이 올바르지 않다") from None
        if image not in digests:
            raise InfraError("로컬 실제 RepoDigest와 선언 이미지가 다르다")
    namespaces = {ns for ns in (*ENVS, MESSAGING, DATA) if get_name("namespace", ns)}
    if not set(ENVS).issubset(namespaces):
        raise InfraError("기존 dev/stg/prd namespace가 모두 있어야 한다")
    if namespaces.intersection((MESSAGING, DATA)):
        raise InfraError("기반 namespace가 이미 존재한다. 기존 기반을 채택하거나 변경하지 않고 중단한다")
    for resource in bundle["manifest"]["items"] + [obj("Secret", r["name"], r["namespace"]) for r in bundle["secret_refs"]]:
        meta = resource["metadata"]
        if resource["kind"] == "Namespace" or meta.get("namespace") not in namespaces:
            continue
        if get_name(resource["kind"], meta["name"], meta.get("namespace")):
            raise InfraError("생성 대상 이름이 이미 존재한다. 기존 리소스나 Secret을 읽거나 변경하지 않고 중단한다")
    prepare_private(private_dir, bundle)
    secret_objects = json.loads((private_dir / "secrets.json").read_bytes())["items"]
    public_objects = bundle["manifest"]["items"]
    ordered = [o for o in public_objects if o["kind"] == "Namespace"]
    ordered += secret_objects
    ordered += [o for o in public_objects if o["kind"] == "NetworkPolicy"]
    ordered += [o for o in public_objects if o["kind"] not in ("Namespace", "NetworkPolicy", "StatefulSet")]
    # 작은 DB부터 직렬 준비시켜 동시 초기화 피크를 줄인다.
    ordered += sorted([o for o in public_objects if o["kind"] == "StatefulSet"], key=lambda o: o["metadata"]["name"] == "kafka")
    receipt = {"state": "creating", "context": context, "inputs": bundle["inputs"], "created": []}
    private_file(private_dir / "attempt.json", json.dumps(receipt))
    for resource in ordered:
        meta = resource["metadata"]
        runner(prefix + ["create", "-f", "-", "-o", "name"], json.dumps(resource).encode())
        if not get_name(resource["kind"], meta["name"], meta.get("namespace")):
            raise InfraError("생성 직후 정확한 대상 이름 재조회에 실패했다. 부분 생성 상태를 보존한다")
        receipt["created"].append({"kind": resource["kind"], "namespace": meta.get("namespace"), "name": meta["name"]})
        if resource["kind"] == "StatefulSet":
            runner(prefix + ["rollout", "status", "statefulset/" + meta["name"], "-n", meta["namespace"], "--timeout=600s"], timeout=660)
            if not get_name("StatefulSet", meta["name"], meta["namespace"]):
                raise InfraError("준비 확인 후 StatefulSet 이름이 사라졌다")
    receipt["state"] = "created_and_rollout_ready"
    private_file(private_dir / "completed.json", json.dumps(receipt, ensure_ascii=False, indent=2))
    return receipt


def write_public(path, bundle):
    """비밀이 없는 선언만 신규 공개 파일로 저장한다. 기존 파일은 덮지 않는다."""
    path = Path(path).absolute()
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            raise InfraError("출력 경로의 심볼릭 링크는 허용하지 않는다")
    if path.exists() or not path.parent.is_dir():
        raise InfraError("공개 출력은 기존 파일이 아닌 준비된 상위 디렉터리 안 새 경로여야 한다")
    private_file(path, json.dumps(bundle, ensure_ascii=False, indent=2) + "\n")


def main(argv=None):
    import argparse
    import sys
    parser = argparse.ArgumentParser(description="삼성 실험 기반 선언 생성 및 명시적 신규 초기화")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--render", action="store_true", help="비밀 없는 선언만 생성; 외부 명령·자격 생성 없음")
    mode.add_argument("--execute", action="store_true", help="신규 비공개 자격 생성 및 새 기반만 create")
    parser.add_argument("--context", default="rancher-desktop", help="모든 kubectl 명령에 전달할 명시적 context")
    parser.add_argument("--output", required=True, help="신규 공개 결과 JSON 경로")
    parser.add_argument("--private-dir", help="실행 때 새로 생성할 비공개 상태 경로; 기존 경로는 거절")
    args = parser.parse_args(argv)
    try:
        bundle = render()
        if args.execute and not args.private_dir:
            raise InfraError("--execute에는 --private-dir 새 경로가 필요하다")
        if args.private_dir and (Path(args.output).absolute() == Path(args.private_dir).absolute() or Path(args.private_dir).absolute() in Path(args.output).absolute().parents):
            raise InfraError("공개 출력과 비공개 상태 경로를 분리해야 한다")
        # 출력 오류를 외부 변경 전에 확인한다. 선언 자체에는 비밀이 없다.
        write_public(args.output, bundle)
        if args.execute:
            receipt = execute(bundle, args.context, args.private_dir)
            print(json.dumps({"state": receipt["state"], "created_count": len(receipt["created"]), "output": str(Path(args.output).absolute())}, ensure_ascii=False))
        else:
            print(json.dumps({"state": "rendered", "resource_count": len(bundle["manifest"]["items"]), "secret_ref_count": len(bundle["secret_refs"]), "output": str(Path(args.output).absolute())}, ensure_ascii=False))
        return 0
    except InfraError as exc:
        print("오류: " + str(exc), file=sys.stderr)
        return 2
    except Exception:
        # 도구·경로·파서 예외에 포함될 수 있는 자격값과 traceback도 출력하지 않는다.
        print("오류: 내부 처리 실패. 비공개 상태를 보존하고 중단했다", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
