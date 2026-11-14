package io.liveprobe.target.inventory;

import java.io.IOException;
import java.io.PrintStream;
import java.lang.management.ManagementFactory;
import java.nio.file.Path;
import java.time.Instant;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.OptionalLong;
import java.util.function.Consumer;
import java.util.function.IntConsumer;
import java.util.function.LongSupplier;

import com.fasterxml.jackson.core.JsonProcessingException;
import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

/**
 * Static bootstrap guard run in main() BEFORE SpringApplication.run (contract §1.6). A no-op unless
 * SPRING_PROFILES_ACTIVE contains "chaos" AND CHAOS_STATE_DIR is set. Expiry therefore needs no live JVM, timer,
 * port-forward or runner: the first start at or after armed_until_ms deletes the intent and starts normally.
 * Log lines are ECS JSON written directly to stdout (Spring logging is not initialised yet).
 */
public final class ChaosBootstrapGuard {
    static final long MAX_WINDOW_MS = 30_000;
    static final int CRASH_EXIT_CODE = 70;
    private static final ObjectMapper MAPPER = new ObjectMapper();

    public enum Outcome { NOOP, NONE, INVALID, EXPIRED, CRASHED, OOM_NOT_KILLED, OOM_PRECONDITION }

    private final Map<String, String> env;
    private final Path cgroupRoot;
    private final LongSupplier clock;
    private final IntConsumer exit;
    private final NativeMemoryHog hog;
    private final Consumer<String> out;

    public ChaosBootstrapGuard(Map<String, String> env, Path cgroupRoot, LongSupplier clock, IntConsumer exit,
                               NativeMemoryHog hog, Consumer<String> out) {
        this.env = env;
        this.cgroupRoot = cgroupRoot;
        this.clock = clock;
        this.exit = exit;
        this.hog = hog;
        this.out = out;
    }

    public static ChaosBootstrapGuard fromEnvironment() {
        PrintStream stdout = System.out;
        return new ChaosBootstrapGuard(System.getenv(), Path.of("/sys/fs/cgroup"), System::currentTimeMillis,
                code -> Runtime.getRuntime().halt(code), new NativeMemoryHog(), line -> {
                    stdout.println(line);
                    stdout.flush();
                });
    }

    static boolean chaosProfile(String profiles) {
        return profiles != null && Arrays.stream(profiles.split(",")).map(String::trim).anyMatch("chaos"::equals);
    }

    public Outcome run() {
        String stateDir = env.get("CHAOS_STATE_DIR");
        if (!chaosProfile(env.get("SPRING_PROFILES_ACTIVE")) || stateDir == null || stateDir.isBlank()) {
            return Outcome.NOOP;
        }
        ChaosIntentStore store = new ChaosIntentStore(Path.of(stateDir));
        JsonNode intent;
        try {
            intent = store.read();
        } catch (IOException e) {
            store.delete();
            return Outcome.INVALID;
        }
        if (intent == null) return Outcome.NONE;
        long now = clock.getAsLong();
        if (!valid(intent, now)) {
            store.delete();
            return Outcome.INVALID;
        }
        String mode = intent.get("mode").asText();
        String operationId = intent.get("operation_id").asText();
        long createdMs = intent.get("created_ms").asLong();
        long armedUntilMs = intent.get("armed_until_ms").asLong();
        boolean aborted = intent.path("aborted").asBoolean(false);
        if (aborted || now >= armedUntilMs) {
            store.delete();
            emit("expired", mode, operationId);
            return Outcome.EXPIRED;
        }
        if (mode.equals("crash")) {
            emit("crash_exit", "crash", operationId);
            exit.accept(CRASH_EXIT_CODE);
            return Outcome.CRASHED;
        }
        OptionalLong limit = CgroupMemoryLimit.read(cgroupRoot);
        if (OomActuator.check(limit) != OomActuator.Precondition.OK) {
            store.delete();
            emit("oom_not_killed", "oom", operationId);
            return Outcome.OOM_PRECONDITION;
        }
        OomActuator.allocate(hog, limit.getAsLong(), store, operationId, createdMs, armedUntilMs,
                () -> clock.getAsLong() >= armedUntilMs, this::emit);
        return Outcome.OOM_NOT_KILLED;
    }

    /** v == 1, known mode, integral timestamps, window <= 30 s and not armed more than 30 s into the future. */
    static boolean valid(JsonNode intent, long now) {
        if (!intent.path("v").isIntegralNumber() || intent.get("v").asLong() != 1) return false;
        String mode = intent.path("mode").asText("");
        if (!mode.equals("crash") && !mode.equals("oom")) return false;
        if (!intent.path("operation_id").isTextual() || intent.get("operation_id").asText().isBlank()) return false;
        if (!intent.path("created_ms").isIntegralNumber() || !intent.path("armed_until_ms").isIntegralNumber()) {
            return false;
        }
        long created = intent.get("created_ms").asLong();
        long armed = intent.get("armed_until_ms").asLong();
        if (armed < created) return false;
        if (armed - created > MAX_WINDOW_MS) return false;
        return armed <= now + MAX_WINDOW_MS;
    }

    private void emit(String event, String mode, String operationId) {
        Map<String, Object> line = new LinkedHashMap<>();
        line.put("@timestamp", ChaosFaultGate.format(Instant.ofEpochMilli(clock.getAsLong())));
        line.put("log.level", "WARN");
        line.put("process.pid", ManagementFactory.getRuntimeMXBean().getPid());
        line.put("process.thread.name", Thread.currentThread().getName());
        line.put("service.name", "inventory");
        line.put("log.logger", ChaosBootstrapGuard.class.getName());
        line.put("message", "chaos_event=" + event + " mode=" + mode + " operation_id=" + operationId);
        line.put("chaos_event", event);
        line.put("mode", mode);
        line.put("operation_id", operationId);
        line.put("ecs.version", "8.11");
        try {
            out.accept(MAPPER.writeValueAsString(line));
        } catch (JsonProcessingException e) {
            out.accept("{\"chaos_event\":\"" + event + "\"}");
        }
    }
}
