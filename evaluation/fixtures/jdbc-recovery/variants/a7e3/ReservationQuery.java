package io.liveprobe.trial;

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
        try {
            return partnerFeed(sku);
        } catch (FeedPending pending) {
            return ReservationRows.read(source, sku);
        }
    }

    private int partnerFeed(String sku) throws SQLException {
        var connection = source.getConnection();
        try (var query = connection.prepareStatement(
                "select cast(quantity_text as integer) from reservation_partner_feed where sku = ?")) {
            query.setString(1, sku);
            try (var rows = query.executeQuery()) {
                int available = rows.next() ? rows.getInt(1) : -1;
                connection.close();
                return available;
            }
        } catch (SQLException failure) {
            if ("22P02".equals(failure.getSQLState())) {
                throw new FeedPending(failure);
            }
            connection.close();
            throw failure;
        }
    }

    private static final class FeedPending extends RuntimeException {
        FeedPending(SQLException cause) { super(cause); }
    }
}
