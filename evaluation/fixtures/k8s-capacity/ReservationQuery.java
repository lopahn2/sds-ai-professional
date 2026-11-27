package io.liveprobe.trial;

import java.sql.SQLException;
import java.sql.SQLTransientException;
import java.util.concurrent.Semaphore;
import java.util.concurrent.TimeUnit;
import javax.sql.DataSource;
import io.micrometer.core.instrument.MeterRegistry;
import io.micrometer.core.instrument.Timer;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;

@Component
final class ReservationQuery {
    private static final Logger LOGGER = LoggerFactory.getLogger(ReservationQuery.class);
    private final DataSource source;
    private final Semaphore capacity = new Semaphore(1, true);
    private final Timer databaseTime;

    ReservationQuery(DataSource source, MeterRegistry registry) {
        this.source = source;
        registry.gauge("reservation.queue.waiting", capacity, Semaphore::getQueueLength);
        databaseTime = registry.timer("reservation.database.execution");
    }

    int available(String sku, String channel) throws SQLException {
        long submitted = System.nanoTime();
        try {
            if (!capacity.tryAcquire(12, TimeUnit.SECONDS)) {
                throw new SQLTransientException("reservation worker queue timed out");
            }
        } catch (InterruptedException interrupted) {
            Thread.currentThread().interrupt();
            throw new SQLTransientException("reservation worker interrupted", interrupted);
        }
        try {
            long queueMillis = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - submitted);
            if (queueMillis >= 750) {
                LOGGER.warn("reservation request exceeded queue delay target: waiting_ms={}", queueMillis);
            }
            return readReservation(sku);
        } finally {
            capacity.release();
        }
    }

    private int readReservation(String sku) throws SQLException {
        long began = System.nanoTime();
        try (var connection = source.getConnection();
             var query = connection.prepareStatement("select pg_sleep(0.45)")) {
            query.setQueryTimeout(2);
            query.execute();
            return ReservationRows.read(connection, sku);
        } finally {
            databaseTime.record(System.nanoTime() - began, TimeUnit.NANOSECONDS);
        }
    }
}
