package io.liveprobe.target.inventory;

import java.nio.file.Path;
import java.util.function.IntConsumer;
import java.util.function.LongSupplier;

/**
 * Process-level hooks the v1 chaos API needs, injectable for tests: the halt hook (Runtime.halt in production),
 * the clock, the durable state dir, the cgroup root and the native allocator.
 */
public record ChaosRuntime(Path stateDir, Path cgroupRoot, IntConsumer halt, LongSupplier clock,
                           NativeMemoryHog hog, long actionDelayMs) {
    public static final long DEFAULT_ACTION_DELAY_MS = 250;

    public static ChaosRuntime production(String stateDir, String cgroupRoot) {
        return new ChaosRuntime(
                stateDir == null || stateDir.isBlank() ? null : Path.of(stateDir),
                Path.of(cgroupRoot == null || cgroupRoot.isBlank() ? "/sys/fs/cgroup" : cgroupRoot),
                code -> Runtime.getRuntime().halt(code),
                System::currentTimeMillis,
                new NativeMemoryHog(),
                DEFAULT_ACTION_DELAY_MS);
    }

    public ChaosIntentStore intentStore() {
        return new ChaosIntentStore(stateDir);
    }
}
