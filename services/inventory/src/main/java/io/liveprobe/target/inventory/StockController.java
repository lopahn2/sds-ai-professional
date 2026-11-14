package io.liveprobe.target.inventory;

import java.util.Optional;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.dao.DataAccessException;
import org.springframework.http.HttpStatus;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.server.ResponseStatusException;

import io.micrometer.core.instrument.MeterRegistry;

/** Service B: stock lookup backed by the dedicated inventory PostgreSQL database (KAN-67). */
@RestController
@RequestMapping("/api/stock")
public class StockController {
    private static final Logger log = LoggerFactory.getLogger(StockController.class);

    private final StockRepository repository;
    private final MeterRegistry meters;
    private final ObjectProvider<ChaosController> chaos;
    private final ObjectProvider<ChaosLatency> v1Latency;

    @org.springframework.beans.factory.annotation.Autowired(required = false) private AppFaultBusiness appFaults;

    public StockController(StockRepository repository, MeterRegistry meters,
                           ObjectProvider<ChaosController> chaos, ObjectProvider<ChaosLatency> v1Latency) {
        this.repository = repository;
        this.meters = meters;
        this.chaos = chaos;
        this.v1Latency = v1Latency;
    }

    public record Stock(String item, int quantity, boolean available) {}

    @GetMapping("/{item}")
    public Stock get(@PathVariable String item) {
        if (appFaults != null) item = appFaults.lookupStockItem(item);
        // KAN-96 v1 latency hook runs before the DB lookup; absent outside the chaos profile.
        ChaosLatency latency = v1Latency.getIfAvailable();
        if (latency != null) latency.beforeLookup();
        ChaosController injection = chaos.getIfAvailable();
        if (injection != null) injection.beforeLookup();
        Optional<Integer> q;
        try {
            q = repository.quantity(item);
        } catch (DataAccessException e) {
            // Never fabricate stock when the database is unavailable or the pool is exhausted.
            meters.counter("inventory_stock_lookups", "outcome", "db_error").increment();
            log.error("stock lookup db_error item={} error={}", item, e.getClass().getSimpleName());
            throw new ResponseStatusException(HttpStatus.SERVICE_UNAVAILABLE, "stock database unavailable");
        }
        if (q.isEmpty()) {
            meters.counter("inventory_stock_lookups", "outcome", "miss").increment();
            log.info("stock lookup miss item={}", item);
            throw new ResponseStatusException(HttpStatus.NOT_FOUND, "unknown item");
        }
        meters.counter("inventory_stock_lookups", "outcome", "hit").increment();
        int quantity = q.get();
        return new Stock(item, quantity, quantity > 0);
    }
}
