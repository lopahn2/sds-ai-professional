"""삼성 기반 생성기의 독립 오프라인 시험. 기존 시험 fixture는 읽지 않는다."""
import importlib.util
import io
import json
import os
import tarfile
import time
import uuid
import stat
import subprocess
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "scripts/samsung_infra.py"


class PublicManifestTests(unittest.TestCase):
    def test_public_bundle_is_complete_and_least_privileged(self):
        self.assertTrue(SOURCE.exists(), "기반 생성기 구현이 필요하다")
        spec = importlib.util.spec_from_file_location("samsung_infra", SOURCE)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        bundle = module.render()
        objects = bundle["manifest"]["items"]
        by_name = {(o["kind"], o["metadata"].get("namespace"), o["metadata"]["name"]): o for o in objects}
        kafka = by_name[("StatefulSet", "liveprobe-messaging", "kafka")]
        self.assertEqual(kafka["spec"]["replicas"], 1)
        container = kafka["spec"]["template"]["spec"]["containers"][0]
        env = {e["name"]: e["value"] for e in container["env"]}
        self.assertEqual(env["KAFKA_PROCESS_ROLES"], "broker,controller")
        self.assertEqual(env["KAFKA_SUPER_USERS"], "User:bootstrap-admin")
        self.assertEqual(env["KAFKA_AUTO_CREATE_TOPICS_ENABLE"], "false")
        self.assertIn("127.0.0.1:9093", env["KAFKA_LISTENERS"])
        self.assertEqual(container["resources"]["limits"]["memory"], "512Mi")
        self.assertIn("@sha256:", container["image"])
        self.assertEqual(kafka["spec"]["volumeClaimTemplates"][0]["spec"]["resources"]["requests"]["storage"], "4Gi")
        self.assertNotIn("hostPort", json.dumps(objects))
        self.assertFalse(any(o["kind"] == "Secret" for o in objects))
        self.assertEqual(len(bundle["topics"]), 6)
        for topic in bundle["topics"]:
            self.assertEqual(topic["partitions"], 1)
            self.assertEqual(topic["config"]["cleanup.policy"], "compact,delete")
            self.assertEqual(topic["config"]["retention.bytes"], 67108864)
            self.assertEqual(topic["config"]["max.message.bytes"], 65536)
        for env_name in ("dev", "stg", "prd"):
            acls = [a for a in bundle["acls"] if a["principal"] == f"User:business-{env_name}"]
            self.assertEqual({a["resource"] for a in acls}, {f"{env_name}.business.trade.master.v1", f"{env_name}.business.delivery.master.v1"})
            self.assertEqual({op for a in acls for op in a["operations"]}, {"Write", "Describe"})
            for target in ("trade", "delivery"):
                acls = [a for a in bundle["acls"] if a["principal"] == f"User:{target}-{env_name}"]
                self.assertEqual({a["resource"] for a in acls}, {f"{env_name}.business.{target}.master.v1", f"{env_name}.samsung-{target}.master.v1"})
                self.assertTrue(all(a["pattern"] == "literal" for a in acls))
        for system in ("trade", "delivery", "business"):
            pg = by_name[("StatefulSet", "liveprobe-data", f"{system}-db")]
            self.assertEqual(pg["spec"]["template"]["spec"]["securityContext"]["runAsUser"], 70)
            self.assertEqual(pg["spec"]["volumeClaimTemplates"][0]["spec"]["resources"]["requests"]["storage"], "2Gi")
            probes = pg["spec"]["template"]["spec"]["containers"][0]
            for key in ("startupProbe", "readinessProbe", "livenessProbe"):
                self.assertIn("127.0.0.1", probes[key]["exec"]["command"])
        refs = {(r["namespace"], r["name"]): r["keys"] for r in bundle["secret_refs"]}
        self.assertEqual(refs[("dev", "business-api-api")], ["credentials.json"])
        self.assertEqual(refs[("prd", "trade-member-api")], ["credentials.json", "reader-token"])
        self.assertEqual(refs[("stg", "samsung-inventory-db")], ["password"])
        self.assertNotIn("inventory-db", [r["name"] for r in bundle["secret_refs"]])


def load_module():
    spec = importlib.util.spec_from_file_location("samsung_infra", SOURCE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ExistingProtectionTests(unittest.TestCase):
    def test_initial_infra_does_not_isolate_existing_inventory(self):
        module = load_module()
        policies = [o for o in module.render()["manifest"]["items"] if o["kind"] == "NetworkPolicy" and o["metadata"]["name"] == "samsung-inventory-db"]
        self.assertEqual(len(policies), 3)
        for policy in policies:
            labels = policy["spec"]["podSelector"]["matchLabels"]
            self.assertEqual(labels.get("app"), "inventory")
            self.assertEqual(labels.get("liveprobe.io/database"), "samsung-trade", "기존 inventory를 즉시 격리하지 않도록 전환 label이 필요하다")


class BootstrapCompletionTests(unittest.TestCase):
    def test_restarted_partial_pgdata_is_not_ready_even_when_tcp_accepts(self):
        module = load_module()
        bundle = module.render()
        with tempfile.TemporaryDirectory(dir=Path.home() / ".hermes/cache/scratch") as temp:
            root = Path(temp)
            data, bootstrap, binaries = root / "data", root / "bootstrap", root / "bin"
            for directory in (data, bootstrap, binaries):
                directory.mkdir()
            (data / "PG_VERSION").write_text("17\n")
            expected = "samsung-postgres-bootstrap-v1:synthetic-sql-hash\n"
            (bootstrap / "bootstrap.contract").write_text(expected)
            ready = binaries / "pg_isready"
            ready.write_text("#!/bin/sh\nexit 0\n")
            ready.chmod(0o700)
            env = dict(os.environ, PGDATA=str(data), SAMSUNG_BOOTSTRAP_DIR=str(bootstrap),
                       PATH=str(binaries) + os.pathsep + os.environ["PATH"])
            for resource in bundle["manifest"]["items"]:
                if resource["kind"] != "StatefulSet" or resource["metadata"]["namespace"] != module.DATA:
                    continue
                container = resource["spec"]["template"]["spec"]["containers"][0]
                for key in ("startupProbe", "readinessProbe"):
                    command = container[key]["exec"]["command"]
                    with self.subTest(system=resource["metadata"]["name"], probe=key):
                        marker = data / ".samsung-bootstrap-complete"
                        marker.unlink(missing_ok=True)
                        self.assertNotEqual(subprocess.run(command, env=env, capture_output=True).returncode, 0,
                                            "TCP만 열리고 초기화가 미완료인 PVC는 준비됨이 아니어야 한다")
                        marker.write_text("samsung-postgres-bootstrap-v0:old-contract\n")
                        self.assertNotEqual(subprocess.run(command, env=env, capture_output=True).returncode, 0,
                                            "다른 SQL/계약의 완료 표식은 재사용하면 안 된다")
                        marker.write_text(expected)
                        self.assertEqual(subprocess.run(command, env=env, capture_output=True).returncode, 0,
                                         "같은 초기화 계약이 완료된 PVC 재기동은 준비되어야 한다")


    def test_completion_marker_follows_sql_success_and_matches_generated_contract(self):
        module = load_module()
        with tempfile.TemporaryDirectory(dir=Path.home() / ".hermes/cache/scratch") as temp:
            root = Path(temp)
            state = root / "state"
            module.prepare_private(state, module.render())
            items = json.loads((state / "secrets.json").read_bytes())["items"]
            secret = next(o for o in items if o["metadata"]["name"] == "samsung-trade-bootstrap")
            values = {k: __import__("base64").b64decode(v) for k, v in secret["data"].items()}
            self.assertTrue("bootstrap.contract" in values, "완료 표식을 현재 SQL·계약에 바인딩해야 한다")
            expected = ("samsung-postgres-bootstrap-v1:" + __import__("hashlib").sha256(values["bootstrap.sql"]).hexdigest() + "\n").encode()
            self.assertEqual(values["bootstrap.contract"], expected)
            data, bootstrap, binaries = root / "data", root / "bootstrap", root / "bin"
            for directory in (data, bootstrap, binaries):
                directory.mkdir(mode=0o700)
            for key, value in values.items():
                module.private_file(bootstrap / key, value)
            psql = binaries / "psql"
            psql.write_text('#!/bin/sh\nif [ "${SYNTHETIC_PSQL_FAIL:-1}" = 1 ]; then exit 17; fi\nexit 0\n')
            psql.chmod(0o700)
            env = dict(os.environ, PGDATA=str(data), SAMSUNG_BOOTSTRAP_DIR=str(bootstrap),
                       PATH=str(binaries) + os.pathsep + os.environ["PATH"])
            command = ["/bin/sh", str(bootstrap / "10-init.sh")]
            marker = data / ".samsung-bootstrap-complete"
            failed = subprocess.run(command, env=env, capture_output=True)
            self.assertEqual(failed.returncode, 17)
            self.assertFalse(marker.exists(), "실패한 초기화에 완료 표식을 남기면 안 된다")
            completed = subprocess.run(command, env=dict(env, SYNTHETIC_PSQL_FAIL="0"), capture_output=True)
            self.assertEqual(completed.returncode, 0)
            self.assertEqual(marker.read_bytes(), expected)
            self.assertFalse(list(data.glob(".samsung-bootstrap-complete.tmp.*")))


class PrivateStateTests(unittest.TestCase):
    def test_private_generation_has_valid_tls_scoped_secrets_and_no_reuse(self):
        module = load_module()
        self.assertTrue(hasattr(module, "prepare_private"), "비공개 생성 구현이 필요하다")
        with tempfile.TemporaryDirectory(dir=Path.home() / ".hermes/cache/scratch") as temp:
            private = Path(temp) / "state"
            bundle = module.render()
            module.prepare_private(private, bundle)
            self.assertEqual(stat.S_IMODE(private.stat().st_mode), 0o700)
            for path in private.rglob("*"):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600)
            secrets = json.loads((private / "secrets.json").read_text())["items"]
            decoded = {(s["metadata"]["namespace"], s["metadata"]["name"]): {k: __import__("base64").b64decode(v) for k, v in s["data"].items()} for s in secrets}
            self.assertEqual(len(secrets), len(bundle["secret_refs"]))
            tokens = []
            for env in module.ENVS:
                business = json.loads(decoded[(env, "business-api-api")]["credentials.json"])["principals"]
                self.assertEqual({p["role"] for p in business}, {"business-admin", "trade-reader", "delivery-reader"})
                self.assertTrue(all(p["environment"] == env for p in business))
                for target in ("trade", "delivery"):
                    service = module.SERVICES[target]
                    principal = json.loads(decoded[(env, service + "-api")]["credentials.json"])["principals"]
                    self.assertEqual(len(principal), 1)
                    self.assertEqual(principal[0]["target_system"], f"samsung-{target}")
                    self.assertEqual(decoded[(env, service + "-api")]["reader-token"].decode(), principal[0]["token"])
                    self.assertIn(principal[0], business)
                for system, service in module.SERVICES.items():
                    client = json.loads(decoded[(env, service + "-kafka")]["client.json"])
                    self.assertEqual(client["security.protocol"], "SASL_SSL")
                    self.assertEqual(client["sasl.username"], f"{system}-{env}")
                    self.assertNotIn("bootstrap-admin", json.dumps(client))
                    tokens.append(client["sasl.password"])
                    self.assertIn(f"/{system}_{env}", decoded[(env, service + "-db")]["dsn"].decode())
                    tokens += [p["token"] for p in json.loads(decoded[(env, service + "-api")]["credentials.json"])["principals"]]
                sql = decoded[(module.DATA, "samsung-trade-bootstrap")]["bootstrap.sql"].decode()
                self.assertIn(f"CREATE DATABASE inventory_{env}", sql)
                self.assertIn("NOSUPERUSER NOCREATEDB NOCREATEROLE", sql)
                self.assertIn("REVOKE CONNECT ON DATABASE postgres, template0, template1 FROM PUBLIC", sql)
            public = json.dumps(bundle)
            self.assertTrue(all(value not in public for value in tokens))
            ca = private / "pki/ca.crt"
            cert = private / "pki/business-api-dev.crt"
            result = subprocess.run(["openssl", "verify", "-CAfile", str(ca), str(cert)], capture_output=True)
            self.assertEqual(result.returncode, 0)
            san = subprocess.run(["openssl", "x509", "-in", str(cert), "-noout", "-text"], capture_output=True).stdout
            self.assertIn(b"DNS:business-api.dev.svc", san)
            broker = subprocess.run(["openssl", "x509", "-in", str(private / "pki/broker.crt"), "-noout", "-text"], capture_output=True).stdout
            self.assertIn(b"DNS:kafka.liveprobe-messaging.svc", broker)
            self.assertIn(b"IP Address:127.0.0.1", broker)
            before = (private / "secrets.json").read_bytes()
            with self.assertRaises(module.InfraError):
                module.prepare_private(private, bundle)
            self.assertEqual(before, (private / "secrets.json").read_bytes())


class ExecutionTests(unittest.TestCase):
    def test_execute_is_explicit_metadata_only_and_stops_before_rotation(self):
        module = load_module()
        self.assertTrue(hasattr(module, "execute"), "실행 CLI 구현이 필요하다")
        bundle = module.render()
        calls = []
        created = set()

        def fake(args, payload=None, timeout=120):
            calls.append((args, payload))
            if args[0] == "docker":
                return json.dumps([args[3]]).encode()
            self.assertEqual(args[:3], ["kubectl", "--context", "test-context"])
            action = args[3]
            if action == "get":
                self.assertIn("name", args)
                self.assertNotIn("json", args)
                target = args[4]
                if target.startswith("namespace/") and target.split("/")[1] in module.ENVS:
                    return target.encode()
                return target.encode() if target in created else b""
            if action == "create":
                body = json.loads(payload)
                created.add(body["kind"].lower() + "/" + body["metadata"]["name"])
                return b""
            if action == "rollout":
                return b""
            self.fail("허용되지 않은 외부 명령")

        with tempfile.TemporaryDirectory(dir=Path.home() / ".hermes/cache/scratch") as temp:
            private = Path(temp) / "state"
            result = module.execute(bundle, "test-context", private, runner=fake)
            self.assertEqual(result["state"], "created_and_rollout_ready")
            self.assertEqual(len(result["created"]), len(bundle["manifest"]["items"]) + len(bundle["secret_refs"]))
            count = len(calls)
            with self.assertRaises(module.InfraError):
                module.execute(bundle, "test-context", private, runner=fake)
            self.assertEqual(len(calls), count)
            for args, payload in calls:
                self.assertFalse(any(word in args for word in ("apply", "delete", "exec", "patch")))
                if "create" in args:
                    self.assertEqual(args[-2:], ["-o", "name"])
                    self.assertIsNotNone(payload)
            api_secret = next(json.loads(p) for a, p in calls if "create" in a and p and json.loads(p).get("kind") == "Secret")
            values = list(api_secret["data"].values())
            self.assertTrue(all(v not in json.dumps([a for a, _ in calls]) for v in values))

    def test_existing_secret_name_refuses_before_generating_private_state(self):
        module = load_module()
        self.assertTrue(hasattr(module, "execute"), "기존 이름 보호 구현이 필요하다")
        calls = []

        def fake(args, payload=None, timeout=120):
            calls.append(args)
            if args[0] == "docker":
                return json.dumps([args[3]]).encode()
            if args[4] in ("namespace/dev", "namespace/stg", "namespace/prd", "secret/business-api-db"):
                return args[4].encode()
            return b""

        with tempfile.TemporaryDirectory(dir=Path.home() / ".hermes/cache/scratch") as temp:
            private = Path(temp) / "state"
            with self.assertRaises(module.InfraError):
                module.execute(module.render(), "test-context", private, runner=fake)
            self.assertFalse(private.exists())
            self.assertTrue(all("create" not in a for a in calls))
            self.assertTrue(all("-o" not in a or a[a.index("-o") + 1] == "name" for a in calls))

    def test_real_cli_render_and_existing_private_execute_do_not_call_external_tools(self):
        with tempfile.TemporaryDirectory(dir=Path.home() / ".hermes/cache/scratch") as temp:
            private, output = Path(temp) / "private", Path(temp) / "public.json"
            private.mkdir()
            env = dict(os.environ, PATH="/nonexistent")
            import sys
            result = subprocess.run([sys.executable, "-B", str(SOURCE), "--render", "--context", "offline", "--output", str(output), "--private-dir", str(private)], capture_output=True, env=env)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertTrue(output.is_file())
            self.assertEqual(list(private.iterdir()), [])
            result = subprocess.run([sys.executable, "-B", str(SOURCE), "--execute", "--context", "offline", "--output", str(Path(temp) / "execute.json"), "--private-dir", str(private)], capture_output=True, env=env)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn(b"Traceback", result.stderr)
            self.assertIn("기존 자격".encode(), result.stderr)

    def test_symlink_and_existing_public_outputs_are_refused(self):
        module = load_module()
        bundle = module.render()
        with tempfile.TemporaryDirectory(dir=Path.home() / ".hermes/cache/scratch") as temp:
            root = Path(temp)
            target = root / "original"
            target.write_text("합성 기존 파일")
            alias = root / "alias"
            alias.symlink_to(target)
            with self.assertRaises(module.InfraError):
                module.write_public(alias, bundle)
            self.assertEqual(target.read_text(), "합성 기존 파일")
            with self.assertRaises(module.InfraError):
                module.prepare_private(alias, bundle)
            with self.assertRaises(module.InfraError):
                module.write_public(target, bundle)
            original = os.umask(0)
            os.umask(original)
            with patch.object(module, "generate_pki", side_effect=module.InfraError("합성 생성 실패")):
                with self.assertRaises(module.InfraError):
                    module.prepare_private(root / "failed-private", bundle)
            after = os.umask(original)
            self.assertEqual(after, original)

    def test_execution_preserves_private_state_after_partial_failure(self):
        module = load_module()
        calls = []

        def fake(args, payload=None, timeout=120):
            calls.append(args)
            if args[0] == "docker":
                return json.dumps([args[3]]).encode()
            if "create" in args:
                raise module.InfraError("합성 생성 실패")
            if args[4].startswith("namespace/") and args[4].split("/")[1] in module.ENVS:
                return args[4].encode()
            return b""

        with tempfile.TemporaryDirectory(dir=Path.home() / ".hermes/cache/scratch") as temp:
            private = Path(temp) / "private"
            with self.assertRaises(module.InfraError):
                module.execute(module.render(), "test-context", private, runner=fake)
            self.assertTrue((private / "secrets.json").exists())
            self.assertTrue((private / "attempt.json").exists())
            self.assertFalse((private / "completed.json").exists())
            before = len(calls)
            with self.assertRaises(module.InfraError):
                module.execute(module.render(), "test-context", private, runner=fake)
            self.assertEqual(before, len(calls))

    def test_subprocess_error_output_never_reaches_exception(self):
        module = load_module()
        self.assertTrue(hasattr(module, "run_quiet"))
        fixture = "SYNTHETIC_SECRET_FIXTURE_8af53"
        with patch.object(module.subprocess, "run", return_value=subprocess.CompletedProcess([], 37, fixture.encode(), fixture.encode())):
            with self.assertRaises(module.InfraError) as caught:
                module.run_quiet(["synthetic-tool"])
        self.assertNotIn(fixture, str(caught.exception))
        self.assertIn("37", str(caught.exception))


@unittest.skipUnless(os.environ.get("SAMSUNG_INFRA_DOCKER_TEST") == "1", "자체 Docker 합성 시험은 명시적으로만 실행한다")
class DockerRuntimeTests(unittest.TestCase):
    def test_isolated_kafka_tls_acl_and_postgres_boundaries(self):
        module = load_module()
        bundle = module.render()
        label = "samsung-infra-test-" + uuid.uuid4().hex
        names, volume_names = [], []
        scratch = Path.home() / ".hermes/cache/scratch"
        with tempfile.TemporaryDirectory(dir=scratch) as temp:
            private = Path(temp) / "private"
            module.prepare_private(private, bundle)
            secret_objects = json.loads((private / "secrets.json").read_bytes())["items"]
            secret_map = {(o["metadata"]["namespace"], o["metadata"]["name"]): {k: __import__("base64").b64decode(v) for k, v in o["data"].items()} for o in secret_objects}

            def run(args, data=None, timeout=120):
                result = subprocess.run(args, input=data, capture_output=True, timeout=timeout)
                return result

            def archive(container, directories, files, uid):
                stream = io.BytesIO()
                with tarfile.open(fileobj=stream, mode="w") as tar:
                    for directory in directories:
                        info = tarfile.TarInfo(directory)
                        info.type, info.mode, info.uid, info.gid = tarfile.DIRTYPE, 0o700, uid, uid
                        tar.addfile(info)
                    for name, data in files.items():
                        info = tarfile.TarInfo(name)
                        info.mode, info.uid, info.gid, info.size = (0o600 if name.endswith(".pgpass") else 0o440), uid, uid, len(data)
                        tar.addfile(info, io.BytesIO(data))
                self.assertEqual(run(["docker", "cp", "-", container + ":/"], stream.getvalue()).returncode, 0)

            def create(name, spec, memory, uid):
                names.append(name)
                args = ["docker", "create", "--name", name, "--label", "samsung.test.owner=" + label,
                        "--memory", memory, "--memory-swap", memory, "--cpus", "0.5", "--network", "none", "--log-driver", "none", "--user", f"{uid}:{uid}",
                        "--hostname", name, "--add-host", name + ":127.0.0.1"]
                if uid == 1000:
                    args += ["--add-host", "kafka.liveprobe-messaging.svc:127.0.0.1", "--add-host", "wrong-broker.example.internal:127.0.0.1"]
                for env in spec["env"]:
                    args += ["--env", env["name"] + "=" + env["value"]]
                args += [spec["image"]]
                args += spec.get("command", spec.get("args", []))
                self.assertEqual(run(args).returncode, 0, "자체 컨테이너 생성 실패")
                info = json.loads(run(["docker", "inspect", name]).stdout)[0]
                volume_names.extend(m["Name"] for m in info["Mounts"] if m["Type"] == "volume")

            def execute(name, args, payload=None, timeout=120):
                return run(["docker", "exec", "-i", name, *args], payload, timeout)

            try:
                kafka = next(o for o in bundle["manifest"]["items"] if o["kind"] == "StatefulSet" and o["metadata"]["name"] == "kafka")
                spec = json.loads(json.dumps(kafka["spec"]["template"]["spec"]["containers"][0]))
                spec["command"][-1] = spec["command"][-1].replace(">/dev/null 2>&1", ">>/tmp/samsung-test.log 2>&1")
                name = label + "-kafka"
                create(name, spec, "512m", 1000)
                files = {"etc/kafka/secrets/" + key: value for key, value in secret_map[(module.MESSAGING, "samsung-kafka-bootstrap")].items()}
                cm = next(o for o in bundle["manifest"]["items"] if o["kind"] == "ConfigMap")
                files["opt/samsung/bootstrap.sh"] = cm["data"]["bootstrap.sh"].encode()
                for system in module.SERVICES:
                    for env in module.ENVS:
                        c = json.loads(secret_map[(env, module.SERVICES[system] + "-kafka")]["client.json"])
                        properties = f'security.protocol=SASL_SSL\nsasl.mechanism=PLAIN\nssl.truststore.type=PEM\nssl.truststore.location=/etc/kafka/secrets/ca.crt\nssl.endpoint.identification.algorithm=https\nsasl.jaas.config=org.apache.kafka.common.security.plain.PlainLoginModule required username="{c["sasl.username"]}" password="{c["sasl.password"]}";\nrequest.timeout.ms=5000\ndefault.api.timeout.ms=10000\n'
                        files[f"opt/samsung/{system}-{env}.properties"] = properties.encode()
                files["opt/samsung/untrusted.properties"] = b"\n".join(line for line in files["opt/samsung/business-dev.properties"].splitlines() if not line.startswith(b"ssl.truststore.")) + b"\n"
                archive(name, ["etc/kafka/secrets", "opt/samsung", "var/lib/kafka/data"], files, 1000)
                self.assertEqual(run(["docker", "start", name]).returncode, 0)
                deadline = time.monotonic() + 300
                ready = False
                while time.monotonic() < deadline:
                    state = json.loads(run(["docker", "inspect", "--format", "{{json .State}}", name]).stdout)
                    self.assertTrue(state["Running"], "합성 Kafka가 준비 전 종료했다")
                    ready = execute(name, ["test", "-f", "/tmp/samsung-ready"]).returncode == 0
                    if ready:
                        break
                    if deadline - time.monotonic() < 260:
                        diagnostic = execute(name, ["cat", "/tmp/samsung-test.log"]).stdout.decode(errors="replace")
                        import re
                        classes = sorted(set(re.findall(r"[A-Za-z.$]+(?:Exception|Error)\b", diagnostic)))
                        if classes:
                            hosts = sorted(set(re.findall(r"UnknownHostException: ([A-Za-z0-9_.:-]+)", diagnostic)))
                            self.fail("합성 Kafka 진단 오류 유형: " + ", ".join(classes) + "; 미해석 이름: " + ", ".join(hosts))
                    time.sleep(2)
                self.assertTrue(ready, "512Mi Kafka 준비 제한시간 초과")
                small = ["env", "KAFKA_HEAP_OPTS=-Xms32m -Xmx64m", "KAFKA_OPTS=", "KAFKA_JMX_OPTS=-Dcom.sun.management.jmxremote=false"]
                def kafka_cli(tool, principal, args, payload=None):
                    option = "--producer.config" if tool == "kafka-console-producer.sh" else "--consumer.config" if tool == "kafka-console-consumer.sh" else "--command-config"
                    return execute(name, small + ["/opt/kafka/bin/" + tool, "--bootstrap-server", module.BROKER, option, f"/opt/samsung/{principal}.properties", *args], payload, 60)
                for target in ("trade", "delivery"):
                    topic = f"dev.business.{target}.master.v1"
                    produced = kafka_cli("kafka-console-producer.sh", "business-dev", ["--topic", topic, "--producer-property", "acks=all", "--producer-property", "enable.idempotence=true", "--property", "parse.key=true", "--property", "key.separator=:"], b"employee-1:synthetic-payload\n")
                    self.assertEqual(produced.returncode, 0, "허용 프로듀서 발행 실패")
                    consumed = kafka_cli("kafka-console-consumer.sh", target + "-dev", ["--topic", topic, "--group", f"dev.samsung-{target}.master.v1", "--from-beginning", "--max-messages", "1", "--timeout-ms", "15000"])
                    self.assertEqual(consumed.returncode, 0, "자기 대상 소비 실패")
                    self.assertIn(b"synthetic-payload", consumed.stdout)
                denied = kafka_cli("kafka-topics.sh", "trade-dev", ["--describe", "--topic", "dev.business.delivery.master.v1"])
                self.assertNotEqual(denied.returncode, 0, "다른 대상 토픽을 조회했다")
                denied_env = kafka_cli("kafka-topics.sh", "trade-dev", ["--describe", "--topic", "stg.business.trade.master.v1"])
                self.assertNotEqual(denied_env.returncode, 0, "다른 환경 토픽을 조회했다")
                denied_group = kafka_cli("kafka-console-consumer.sh", "trade-dev", ["--topic", "dev.business.trade.master.v1", "--group", "wrong-group", "--max-messages", "1", "--timeout-ms", "10000"])
                self.assertIn(b"GroupAuthorizationException", denied_group.stderr + denied_group.stdout, "다른 그룹 거절 진단이 없다")
                self.assertNotIn(b"synthetic-payload", denied_group.stdout)
                denied_write = kafka_cli("kafka-console-producer.sh", "trade-dev", ["--topic", "dev.business.trade.master.v1", "--producer-property", "enable.idempotence=false", "--producer-property", "max.block.ms=5000", "--producer-property", "delivery.timeout.ms=5000", "--producer-property", "request.timeout.ms=3000"], b"synthetic-denied\n")
                self.assertIn(b"TopicAuthorizationException", denied_write.stderr + denied_write.stdout)
                denied_create = kafka_cli("kafka-topics.sh", "business-dev", ["--create", "--topic", "dev.unauthorized", "--partitions", "1", "--replication-factor", "1"])
                self.assertNotEqual(denied_create.returncode, 0)
                untrusted = kafka_cli("kafka-topics.sh", "untrusted", ["--list"])
                self.assertNotEqual(untrusted.returncode, 0)
                self.assertIn(b"SslAuthenticationException", untrusted.stderr + untrusted.stdout)
                bad_hostname = execute(name, small + ["/opt/kafka/bin/kafka-topics.sh", "--bootstrap-server", "wrong-broker.example.internal:9092", "--command-config", "/opt/samsung/business-dev.properties", "--list"], timeout=60)
                self.assertNotEqual(bad_hostname.returncode, 0)
                self.assertIn(b"SslAuthenticationException", bad_hostname.stderr + bad_hostname.stdout)
                stats = run(["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", name])
                self.assertEqual(stats.returncode, 0)
                print("합성 Kafka: TLS/SASL 기동·두 대상 발행/소비·대상/환경/그룹/쓰기/생성 거절 통과; 자원 " + stats.stdout.decode().strip())
                self.assertEqual(run(["docker", "rm", "-f", "-v", name]).returncode, 0)
                names.remove(name)
                for system in ("trade", "delivery", "business"):
                    pg = next(o for o in bundle["manifest"]["items"] if o["kind"] == "StatefulSet" and o["metadata"]["name"] == system + "-db")
                    pg_spec = pg["spec"]["template"]["spec"]["containers"][0]
                    name = label + "-" + system
                    create(name, pg_spec, "256m", 70)
                    values = secret_map[(module.DATA, "samsung-" + system + "-bootstrap")]
                    files = {"var/run/samsung/bootstrap/" + key: value for key, value in values.items()}
                    files["docker-entrypoint-initdb.d/10-init.sh"] = values["10-init.sh"]
                    from urllib.parse import urlsplit
                    dsn = secret_map[("dev", module.SERVICES[system] + "-db")]["dsn"].decode()
                    password = urlsplit(dsn).password
                    self.assertIsNotNone(password)
                    files["var/run/samsung/bootstrap/fixture.pgpass"] = f"*:*:*:{system}_dev:{password}\n".encode()
                    archive(name, ["var/run/samsung/bootstrap", "docker-entrypoint-initdb.d", "var/lib/postgresql/data"], files, 70)
                    self.assertEqual(run(["docker", "start", name]).returncode, 0)
                    deadline = time.monotonic() + 45
                    while time.monotonic() < deadline:
                        result = execute(name, ["pg_isready", "-h", "127.0.0.1", "-U", "postgres"])
                        if result.returncode == 0:
                            # 초기 서버와 최종 서버 사이의 간격을 피하고 role 존재로 완료를 확인한다.
                            result = execute(name, ["psql", "-U", "postgres", "-Atc", f"SELECT count(*) FROM pg_roles WHERE rolname='{system}_dev'"])
                            if result.returncode == 0 and result.stdout.strip() == b"1":
                                break
                        time.sleep(1)
                    self.assertEqual(result.stdout.strip(), b"1", "합성 Postgres 초기화 실패")
                    query = f"SELECT rolname,rolsuper,rolcreatedb,rolcreaterole,rolconnlimit FROM pg_roles WHERE rolname IN ('{system}_dev','{system}_stg','{system}_prd') ORDER BY rolname"
                    roles = execute(name, ["psql", "-U", "postgres", "-Atc", query])
                    self.assertEqual(roles.returncode, 0)
                    self.assertEqual(len(roles.stdout.strip().splitlines()), 3)
                    self.assertTrue(all(b"|f|f|f|6" in row for row in roles.stdout.splitlines()))
                    boundary = execute(name, ["psql", "-U", "postgres", "-Atc", f"SELECT has_database_privilege('{system}_dev','{system}_stg','CONNECT'),has_database_privilege('{system}_dev','postgres','CONNECT'),has_database_privilege('{system}_dev','{system}_dev','CONNECT')"])
                    self.assertEqual(boundary.stdout.strip(), b"f|f|t")
                    own_db = execute(name, ["env", "PGPASSFILE=/var/run/samsung/bootstrap/fixture.pgpass", "psql", "-w", "-h", "127.0.0.1", "-U", system + "_dev", "-d", system + "_dev", "-Atc", "SELECT current_database()"])
                    self.assertEqual(own_db.returncode, 0, "자기 DB SCRAM 접속 실패")
                    self.assertEqual(own_db.stdout.strip(), (system + "_dev").encode())
                    other_db = execute(name, ["env", "PGPASSFILE=/var/run/samsung/bootstrap/fixture.pgpass", "psql", "-w", "-h", "127.0.0.1", "-U", system + "_dev", "-d", system + "_stg", "-Atc", "SELECT 1"])
                    self.assertNotEqual(other_db.returncode, 0, "다른 환경 DB에 접속했다")
                    self.assertIn(b"permission denied", other_db.stderr)
                    self.assertEqual(run(["docker", "rm", "-f", "-v", name]).returncode, 0)
                    names.remove(name)
                print("합성 Postgres: 3개 인스턴스 UID70·256Mi 초기화, 환경별 role/DB 및 교차 CONNECT 거절 통과")
            finally:
                for name in names:
                    run(["docker", "rm", "-f", "-v", name])
                for name in names:
                    self.assertNotEqual(run(["docker", "inspect", name]).returncode, 0)
                for volume in volume_names:
                    self.assertNotEqual(run(["docker", "volume", "inspect", volume]).returncode, 0, "자체 익명 볼륨 정리 실패")
                leftovers = run(["docker", "ps", "-aq", "--filter", "label=samsung.test.owner=" + label])
                self.assertEqual(leftovers.stdout.strip(), b"", "자체 컨테이너 정리 실패")
                print("정리 확인: 자체 label 컨테이너·익명 볼륨 잔존 없음")


if __name__ == "__main__":
    unittest.main()
