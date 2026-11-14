package io.liveprobe.target.order.chaos;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.OptionalLong;

/**
 * KAN-96 contract 2.2: reads the container memory limit from cgroup v2 memory.max, falling back to the
 * cgroup v1 memory.limit_in_bytes. Empty when unreadable or "max" (unlimited).
 */
public class MemoryLimitProbe {
    public static final long MIB = 1024L * 1024L;
    /** Limits above 512 MiB are refused (memory_limit_too_large), so the cap never exceeds 576 MiB. */
    public static final long MAX_LIMIT_BYTES = 512L * MIB;
    public static final long HEADROOM_MIB = 64;

    private final Path cgroupRoot;

    public MemoryLimitProbe(Path cgroupRoot) {
        this.cgroupRoot = cgroupRoot;
    }

    public OptionalLong limitBytes() {
        for (Path p : new Path[] {
                cgroupRoot.resolve("memory.max"),
                cgroupRoot.resolve("memory").resolve("memory.limit_in_bytes"),
                cgroupRoot.resolve("memory.limit_in_bytes")}) {
            try {
                if (!Files.isReadable(p)) continue;
                String v = Files.readString(p).trim();
                if (v.isEmpty() || v.equals("max")) return OptionalLong.empty();
                long n = Long.parseLong(v);
                return n > 0 ? OptionalLong.of(n) : OptionalLong.empty();
            } catch (IOException | NumberFormatException e) {
                return OptionalLong.empty();
            }
        }
        return OptionalLong.empty();
    }

    /** Result of the oom precondition: either a cap in MiB, or a precondition_failed reason. */
    public record Precondition(String reason, long limitBytes, long capMib) {
        public boolean ok() { return reason == null; }
        public long capBytes() { return capMib * MIB; }
    }

    public Precondition precondition() {
        OptionalLong limit = limitBytes();
        if (limit.isEmpty()) return new Precondition("memory_limit_unknown", -1, -1);
        long bytes = limit.getAsLong();
        if (bytes > MAX_LIMIT_BYTES) return new Precondition("memory_limit_too_large", bytes, -1);
        long limitMib = (bytes + MIB - 1) / MIB;
        return new Precondition(null, bytes, limitMib + HEADROOM_MIB);
    }
}
