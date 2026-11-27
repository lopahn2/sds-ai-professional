package io.liveprobe.trial;

import java.sql.Connection;
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
        if (!channel.equals("partner")) {
            return ReservationRows.read(source, sku);
        }
        var transaction = source.getConnection();
        boolean superseded = false;
        try {
            transaction.setAutoCommit(false);
            int available;
            try {
                available = latest(transaction, sku, 1);
            } catch (SQLException conflict) {
                if (!"40001".equals(conflict.getSQLState())) {
                    throw conflict;
                }
                transaction.rollback();
                superseded = true;
                return retryAtCurrentRevision(sku);
            }
            transaction.commit();
            return available;
        } finally {
            if (!superseded) {
                transaction.close();
            }
        }
    }

    private int retryAtCurrentRevision(String sku) throws SQLException {
        try (var retry = source.getConnection()) {
            retry.setAutoCommit(false);
            int available = latest(retry, sku, 2);
            retry.commit();
            return available;
        }
    }

    private static int latest(Connection connection, String sku, int revision) throws SQLException {
        try (var query = connection.prepareStatement("select reservation_available_latest(?, ?)")) {
            query.setString(1, sku);
            query.setInt(2, revision);
            try (var result = query.executeQuery()) {
                result.next();
                int available = result.getInt(1);
                return result.wasNull() ? -1 : available;
            }
        }
    }
}
