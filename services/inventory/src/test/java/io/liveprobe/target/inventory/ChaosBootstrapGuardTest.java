package io.liveprobe.target.inventory;

import static org.junit.jupiter.api.Assertions.*;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.attribute.PosixFilePermissions;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;

import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;

/** KAN-96 §1.6/§2.1/§2.2: bootstrap guard with an injectable clock, exit hook and small allocation cap. */
class ChaosBootstrapGuardTest {
    private static final ObjectMapper JSON = new ObjectMapper();
    private static final long NOW = 1_790_000_000_000L;

    @TempDir Path state;
    @TempDir Path cgroup;

    private final AtomicInteger exitCode = new AtomicInteger(-1);
    private final List<String> lines = new ArrayList<>();
    private final AtomicLong clock = new AtomicLong(NOW);
    private final NativeMemoryHog hog = new NativeMemoryHog(1024 * 1024, 8L * 1024 * 1024);

    private ChaosBootstrapGuard guard(Map<String, String> env) {
        return new ChaosBootstrapGuard(env, cgroup, clock::get, exitCode::set, hog, lines::add);
    }

    private Map<String, String> chaosEnv() {
        Map<String, String> env = new HashMap<>();
        env.put("SPRING_PROFILES_ACTIVE", "chaos");
        env.put("CHAOS_STATE_DIR", state.toString());
        return env;
    }

    private Path intent(String mode, long created, long armedUntil) throws IOException {
        new ChaosIntentStore(state).write(OomActuator.intent("op-1", mode, created, armedUntil));
        return state.resolve("intent.json");
    }

    @Test
    void noopWithoutProfileOrStateDir() throws Exception {
        Path f = intent("crash", NOW, NOW + 7000);
        Map<String, String> noProfile = chaosEnv();
        noProfile.put("SPRING_PROFILES_ACTIVE", "default");
        assertEquals(ChaosBootstrapGuard.Outcome.NOOP, guard(noProfile).run());
        Map<String, String> noDir = chaosEnv();
        noDir.remove("CHAOS_STATE_DIR");
        assertEquals(ChaosBootstrapGuard.Outcome.NOOP, guard(noDir).run());
        assertEquals(ChaosBootstrapGuard.Outcome.NOOP, guard(Map.of()).run());
        assertEquals(-1, exitCode.get());
        assertTrue(Files.exists(f), "no-op must not touch the intent");
        assertTrue(lines.isEmpty());
    }

    @Test
    void noIntentStartsNormally() {
        assertEquals(ChaosBootstrapGuard.Outcome.NONE, guard(chaosEnv()).run());
        assertEquals(-1, exitCode.get());
    }

    @Test
    void armedCrashHaltsWith70BeforeSpring() throws Exception {
        Path f = intent("crash", NOW - 1000, NOW + 6000);
        assertEquals("rw-------", PosixFilePermissions.toString(Files.getPosixFilePermissions(f)));
        Map<String, String> env = chaosEnv();
        env.put("SPRING_PROFILES_ACTIVE", "prod, chaos");
        assertEquals(ChaosBootstrapGuard.Outcome.CRASHED, guard(env).run());
        assertEquals(70, exitCode.get());
        assertTrue(Files.exists(f), "intent stays armed across restarts");
        JsonNode line = JSON.readTree(lines.get(0));
        assertEquals("crash_exit", line.get("chaos_event").asText());
        assertEquals("crash", line.get("mode").asText());
        assertEquals("op-1", line.get("operation_id").asText());
    }

    @Test
    void expiredIntentIsDeletedAndStartsNormally() throws Exception {
        Path f = intent("crash", NOW - 8000, NOW - 1);
        assertEquals(ChaosBootstrapGuard.Outcome.EXPIRED, guard(chaosEnv()).run());
        assertFalse(Files.exists(f));
        assertEquals(-1, exitCode.get());
        assertEquals("expired", JSON.readTree(lines.get(0)).get("chaos_event").asText());
        // Exactly at armed_until_ms also counts as expired.
        intent("oom", NOW - 5000, NOW);
        assertEquals(ChaosBootstrapGuard.Outcome.EXPIRED, guard(chaosEnv()).run());
    }

    @Test
    void invalidIntentsAreDeletedAndIgnored() throws Exception {
        String[] bad = {
                "not json",
                "{\"v\":2,\"operation_id\":\"x\",\"mode\":\"crash\",\"created_ms\":" + NOW + ",\"armed_until_ms\":" + (NOW + 5000) + "}",
                "{\"v\":1,\"operation_id\":\"x\",\"mode\":\"latency\",\"created_ms\":" + NOW + ",\"armed_until_ms\":" + (NOW + 5000) + "}",
                // window > 30 s
                "{\"v\":1,\"operation_id\":\"x\",\"mode\":\"crash\",\"created_ms\":" + NOW + ",\"armed_until_ms\":" + (NOW + 30001) + "}",
                // armed more than 30 s in the future (clock skew)
                "{\"v\":1,\"operation_id\":\"x\",\"mode\":\"crash\",\"created_ms\":" + (NOW + 60000) + ",\"armed_until_ms\":" + (NOW + 70000) + "}",
                "{\"v\":1,\"mode\":\"crash\",\"created_ms\":" + NOW + ",\"armed_until_ms\":" + (NOW + 5000) + "}",
        };
        for (String content : bad) {
            Path f = state.resolve("intent.json");
            Files.writeString(f, content);
            assertEquals(ChaosBootstrapGuard.Outcome.INVALID, guard(chaosEnv()).run(), content);
            assertFalse(Files.exists(f), content);
        }
        assertEquals(-1, exitCode.get());
    }

    @Test
    void armedOomAllocatesThenFreesAndAbortsWhenNotKilled() throws Exception {
        Files.writeString(cgroup.resolve("memory.max"), "268435456\n");
        Path f = intent("oom", NOW, NOW + 6000);
        assertEquals(ChaosBootstrapGuard.Outcome.OOM_NOT_KILLED, guard(chaosEnv()).run());
        assertEquals(0, hog.liveBytes(), "all native memory freed");
        JsonNode rewritten = JSON.readTree(Files.readAllBytes(f));
        assertTrue(rewritten.get("aborted").asBoolean());
        assertEquals(List.of("oom_alloc_start", "oom_not_killed"),
                lines.stream().map(l -> readEvent(l)).toList());
        // The aborted intent is treated like expired on the next start.
        lines.clear();
        assertEquals(ChaosBootstrapGuard.Outcome.EXPIRED, guard(chaosEnv()).run());
        assertFalse(Files.exists(f));
    }

    @Test
    void oomWithUnknownLimitDoesNotAllocate() throws Exception {
        Files.writeString(cgroup.resolve("memory.max"), "max\n");
        Path f = intent("oom", NOW, NOW + 6000);
        assertEquals(ChaosBootstrapGuard.Outcome.OOM_PRECONDITION, guard(chaosEnv()).run());
        assertFalse(Files.exists(f));
    }

    @Test
    void cgroupV1FallbackAndCap() throws Exception {
        Files.createDirectories(cgroup.resolve("memory"));
        Files.writeString(cgroup.resolve("memory").resolve("memory.limit_in_bytes"), "402653184");
        assertEquals(402653184L, CgroupMemoryLimit.read(cgroup).getAsLong());
        assertEquals(384 + 64, CgroupMemoryLimit.capMib(402653184L));
        assertEquals(576, CgroupMemoryLimit.capMib(CgroupMemoryLimit.MAX_SUPPORTED_LIMIT));
    }

    private static String readEvent(String line) {
        try {
            return JSON.readTree(line).get("chaos_event").asText();
        } catch (IOException e) {
            throw new AssertionError(e);
        }
    }
}
