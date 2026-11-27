package io.liveprobe.trial;

import java.sql.Connection;
import java.sql.SQLException;
import javax.sql.DataSource;

final class ReservationRows {
    static final String QUERY = "select available from reservation_stock where sku = ? order by recorded_at desc";

    private ReservationRows() {}

    static int read(DataSource source, String sku) throws SQLException {
        try (var connection = source.getConnection()) {
            return read(connection, sku);
        }
    }

    static int read(Connection connection, String sku) throws SQLException {
        try (var query = connection.prepareStatement(QUERY)) {
            query.setString(1, sku);
            try (var rows = query.executeQuery()) {
                return rows.next() ? rows.getInt(1) : -1;
            }
        }
    }
}
