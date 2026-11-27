package io.liveprobe.target.inventory;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.sql.SQLException;
import java.sql.SQLTransientConnectionException;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.OptionalLong;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;

import javax.sql.DataSource;

import jakarta.annotation.PreDestroy;
import jakarta.servlet.http.HttpServletRequest;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.boot.availability.ApplicationAvailability;
import org.springframework.boot.availability.AvailabilityChangeEvent;
import org.springframework.boot.availability.ReadinessState;
import org.springframework.context.ApplicationEventPublisher;
import org.springframework.context.annotation.Profile;
import org.springframework.http.HttpStatus;
import org.springframework.http.MediaType;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

import com.fasterxml.jackson.core.JsonParser;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariDataSource;
import com.zaxxer.hikari.HikariPoolMXBean;

/**
 * KAN-96 v1 fault API for inventory (contract §1–§2). Chaos profile only; loopback-only via ChaosLoopbackFilter;
 * Bearer CHAOS_OPERATOR_TOKEN checked before the body is read. Every fault is bounded (D 1..30 s) and auto-expires;
 * POST /internal/chaos/v1/reset restores normal behaviour immediately and releases all resources.
 */
@RestController
@Profile("chaos")
@RequestMapping(value = "/internal/chaos/v1", produces = MediaType.APPLICATION_JSON_VALUE)
public class ChaosV1Controller {
    static final int MAX_BODY_BYTES = 1024;
    static final int POOL_HARD_CAP = 8;
    static final long POOL_ACQUIRE_DEADLINE_MS = 3000;
    static final Set<String> MODES = Set.of("crash", "oom", "latency", "pool_exhaust", "endpoint_off");
    private static final Set<String> STATUS_MODES = Set.of("latency", "pool_exhaust", "endpoint_off");
    private static final ObjectMapper STRICT_JSON = new ObjectMapper()
            .enable(JsonParser.Feature.STRICT_DUPLICATE_DETECTION);

    private final OperatorAuth auth;
    private final ObjectProvider<DataSource> dataSource;
    private final ObjectProvider<ChaosController> legacy;
    private final ApplicationEventPublisher publisher;
    private final ApplicationAvailability availability;
    private final ChaosEvents events;
    private final ChaosLatency latency;
    private final ChaosFaultGate gate;
    private final ChaosRuntime runtime;
    private final ChaosIntentStore intents;
    private final ScheduledExecutorService timer = Executors.newSingleThreadScheduledExecutor(r -> {
        Thread t = new Thread(r, "chaos-v1-expiry");
        t.setDaemon(true);
        return t;
    });

    // Guarded by this.
    private String mode = "off";
    private String operationId;
    private Instant expiresAt;
    private long generation;
    private final List<Connection> held = new ArrayList<>();
    /** Connections evicted by the last {@link #closeHeld()} (for the pool_released event). Guarded like held. */
    private int lastEvicted;
    private int latencyDelayMs;
    private boolean closing;

    public ChaosV1Controller(OperatorAuth auth, ObjectProvider<DataSource> dataSource,
                             ObjectProvider<ChaosController> legacy, ApplicationEventPublisher publisher,
                             ApplicationAvailability availability, ChaosEvents events, ChaosLatency latency,
                             ChaosFaultGate gate, ChaosRuntime runtime) {
        this.auth = auth;
        this.dataSource = dataSource;
        this.legacy = legacy;
        this.publisher = publisher;
        this.availability = availability;
        this.events = events;
        this.latency = latency;
        this.gate = gate;
        this.runtime = runtime;
        this.intents = runtime.intentStore();
    }

    /** A validated injection request. delayMs is 0 unless mode == latency. */
    record Fault(String mode, int durationSeconds, int delayMs) {}

    // ---------------------------------------------------------------------------------------------- endpoints

    @PostMapping("/faults")
    public ResponseEntity<Map<String, Object>> inject(HttpServletRequest request) {
        if (!auth.authorized(request)) return ResponseEntity.status(HttpStatus.UNAUTHORIZED).build();
        Fault fault = parseFault(readBody(request));
        if (fault == null) {
            events.request("invalid", "rejected");
            return error(HttpStatus.BAD_REQUEST, Map.of("error", "invalid_request"));
        }
        synchronized (this) {
            ResponseEntity<Map<String, Object>> conflict = conflict();
            if (conflict != null) {
                events.request(fault.mode(), "rejected");
                return conflict;
            }
            long acceptedMs = runtime.clock().getAsLong();
            Instant acceptedAt = Instant.ofEpochMilli(acceptedMs);
            Instant expires = acceptedAt.plusSeconds(fault.durationSeconds());
            String id = UUID.randomUUID().toString();
            Map<String, Object> body = new LinkedHashMap<>();
            body.put("operation_id", id);
            body.put("mode", fault.mode());
            body.put("accepted_at", ChaosFaultGate.format(acceptedAt));
            body.put("expires_at", ChaosFaultGate.format(expires));
            String failure = switch (fault.mode()) {
                case "crash" -> armCrash(id, acceptedMs, expires, body);
                case "oom" -> armOom(id, acceptedMs, expires, body);
                case "latency" -> startLatency(fault.delayMs(), body);
                case "pool_exhaust" -> holdPool(body);
                case "endpoint_off" -> refuseTraffic(body);
                default -> throw new IllegalStateException("unreachable");
            };
            if (failure != null) {
                events.request(fault.mode(), "rejected");
                return error(HttpStatus.SERVICE_UNAVAILABLE,
                        Map.of("error", "precondition_failed", "reason", failure));
            }
            activate(fault.mode(), id, expires);
            events.request(fault.mode(), "accepted");
            events.event("accepted", fault.mode(), id, "duration_seconds", fault.durationSeconds());
            if (fault.mode().equals("pool_exhaust")) events.event("pool_held", "pool_exhaust", id,
                    "held_connections", held.size());
            if (fault.mode().equals("endpoint_off")) events.event("readiness_refusing", "endpoint_off", id);
            scheduleFollowUp(fault, id, acceptedMs, expires);
            return ResponseEntity.status(HttpStatus.ACCEPTED).body(body);
        }
    }

    @GetMapping("/faults")
    public synchronized ResponseEntity<Map<String, Object>> status(HttpServletRequest request) {
        if (!auth.authorized(request)) return ResponseEntity.status(HttpStatus.UNAUTHORIZED).build();
        boolean shown = STATUS_MODES.contains(mode);
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("mode", shown ? mode : "off");
        body.put("operation_id", shown ? operationId : null);
        body.put("expires_at", shown && expiresAt != null ? ChaosFaultGate.format(expiresAt) : null);
        body.put("latency_delay_ms", latency.delayMs());
        body.put("held_connections", held.size());
        body.put("pool_max", poolMax());
        body.put("readiness", availability.getReadinessState() == ReadinessState.REFUSING_TRAFFIC
                ? "REFUSING_TRAFFIC" : "ACCEPTING_TRAFFIC");
        return ResponseEntity.ok(body);
    }

    @PostMapping("/reset")
    public ResponseEntity<Map<String, Object>> reset(HttpServletRequest request) {
        if (!auth.authorized(request)) return ResponseEntity.status(HttpStatus.UNAUTHORIZED).build();
        JsonNode body = resetBody(readBody(request));
        if (body == null) {
            return error(HttpStatus.BAD_REQUEST, Map.of("error", "invalid_request"));
        }
        String expected = body.has("expected_operation_id") ? body.get("expected_operation_id").textValue() : null;
        synchronized (this) {
            if (expected != null && (!STATUS_MODES.contains(mode) || !expected.equals(operationId)
                    || !expiresAt.isAfter(Instant.ofEpochMilli(runtime.clock().getAsLong())))) {
                return error(HttpStatus.CONFLICT, Map.of("error", "operation_not_current"));
            }
            String previous = mode;
            String previousId = operationId;
            if (previous.equals("off")) {
                ChaosFaultGate.Active other = gate.current();
                if (other != null) previous = other.mode();
            }
            if (previous.equals("off")) {
                String intentMode = intentMode();
                if (intentMode != null) previous = intentMode;
            }
            int released = clear();
            boolean intentCleared = false;
            if (expected == null) {
                ChaosController legacyController = legacy.getIfAvailable();
                if (legacyController != null) released += legacyController.resetForV1();
                intentCleared = intents.delete();
            }
            if (ChaosEvents.MODES.contains(previous)) events.request(previous, "reset");
            events.event("reset", previous, previousId, "released_connections", released,
                    "intent_cleared", intentCleared);
            Map<String, Object> receipt = new LinkedHashMap<>();
            receipt.put("mode", "off");
            receipt.put("previous_mode", previous);
            receipt.put("released_connections", released);
            receipt.put("intent_cleared", intentCleared);
            if (expected != null) receipt.put("reset_operation_id", previousId);
            return ResponseEntity.ok(receipt);
        }
    }

    // ---------------------------------------------------------------------------------------------- modes

    private String armCrash(String id, long acceptedMs, Instant expires, Map<String, Object> body) {
        String failure = writeIntent(id, "crash", acceptedMs, expires);
        if (failure != null) return failure;
        body.put("effect", "pending");
        body.put("exit_code", ChaosBootstrapGuard.CRASH_EXIT_CODE);
        return null;
    }

    private String armOom(String id, long acceptedMs, Instant expires, Map<String, Object> body) {
        OptionalLong limit = CgroupMemoryLimit.read(runtime.cgroupRoot());
        switch (OomActuator.check(limit)) {
            case MEMORY_LIMIT_UNKNOWN: return "memory_limit_unknown";
            case MEMORY_LIMIT_TOO_LARGE: return "memory_limit_too_large";
            default: break;
        }
        String failure = writeIntent(id, "oom", acceptedMs, expires);
        if (failure != null) return failure;
        body.put("effect", "pending");
        body.put("allocation_cap_mib", CgroupMemoryLimit.capMib(limit.getAsLong()));
        return null;
    }

    private String startLatency(int delayMs, Map<String, Object> body) {
        latencyDelayMs = delayMs;
        latency.set(delayMs);
        body.put("effect", "held");
        body.put("delay_ms", delayMs);
        return null;
    }

    private String holdPool(Map<String, Object> body) {
        DataSource ds = dataSource.getIfAvailable();
        HikariDataSource hikari = hikari(ds);
        if (hikari == null) return "db_unavailable";
        int n = hikari.getMaximumPoolSize();
        if (n < 1 || n > POOL_HARD_CAP) return "pool_busy";
        long deadline = System.nanoTime() + TimeUnit.MILLISECONDS.toNanos(POOL_ACQUIRE_DEADLINE_MS);
        String failure = null;
        try {
            while (held.size() < n) {
                if (System.nanoTime() > deadline) { failure = "pool_busy"; break; }
                Connection c = hikari.getConnection();
                held.add(c);
                if (!c.isValid(1)) { failure = "db_unavailable"; break; }
            }
        } catch (SQLTransientConnectionException e) {
            // Hikari connectionTimeout: busy pool, unless the driver reported a DB-side cause.
            failure = e.getCause() == null ? "pool_busy" : "db_unavailable";
        } catch (SQLException e) {
            failure = "db_unavailable";
        }
        if (failure == null && System.nanoTime() > deadline) failure = "pool_busy";
        if (failure == null) {
            HikariPoolMXBean pool = hikari.getHikariPoolMXBean();
            if (pool != null && pool.getActiveConnections() != n) failure = "pool_busy";
        }
        if (failure != null) {
            closeHeld();
            return failure;
        }
        body.put("effect", "held");
        body.put("held_connections", n);
        body.put("pool_max", n);
        return null;
    }

    private String refuseTraffic(Map<String, Object> body) {
        AvailabilityChangeEvent.publish(publisher, this, ReadinessState.REFUSING_TRAFFIC);
        body.put("effect", "held");
        body.put("readiness", "REFUSING_TRAFFIC");
        return null;
    }

    private void scheduleFollowUp(Fault fault, String id, long acceptedMs, Instant expires) {
        long gen = generation;
        long armedUntilMs = expires.toEpochMilli();
        switch (fault.mode()) {
            case "crash" -> timer.schedule(() -> {
                synchronized (this) {
                    if (generation != gen) return; // reset before the halt
                    events.event("crash_exit", "crash", id, "exit_code", ChaosBootstrapGuard.CRASH_EXIT_CODE);
                }
                runtime.halt().accept(ChaosBootstrapGuard.CRASH_EXIT_CODE);
                expireIf(gen, false); // only reached when halt is a test hook
            }, runtime.actionDelayMs(), TimeUnit.MILLISECONDS);
            case "oom" -> {
                Thread worker = new Thread(() -> {
                    sleepQuietly(runtime.actionDelayMs());
                    long limit = CgroupMemoryLimit.read(runtime.cgroupRoot()).orElse(0);
                    if (limit <= 0 || !isCurrent(gen)) return;
                    OomActuator.allocate(runtime.hog(), limit, intents, id, acceptedMs, armedUntilMs,
                            () -> !isCurrent(gen) || runtime.clock().getAsLong() >= armedUntilMs,
                            (event, m, op) -> events.event(event, m, op));
                    // Not killed: memory is already freed and the intent aborted; clear the in-JVM state.
                    expireIf(gen, false);
                }, "chaos-v1-oom");
                worker.setDaemon(true);
                worker.start();
            }
            default -> timer.schedule(() -> expireIf(gen, true), fault.durationSeconds(), TimeUnit.SECONDS);
        }
    }

    // ---------------------------------------------------------------------------------------------- state

    private void activate(String m, String id, Instant expires) {
        mode = m;
        operationId = id;
        expiresAt = expires;
        generation++;
        gate.claim(ChaosFaultGate.V1, m, expires);
        events.active(m, true);
    }

    private synchronized boolean isCurrent(long gen) {
        return generation == gen && !mode.equals("off");
    }

    private void expireIf(long gen, boolean countExpired) {
        synchronized (this) {
            if (generation != gen || mode.equals("off")) return;
            String m = mode;
            String id = operationId;
            clear();
            if (countExpired) {
                events.request(m, "expired");
                events.event("expired", m, id);
            }
        }
    }

    /** Clears every v1 effect; returns the number of released pool connections. Caller holds the lock. */
    private int clear() {
        String m = mode;
        String id = operationId;
        generation++;
        latency.set(0);
        latencyDelayMs = 0;
        int released = closeHeld();
        if (released > 0) events.event("pool_released", "pool_exhaust", id, "released_connections", released,
                "evicted_connections", lastEvicted);
        if (m.equals("endpoint_off") && !closing) {
            AvailabilityChangeEvent.publish(publisher, this, ReadinessState.ACCEPTING_TRAFFIC);
            events.event("readiness_accepting", "endpoint_off", id);
        }
        if (!m.equals("off")) events.active(m, false);
        gate.release(ChaosFaultGate.V1);
        mode = "off";
        operationId = null;
        expiresAt = null;
        return released;
    }

    /**
     * Releases every held connection and evicts it from Hikari (KAN-119). A plain close() would return it to the pool
     * as idle, and the DB session would stay open until idleTimeout (~10 min live), breaking the contract's
     * pg_stat_activity <= pre-inject + minimumIdle release check. evictConnection() on a borrowed connection marks the
     * entry evicted, removes it from the pool and closes the physical connection now; Hikari then refills only up to
     * minimumIdle. Evict replaces close(): closing the proxy afterwards would make Hikari recycle an entry it already
     * removed. close() stays as the fallback when the pool is closed or eviction fails.
     * Sets {@link #lastEvicted}. Returns the number of released connections (API released_connections).
     */
    private int closeHeld() {
        HikariDataSource hikari = held.isEmpty() ? null : hikari(dataSource.getIfAvailable());
        int n = 0;
        int evicted = 0;
        for (Iterator<Connection> it = held.iterator(); it.hasNext(); ) {
            Connection c = it.next();
            if (HikariEviction.evict(hikari, c)) {
                evicted++;
            } else {
                try {
                    c.close();
                } catch (SQLException e) {
                    // Hikari evicts a connection whose close failed; nothing else to release.
                }
            }
            it.remove();
            n++;
        }
        lastEvicted = evicted;
        return n;
    }

    /** Test/diagnostic view of how many connections the last release evicted (KAN-119). */
    synchronized int lastEvictedConnections() {
        return lastEvicted;
    }

    private ResponseEntity<Map<String, Object>> conflict() {
        if (!mode.equals("off")) {
            return conflictBody(mode, expiresAt == null ? "" : ChaosFaultGate.format(expiresAt));
        }
        ChaosFaultGate.Active other = gate.conflict(ChaosFaultGate.V1);
        if (other != null) return conflictBody(other.mode(), other.expiresAtText());
        return null;
    }

    private static ResponseEntity<Map<String, Object>> conflictBody(String m, String expires) {
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("error", "fault_active");
        body.put("mode", m);
        body.put("expires_at", expires);
        return ResponseEntity.status(HttpStatus.CONFLICT).body(body);
    }

    private Integer poolMax() {
        HikariDataSource hikari = hikari(dataSource.getIfAvailable());
        return hikari == null ? null : hikari.getMaximumPoolSize();
    }

    private static HikariDataSource hikari(DataSource ds) {
        if (ds == null) return null;
        if (ds instanceof HikariDataSource h) return h;
        try {
            return ds.isWrapperFor(HikariDataSource.class) ? ds.unwrap(HikariDataSource.class) : null;
        } catch (SQLException e) {
            return null;
        }
    }

    private String intentMode() {
        try {
            JsonNode node = intents.read();
            if (node == null || node.path("aborted").asBoolean(false)) return null;
            String m = node.path("mode").asText("");
            return m.equals("crash") || m.equals("oom") ? m : null;
        } catch (IOException e) {
            return null;
        }
    }

    private String writeIntent(String id, String m, long createdMs, Instant expires) {
        if (!intents.usable()) return "state_dir_unavailable";
        try {
            intents.write(OomActuator.intent(id, m, createdMs, expires.toEpochMilli()));
            return null;
        } catch (IOException e) {
            intents.delete();
            return "state_dir_unavailable";
        }
    }

    // ---------------------------------------------------------------------------------------------- parsing

    /** Reads at most MAX_BODY_BYTES + 1 bytes; returns null when the body is too large or unreadable. */
    static String readBody(HttpServletRequest request) {
        try (InputStream in = request.getInputStream()) {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[256];
            int r;
            while ((r = in.read(buf)) != -1) {
                out.write(buf, 0, r);
                if (out.size() > MAX_BODY_BYTES) return null;
            }
            return out.toString(StandardCharsets.UTF_8);
        } catch (IOException e) {
            return null;
        }
    }

    /** Strict parse per contract §1.4; null means 400 invalid_request. */
    static Fault parseFault(String body) {
        if (body == null || body.isBlank()) return null;
        JsonNode node;
        try {
            node = STRICT_JSON.readTree(body);
        } catch (IOException e) {
            return null;
        }
        if (node == null || !node.isObject()) return null;
        JsonNode modeNode = node.get("mode");
        if (modeNode == null || !modeNode.isTextual() || !MODES.contains(modeNode.asText())) return null;
        String m = modeNode.asText();
        Set<String> allowed = m.equals("latency") ? Set.of("mode", "duration_seconds", "delay_ms")
                : Set.of("mode", "duration_seconds");
        for (Iterator<String> it = node.fieldNames(); it.hasNext(); ) {
            if (!allowed.contains(it.next())) return null;
        }
        Integer d = boundedInt(node.get("duration_seconds"), 1, 30);
        if (d == null) return null;
        int delay = 0;
        if (m.equals("latency")) {
            Integer l = boundedInt(node.get("delay_ms"), 1, 1500);
            if (l == null) return null;
            delay = l;
        }
        return new Fault(m, d, delay);
    }

    private static Integer boundedInt(JsonNode n, int lo, int hi) {
        if (n == null || !n.isIntegralNumber() || !n.canConvertToInt()) return null;
        int v = n.intValue();
        return v >= lo && v <= hi ? v : null;
    }

    private static JsonNode resetBody(String body) {
        if (body == null) return null;
        if (body.isBlank()) return STRICT_JSON.createObjectNode();
        try {
            JsonNode node = STRICT_JSON.readTree(body);
            if (node == null || !node.isObject()) return null;
            if (node.isEmpty()) return node;
            String id = node.path("expected_operation_id").textValue();
            return node.size() == 1 && id != null && id.matches("[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
                    ? node : null;
        } catch (IOException e) {
            return null;
        }
    }

    private static ResponseEntity<Map<String, Object>> error(HttpStatus status, Map<String, Object> body) {
        return ResponseEntity.status(status).body(body);
    }

    private static void sleepQuietly(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }

    @PreDestroy
    public void close() {
        timer.shutdownNow();
        synchronized (this) {
            // Never flip readiness back to ACCEPTING during graceful shutdown.
            closing = true;
            clear();
        }
    }
}
