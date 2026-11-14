package io.liveprobe.target.inventory;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.OptionalLong;

/** Reads the container memory limit: cgroup v2 memory.max, falling back to cgroup v1 memory.limit_in_bytes. */
public final class CgroupMemoryLimit {
    static final long MIB = 1024L * 1024L;
    /** 512 MiB: larger limits are refused (contract §2.2), which keeps the allocation cap <= 576 MiB. */
    static final long MAX_SUPPORTED_LIMIT = 512L * MIB;
    static final long HEADROOM_MIB = 64;

    private CgroupMemoryLimit() {}

    /** Empty when unreadable, unparseable or unlimited ("max"). */
    public static OptionalLong read(Path cgroupRoot) {
        if (cgroupRoot == null) return OptionalLong.empty();
        Path[] candidates = {
                cgroupRoot.resolve("memory.max"),
                cgroupRoot.resolve("memory").resolve("memory.limit_in_bytes"),
                cgroupRoot.resolve("memory.limit_in_bytes"),
        };
        for (Path p : candidates) {
            if (!Files.isRegularFile(p)) continue;
            try {
                String v = Files.readString(p).trim();
                if (v.isEmpty() || v.equals("max")) return OptionalLong.empty();
                long n = Long.parseLong(v);
                // cgroup v1 reports "unlimited" as a huge page-aligned number; treat as unknown.
                if (n <= 0 || n >= Long.MAX_VALUE / 2) return OptionalLong.empty();
                return OptionalLong.of(n);
            } catch (IOException | NumberFormatException e) {
                return OptionalLong.empty();
            }
        }
        return OptionalLong.empty();
    }

    /** allocation_cap_mib = ceil(limit / MiB) + 64. */
    public static long capMib(long limitBytes) {
        return (limitBytes + MIB - 1) / MIB + HEADROOM_MIB;
    }
}
