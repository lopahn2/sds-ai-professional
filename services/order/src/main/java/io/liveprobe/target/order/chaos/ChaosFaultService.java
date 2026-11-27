package io.liveprobe.target.order.chaos;

import java.io.IOException;
import java.nio.file.Path;
import java.sql.Connection;
import java.sql.SQLException;
import java.sql.SQLTransientConnectionException;
import java.time.Instant;
import java.time.ZoneOffset;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.Semaphore;
import java.util.concurrent.TimeUnit;

import javax.sql.DataSource;

import jakarta.annotation.PreDestroy;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.ObjectProvider;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.availability.AvailabilityChangeEvent;
import org.springframework.boot.availability.ReadinessState;
import org.springframework.context.ApplicationEventPublisher;
import org.springframework.context.annotation.Profile;
import org.springframework.http.HttpStatus;
import org.springframework.stereotype.Service;

import com.zaxxer.hikari.HikariDataSource;
import com.zaxxer.hikari.HikariPoolMXBean;

import io.liveprobe.target.order.DataPathHook;
import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.Gauge;
import io.micrometer.core.instrument.MeterRegistry;

/**
 * KAN-96 v1 fault engine for order (contract 1.3-1.7, 2.1-2.5). One active fault per process; every fault
 * auto-expires at its TTL and reset restores normal behaviour immediately. Registered only in profile chaos.
 */
@Service
@Profile("chaos")
public class ChaosFaultService implements DataPathHook {
    private static final Logger log = LoggerFactory.getLogger(ChaosFaultService.class);
    static final List<String> MODES = List.of("crash", "oom", "latency", "pool_exhaust", "endpoint_off");
    private static final Set<String> IN_PROCESS_MODES = Set.of("latency", "pool_exhaust", "endpoint_off");
    static final int MAX_DURATION_S = 30;
    static final int MAX_DELAY_MS = 1500;
    static final int MAX_SLEEPERS = 32;
    static final int POOL_HARD_CAP = 8;
    static final long POOL_ACQUIRE_DEADLINE_MS = 3000;
    static final DateTimeFormatter RFC3339_MS =
            DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss.SSS'Z'").withZone(ZoneOffset.UTC);

    /** Outcome of a control call: HTTP status + JSON body. */
    public record Result(HttpStatus status, Map<String, Object> body) {}

    private final ObjectProvider<DataSource> dataSource;
    private final ApplicationEventPublisher events;
    private final MeterRegistry meters;
    private final IntentStore intents;
    private final MemoryLimitProbe memoryProbe;
    private final NativeAllocator allocator;
    private final ProcessExit exit;
    private final long actuationDelayMs;
    private final ScheduledExecutorService timer = Executors.newSingleThreadScheduledExecutor(r -> {
        Thread t = new Thread(r, "chaos-expiry");
        t.setDaemon(true);
        return t;
    });
    private final Semaphore sleepers = new Semaphore(MAX_SLEEPERS);
    private final Counter latencyBypassed;

    // Guarded by this.
    private final List<Connection> held = new ArrayList<>();
    /** Connections evicted by the last {@link #releaseConnections()} (for the pool_released event and tests). */
    private int lastEvicted;
    private long generation;
    private String mode = "off";
    private String operationId;
    private Instant expiresAt;
    private volatile int latencyDelayMs;
    private volatile boolean refusingTraffic;

    public ChaosFaultService(ObjectProvider<DataSource> dataSource, ApplicationEventPublisher events,
                             MeterRegistry meters,
                             ObjectProvider<MemoryLimitProbe> memoryProbe,
                             ObjectProvider<NativeAllocator> allocator,
                             ObjectProvider<ProcessExit> exit,
                             @Value("${chaos.state-dir:}") String stateDir,
                             @Value("${chaos.cgroup-root:/sys/fs/cgroup}") String cgroupRoot,
                             @Value("${chaos.actuation-delay-ms:250}") long actuationDelayMs) {
        this.dataSource = dataSource;
        this.events = events;
        this.meters = meters;
        this.intents = IntentStore.of(stateDir);
        this.memoryProbe = memoryProbe.getIfAvailable(() -> new MemoryLimitProbe(Path.of(cgroupRoot)));
        this.allocator = allocator.getIfAvailable(NativeAllocator::unsafe);
        this.exit = exit.getIfAvailable(() -> ProcessExit.RUNTIME_HALT);
        this.actuationDelayMs = Math.max(0, actuationDelayMs);
        for (String m : MODES) {
            Gauge.builder("liveprobe_chaos_active", this, s -> s.activeMode().equals(m) ? 1 : 0)
                    .description("1 while the given v1 chaos fault is active").tag("mode", m).register(meters);
        }
        this.latencyBypassed = Counter.builder("liveprobe_chaos_latency_bypassed")
                .description("Data-path requests not delayed because 32 requests were already sleeping")
                .register(meters);
    }

    // ------------------------------------------------------------------ data path

    @Override
    public void beforeDataPath() {
        int delay = latencyDelayMs;
        if (delay <= 0) return;
        if (!sleepers.tryAcquire()) {
            latencyBypassed.increment();
            return;
        }
        try {
            Thread.sleep(delay);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        } finally {
            sleepers.release();
        }
    }

    // ------------------------------------------------------------------ inject

    /** Validated request: mode, duration seconds, delay ms (latency only, else 0). */
    public record Fault(String mode, int durationSeconds, int delayMs) {}

    public synchronized Result inject(Fault f) {
        if (!"off".equals(mode)) {
            count(f.mode(), "rejected");
            Map<String, Object> body = new LinkedHashMap<>();
            body.put("error", "fault_active");
            body.put("mode", mode);
            body.put("expires_at", RFC3339_MS.format(expiresAt));
            return new Result(HttpStatus.CONFLICT, body);
        }
        Instant accepted = Instant.ofEpochMilli(System.currentTimeMillis());
        Instant expires = accepted.plusSeconds(f.durationSeconds());
        String op = UUID.randomUUID().toString();
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("operation_id", op);
        body.put("mode", f.mode());
        body.put("accepted_at", RFC3339_MS.format(accepted));
        body.put("expires_at", RFC3339_MS.format(expires));
        body.put("effect", "pending");

        long gen = ++generation;
        switch (f.mode()) {
            case "crash" -> {
                if (!writeIntent(op, "crash", accepted, expires)) return precondition(f.mode(), "state_dir_unavailable");
                body.put("exit_code", BootstrapGuard.CRASH_EXIT_CODE);
                timer.schedule(() -> crashNow(gen, op), actuationDelayMs, TimeUnit.MILLISECONDS);
            }
            case "oom" -> {
                MemoryLimitProbe.Precondition p = memoryProbe.precondition();
                if (!p.ok()) return precondition(f.mode(), p.reason());
                if (!writeIntent(op, "oom", accepted, expires)) return precondition(f.mode(), "state_dir_unavailable");
                body.put("allocation_cap_mib", p.capMib());
                long armedUntil = expires.toEpochMilli();
                timer.schedule(() -> startOomAllocator(gen, op, p, armedUntil), actuationDelayMs, TimeUnit.MILLISECONDS);
            }
            case "latency" -> {
                latencyDelayMs = f.delayMs();
                body.put("delay_ms", f.delayMs());
            }
            case "pool_exhaust" -> {
                String failure = acquirePool();
                if (failure != null) return precondition(f.mode(), failure);
                // The hold is not accepted until all N physical connections have been acquired.
                // Start its TTL then (not before the potentially 3 s acquisition).
                accepted = Instant.ofEpochMilli(System.currentTimeMillis());
                expires = accepted.plusSeconds(f.durationSeconds());
                body.put("accepted_at", RFC3339_MS.format(accepted));
                body.put("expires_at", RFC3339_MS.format(expires));
                body.put("effect", "held");
                body.put("held_connections", held.size());
                body.put("pool_max", held.size());
            }
            case "endpoint_off" -> {
                refusingTraffic = true;
                AvailabilityChangeEvent.publish(events, this, ReadinessState.REFUSING_TRAFFIC);
                body.put("readiness", "REFUSING_TRAFFIC");
            }
            default -> throw new IllegalArgumentException("mode");
        }
        mode = f.mode();
        operationId = op;
        expiresAt = expires;
        timer.schedule(() -> expire(gen), Math.max(0, expires.toEpochMilli() - System.currentTimeMillis()),
                TimeUnit.MILLISECONDS);
        count(f.mode(), "accepted");
        event("accepted", f.mode(), op, "chaos fault accepted duration_seconds=" + f.durationSeconds());
        if ("pool_exhaust".equals(f.mode())) {
            event("pool_held", f.mode(), op, "chaos pool held connections=" + held.size());
        } else if ("endpoint_off".equals(f.mode())) {
            event("readiness_refusing", f.mode(), op, "chaos readiness REFUSING_TRAFFIC");
        }
        return new Result(HttpStatus.ACCEPTED, body);
    }

    private Result precondition(String m, String reason) {
        releaseConnections();
        count(m, "rejected");
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("error", "precondition_failed");
        body.put("reason", reason);
        return new Result(HttpStatus.SERVICE_UNAVAILABLE, body);
    }

    private boolean writeIntent(String op, String m, Instant accepted, Instant expires) {
        if (!intents.available()) return false;
        try {
            intents.write(op, m, accepted.toEpochMilli(), expires.toEpochMilli());
            return true;
        } catch (IOException e) {
            return false;
        }
    }

    /** Acquires exactly N = maximumPoolSize physical connections, or returns a failure reason (all released). */
    private String acquirePool() {
        DataSource ds = dataSource.getIfAvailable();
        if (!(ds instanceof HikariDataSource hikari)) return "db_unavailable";
        int n = hikari.getMaximumPoolSize();
        if (n < 1 || n > POOL_HARD_CAP) return "pool_busy";
        long deadline = System.currentTimeMillis() + POOL_ACQUIRE_DEADLINE_MS;
        try {
            while (held.size() < n) {
                if (System.currentTimeMillis() >= deadline) {
                    releaseConnections();
                    return "pool_busy";
                }
                Connection c = hikari.getConnection();
                held.add(c);
                if (!c.isValid(1)) {
                    releaseConnections();
                    return "db_unavailable";
                }
            }
        } catch (SQLTransientConnectionException e) {
            releaseConnections(); // Hikari connectionTimeout: someone else holds pool slots
            return "pool_busy";
        } catch (SQLException e) {
            releaseConnections();
            return "db_unavailable";
        }
        HikariPoolMXBean pool = hikari.getHikariPoolMXBean();
        if (System.currentTimeMillis() > deadline || pool == null || pool.getActiveConnections() != n) {
            releaseConnections();
            return "pool_busy";
        }
        return null;
    }

    /**
     * Releases every held connection; returns the count (API released_connections). KAN-119: each connection is
     * evicted from Hikari rather than closed. close() would return it to the pool as idle and keep the DB session
     * open until idleTimeout (~10 min), breaking contract 2.4's pg_stat_activity <= pre-inject + minimumIdle check.
     * evictConnection() removes the entry, closes the physical connection now, and Hikari refills only to
     * minimumIdle. Evict replaces close(); close() is the fallback when the pool is closed or eviction fails.
     */
    private int releaseConnections() {
        DataSource ds = held.isEmpty() ? null : dataSource.getIfAvailable();
        HikariDataSource hikari = ds instanceof HikariDataSource h ? h : null;
        int released = 0;
        int evicted = 0;
        for (Connection c : held) {
            if (HikariEviction.evict(hikari, c)) {
                evicted++;
                released++;
                continue;
            }
            try {
                c.close();
                released++;
            } catch (SQLException e) {
                log.warn("chaos connection release failed type={}", e.getClass().getSimpleName());
            }
        }
        held.clear();
        lastEvicted = evicted;
        return released;
    }

    /** Test/diagnostic view of how many connections the last release evicted (KAN-119). */
    synchronized int lastEvictedConnections() {
        return lastEvicted;
    }

    // ------------------------------------------------------------------ crash / oom actuation

    private void crashNow(long gen, String op) {
        synchronized (this) {
            if (generation != gen) return; // reset before actuation
        }
        event("crash_exit", "crash", op, "chaos crash: halting with exit code " + BootstrapGuard.CRASH_EXIT_CODE);
        exit.halt(BootstrapGuard.CRASH_EXIT_CODE);
    }

    private void startOomAllocator(long gen, String op, MemoryLimitProbe.Precondition p, long armedUntilMs) {
        synchronized (this) {
            if (generation != gen) return;
        }
        Thread t = new Thread(() -> {
            event("oom_alloc_start", "oom", op, "chaos oom: native allocation up to " + p.capMib() + " MiB");
            NativeAllocator.Result r = allocator.allocate(p.capBytes(), () -> {
                synchronized (ChaosFaultService.this) {
                    return generation != gen || System.currentTimeMillis() >= armedUntilMs;
                }
            });
            if (r.reachedCap()) oomNotKilled(gen, op);
        }, "chaos-oom");
        t.setDaemon(true);
        t.start();
    }

    private synchronized void oomNotKilled(long gen, String op) {
        if (generation != gen) return;
        try {
            intents.markAborted();
        } catch (IOException e) {
            intents.delete();
        }
        ++generation;
        clearState();
        event("oom_not_killed", "oom", op, "chaos oom: cap reached without a kernel kill; memory freed");
    }

    // ------------------------------------------------------------------ expiry / reset / status

    private synchronized void expire(long gen) {
        if (generation != gen) return;
        ++generation;
        String m = mode;
        String op = operationId;
        undo(m, op);
        count(m, "expired");
        event("expired", m, op, "chaos fault expired");
    }

    public synchronized Result reset(String expectedOperationId) {
        if (expectedOperationId != null && (!IN_PROCESS_MODES.contains(mode)
                || !expectedOperationId.equals(operationId) || !expiresAt.isAfter(Instant.now()))) {
            return new Result(HttpStatus.CONFLICT, Map.of("error", "operation_not_current"));
        }
        ++generation;
        String previous = mode;
        String op = operationId;
        boolean intentCleared = expectedOperationId == null && intents.exists();
        int released = undo(previous, op);
        if (expectedOperationId == null) intents.delete();
        if (expectedOperationId == null && intents.exists()) {
            count(previous, "rejected");
            return new Result(HttpStatus.SERVICE_UNAVAILABLE,
                    Map.of("error", "precondition_failed", "reason", "state_dir_unavailable"));
        }
        count(previous, "reset");
        event("reset", previous, op, "chaos reset");
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("mode", "off");
        body.put("previous_mode", previous);
        body.put("released_connections", released);
        body.put("intent_cleared", intentCleared);
        if (expectedOperationId != null) body.put("reset_operation_id", op);
        return new Result(HttpStatus.OK, body);
    }

    /** Reverts every effect (idempotent); returns the number of released connections. */
    private int undo(String m, String op) {
        latencyDelayMs = 0;
        int released = releaseConnections();
        if (released > 0) event("pool_released", m, op, "chaos pool released connections=" + released
                + " evicted_connections=" + lastEvicted);
        if (refusingTraffic) {
            refusingTraffic = false;
            AvailabilityChangeEvent.publish(events, this, ReadinessState.ACCEPTING_TRAFFIC);
            event("readiness_accepting", m, op, "chaos readiness ACCEPTING_TRAFFIC");
        }
        if ("crash".equals(m) || "oom".equals(m)) intents.delete();
        clearState();
        return released;
    }

    private void clearState() {
        mode = "off";
        operationId = null;
        expiresAt = null;
    }

    public synchronized Map<String, Object> status() {
        String visible = IN_PROCESS_MODES.contains(mode) ? mode : "off";
        boolean off = "off".equals(visible);
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("mode", visible);
        body.put("operation_id", off ? null : operationId);
        body.put("expires_at", off ? null : RFC3339_MS.format(expiresAt));
        body.put("latency_delay_ms", latencyDelayMs);
        body.put("held_connections", held.size());
        DataSource ds = dataSource.getIfAvailable();
        body.put("pool_max", ds instanceof HikariDataSource h ? h.getMaximumPoolSize() : null);
        body.put("readiness", refusingTraffic ? "REFUSING_TRAFFIC" : "ACCEPTING_TRAFFIC");
        return body;
    }

    synchronized String activeMode() {
        return mode;
    }

    void countRejected(String m) {
        count(m, "rejected");
    }

    @PreDestroy
    public synchronized void close() {
        ++generation;
        timer.shutdownNow();
        latencyDelayMs = 0;
        releaseConnections();
        refusingTraffic = false;
        clearState();
    }

    // ------------------------------------------------------------------ observability

    private void count(String m, String outcome) {
        String tag = m != null && (MODES.contains(m) || "off".equals(m)) ? m : "invalid";
        meters.counter("liveprobe_chaos_requests", "mode", tag, "outcome", outcome).increment();
    }

    /** ECS log line with chaos_event/mode/operation_id as structured key-value pairs. Never carries secrets. */
    private static void event(String chaosEvent, String m, String op, String message) {
        log.atWarn()
                .addKeyValue("chaos_event", chaosEvent)
                .addKeyValue("mode", m == null ? "" : m)
                .addKeyValue("operation_id", op == null ? "" : op)
                .log(message);
    }
}
