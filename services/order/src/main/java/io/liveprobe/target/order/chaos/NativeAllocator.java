package io.liveprobe.target.order.chaos;

import java.lang.reflect.Field;
import java.util.ArrayDeque;
import java.util.Deque;
import java.util.function.BooleanSupplier;

/**
 * KAN-96 contract 2.2: bounded native allocation for the oom mode. Allocates off-heap memory in chunks
 * (16 MiB in production) and touches every page, so the container cgroup - not the Java heap - hits its
 * limit and the kernel OOM-kills the container. Never Java heap, never direct ByteBuffers.
 * Everything allocated is always freed before returning (cap reached, cancelled, or failure).
 */
public class NativeAllocator {
    public static final long DEFAULT_CHUNK_BYTES = 16L * MemoryLimitProbe.MIB;

    /** Raw memory operations; production is sun.misc.Unsafe, tests may wrap it to count live bytes. */
    public interface Memory {
        long allocate(long bytes);
        void touch(long address, long bytes);
        void free(long address);
    }

    public record Result(long allocatedBytes, boolean reachedCap, boolean cancelled) {}

    private final Memory memory;
    private final long chunkBytes;

    public NativeAllocator(Memory memory, long chunkBytes) {
        if (chunkBytes <= 0) throw new IllegalArgumentException("chunkBytes");
        this.memory = memory;
        this.chunkBytes = chunkBytes;
    }

    public static NativeAllocator unsafe() {
        return new NativeAllocator(UnsafeMemory.INSTANCE, DEFAULT_CHUNK_BYTES);
    }

    public long chunkBytes() {
        return chunkBytes;
    }

    /** Allocates up to capBytes; stops early when cancelled. Always frees everything before returning. */
    public Result allocate(long capBytes, BooleanSupplier cancelled) {
        Deque<Long> addresses = new ArrayDeque<>();
        long total = 0;
        boolean wasCancelled = false;
        try {
            while (total < capBytes) {
                if (cancelled.getAsBoolean()) {
                    wasCancelled = true;
                    break;
                }
                long size = Math.min(chunkBytes, capBytes - total);
                long addr = memory.allocate(size);
                addresses.push(addr);
                memory.touch(addr, size);
                total += size;
            }
        } finally {
            while (!addresses.isEmpty()) memory.free(addresses.pop());
        }
        return new Result(total, !wasCancelled && total >= capBytes, wasCancelled);
    }

    /** sun.misc.Unsafe off-heap memory (jdk.unsupported). */
    public static final class UnsafeMemory implements Memory {
        public static final UnsafeMemory INSTANCE = new UnsafeMemory();
        private final sun.misc.Unsafe unsafe;

        private UnsafeMemory() {
            try {
                Field f = sun.misc.Unsafe.class.getDeclaredField("theUnsafe");
                f.setAccessible(true);
                unsafe = (sun.misc.Unsafe) f.get(null);
            } catch (ReflectiveOperationException e) {
                throw new IllegalStateException("sun.misc.Unsafe unavailable", e);
            }
        }

        @Override public long allocate(long bytes) { return unsafe.allocateMemory(bytes); }
        @Override public void touch(long address, long bytes) { unsafe.setMemory(address, bytes, (byte) 1); }
        @Override public void free(long address) { unsafe.freeMemory(address); }
    }
}
