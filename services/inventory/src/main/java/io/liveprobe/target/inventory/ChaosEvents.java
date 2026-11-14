package io.liveprobe.target.inventory;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.Consumer;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.slf4j.spi.LoggingEventBuilder;

import io.micrometer.core.instrument.Counter;
import io.micrometer.core.instrument.Gauge;
import io.micrometer.core.instrument.MeterRegistry;

/**
 * Chaos observability (contract §1.7): Micrometer metrics and ECS log lines keyed by chaos_event with mode and
 * operation_id. Never carries the token, a case id or a case label.
 */
public class ChaosEvents {
    public static final List<String> MODES = List.of("crash", "oom", "latency", "pool_exhaust", "endpoint_off");
    private static final Logger log = LoggerFactory.getLogger(ChaosEvents.class);

    private final MeterRegistry registry;
    private final Map<String, AtomicInteger> active = new ConcurrentHashMap<>();
    private final Counter bypassed;
    private final List<Consumer<Map<String, Object>>> listeners = new CopyOnWriteArrayList<>();

    public ChaosEvents(MeterRegistry registry) {
        this.registry = registry;
        for (String mode : MODES) {
            AtomicInteger value = new AtomicInteger();
            active.put(mode, value);
            Gauge.builder("liveprobe_chaos_active", value, AtomicInteger::get)
                    .description("1 while a chaos fault of this mode is active")
                    .tag("mode", mode).register(registry);
        }
        this.bypassed = Counter.builder("liveprobe_chaos_latency_bypassed")
                .description("Requests not delayed because 32 requests were already sleeping")
                .register(registry);
        for (String mode : MODES) {
            for (String outcome : List.of("accepted", "rejected", "expired", "reset")) {
                requestCounter(mode, outcome);
            }
        }
    }

    private Counter requestCounter(String mode, String outcome) {
        return Counter.builder("liveprobe_chaos_requests")
                .description("Chaos fault requests by outcome")
                .tag("mode", mode).tag("outcome", outcome).register(registry);
    }

    public void request(String mode, String outcome) {
        requestCounter(MODES.contains(mode) ? mode : "invalid", outcome).increment();
    }

    public void active(String mode, boolean on) {
        AtomicInteger v = active.get(mode);
        if (v != null) v.set(on ? 1 : 0);
    }

    public void bypassed() {
        bypassed.increment();
    }

    /** Emits one ECS line `chaos_event=<event>` with mode, operation_id and optional extra key/values. */
    public void event(String event, String mode, String operationId, Object... extra) {
        Map<String, Object> kv = new LinkedHashMap<>();
        kv.put("chaos_event", event);
        kv.put("mode", mode);
        kv.put("operation_id", operationId == null ? "" : operationId);
        for (int i = 0; i + 1 < extra.length; i += 2) kv.put(String.valueOf(extra[i]), extra[i + 1]);
        LoggingEventBuilder b = log.atWarn();
        for (Map.Entry<String, Object> e : kv.entrySet()) b = b.addKeyValue(e.getKey(), e.getValue());
        b.log("chaos_event={} mode={} operation_id={}", event, mode, kv.get("operation_id"));
        for (Consumer<Map<String, Object>> l : listeners) l.accept(Map.copyOf(kv));
    }

    /** Test/diagnostic hook; receives a copy of each event's key/values. */
    public void addListener(Consumer<Map<String, Object>> listener) {
        listeners.add(listener);
    }

    public void removeListener(Consumer<Map<String, Object>> listener) {
        listeners.remove(listener);
    }
}
