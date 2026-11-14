package io.liveprobe.target.inventory;

import java.lang.reflect.Field;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.atomic.AtomicLong;
import java.util.function.BooleanSupplier;

/**
 * Native (off-heap, non-direct-buffer) allocator for the oom mode (contract §2.2). Allocates with
 * sun.misc.Unsafe.allocateMemory in fixed chunks and touches every page with setMemory so the cgroup charges
 * it. Neither Java heap nor ExitOnOutOfMemoryError is involved: the intended outcome is a kernel OOMKill.
 * If the cap is reached (or stop() turns true) without being killed, everything is freed.
 */
public class NativeMemoryHog {
    static final long DEFAULT_CHUNK = 16L * 1024 * 1024;

    private final long chunkBytes;
    private final long hardLimitBytes;
    private final AtomicLong liveBytes = new AtomicLong();

    public NativeMemoryHog() {
        this(DEFAULT_CHUNK, Long.MAX_VALUE);
    }

    /** @param hardLimitBytes an extra ceiling applied on top of the requested cap (tests inject a small one). */
    public NativeMemoryHog(long chunkBytes, long hardLimitBytes) {
        if (chunkBytes <= 0) throw new IllegalArgumentException("chunkBytes");
        this.chunkBytes = chunkBytes;
        this.hardLimitBytes = hardLimitBytes;
    }

    /** Allocates up to capBytes, then frees everything. Returns the bytes that were allocated and touched. */
    public long allocateThenFree(long capBytes, BooleanSupplier stop) {
        sun.misc.Unsafe unsafe = unsafe();
        long cap = Math.min(capBytes, hardLimitBytes);
        List<Long> addresses = new ArrayList<>();
        long total = 0;
        try {
            while (total + chunkBytes <= cap && !stop.getAsBoolean()) {
                long address = unsafe.allocateMemory(chunkBytes);
                addresses.add(address);
                liveBytes.addAndGet(chunkBytes);
                unsafe.setMemory(address, chunkBytes, (byte) 0x5A);
                total += chunkBytes;
            }
        } finally {
            for (long address : addresses) {
                unsafe.freeMemory(address);
                liveBytes.addAndGet(-chunkBytes);
            }
        }
        return total;
    }

    /** Bytes currently held (0 after every call returns). */
    public long liveBytes() {
        return liveBytes.get();
    }

    private static sun.misc.Unsafe unsafe() {
        try {
            Field f = sun.misc.Unsafe.class.getDeclaredField("theUnsafe");
            f.setAccessible(true);
            return (sun.misc.Unsafe) f.get(null);
        } catch (ReflectiveOperationException e) {
            throw new IllegalStateException("native allocator unavailable", e);
        }
    }
}
