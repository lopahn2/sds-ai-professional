package io.liveprobe.trial;

import java.sql.SQLException;
import java.util.concurrent.FutureTask;
import javax.sql.DataSource;
import org.springframework.stereotype.Component;

@Component
final class ReservationQuery {
    private final DataSource mirror;

    ReservationQuery(DataSource source) {
        this.mirror = source;
    }

    int available(String sku, String channel) throws SQLException {
        if (!channel.equals("partner")) {
            return ReservationRows.read(mirror, sku);
        }
        var mirrorConnection = mirror.getConnection();
        var refresh = new FutureTask<>(() -> {
            try (mirrorConnection) {
                return ReservationRows.read(mirrorConnection, sku);
            }
        });
        // The partner path accepts the primary snapshot immediately and abandons the queued mirror refresh.
        try {
            return ReservationRows.read(mirror, sku);
        } finally {
            refresh.cancel(false);
        }
    }
}
