"""Stage a real JDBC workload whose capacity is controlled by Kubernetes configuration."""

from argparse import ArgumentParser
import json
from pathlib import Path
import shutil

from evaluation.matched_recovery_campaign import canonical_digest
from evaluation.prepare_jdbc_fixture import FIXTURE, JAVA_PACKAGE


QUERY = """package io.liveprobe.trial;

import java.sql.SQLException;
import javax.sql.DataSource;
import org.springframework.stereotype.Component;

@Component
final class ReservationQuery {
    private final DataSource source;

    ReservationQuery(DataSource source) {
        this.source = source;
    }

    int available(String sku, String channel) throws SQLException {
        try (var connection = source.getConnection()) {
            if (channel.equals("partner")) {
                try (var upstream = connection.prepareStatement("select pg_sleep(0.55)")) {
                    upstream.setQueryTimeout(2);
                    upstream.execute();
                }
            }
            return ReservationRows.read(connection, sku);
        }
    }
}
"""
CONFIG = Path("src/main/resources/application.properties")
POOL_KEY = "spring.datasource.hikari.maximum-pool-size"
WAIT_KEY = "spring.datasource.hikari.connection-timeout"


def prepare(destination: Path) -> dict:
    destination = destination.resolve()
    if destination.is_relative_to(FIXTURE.parents[2]) or not destination.parent.is_dir():
        raise ValueError("build the fixture in a new external directory")
    common = FIXTURE / "common"
    properties = (common / CONFIG).read_text()
    if properties.count(POOL_KEY + "=4\n") != 1 or properties.count(WAIT_KEY + "=1000\n") != 1:
        raise ValueError("review the shared datasource properties before modifying the fixture")
    shutil.copytree(common, destination)
    (destination / CONFIG).write_text(
        properties.replace(POOL_KEY + "=4\n", POOL_KEY + "=${RESERVATION_POOL_MAX:4}\n")
                  .replace(WAIT_KEY + "=1000\n", WAIT_KEY + "=250\n")
    )
    (destination / JAVA_PACKAGE / "ReservationQuery.java").write_text(QUERY)
    source = {p.relative_to(destination).as_posix(): p.read_text() for p in sorted(destination.rglob("*")) if p.is_file()}
    return {"source_digest": canonical_digest(source), "config_key": "RESERVATION_POOL_MAX",
            "healthy_pool": 4, "faulted_pool": 1, "query_hold_ms": 550, "connection_wait_ms": 250}


if __name__ == "__main__":
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    print(json.dumps(prepare(parser.parse_args().destination)))
