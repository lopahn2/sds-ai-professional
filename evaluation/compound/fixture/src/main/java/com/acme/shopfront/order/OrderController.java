package com.acme.shopfront.order;

import java.util.Map;
import java.util.UUID;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.context.annotation.Profile;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

@RestController
@Profile("order")
class OrderController {
    private static final Logger log = LoggerFactory.getLogger(OrderController.class);
    private final StockClient stock;

    OrderController(StockClient stock) {
        this.stock = stock;
    }

    @PostMapping("/api/orders")
    ResponseEntity<Map<String, Object>> place(@RequestParam String sku,
                                              @RequestParam(defaultValue = "web") String channel,
                                              @RequestParam(defaultValue = "1") int quantity) {
        String orderId = UUID.randomUUID().toString().substring(0, 8);
        int status = stock.reserve(orderId, sku, channel, quantity);
        if (status == 201) {
            return ResponseEntity.status(201).body(Map.of("order", orderId, "state", "confirmed"));
        }
        if (status == 202) {
            return ResponseEntity.status(202).body(Map.of("order", orderId, "state", "awaiting_partner"));
        }
        if (status == 409) {
            return ResponseEntity.status(409).body(Map.of("order", orderId, "state", "sold_out"));
        }
        log.error("order {} failed: stock service unavailable (sku={}, channel={})", orderId, sku, channel);
        return ResponseEntity.status(503).body(Map.of("order", orderId, "state", "failed"));
    }
}
