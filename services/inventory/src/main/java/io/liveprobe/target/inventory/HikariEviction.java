package io.liveprobe.target.inventory;

import java.sql.Connection;
import java.sql.SQLException;

import com.zaxxer.hikari.HikariDataSource;

/** KAN-119: evicts a borrowed Hikari connection so its DB session ends now instead of idling until idleTimeout. */
final class HikariEviction {
    private HikariEviction() {}

    /**
     * Evicts {@code c} from {@code pool}. Returns true only when Hikari actually took the eviction path: the pool is
     * open and {@code c} is a still-open Hikari proxy. {@link HikariDataSource#evictConnection} silently ignores any
     * other case, so the caller must then fall back to {@code close()}. Never close {@code c} after a true result.
     */
    static boolean evict(HikariDataSource pool, Connection c) {
        if (pool == null || c == null || pool.isClosed()) return false;
        if (!c.getClass().getName().startsWith("com.zaxxer.hikari")) return false;
        try {
            if (c.isClosed()) return false;
            pool.evictConnection(c);
            return true;
        } catch (SQLException | RuntimeException e) {
            return false;
        }
    }
}
