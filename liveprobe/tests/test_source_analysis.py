"""Source and configuration evidence: leak paths, call sites, profiles, retries, dependency access lines."""
import json
from pathlib import Path

from app import source_analysis as S

LEAKY = """package x;
class Ledger {
    Thing reserve(String sku) throws SQLException {
        return shipped(sku);
    }
    private Thing shipped(String sku) throws SQLException {
        Connection connection = source.getConnection();
        try {
            if (held(sku)) {
                throw new Held(sku);
            }
            connection.commit();
            connection.close();
            return Thing.ok();
        } catch (SQLException failure) {
            connection.rollback();
            connection.close();
            throw failure;
        }
    }
    private Thing safe(String sku) throws SQLException {
        try (Connection c = source.getConnection()) {
            return Thing.ok();
        }
    }
    private Thing alsoSafe(String sku) throws SQLException {
        Connection c = source.getConnection();
        try {
            return Thing.ok();
        } finally {
            c.close();
        }
    }
}
"""
CALLER = """package x;
class Service {
    private final Ledger ledger;
    Thing reserve(String sku, String channel) throws SQLException {
        if ("partner".equals(channel) && settings.ledgerEnabled()) {
            try {
                return ledger.reserve(sku);
            } catch (Held held) {
                return Thing.deferred();
            }
        }
        return Thing.ok();
    }
}
"""


def project(tmp_path: Path) -> Path:
    java = tmp_path / "src/main/java/x"
    java.mkdir(parents=True)
    (java / "Ledger.java").write_text(LEAKY)
    (java / "Service.java").write_text(CALLER)
    res = tmp_path / "src/main/resources"
    res.mkdir(parents=True)
    (res / "application.properties").write_text("spring.application.name=demo\nspring.datasource.password=s3cret\n")
    (res / "application-stock.properties").write_text("app.ledger-enabled=true\nspring.datasource.hikari.max-lifetime=1800000\n")
    (res / "application-order.properties").write_text("app.stock.retries=3\napp.stock.backoff-ms=0\n")
    (tmp_path / "pom.xml").write_text("<project><parent><version>9</version></parent><version>1.4.2</version></project>")
    return tmp_path


def test_finds_only_the_manual_connection_that_an_exception_path_leaks(tmp_path):
    root = project(tmp_path)
    leaks = S.connection_leaks(root)
    assert [(l["method"], l["exits_without_close"], l["caught"]) for l in leaks] == [
        ("shipped", ["throw new Held"], ["SQLException"])]
    sites = S.call_sites(root, leaks[0])
    assert sites[0]["file"].endswith("Service.java")
    assert any("settings.ledgerEnabled()" in line for line in sites[0]["excerpt"])
    assert any("catch (Held held)" in line for line in sites[0]["excerpt"])


def test_profiles_env_and_secrets(tmp_path):
    root = project(tmp_path)
    env = S.deployment_env({"spec": {"containers": [{"args": ["--spring.profiles.active=stock"],
        "env": [{"name": "DB_PASSWORD", "value": "x"}, {"name": "POOL", "value": "6"},
                {"name": "TOKEN", "valueFrom": {"secretKeyRef": {"name": "t"}}}]}]}})
    assert env == {"DB_PASSWORD": "<redacted>", "POOL": "6", "TOKEN": "<from reference>",
                   "args": "--spring.profiles.active=stock"}
    props = S.properties(root, S.active_profiles(env))
    assert set(props) == {"application.properties", "application-stock.properties"}
    assert "spring.datasource.password=<redacted>" in props["application.properties"]


def test_retry_without_backoff_honours_env_overrides(tmp_path):
    props = S.properties(project(tmp_path), ["order"])
    assert S.retry_settings(props, {}) == [{"setting": "app.stock.retries", "retries": 3, "backoff": "0",
                                           "kind": "retry_without_backoff"}]
    assert S.retry_settings(props, {"APP_STOCK_BACKOFF-MS": "300"}) == []


def test_access_lines_keep_the_enclosing_condition():
    script = ("set -e\nord=${HOSTNAME##*-}\nif [ \"$ord\" != 0 ]; then\n  basebackup\n"
              "  # replica profile: only the reporting role may connect over TCP\n"
              "  sed -i 's/^host all all all/host all reporting all/' pg_hba.conf\nfi\nexec postgres\n")
    lines = S.access_lines(script)
    assert lines[0].startswith("3: if [") and any("host all reporting all" in l for l in lines)


def test_registry_lookup_and_workload_view(tmp_path):
    root = project(tmp_path)
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({"sources": [{"image": "registry.local/demo/", "root": str(root), "name": "demo"}]}))
    registry = S.load_registry(reg)
    template = {"spec": {"containers": [{"image": "registry.local/demo/stock:1.4.2", "args": ["--spring.profiles.active=stock"]}]}}
    view = S.workload_source("registry.local/demo/stock:1.4.2", template, registry)
    assert view["version_matches_tag"] is True and view["source_version"] == "1.4.2"
    assert view["connection_leaks"][0]["method"] == "shipped"
    assert S.workload_source("other/img:1", template, registry)["source"] == "no registered source for this image"
    findings = S.source_findings({"workload": view, "callers": {"order": {"retry_settings": [
        {"setting": "app.stock.retries", "retries": 3, "backoff": "0"}]}},
        "dependencies": {"db": {"access_config": {"db-init/start.sh": ["7: host all reporting all"]}}}})
    assert [f["kind"] for f in findings] == ["source_connection_leak", "caller_retry_without_backoff",
                                            "dependency_access_config"]
