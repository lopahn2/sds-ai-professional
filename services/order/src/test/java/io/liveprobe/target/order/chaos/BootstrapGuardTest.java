package io.liveprobe.target.order.chaos;

import static org.assertj.core.api.Assertions.assertThat;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicLong;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

/** KAN-96 contract 1.6 / 2.1 / 2.2: pre-Spring guard with injectable clock, exit hook and native allocator. */
class BootstrapGuardTest {
    @TempDir Path tmp;
    Path state;
    Path cgroup;
    IntentStore store;
    AtomicLong now = new AtomicLong(1_700_000_000_000L);
    TestHooks.RecordingExit exit = new TestHooks.RecordingExit();
    TestHooks.CountingMemory memory = new TestHooks.CountingMemory();
    List<String> logs = new ArrayList<>();
    Map<String, String> env = new HashMap<>();

    @BeforeEach
    void setup() throws IOException {
        state = Files.createDirectory(tmp.resolve("state"));
        cgroup = Files.createDirectory(tmp.resolve("cgroup"));
        store = new IntentStore(state);
        env.put("SPRING_PROFILES_ACTIVE", "prod,chaos");
        env.put("CHAOS_STATE_DIR", state.toString());
    }

    BootstrapGuard guard() {
        return new BootstrapGuard(env::get, now::get, exit, new NativeAllocator(memory, MemoryLimitProbe.MIB),
                cgroup, logs::add);
    }

    @Test
    void noOpWithoutProfileOrStateDir() throws Exception {
        store.write("op", "crash", now.get(), now.get() + 5000);
        env.put("SPRING_PROFILES_ACTIVE", "production");
        assertThat(guard().run()).isEqualTo(BootstrapGuard.Outcome.NOOP);
        assertThat(store.exists()).isTrue();
        env.put("SPRING_PROFILES_ACTIVE", "chaos");
        env.remove("CHAOS_STATE_DIR");
        assertThat(guard().run()).isEqualTo(BootstrapGuard.Outcome.NOOP);
        assertThat(store.exists()).isTrue();
        assertThat(exit.codes).isEmpty();
    }

    @Test
    void armedCrashHaltsBeforeSpringWith70() throws Exception {
        store.write("op-1", "crash", now.get() - 500, now.get() + 4500);
        assertThat(guard().run()).isEqualTo(BootstrapGuard.Outcome.CRASH_HALTED);
        assertThat(exit.codes).containsExactly(70);
        assertThat(logs).anySatisfy(s -> assertThat(s).contains("\"chaos_event\":\"crash_exit\"")
                .contains("\"mode\":\"crash\"").contains("\"operation_id\":\"op-1\""));
        assertThat(store.exists()).isTrue(); // survives restart until expiry
        now.addAndGet(4500);
        assertThat(guard().run()).isEqualTo(BootstrapGuard.Outcome.EXPIRED);
        assertThat(store.exists()).isFalse();
        assertThat(logs).anySatisfy(s -> assertThat(s).contains("\"chaos_event\":\"expired\""));
    }

    @Test
    void invalidAndExpiredIntentsAreDeletedAndIgnored() throws Exception {
        Files.writeString(store.file(), "not json");
        assertThat(guard().run()).isEqualTo(BootstrapGuard.Outcome.INVALID_DELETED);
        store.write("op", "crash", now.get(), now.get() + 30_001);
        assertThat(guard().run()).isEqualTo(BootstrapGuard.Outcome.INVALID_DELETED); // window > 30 s
        store.write("op", "crash", now.get() + 29_000, now.get() + 30_001);
        assertThat(guard().run()).isEqualTo(BootstrapGuard.Outcome.INVALID_DELETED); // future skew
        store.write("op", "crash", now.get() - 5000, now.get());
        assertThat(guard().run()).isEqualTo(BootstrapGuard.Outcome.EXPIRED);
        assertThat(store.exists()).isFalse();
        assertThat(exit.codes).isEmpty();
        assertThat(logs).noneSatisfy(s -> assertThat(s).contains("\"chaos_event\":\"crash_exit\""));
    }

    @Test
    void armedOomUsesNativeAllocatorAndAbortsWhenKernelDoesNotKill() throws Exception {
        Files.writeString(cgroup.resolve("memory.max"), String.valueOf(4L * MemoryLimitProbe.MIB));
        store.write("op-oom", "oom", now.get(), now.get() + 20_000);
        assertThat(guard().run()).isEqualTo(BootstrapGuard.Outcome.OOM_NOT_KILLED);
        assertThat(memory.peak.get()).isEqualTo(68L * MemoryLimitProbe.MIB);
        assertThat(memory.touched.get()).isEqualTo(68L * MemoryLimitProbe.MIB);
        assertThat(memory.live.get()).isZero();
        assertThat(store.read().orElseThrow().get("aborted").asBoolean()).isTrue();
        assertThat(logs).anySatisfy(s -> assertThat(s).contains("\"chaos_event\":\"oom_alloc_start\""));
        assertThat(logs).anySatisfy(s -> assertThat(s).contains("\"chaos_event\":\"oom_not_killed\""));
        assertThat(guard().run()).isEqualTo(BootstrapGuard.Outcome.EXPIRED);
        assertThat(store.exists()).isFalse();
    }

    @Test
    void nativeAllocatorFreesOnCancellationAndNeverExceedsCap() {
        NativeAllocator a = new NativeAllocator(memory, MemoryLimitProbe.MIB);
        var cancelled = a.allocate(5L * MemoryLimitProbe.MIB, () -> memory.live.get() >= 2L * MemoryLimitProbe.MIB);
        assertThat(cancelled.cancelled()).isTrue();
        assertThat(cancelled.allocatedBytes()).isEqualTo(2L * MemoryLimitProbe.MIB);
        assertThat(memory.live.get()).isZero();
        var complete = a.allocate(3L * MemoryLimitProbe.MIB, () -> false);
        assertThat(complete.reachedCap()).isTrue();
        assertThat(complete.allocatedBytes()).isEqualTo(3L * MemoryLimitProbe.MIB);
        assertThat(memory.live.get()).isZero();
        assertThat(memory.peak.get()).isLessThanOrEqualTo(3L * MemoryLimitProbe.MIB);
    }
}
