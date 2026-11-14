package io.liveprobe.target.order;

import java.util.Map;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.dao.DataAccessException;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.client.HttpClientErrorException;
import org.springframework.web.client.HttpServerErrorException;
import org.springframework.web.client.ResourceAccessException;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientException;

import io.micrometer.core.instrument.MeterRegistry;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;

@RestController
@RequestMapping("/api/orders")
public class OrderController {
    private static final Logger log = LoggerFactory.getLogger(OrderController.class);

    private final RestClient inventory;
    private final MeterRegistry meters;
    private final int maxRetries;
    private final long retryBackoffMs;
    private final DataPathHook hook;
    private final OrderDbLookup db;

    @Autowired(required = false) private AppFaultBusiness appFaults;

    @Autowired
    public OrderController(RestClient inventoryClient, MeterRegistry meters,
                           @Value("${inventory.max-retries:1}") int maxRetries,
                           @Value("${inventory.retry-backoff-ms:100}") long retryBackoffMs,
                           ObjectProvider<DataPathHook> hook,
                           ObjectProvider<OrderDbLookup> db) {
        this(inventoryClient, meters, maxRetries, retryBackoffMs, hook.getIfAvailable(), db.getIfAvailable());
    }

    /** hook: chaos latency (profile chaos only); db: order_app SELECT 1 (ORDER_DB_ENABLED=true only). */
    OrderController(RestClient inventoryClient, MeterRegistry meters, int maxRetries, long retryBackoffMs,
                    DataPathHook hook, OrderDbLookup db) {
        this.inventory = inventoryClient;
        this.meters = meters;
        // Hard cap: at most one retry of an idempotent GET, so a slow B can never cause a retry storm.
        this.maxRetries = Math.max(0, Math.min(maxRetries, 1));
        this.retryBackoffMs = Math.max(0, Math.min(retryBackoffMs, 500));
        this.hook = hook;
        this.db = db;
    }

    /** Convenience for unit tests. */
    OrderController(RestClient inventoryClient, MeterRegistry meters, int maxRetries, long retryBackoffMs) {
        this(inventoryClient, meters, maxRetries, retryBackoffMs, (DataPathHook) null, (OrderDbLookup) null);
    }

    /** Convenience for unit tests. */
    OrderController(RestClient inventoryClient) {
        this(inventoryClient, new SimpleMeterRegistry(), 1, 0);
    }

    public record InventoryStock(String item, int quantity, boolean available) {}

    @GetMapping("/check")
    public ResponseEntity<Map<String, Object>> check(@RequestParam String item) {
        // KAN-96 contract 3 request path: 1. latency hook, 2. SELECT 1 (no retry), 3. inventory call (unchanged).
        if (appFaults != null) appFaults.calculateOrderAmount(100);
        if (hook != null) hook.beforeDataPath();
        if (db != null) {
            try {
                db.selectOne();
                meters.counter("order_db_lookups", "outcome", "ok").increment();
            } catch (DataAccessException e) {
                meters.counter("order_db_lookups", "outcome", "db_error").increment();
                log.warn("order db lookup failed item={} error={}", item, e.getClass().getSimpleName());
                return ResponseEntity.status(HttpStatus.SERVICE_UNAVAILABLE)
                        .body(Map.of("item", item, "error", "order database unavailable"));
            }
        }
        int attempt = 0;
        while (true) {
            try {
                InventoryStock s = inventory.get().uri("/api/stock/{item}", item)
                        .retrieve().body(InventoryStock.class);
                meters.counter("order_inventory_calls", "outcome", "ok").increment();
                return ResponseEntity.ok(Map.of(
                        "item", item,
                        "orderable", s != null && s.available(),
                        "inventory", s == null ? Map.of() : s));
            } catch (HttpClientErrorException.NotFound e) {
                meters.counter("order_inventory_calls", "outcome", "not_found").increment();
                return ResponseEntity.status(HttpStatus.NOT_FOUND)
                        .body(Map.of("item", item, "error", "unknown item"));
            } catch (ResourceAccessException | HttpServerErrorException e) {
                // Timeouts / connection errors / 5xx are retryable (GET is idempotent), bounded above.
                if (attempt < maxRetries) {
                    attempt++;
                    meters.counter("order_inventory_calls", "outcome", "retry").increment();
                    log.warn("inventory call retry item={} attempt={} error={}", item, attempt,
                            e.getClass().getSimpleName());
                    sleep(retryBackoffMs);
                    continue;
                }
                return failure(item, e);
            } catch (RestClientException e) {
                return failure(item, e);
            }
        }
    }

    private ResponseEntity<Map<String, Object>> failure(String item, RestClientException e) {
        meters.counter("order_inventory_calls", "outcome", "error").increment();
        log.error("inventory call failed item={} error={}", item, e.getClass().getSimpleName() + ": " + e.getMessage());
        return ResponseEntity.status(HttpStatus.BAD_GATEWAY)
                .body(Map.of("item", item, "error", "inventory unavailable"));
    }

    private static void sleep(long ms) {
        if (ms <= 0) return;
        try {
            Thread.sleep(ms);
        } catch (InterruptedException ie) {
            Thread.currentThread().interrupt();
        }
    }
}
