package io.liveprobe.target.order.chaos;

import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.atomic.AtomicLong;

/** Test doubles for the process-killing mechanisms. */
final class TestHooks {
    private TestHooks() {}

    /** Records halt() calls instead of killing the test JVM. */
    static final class RecordingExit implements ProcessExit {
        final CopyOnWriteArrayList<Integer> codes = new CopyOnWriteArrayList<>();

        @Override public void halt(int status) { codes.add(status); }
    }

    /** Fake native memory: tracks live and peak bytes without allocating anything real. */
    static final class CountingMemory implements NativeAllocator.Memory {
        final AtomicLong live = new AtomicLong();
        final AtomicLong peak = new AtomicLong();
        final AtomicLong touched = new AtomicLong();
        private final java.util.Map<Long, Long> sizes = new java.util.concurrent.ConcurrentHashMap<>();
        private final AtomicLong next = new AtomicLong(1);
        volatile long sleepPerChunkMs;

        @Override public long allocate(long bytes) {
            long addr = next.getAndIncrement();
            sizes.put(addr, bytes);
            long now = live.addAndGet(bytes);
            peak.accumulateAndGet(now, Math::max);
            return addr;
        }

        @Override public void touch(long address, long bytes) {
            touched.addAndGet(bytes);
            if (sleepPerChunkMs > 0) {
                try { Thread.sleep(sleepPerChunkMs); } catch (InterruptedException e) { Thread.currentThread().interrupt(); }
            }
        }

        @Override public void free(long address) {
            Long b = sizes.remove(address);
            if (b != null) live.addAndGet(-b);
        }
    }
}
