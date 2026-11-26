package com.acme.shopfront.stock;

import org.springframework.boot.context.properties.ConfigurationProperties;

/**
 * partnerLedger: route partner-channel reservations through the partner allocation ledger.
 * ledgerRelease: which ledger implementation is deployed ("1.4.2" shipped, "1.4.3" patched).
 * scoringRounds: CPU work of the allocation plan, computed while the reservation row is locked.
 */
@ConfigurationProperties(prefix = "shopfront.stock")
public record StockSettings(boolean partnerLedger, String ledgerRelease, int scoringRounds) {
    public StockSettings {
        if (ledgerRelease == null || ledgerRelease.isBlank()) {
            ledgerRelease = "1.4.2";
        }
        if (scoringRounds <= 0) {
            scoringRounds = 2000;
        }
    }
}
