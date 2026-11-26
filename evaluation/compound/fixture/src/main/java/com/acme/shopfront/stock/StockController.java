package com.acme.shopfront.stock;

import java.sql.SQLException;
import java.sql.SQLTransientConnectionException;
import java.util.Map;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.context.annotation.Profile;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

@RestController
@Profile("stock")
class StockController {
    private static final Logger log = LoggerFactory.getLogger(StockController.class);
    private final ReservationService reservations;

    StockController(ReservationService reservations) {
        this.reservations = reservations;
    }

    @PostMapping("/api/stock/{sku}/reservations")
    ResponseEntity<Map<String, Object>> reserve(@PathVariable String sku,
                                                @RequestParam(defaultValue = "web") String channel,
                                                @RequestParam(defaultValue = "1") int quantity) {
        try {
            Reservation result = reservations.reserve(sku, channel, quantity);
            HttpStatus status = switch (result.outcome()) {
                case "reserved" -> HttpStatus.CREATED;
                case "deferred" -> HttpStatus.ACCEPTED;
                default -> HttpStatus.CONFLICT;
            };
            return ResponseEntity.status(status).body(Map.of("sku", sku, "outcome", result.outcome()));
        } catch (SQLTransientConnectionException unavailable) {
            log.error("reservation failed sku={} channel={}: {}", sku, channel, unavailable.getMessage());
            return ResponseEntity.status(HttpStatus.SERVICE_UNAVAILABLE).body(Map.of("sku", sku, "error", "database unavailable"));
        } catch (SQLException failure) {
            log.error("reservation failed sku={} channel={} sqlstate={}", sku, channel, failure.getSQLState(), failure);
            return ResponseEntity.status(HttpStatus.INTERNAL_SERVER_ERROR).body(Map.of("sku", sku, "error", "reservation failed"));
        }
    }
}
