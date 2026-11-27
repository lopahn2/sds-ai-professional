package io.liveprobe.trial;

import java.sql.Connection;
import java.sql.SQLException;
import java.util.Spliterator;
import java.util.Spliterators;
import java.util.function.IntConsumer;
import java.util.stream.IntStream;
import java.util.stream.StreamSupport;
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
        return snapshots(sku).findFirst().orElse(-1);
    }

    private IntStream snapshots(String sku) throws SQLException {
        var connection = source.getConnection();
        try {
            var query = connection.prepareStatement(ReservationRows.QUERY);
            query.setString(1, sku);
            var rows = query.executeQuery();
            var cursor = new Spliterators.AbstractIntSpliterator(Long.MAX_VALUE, Spliterator.ORDERED) {
                @Override
                public boolean tryAdvance(IntConsumer consumer) {
                    try {
                        if (!rows.next()) {
                            close(connection);
                            return false;
                        }
                        consumer.accept(rows.getInt(1));
                        return true;
                    } catch (SQLException failure) {
                        close(connection);
                        throw new IllegalStateException(failure);
                    }
                }
            };
            return StreamSupport.intStream(cursor, false).onClose(() -> close(connection));
        } catch (SQLException failure) {
            connection.close();
            throw failure;
        }
    }

    private static void close(Connection connection) {
        try {
            connection.close();
        } catch (SQLException failure) {
            throw new IllegalStateException(failure);
        }
    }
}
