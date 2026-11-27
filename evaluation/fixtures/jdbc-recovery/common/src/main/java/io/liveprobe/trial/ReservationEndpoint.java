package io.liveprobe.trial;

import java.sql.SQLException;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.server.ResponseStatusException;

@RestController
final class ReservationEndpoint {
    private final ReservationQuery query;

    ReservationEndpoint(ReservationQuery query) {
        this.query = query;
    }

    @GetMapping("/api/reservations/availability")
    Availability availability(@RequestParam String sku, @RequestParam int quantity,
                              @RequestParam(defaultValue = "standard") String channel) throws SQLException {
        if (quantity <= 0 || quantity > 100 || !sku.matches("[a-z0-9-]{1,40}")) {
            throw new ResponseStatusException(HttpStatus.BAD_REQUEST);
        }
        int stock = query.available(sku, channel);
        if (stock < 0) {
            throw new ResponseStatusException(HttpStatus.NOT_FOUND);
        }
        return new Availability(sku, quantity, stock, quantity <= stock);
    }

    @GetMapping("/api/stock/a")
    Stock stock() throws SQLException {
        int available = query.available("stock-cedar", "partner");
        if (available < 0) {
            throw new ResponseStatusException(HttpStatus.NOT_FOUND);
        }
        return new Stock("a", available, available > 0);
    }

    record Availability(String sku, int requested, int available, boolean fulfillable) {}
    record Stock(String item, int quantity, boolean available) {}
}
