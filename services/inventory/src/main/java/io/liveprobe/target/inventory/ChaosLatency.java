package io.liveprobe.target.inventory;

import java.util.concurrent.Semaphore;

/**
 * v1 latency mode (contract §2.3): an in-memory Thread.sleep at the start of each data-path request. At most 32
 * requests sleep at once; excess requests proceed undelayed and increment liveprobe_chaos_latency_bypassed_total.
 */
public class ChaosLatency {
    static final int MAX_SLEEPERS = 32;

    private final Semaphore sleepers = new Semaphore(MAX_SLEEPERS);
    private final ChaosEvents events;
    private volatile int delayMs;

    public ChaosLatency(ChaosEvents events) {
        this.events = events;
    }

    public void set(int delayMs) {
        this.delayMs = Math.max(0, delayMs);
    }

    public int delayMs() {
        return delayMs;
    }

    public void beforeLookup() {
        int delay = delayMs;
        if (delay <= 0) return;
        if (!sleepers.tryAcquire()) {
            events.bypassed();
            return;
        }
        try {
            Thread.sleep(delay);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        } finally {
            sleepers.release();
        }
    }
}
