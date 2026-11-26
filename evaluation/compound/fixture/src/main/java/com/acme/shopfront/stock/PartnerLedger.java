package com.acme.shopfront.stock;

import java.sql.Connection;
import java.sql.SQLException;
import javax.sql.DataSource;
import org.springframework.context.annotation.Profile;
import org.springframework.stereotype.Component;

/**
 * Partner allocations are consumed under the partner's own quota.
 * Release 1.4.2 manages the connection by hand and only cleans up on SQLException; the hold path throws an
 * unchecked exception past that cleanup. Release 1.4.3 is the patched build.
 */
@Component
@Profile("stock")
class PartnerLedger {
    private final DataSource source;
    private final StockSettings settings;

    PartnerLedger(DataSource source, StockSettings settings) {
        this.source = source;
        this.settings = settings;
    }

    Reservation reserve(String sku, int quantity) throws SQLException {
        return "1.4.3".equals(settings.ledgerRelease()) ? reservePatched(sku, quantity) : reserveShipped(sku, quantity);
    }

    private Reservation reserveShipped(String sku, int quantity) throws SQLException {
        Connection connection = source.getConnection();
        try {
            connection.setAutoCommit(false);
            String status = status(connection, sku);
            if ("HOLD".equals(status)) {
                throw new PartnerAllocationHeld(sku);
            }
            int remaining = consume(connection, sku, quantity);
            connection.commit();
            connection.close();
            return Reservation.reserved(sku, remaining);
        } catch (SQLException failure) {
            connection.rollback();
            connection.close();
            throw failure;
        }
    }

    private Reservation reservePatched(String sku, int quantity) throws SQLException {
        try (Connection connection = source.getConnection()) {
            connection.setAutoCommit(false);
            try {
                String status = status(connection, sku);
                if ("HOLD".equals(status)) {
                    connection.rollback();
                    throw new PartnerAllocationHeld(sku);
                }
                int remaining = consume(connection, sku, quantity);
                connection.commit();
                return Reservation.reserved(sku, remaining);
            } catch (SQLException failure) {
                connection.rollback();
                throw failure;
            }
        }
    }

    private static String status(Connection connection, String sku) throws SQLException {
        try (var query = connection.prepareStatement("select status from partner_allocation where sku = ?")) {
            query.setString(1, sku);
            try (var rows = query.executeQuery()) {
                return rows.next() ? rows.getString(1) : "NONE";
            }
        }
    }

    private static int consume(Connection connection, String sku, int quantity) throws SQLException {
        try (var update = connection.prepareStatement(
                "update partner_allocation set allocatable = allocatable - ? where sku = ? and allocatable >= ? "
                        + "returning allocatable")) {
            update.setInt(1, quantity);
            update.setString(2, sku);
            update.setInt(3, quantity);
            try (var rows = update.executeQuery()) {
                return rows.next() ? rows.getInt(1) : -1;
            }
        }
    }
}
