package com.acme.shopfront.stock;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.sql.Connection;
import java.sql.SQLException;
import javax.sql.DataSource;
import org.springframework.context.annotation.Profile;
import org.springframework.stereotype.Service;

@Service
@Profile("stock")
class ReservationService {
    private final DataSource source;
    private final PartnerLedger partners;
    private final StockSettings settings;

    ReservationService(DataSource source, PartnerLedger partners, StockSettings settings) {
        this.source = source;
        this.partners = partners;
        this.settings = settings;
    }

    Reservation reserve(String sku, String channel, int quantity) throws SQLException {
        if ("partner".equals(channel) && settings.partnerLedger()) {
            try {
                return partners.reserve(sku, quantity);
            } catch (PartnerAllocationHeld held) {
                return Reservation.deferred(sku);
            }
        }
        try (Connection connection = source.getConnection()) {
            connection.setAutoCommit(false);
            try {
                int available = lock(connection, sku);
                if (available < quantity) {
                    connection.rollback();
                    return Reservation.reserved(sku, -1);
                }
                long plan = plan(sku, quantity, available);
                int remaining = apply(connection, sku, channel, quantity, plan);
                connection.commit();
                return Reservation.reserved(sku, remaining);
            } catch (SQLException failure) {
                connection.rollback();
                throw failure;
            }
        }
    }

    private static int lock(Connection connection, String sku) throws SQLException {
        try (var query = connection.prepareStatement("select available from stock_item where sku = ? for update")) {
            query.setString(1, sku);
            try (var rows = query.executeQuery()) {
                return rows.next() ? rows.getInt(1) : 0;
            }
        }
    }

    /** Allocation plan across warehouses; deterministic CPU work done while the row is locked. */
    private long plan(String sku, int quantity, int available) {
        try {
            MessageDigest digest = MessageDigest.getInstance("SHA-256");
            byte[] state = (sku + ':' + quantity + ':' + available).getBytes(StandardCharsets.UTF_8);
            for (int round = 0; round < settings.scoringRounds(); round++) {
                state = digest.digest(state);
            }
            return state[0] & 0xff;
        } catch (NoSuchAlgorithmException impossible) {
            throw new IllegalStateException(impossible);
        }
    }

    private static int apply(Connection connection, String sku, String channel, int quantity, long plan)
            throws SQLException {
        int remaining;
        try (var update = connection.prepareStatement(
                "update stock_item set available = available - ? where sku = ? returning available")) {
            update.setInt(1, quantity);
            update.setString(2, sku);
            try (var rows = update.executeQuery()) {
                remaining = rows.next() ? rows.getInt(1) : -1;
            }
        }
        try (var insert = connection.prepareStatement(
                "insert into reservation_log (sku, channel, quantity, plan) values (?, ?, ?, ?)")) {
            insert.setString(1, sku);
            insert.setString(2, channel);
            insert.setInt(3, quantity);
            insert.setLong(4, plan);
            insert.executeUpdate();
        }
        return remaining;
    }
}
