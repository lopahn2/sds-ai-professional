package io.liveprobe.target.inventory;

import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.sql.Connection;
import java.sql.SQLException;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;

import javax.sql.DataSource;

import jakarta.annotation.PreDestroy;
import jakarta.servlet.http.HttpServletRequest;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.context.annotation.Profile;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.PostMapping;
import org.springframework.web.bind.annotation.RequestBody;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;

/** Test-only, operator-authenticated fault control. Never route through an Ingress. */
@RestController
@Profile("chaos")
@RequestMapping("/internal/chaos")
public class ChaosController {
    private static final Logger log = LoggerFactory.getLogger(ChaosController.class);
    private final DataSource dataSource;
    private final byte[] tokenHash;
    private final ScheduledExecutorService timer = Executors.newSingleThreadScheduledExecutor(r -> {
        Thread t = new Thread(r, "chaos-expiry");
        t.setDaemon(true);
        return t;
    });
    private final List<Connection> held = new ArrayList<>();
    private final List<byte[]> allocations = new ArrayList<>();
    private final AtomicInteger requestCount = new AtomicInteger();
    private volatile int latencyMs;
    private volatile int errorPercent;
    private volatile long generation;
    private volatile Instant expiresAt;
    private volatile String mode = "off";

    /** Shared with the v1 API: v1 and legacy /inject are mutually exclusive (KAN-96 contract §1.5). */
    private final ChaosFaultGate gate;

    public ChaosController(DataSource dataSource, String token) {
        this(dataSource, token, new ChaosFaultGate());
    }

    @org.springframework.beans.factory.annotation.Autowired
    public ChaosController(DataSource dataSource,
                           @org.springframework.beans.factory.annotation.Value("${CHAOS_OPERATOR_TOKEN:}") String token,
                           ChaosFaultGate gate) {
        if (token.isBlank() || token.length() < 32) {
            throw new IllegalStateException("chaos profile requires a strong operator token");
        }
        this.dataSource = dataSource;
        this.tokenHash = digest(token);
        this.gate = gate;
    }

    private static byte[] digest(String value) {
        try {
            return MessageDigest.getInstance("SHA-256").digest(value.getBytes(StandardCharsets.UTF_8));
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    private boolean authorized(HttpServletRequest request) {
        String header = request.getHeader("Authorization");
        return header != null && header.startsWith("Bearer ")
                && MessageDigest.isEqual(tokenHash, digest(header.substring(7)));
    }

    public record FaultRequest(Integer duration_seconds, Integer delay_ms, Integer connections,
                               Integer megabytes, Integer error_percent) {}

    @PostMapping("/inject")
    public synchronized ResponseEntity<Map<String, Object>> inject(HttpServletRequest request,
                                                                     @RequestBody FaultRequest fault) {
        if (!authorized(request)) return ResponseEntity.status(HttpStatus.UNAUTHORIZED).build();
        if (fault == null || fault.duration_seconds() == null || fault.duration_seconds() < 1 || fault.duration_seconds() > 30)
            return ResponseEntity.badRequest().build();
        int count = supplied(fault);
        if (count != 1) return ResponseEntity.badRequest().build();
        String next;
        if (fault.delay_ms() != null && between(fault.delay_ms(), 1, 1500)) next = "latency";
        else if (fault.connections() != null && between(fault.connections(), 1, 5)) next = "pool_hold";
        else if (fault.megabytes() != null && between(fault.megabytes(), 1, 64)) next = "memory_hold";
        else if (fault.error_percent() != null && between(fault.error_percent(), 1, 100)) next = "error_rate";
        else return ResponseEntity.badRequest().build();
        ChaosFaultGate.Active other = gate.conflict(ChaosFaultGate.LEGACY);
        if (other != null) {
            Map<String, Object> conflict = new java.util.LinkedHashMap<>();
            conflict.put("error", "fault_active");
            conflict.put("mode", other.mode());
            conflict.put("expires_at", other.expiresAtText());
            return ResponseEntity.status(HttpStatus.CONFLICT).body(conflict);
        }
        resetInternal();
        try {
            if (next.equals("pool_hold")) {
                for (int i = 0; i < fault.connections(); i++) held.add(dataSource.getConnection());
            } else if (next.equals("memory_hold")) {
                for (int i = 0; i < fault.megabytes(); i++) allocations.add(new byte[1024 * 1024]);
            } else if (next.equals("latency")) latencyMs = fault.delay_ms();
            else errorPercent = fault.error_percent();
        } catch (SQLException | OutOfMemoryError e) {
            resetInternal();
            log.warn("chaos injection failed mode={} type={}", next, e.getClass().getSimpleName());
            return ResponseEntity.status(HttpStatus.SERVICE_UNAVAILABLE).build();
        }
        mode = next;
        expiresAt = Instant.now().plusSeconds(fault.duration_seconds());
        gate.claim(ChaosFaultGate.LEGACY, next, expiresAt);
        long current = ++generation;
        timer.schedule(() -> {
            synchronized (ChaosController.this) {
                if (generation == current) resetInternal();
            }
        }, fault.duration_seconds(), TimeUnit.SECONDS);
        log.warn("chaos inject mode={} duration_seconds={}", next, fault.duration_seconds());
        return ResponseEntity.ok(state());
    }

    private static boolean between(int n, int lo, int hi) { return n >= lo && n <= hi; }
    private static int supplied(FaultRequest f) {
        return (f.delay_ms() != null ? 1 : 0) + (f.connections() != null ? 1 : 0)
                + (f.megabytes() != null ? 1 : 0) + (f.error_percent() != null ? 1 : 0);
    }

    /** Deliberate JVM OOME; ExitOnOutOfMemoryError restarts the isolated inventory pod. */
    @PostMapping("/oom")
    public ResponseEntity<Map<String, Object>> oom(HttpServletRequest request) {
        if (!authorized(request)) return ResponseEntity.status(HttpStatus.UNAUTHORIZED).build();
        log.warn("chaos inject mode=oom allocation_cap_mib=384");
        Thread worker = new Thread(() -> {
            List<byte[]> chunks = new ArrayList<>();
            for (int i = 0; i < 384; i++) {
                chunks.add(new byte[1024 * 1024]);
                chunks.get(i)[0] = 1;
            }
            // If JVM heap sizing changes enough to avoid OOME, release the bounded allocation.
            chunks.clear();
        }, "chaos-oom");
        worker.setDaemon(true);
        worker.start();
        return ResponseEntity.accepted().body(Map.of("mode", "oom", "allocation_cap_mib", 384));
    }

    @PostMapping("/reset")
    public synchronized ResponseEntity<Map<String, Object>> reset(HttpServletRequest request) {
        if (!authorized(request)) return ResponseEntity.status(HttpStatus.UNAUTHORIZED).build();
        resetInternal();
        log.warn("chaos reset");
        return ResponseEntity.ok(state());
    }

    private Map<String, Object> state() {
        return Map.of("mode", mode, "expires_at", expiresAt == null ? "" : expiresAt.toString());
    }

    private void resetInternal() {
        ++generation;
        latencyMs = 0;
        errorPercent = 0;
        for (Connection c : held) {
            try { c.close(); } catch (SQLException e) { log.warn("chaos connection cleanup failed"); }
        }
        held.clear();
        allocations.clear();
        mode = "off";
        expiresAt = null;
        requestCount.set(0);
        gate.release(ChaosFaultGate.LEGACY);
    }

    /** Called by POST /internal/chaos/v1/reset: clears legacy state; returns the released connection count. */
    public synchronized int resetForV1() {
        int released = held.size();
        resetInternal();
        return released;
    }

    public void beforeLookup() {
        int delay = latencyMs;
        if (delay > 0) {
            try { Thread.sleep(delay); }
            catch (InterruptedException e) { Thread.currentThread().interrupt(); }
        }
        int percent = errorPercent;
        if (percent > 0 && Math.floorMod(requestCount.getAndIncrement(), 100) < percent) {
            throw new org.springframework.web.server.ResponseStatusException(HttpStatus.SERVICE_UNAVAILABLE, "injected error");
        }
    }

    @PreDestroy
    public synchronized void close() {
        timer.shutdownNow();
        resetInternal();
    }
}
