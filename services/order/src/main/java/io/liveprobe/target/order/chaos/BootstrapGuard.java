package io.liveprobe.target.order.chaos;

import java.io.IOException;
import java.io.PrintStream;
import java.nio.file.Path;
import java.time.Instant;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Optional;
import java.util.function.Consumer;
import java.util.function.Function;
import java.util.function.LongSupplier;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

/**
 * KAN-96 contract 1.6: static check in main() BEFORE SpringApplication.run. Acts on an armed crash/oom intent
 * on every process start, so expiry never depends on a live JVM, a timer thread, a port-forward or the runner.
 *
 * No-op unless BOTH SPRING_PROFILES_ACTIVE contains "chaos" AND CHAOS_STATE_DIR is set.
 * Logs are ECS-shaped JSON lines on stdout (Spring logging is not initialised yet). Never logs secrets.
 */
public final class BootstrapGuard {
    public static final int CRASH_EXIT_CODE = 70;
    static final long MAX_WINDOW_MS = 30_000;

    public enum Outcome { NOOP, NO_INTENT, INVALID_DELETED, EXPIRED, CRASH_HALTED, OOM_NOT_KILLED }

    private static final ObjectMapper JSON = new ObjectMapper();

    private final Function<String, String> env;
    private final LongSupplier clockMs;
    private final ProcessExit exit;
    private final NativeAllocator allocator;
    private final Function<Path, MemoryLimitProbe> probeFactory;
    private final Path cgroupRoot;
    private final Consumer<String> out;

    public BootstrapGuard(Function<String, String> env, LongSupplier clockMs, ProcessExit exit,
                          NativeAllocator allocator, Path cgroupRoot, Consumer<String> out) {
        this.env = env;
        this.clockMs = clockMs;
        this.exit = exit;
        this.allocator = allocator;
        this.probeFactory = MemoryLimitProbe::new;
        this.cgroupRoot = cgroupRoot;
        this.out = out;
    }

    public static BootstrapGuard fromEnvironment() {
        PrintStream stdout = System.out;
        return new BootstrapGuard(System::getenv, System::currentTimeMillis, ProcessExit.RUNTIME_HALT,
                null, Path.of("/sys/fs/cgroup"), stdout::println);
    }

    static boolean chaosProfileActive(String profiles) {
        if (profiles == null) return false;
        return Arrays.stream(profiles.split(",")).map(String::trim).anyMatch("chaos"::equals);
    }

    public Outcome run() {
        String stateDir = env.apply("CHAOS_STATE_DIR");
        if (!chaosProfileActive(env.apply("SPRING_PROFILES_ACTIVE")) || stateDir == null || stateDir.isBlank()) {
            return Outcome.NOOP;
        }
        IntentStore store = IntentStore.of(stateDir);
        Optional<JsonNode> raw;
        try {
            raw = store.read();
        } catch (IOException | RuntimeException e) {
            store.delete();
            log(null, null, null, "chaos intent unreadable; deleted and ignored");
            return Outcome.INVALID_DELETED;
        }
        if (raw.isEmpty()) return Outcome.NO_INTENT;

        JsonNode n = raw.get();
        long now = clockMs.getAsLong();
        String mode = text(n, "mode");
        String op = text(n, "operation_id");
        if (!valid(n, now)) {
            store.delete();
            log(null, null, null, "chaos intent invalid; deleted and ignored");
            return Outcome.INVALID_DELETED;
        }
        long armedUntil = n.get("armed_until_ms").asLong();
        if (n.path("aborted").asBoolean(false) || now >= armedUntil) {
            store.delete();
            log("expired", mode, op, "chaos intent expired; starting normally");
            return Outcome.EXPIRED;
        }
        if ("crash".equals(mode)) {
            log("crash_exit", mode, op, "chaos crash: halting with exit code " + CRASH_EXIT_CODE);
            exit.halt(CRASH_EXIT_CODE);
            return Outcome.CRASH_HALTED; // reached only with a test exit hook
        }
        // oom: same bounded native allocator as the running service; the kernel is expected to OOM-kill us.
        MemoryLimitProbe.Precondition p = probeFactory.apply(cgroupRoot).precondition();
        if (p.ok()) {
            log("oom_alloc_start", mode, op, "chaos oom: native allocation up to " + p.capMib() + " MiB");
            NativeAllocator a = allocator != null ? allocator : NativeAllocator.unsafe();
            a.allocate(p.capBytes(), () -> clockMs.getAsLong() >= armedUntil);
        }
        // Not killed (or precondition gone): memory already freed; never a false pass.
        try {
            store.markAborted();
        } catch (IOException e) {
            store.delete();
        }
        log("oom_not_killed", mode, op, "chaos oom: not killed by the kernel; intent aborted, starting normally");
        return Outcome.OOM_NOT_KILLED;
    }

    static boolean valid(JsonNode n, long now) {
        if (n == null || !n.isObject()) return false;
        if (!n.path("v").isIntegralNumber() || n.get("v").asInt() != 1) return false;
        String mode = text(n, "mode");
        if (!"crash".equals(mode) && !"oom".equals(mode)) return false;
        if (text(n, "operation_id") == null) return false;
        if (!n.path("created_ms").isIntegralNumber() || !n.path("armed_until_ms").isIntegralNumber()) return false;
        long created = n.get("created_ms").asLong();
        long armedUntil = n.get("armed_until_ms").asLong();
        if (armedUntil - created > MAX_WINDOW_MS) return false;
        if (armedUntil > now + MAX_WINDOW_MS) return false;
        return true;
    }

    private static String text(JsonNode n, String field) {
        JsonNode v = n == null ? null : n.get(field);
        return v != null && v.isTextual() ? v.asText() : null;
    }

    private void log(String event, String mode, String operationId, String message) {
        Map<String, Object> line = new LinkedHashMap<>();
        line.put("@timestamp", Instant.ofEpochMilli(clockMs.getAsLong()).toString());
        line.put("log.level", "WARN");
        line.put("log.logger", BootstrapGuard.class.getName());
        line.put("service.name", "order");
        line.put("message", message);
        // chaos_event values are fixed by the contract (1.7); invalid-intent lines carry none.
        if (event != null) line.put("chaos_event", event);
        line.put("mode", mode == null ? "" : mode);
        line.put("operation_id", operationId == null ? "" : operationId);
        line.put("ecs.version", "8.11");
        try {
            out.accept(JSON.writeValueAsString(line));
        } catch (IOException e) {
            out.accept("{\"message\":\"chaos guard log failure\"}");
        }
    }
}
