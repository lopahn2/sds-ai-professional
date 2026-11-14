package io.liveprobe.target.inventory;

import java.io.IOException;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.OptionalLong;
import java.util.function.BooleanSupplier;

/**
 * Shared oom actuation used by both the v1 controller (after the 202) and the bootstrap guard on every armed start
 * (contract §2.2). Allocates native memory up to limit + 64 MiB; the expected outcome is a kernel OOMKill. If the
 * cap is reached without being killed: free everything, rewrite the intent with "aborted":true, log oom_not_killed.
 */
public final class OomActuator {
    /** Sink for chaos_event lines: (event, mode, operationId). */
    @FunctionalInterface
    public interface EventSink {
        void emit(String event, String mode, String operationId);
    }

    public enum Precondition { OK, MEMORY_LIMIT_UNKNOWN, MEMORY_LIMIT_TOO_LARGE }

    private OomActuator() {}

    public static Precondition check(OptionalLong limit) {
        if (limit.isEmpty()) return Precondition.MEMORY_LIMIT_UNKNOWN;
        if (limit.getAsLong() > CgroupMemoryLimit.MAX_SUPPORTED_LIMIT) return Precondition.MEMORY_LIMIT_TOO_LARGE;
        return Precondition.OK;
    }

    public static Map<String, Object> intent(String operationId, String mode, long createdMs, long armedUntilMs) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("v", 1);
        m.put("operation_id", operationId);
        m.put("mode", mode);
        m.put("created_ms", createdMs);
        m.put("armed_until_ms", armedUntilMs);
        return m;
    }

    /**
     * Runs the allocator. Returns only if the process was NOT killed; in that case memory is already freed, the
     * intent is marked aborted and oom_not_killed has been emitted.
     */
    public static long allocate(NativeMemoryHog hog, long limitBytes, ChaosIntentStore store, String operationId,
                                long createdMs, long armedUntilMs, BooleanSupplier stop, EventSink sink) {
        long capBytes = CgroupMemoryLimit.capMib(limitBytes) * CgroupMemoryLimit.MIB;
        sink.emit("oom_alloc_start", "oom", operationId);
        long allocated = hog.allocateThenFree(capBytes, stop);
        markAborted(store, operationId, createdMs, armedUntilMs);
        sink.emit("oom_not_killed", "oom", operationId);
        return allocated;
    }

    static void markAborted(ChaosIntentStore store, String operationId, long createdMs, long armedUntilMs) {
        Map<String, Object> aborted = intent(operationId, "oom", createdMs, armedUntilMs);
        aborted.put("aborted", true);
        try {
            if (store.exists()) store.write(aborted);
        } catch (IOException e) {
            // An intent we cannot rewrite must not keep the container in an OOM loop.
            store.delete();
        }
    }
}
