package io.liveprobe.target.inventory;

import static org.junit.jupiter.api.Assertions.*;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import java.io.IOException;
import java.io.UncheckedIOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Instant;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.Consumer;

import javax.sql.DataSource;

import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.boot.test.system.CapturedOutput;
import org.springframework.boot.test.system.OutputCaptureExtension;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Primary;
import org.springframework.http.MediaType;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.MvcResult;
import org.springframework.test.web.servlet.request.MockHttpServletRequestBuilder;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.zaxxer.hikari.HikariDataSource;
import com.zaxxer.hikari.HikariPoolMXBean;

import io.micrometer.core.instrument.MeterRegistry;

/**
 * KAN-96 v1 fault API, inventory (contract §1–§2, required tests §8). Chaos profile with an H2-backed Hikari pool
 * (max 5, connectionTimeout 1000 ms), a temp state dir / cgroup root, and an injected halt hook + small native cap.
 * These tests prove API behaviour only; every case stays UNVERIFIED until the live signal is observed.
 */
@SpringBootTest(properties = {
        "spring.datasource.url=jdbc:h2:mem:inv-chaos;DB_CLOSE_DELAY=-1",
        "spring.datasource.username=sa",
        "spring.datasource.password=",
        "spring.sql.init.mode=always",
        "spring.sql.init.schema-locations=classpath:chaos-test-schema.sql",
        "spring.datasource.hikari.maximum-pool-size=5",
        "spring.datasource.hikari.connection-timeout=1000",
        "CHAOS_OPERATOR_TOKEN=" + ChaosV1ControllerTest.TOKEN,
})
@ActiveProfiles("chaos")
@AutoConfigureMockMvc
@ExtendWith(OutputCaptureExtension.class)
class ChaosV1ControllerTest {
    static final String TOKEN = "unit-test-operator-token-0123456789-abcdef";
    private static final String AUTH = "Bearer " + TOKEN;
    private static final ObjectMapper JSON = new ObjectMapper();
    static final Path STATE;
    static final Path CGROUP;
    static final AtomicInteger HALT = new AtomicInteger(-1);
    static final NativeMemoryHog HOG = new NativeMemoryHog(1024 * 1024, 4L * 1024 * 1024);

    static {
        try {
            STATE = Files.createTempDirectory("chaos-state");
            CGROUP = Files.createTempDirectory("chaos-cgroup");
        } catch (IOException e) {
            throw new UncheckedIOException(e);
        }
    }

    @TestConfiguration
    static class Hooks {
        @Bean
        @Primary
        ChaosRuntime testChaosRuntime() {
            return new ChaosRuntime(STATE, CGROUP, HALT::set, System::currentTimeMillis, HOG, 20);
        }
    }

    @Autowired MockMvc mvc;
    @Autowired ChaosEvents events;
    @Autowired DataSource dataSource;
    @Autowired MeterRegistry registry;
    @Autowired ChaosV1Controller controller;

    private final List<Map<String, Object>> seen = new CopyOnWriteArrayList<>();
    private final Consumer<Map<String, Object>> listener = seen::add;

    @BeforeEach
    void setUp() throws Exception {
        Files.createDirectories(STATE);
        Files.writeString(CGROUP.resolve("memory.max"), "268435456\n");
        HALT.set(-1);
        events.addListener(listener);
        reset();
    }

    @AfterEach
    void tearDown() throws Exception {
        reset();
        events.removeListener(listener);
    }

    // ------------------------------------------------------------------------------------------------ helpers

    private void reset() throws Exception {
        mvc.perform(post("/internal/chaos/v1/reset").header("Authorization", AUTH)).andExpect(status().isOk());
        seen.clear();
    }

    private MockHttpServletRequestBuilder inject(String body) {
        return post("/internal/chaos/v1/faults").header("Authorization", AUTH)
                .contentType(MediaType.APPLICATION_JSON).content(body);
    }

    private JsonNode json(MvcResult r) throws Exception {
        return JSON.readTree(r.getResponse().getContentAsString());
    }

    private JsonNode faultStatus() throws Exception {
        return json(mvc.perform(get("/internal/chaos/v1/faults").header("Authorization", AUTH))
                .andExpect(status().isOk()).andReturn());
    }

    private List<Object> eventNames() {
        return seen.stream().map(e -> e.get("chaos_event")).toList();
    }

    private HikariPoolMXBean pool() throws Exception {
        return dataSource.unwrap(HikariDataSource.class).getHikariPoolMXBean();
    }

    private double counter(String name, String mode, String outcome) {
        var c = registry.find(name).tag("mode", mode).tag("outcome", outcome).counter();
        return c == null ? 0 : c.count();
    }

    private static void awaitTrue(java.util.function.BooleanSupplier cond, long timeoutMs) throws InterruptedException {
        long deadline = System.currentTimeMillis() + timeoutMs;
        while (!cond.getAsBoolean()) {
            if (System.currentTimeMillis() > deadline) fail("condition not met within " + timeoutMs + " ms");
            Thread.sleep(20);
        }
    }

    // ------------------------------------------------------------------------------------------------ §1.1–1.3

    @Test
    void nonLoopbackCallerGets404BeforeAuth() throws Exception {
        for (String addr : List.of("10.42.0.7", "192.168.1.10", "::ffff:10.0.0.1", "2001:db8::1")) {
            MvcResult r = mvc.perform(inject("{\"mode\":\"latency\",\"delay_ms\":5,\"duration_seconds\":1}")
                    .with(req -> { req.setRemoteAddr(addr); return req; }))
                    .andExpect(status().isNotFound()).andReturn();
            assertEquals("", r.getResponse().getContentAsString());
            mvc.perform(post("/internal/chaos/reset").header("Authorization", AUTH)
                    .with(req -> { req.setRemoteAddr(addr); return req; })).andExpect(status().isNotFound());
        }
        mvc.perform(get("/internal/chaos/v1/faults").header("Authorization", AUTH)
                .with(req -> { req.setRemoteAddr("::1"); return req; })).andExpect(status().isOk());
        assertEquals("off", faultStatus().get("mode").asText());
    }

    @Test
    void missingWrongOrNonBearerTokenIs401EvenWithMalformedBody() throws Exception {
        String[] bad = {null, "Bearer wrong-token", "Basic " + TOKEN, "bearer " + TOKEN, "Bearer " + TOKEN + "x",
                "Bearer " + "x".repeat(4100), TOKEN};
        for (String header : bad) {
            var req = post("/internal/chaos/v1/faults").contentType(MediaType.APPLICATION_JSON).content("{not json");
            if (header != null) req.header("Authorization", header);
            MvcResult r = mvc.perform(req).andExpect(status().isUnauthorized()).andReturn();
            assertEquals("", r.getResponse().getContentAsString());
            var get = get("/internal/chaos/v1/faults");
            var rst = post("/internal/chaos/v1/reset");
            if (header != null) { get.header("Authorization", header); rst.header("Authorization", header); }
            mvc.perform(get).andExpect(status().isUnauthorized());
            mvc.perform(rst).andExpect(status().isUnauthorized());
        }
        assertEquals("off", faultStatus().get("mode").asText());
    }

    @Test
    void outOfBoundsAndMalformedBodiesAre400() throws Exception {
        String[] bad = {
                "{\"mode\":\"crash\",\"duration_seconds\":0}",
                "{\"mode\":\"crash\",\"duration_seconds\":31}",
                "{\"mode\":\"oom\",\"duration_seconds\":31}",
                "{\"mode\":\"pool_exhaust\",\"duration_seconds\":0}",
                "{\"mode\":\"endpoint_off\",\"duration_seconds\":31}",
                "{\"mode\":\"latency\",\"delay_ms\":0,\"duration_seconds\":5}",
                "{\"mode\":\"latency\",\"delay_ms\":1501,\"duration_seconds\":5}",
                "{\"mode\":\"latency\",\"delay_ms\":100,\"duration_seconds\":31}",
                "{\"mode\":\"latency\",\"duration_seconds\":5}",
                "{\"mode\":\"error_rate\",\"duration_seconds\":5}",
                "{\"mode\":\"CRASH\",\"duration_seconds\":5}",
                "{\"mode\":\"crash\",\"duration_seconds\":5,\"extra\":1}",
                "{\"mode\":\"pool_exhaust\",\"duration_seconds\":10,\"connections\":2}",
                "{\"mode\":\"crash\",\"duration_seconds\":5,\"delay_ms\":100}",
                "{\"mode\":\"crash\",\"duration_seconds\":5.5}",
                "{\"mode\":\"crash\",\"duration_seconds\":\"5\"}",
                "{\"mode\":\"crash\"}",
                "{\"duration_seconds\":5}",
                "{\"mode\":\"crash\",\"duration_seconds\":5,\"mode\":\"oom\"}",
                "[{\"mode\":\"crash\",\"duration_seconds\":5}]",
                "{\"mode\":\"crash\",",
                "",
                "{\"mode\":\"endpoint_off\",\"duration_seconds\":5,\"pad\":\"" + "x".repeat(1100) + "\"}",
        };
        for (String body : bad) {
            MvcResult r = mvc.perform(inject(body)).andExpect(status().isBadRequest()).andReturn();
            assertEquals("invalid_request", json(r).get("error").asText(), body);
        }
        // A body just over 1 KiB is rejected even if it would otherwise be valid JSON whitespace.
        String padded = "{\"mode\":\"endpoint_off\",\"duration_seconds\":5}" + " ".repeat(1000);
        mvc.perform(inject(padded)).andExpect(status().isBadRequest());
        mvc.perform(post("/internal/chaos/v1/reset").header("Authorization", AUTH).content("{\"x\":1}"))
                .andExpect(status().isBadRequest());
        assertEquals("off", faultStatus().get("mode").asText());
        assertFalse(Files.exists(STATE.resolve("intent.json")));
    }

    @Test
    void secondFaultAndLegacyInjectAre409WhileActive() throws Exception {
        MvcResult a = mvc.perform(inject("{\"mode\":\"latency\",\"delay_ms\":5,\"duration_seconds\":30}"))
                .andExpect(status().isAccepted()).andReturn();
        String expires = json(a).get("expires_at").asText();
        mvc.perform(inject("{\"mode\":\"endpoint_off\",\"duration_seconds\":5}"))
                .andExpect(status().isConflict())
                .andExpect(jsonPath("$.error").value("fault_active"))
                .andExpect(jsonPath("$.mode").value("latency"))
                .andExpect(jsonPath("$.expires_at").value(expires));
        mvc.perform(post("/internal/chaos/inject").header("Authorization", AUTH)
                        .contentType(MediaType.APPLICATION_JSON).content("{\"duration_seconds\":5,\"delay_ms\":5}"))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.mode").value("latency"));
        reset();
        // Legacy active -> v1 is 409; v1 reset clears legacy state too.
        mvc.perform(post("/internal/chaos/inject").header("Authorization", AUTH)
                        .contentType(MediaType.APPLICATION_JSON).content("{\"duration_seconds\":30,\"connections\":1}"))
                .andExpect(status().isOk());
        mvc.perform(inject("{\"mode\":\"endpoint_off\",\"duration_seconds\":5}"))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.mode").value("pool_hold"));
        mvc.perform(post("/internal/chaos/v1/reset").header("Authorization", AUTH))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.previous_mode").value("pool_hold"))
                .andExpect(jsonPath("$.released_connections").value(1));
        mvc.perform(inject("{\"mode\":\"endpoint_off\",\"duration_seconds\":1}")).andExpect(status().isAccepted());
    }

    // ------------------------------------------------------------------------------------------------ §2.3 latency

    @Test
    void latencyAppliedThenClearedOnExpiryAndOnReset() throws Exception {
        timeStock(); // warm-up the DB path before measuring the injected request
        // Keep the active-path measurement separate from the one-second expiry check: under
        // emulation the status/JSON assertions alone can consume the entire short TTL.
        MvcResult r = mvc.perform(inject("{\"mode\":\"latency\",\"delay_ms\":300,\"duration_seconds\":30}"))
                .andExpect(status().isAccepted())
                .andExpect(jsonPath("$.mode").value("latency"))
                .andExpect(jsonPath("$.effect").value("held"))
                .andExpect(jsonPath("$.delay_ms").value(300)).andReturn();
        JsonNode body = json(r);
        assertEquals(Instant.parse(body.get("accepted_at").asText()).plusSeconds(30),
                Instant.parse(body.get("expires_at").asText()));
        assertTrue(body.get("accepted_at").asText().matches("\\d{4}-\\d\\d-\\d\\dT\\d\\d:\\d\\d:\\d\\d\\.\\d{3}Z"));
        UUIDCheck.assertUuid4(body.get("operation_id").asText());
        JsonNode st = faultStatus();
        assertEquals("latency", st.get("mode").asText());
        assertEquals(300, st.get("latency_delay_ms").asInt());
        assertEquals(5, st.get("pool_max").asInt());
        assertTrue(timeStock() >= 300, "signal: active request duration >= L");
        assertEquals(1.0, registry.get("liveprobe_chaos_active").tag("mode", "latency").gauge().value());
        // First clear the measured fault, then exercise the minimum TTL independently.
        mvc.perform(post("/internal/chaos/v1/reset").header("Authorization", AUTH))
                .andExpect(status().isOk()).andExpect(jsonPath("$.previous_mode").value("latency"));
        MvcResult expiring = mvc.perform(inject("{\"mode\":\"latency\",\"delay_ms\":300,\"duration_seconds\":1}"))
                .andExpect(status().isAccepted()).andReturn();
        JsonNode expiryBody = json(expiring);
        assertEquals(Instant.parse(expiryBody.get("accepted_at").asText()).plusSeconds(1),
                Instant.parse(expiryBody.get("expires_at").asText()));
        awaitTrue(() -> safeMode().equals("off"), 3000);
        assertEquals(0, faultStatus().get("latency_delay_ms").asInt());
        assertTrue(timeStock() < 250, "no delay after expiry");
        assertTrue(eventNames().containsAll(List.of("accepted", "expired")));
        assertEquals(0.0, registry.get("liveprobe_chaos_active").tag("mode", "latency").gauge().value());
        assertTrue(counter("liveprobe_chaos_requests", "latency", "expired") >= 1);

        // Reset clears immediately.
        mvc.perform(inject("{\"mode\":\"latency\",\"delay_ms\":1500,\"duration_seconds\":30}"))
                .andExpect(status().isAccepted());
        mvc.perform(post("/internal/chaos/v1/reset").header("Authorization", AUTH).content("{}"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.mode").value("off"))
                .andExpect(jsonPath("$.previous_mode").value("latency"))
                .andExpect(jsonPath("$.released_connections").value(0))
                .andExpect(jsonPath("$.intent_cleared").value(false));
        assertTrue(timeStock() < 250);
        // Idempotent.
        mvc.perform(post("/internal/chaos/v1/reset").header("Authorization", AUTH))
                .andExpect(status().isOk()).andExpect(jsonPath("$.previous_mode").value("off"));
    }

    @Test
    void latencySleepersAreCappedAt32() throws Exception {
        ChaosLatency latency = new ChaosLatency(events);
        latency.set(400);
        double before = registry.get("liveprobe_chaos_latency_bypassed").counter().count();
        List<CompletableFuture<Void>> sleepers = new java.util.ArrayList<>();
        var pool = java.util.concurrent.Executors.newVirtualThreadPerTaskExecutor();
        for (int i = 0; i < 32; i++) sleepers.add(CompletableFuture.runAsync(latency::beforeLookup, pool));
        Thread.sleep(100);
        long t0 = System.nanoTime();
        latency.beforeLookup(); // 33rd: bypassed
        assertTrue(TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - t0) < 200);
        assertEquals(before + 1, registry.get("liveprobe_chaos_latency_bypassed").counter().count());
        CompletableFuture.allOf(sleepers.toArray(CompletableFuture[]::new)).get(5, TimeUnit.SECONDS);
    }

    private String safeMode() {
        try {
            return faultStatus().get("mode").asText();
        } catch (Exception e) {
            return "error";
        }
    }

    private long timeStock() throws Exception {
        long t0 = System.nanoTime();
        mvc.perform(get("/api/stock/a")).andExpect(status().isOk());
        return TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - t0);
    }

    // ------------------------------------------------------------------------------------------------ §2.4 pool

    @Test
    void poolExhaustHoldsAllNThenReleasesOnExpiryAndOnReset() throws Exception {
        double timeoutsBefore = hikariTimeouts();
        mvc.perform(inject("{\"mode\":\"pool_exhaust\",\"duration_seconds\":2}"))
                .andExpect(status().isAccepted())
                .andExpect(jsonPath("$.effect").value("held"))
                .andExpect(jsonPath("$.held_connections").value(5))
                .andExpect(jsonPath("$.pool_max").value(5));
        HikariPoolMXBean pool = pool();
        assertEquals(5, pool.getActiveConnections(), "active == N");
        assertEquals(0, pool.getIdleConnections(), "idle == 0");
        assertEquals(5, faultStatus().get("held_connections").asInt());
        assertEquals("pool_exhaust", faultStatus().get("mode").asText());
        // Data path request in flight: pending > 0, then 503 after >= 0.9 s.
        long t0 = System.nanoTime();
        CompletableFuture<MvcResult> inFlight = CompletableFuture.supplyAsync(() -> {
            try {
                return mvc.perform(get("/api/stock/a")).andReturn();
            } catch (Exception e) {
                throw new RuntimeException(e);
            }
        });
        awaitTrue(() -> pool.getThreadsAwaitingConnection() > 0, 900);
        MvcResult dataPath = inFlight.get(5, TimeUnit.SECONDS);
        long waited = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - t0);
        assertEquals(503, dataPath.getResponse().getStatus());
        assertEquals("stock database unavailable", dataPath.getResponse().getErrorMessage());
        assertTrue(waited >= 900, "503 after >= 0.9 s, was " + waited);
        assertTrue(hikariTimeouts() > timeoutsBefore, "hikaricp_connections_timeout_total incremented");
        assertTrue(eventNames().contains("pool_held"));
        // Expiry releases.
        awaitTrue(() -> safeMode().equals("off"), 4000);
        assertEquals(0, faultStatus().get("held_connections").asInt());
        assertTrue(pool.getActiveConnections() == 0, "released on expiry");
        assertTrue(eventNames().containsAll(List.of("pool_released", "expired")));
        mvc.perform(get("/api/stock/a")).andExpect(status().isOk());

        // Reset releases immediately.
        mvc.perform(inject("{\"mode\":\"pool_exhaust\",\"duration_seconds\":30}")).andExpect(status().isAccepted());
        assertEquals(5, pool.getActiveConnections());
        mvc.perform(post("/internal/chaos/v1/reset").header("Authorization", AUTH))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.previous_mode").value("pool_exhaust"))
                .andExpect(jsonPath("$.released_connections").value(5));
        assertEquals(0, pool.getActiveConnections());
        mvc.perform(get("/api/stock/a")).andExpect(status().isOk());
    }

    // KAN-119: release must evict, not return N idle connections (contract §2.4 pg_stat_activity <= pre + minimumIdle).

    @Test
    void poolExhaustExpiryEvictsHeldConnectionsSoDbSessionsDropToMinimumIdle() throws Exception {
        HikariDataSource hikari = dataSource.unwrap(HikariDataSource.class);
        HikariPoolMXBean pool = pool();
        int minIdle = hikari.getMinimumIdle();
        int preSessions = h2Sessions();
        mvc.perform(inject("{\"mode\":\"pool_exhaust\",\"duration_seconds\":1}")).andExpect(status().isAccepted());
        assertEquals(5, pool.getActiveConnections());
        assertEquals(5, pool.getTotalConnections());
        awaitTrue(() -> safeMode().equals("off"), 4000);
        assertEquals(0, pool.getActiveConnections(), "active -> 0 on expiry");
        assertEquals(5, controller.lastEvictedConnections(), "every held connection evicted");
        Map<String, Object> released = seen.stream().filter(e -> "pool_released".equals(e.get("chaos_event")))
                .findFirst().orElseThrow();
        assertEquals(5, released.get("released_connections"));
        assertEquals(5, released.get("evicted_connections"));
        // Physical connections are closed (async) and the pool refills only up to minimumIdle, not back to N idle.
        awaitTrue(() -> pool.getTotalConnections() <= minIdle, 3000);
        awaitTrue(() -> safeH2Sessions() <= preSessions + minIdle, 3000);
        assertTrue(pool.getIdleConnections() < 5, "released connections were not kept idle");
        mvc.perform(get("/api/stock/a")).andExpect(status().isOk());
    }

    @Test
    void poolExhaustResetEvictsAndSecondResetIsIdempotent() throws Exception {
        HikariDataSource hikari = dataSource.unwrap(HikariDataSource.class);
        HikariPoolMXBean pool = pool();
        int minIdle = hikari.getMinimumIdle();
        int preSessions = h2Sessions();
        mvc.perform(inject("{\"mode\":\"pool_exhaust\",\"duration_seconds\":30}")).andExpect(status().isAccepted());
        assertEquals(5, pool.getActiveConnections());
        mvc.perform(post("/internal/chaos/v1/reset").header("Authorization", AUTH))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.previous_mode").value("pool_exhaust"))
                .andExpect(jsonPath("$.released_connections").value(5));
        assertEquals(0, pool.getActiveConnections(), "active -> 0 on reset");
        assertEquals(5, controller.lastEvictedConnections());
        awaitTrue(() -> pool.getTotalConnections() <= minIdle, 3000);
        awaitTrue(() -> safeH2Sessions() <= preSessions + minIdle, 3000);
        int totalAfter = pool.getTotalConnections();
        // Idempotent: nothing left to release or evict, pool untouched.
        mvc.perform(post("/internal/chaos/v1/reset").header("Authorization", AUTH))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.released_connections").value(0));
        assertEquals(0, controller.lastEvictedConnections());
        assertEquals(0, pool.getActiveConnections());
        assertEquals(totalAfter, pool.getTotalConnections());
        mvc.perform(get("/api/stock/a")).andExpect(status().isOk());
    }

    @Test
    void evictionHelperFallsBackForNonHikariOrClosedConnections() throws Exception {
        HikariDataSource hikari = dataSource.unwrap(HikariDataSource.class);
        assertFalse(HikariEviction.evict(null, null));
        try (var c = dataSource.getConnection()) {
            assertFalse(HikariEviction.evict(null, c), "no pool -> caller must close");
        }
        var closed = dataSource.getConnection();
        closed.close();
        assertFalse(HikariEviction.evict(hikari, closed), "already closed -> nothing to evict");
        var c = dataSource.getConnection();
        int total = pool().getTotalConnections();
        assertEquals(1, pool().getActiveConnections());
        assertTrue(HikariEviction.evict(hikari, c));
        // The physical connection is removed from the pool (the proxy itself is simply discarded, never closed).
        assertEquals(0, pool().getActiveConnections());
        awaitTrue(() -> safeTotal() < total || safeTotal() <= hikari.getMinimumIdle(), 3000);
    }

    private int h2Sessions() throws Exception {
        try (var c = dataSource.getConnection(); var st = c.createStatement();
             var rs = st.executeQuery("SELECT COUNT(*) FROM INFORMATION_SCHEMA.SESSIONS")) {
            rs.next();
            return rs.getInt(1);
        }
    }

    private int safeH2Sessions() {
        try {
            return h2Sessions();
        } catch (Exception e) {
            return Integer.MAX_VALUE;
        }
    }

    private int safeTotal() {
        try {
            return pool().getTotalConnections();
        } catch (Exception e) {
            return Integer.MAX_VALUE;
        }
    }

    @Test
    void poolBusyReleasesPartialAcquisition() throws Exception {
        // Another holder has a connection, so N cannot be reached within the deadline.
        try (var outside = dataSource.getConnection()) {
            mvc.perform(inject("{\"mode\":\"pool_exhaust\",\"duration_seconds\":5}"))
                    .andExpect(status().isServiceUnavailable())
                    .andExpect(jsonPath("$.error").value("precondition_failed"))
                    .andExpect(jsonPath("$.reason").value("pool_busy"));
            assertEquals(1, pool().getActiveConnections(), "partial acquisitions released");
        }
        assertEquals("off", faultStatus().get("mode").asText());
    }

    private double hikariTimeouts() {
        var c = registry.find("hikaricp.connections.timeout").tag("pool", "inventory").counter();
        return c == null ? 0 : c.count();
    }

    // ------------------------------------------------------------------------------------------------ §2.5

    @Test
    void endpointOffRefusesReadinessThenRestoresOnExpiryAndOnReset() throws Exception {
        mvc.perform(inject("{\"mode\":\"endpoint_off\",\"duration_seconds\":1}"))
                .andExpect(status().isAccepted())
                .andExpect(jsonPath("$.readiness").value("REFUSING_TRAFFIC"))
                .andExpect(jsonPath("$.effect").value("held"));
        mvc.perform(get("/actuator/health/readiness")).andExpect(status().isServiceUnavailable());
        mvc.perform(get("/actuator/health/liveness")).andExpect(status().isOk());
        mvc.perform(get("/api/stock/a")).andExpect(status().isOk()); // still serves if called directly
        assertEquals("REFUSING_TRAFFIC", faultStatus().get("readiness").asText());
        assertTrue(eventNames().contains("readiness_refusing"));
        awaitTrue(() -> safeMode().equals("off"), 3000);
        mvc.perform(get("/actuator/health/readiness")).andExpect(status().isOk());
        assertEquals("ACCEPTING_TRAFFIC", faultStatus().get("readiness").asText());
        assertTrue(eventNames().contains("readiness_accepting"));

        mvc.perform(inject("{\"mode\":\"endpoint_off\",\"duration_seconds\":30}")).andExpect(status().isAccepted());
        mvc.perform(get("/actuator/health/readiness")).andExpect(status().isServiceUnavailable());
        mvc.perform(get("/actuator/health/liveness")).andExpect(status().isOk());
        mvc.perform(post("/internal/chaos/v1/reset").header("Authorization", AUTH))
                .andExpect(status().isOk()).andExpect(jsonPath("$.previous_mode").value("endpoint_off"));
        mvc.perform(get("/actuator/health/readiness")).andExpect(status().isOk());
        mvc.perform(get("/actuator/health/liveness")).andExpect(status().isOk());
    }

    @Test
    void oldResetReceiptCannotClearAnotherOperation() throws Exception {
        String fault = "{\"mode\":\"endpoint_off\",\"duration_seconds\":30}";
        String oldId = json(mvc.perform(inject(fault)).andExpect(status().isAccepted()).andReturn())
                .path("operation_id").asText();
        String oldReset = "{\"expected_operation_id\":\"" + oldId + "\"}";
        mvc.perform(post("/internal/chaos/v1/reset").header("Authorization", AUTH)
                        .contentType(MediaType.APPLICATION_JSON).content(oldReset))
                .andExpect(status().isOk()).andExpect(jsonPath("$.reset_operation_id").value(oldId));

        String currentId = json(mvc.perform(inject(fault)).andExpect(status().isAccepted()).andReturn())
                .path("operation_id").asText();
        mvc.perform(post("/internal/chaos/v1/reset").header("Authorization", AUTH)
                        .contentType(MediaType.APPLICATION_JSON).content(oldReset))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.error").value("operation_not_current"));
        assertEquals(currentId, faultStatus().path("operation_id").asText());
        mvc.perform(get("/actuator/health/readiness")).andExpect(status().isServiceUnavailable());
    }

    // ------------------------------------------------------------------------------------------------ §2.1 crash

    @Test
    void crashWritesIntentAndHaltsWith70AndResetClearsIntent() throws Exception {
        MvcResult r = mvc.perform(inject("{\"mode\":\"crash\",\"duration_seconds\":7}"))
                .andExpect(status().isAccepted())
                .andExpect(jsonPath("$.effect").value("pending"))
                .andExpect(jsonPath("$.exit_code").value(70)).andReturn();
        JsonNode body = json(r);
        Path intent = STATE.resolve("intent.json");
        JsonNode stored = JSON.readTree(Files.readAllBytes(intent));
        assertEquals(1, stored.get("v").asInt());
        assertEquals("crash", stored.get("mode").asText());
        assertEquals(body.get("operation_id").asText(), stored.get("operation_id").asText());
        assertEquals(7000, stored.get("armed_until_ms").asLong() - stored.get("created_ms").asLong());
        assertEquals(Instant.parse(body.get("expires_at").asText()).toEpochMilli(), stored.get("armed_until_ms").asLong());
        assertEquals("rw-------", java.nio.file.attribute.PosixFilePermissions.toString(
                Files.getPosixFilePermissions(intent)));
        awaitTrue(() -> HALT.get() == 70, 2000);
        assertTrue(eventNames().contains("crash_exit"));
        // crash never appears in GET status.
        assertEquals("off", faultStatus().get("mode").asText());
        mvc.perform(post("/internal/chaos/v1/reset").header("Authorization", AUTH))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.previous_mode").value("crash"))
                .andExpect(jsonPath("$.intent_cleared").value(true));
        assertFalse(Files.exists(intent));
    }

    @Test
    void resetBeforeHaltCancelsTheCrash() throws Exception {
        Files.writeString(CGROUP.resolve("memory.max"), "268435456\n");
        // A longer action delay is not needed: reset takes the lock before the 20 ms timer can halt, most runs;
        // the invariant under test is that the intent is gone either way.
        mvc.perform(inject("{\"mode\":\"crash\",\"duration_seconds\":5}")).andExpect(status().isAccepted());
        mvc.perform(post("/internal/chaos/v1/reset").header("Authorization", AUTH))
                .andExpect(status().isOk()).andExpect(jsonPath("$.intent_cleared").value(true));
        assertFalse(Files.exists(STATE.resolve("intent.json")));
    }

    @Test
    void missingStateDirIs503() throws Exception {
        Files.deleteIfExists(STATE.resolve("intent.json"));
        Files.delete(STATE);
        try {
            for (String m : List.of("crash", "oom")) {
                mvc.perform(inject("{\"mode\":\"" + m + "\",\"duration_seconds\":5}"))
                        .andExpect(status().isServiceUnavailable())
                        .andExpect(jsonPath("$.reason").value("state_dir_unavailable"));
            }
        } finally {
            Files.createDirectories(STATE);
        }
        assertEquals("off", faultStatus().get("mode").asText());
    }

    // ------------------------------------------------------------------------------------------------ §2.2 oom

    @Test
    void oomPreconditionsAre503() throws Exception {
        Files.writeString(CGROUP.resolve("memory.max"), "max\n");
        mvc.perform(inject("{\"mode\":\"oom\",\"duration_seconds\":6}"))
                .andExpect(status().isServiceUnavailable())
                .andExpect(jsonPath("$.reason").value("memory_limit_unknown"));
        Files.writeString(CGROUP.resolve("memory.max"), "536870913\n");
        mvc.perform(inject("{\"mode\":\"oom\",\"duration_seconds\":6}"))
                .andExpect(status().isServiceUnavailable())
                .andExpect(jsonPath("$.reason").value("memory_limit_too_large"));
        Files.delete(CGROUP.resolve("memory.max"));
        mvc.perform(inject("{\"mode\":\"oom\",\"duration_seconds\":6}"))
                .andExpect(status().isServiceUnavailable())
                .andExpect(jsonPath("$.reason").value("memory_limit_unknown"));
        assertFalse(Files.exists(STATE.resolve("intent.json")));
    }

    @Test
    void oomNotKilledFreesMemoryAndMarksIntentAborted() throws Exception {
        Files.writeString(CGROUP.resolve("memory.max"), "402653184\n"); // 384 MiB -> cap 448
        mvc.perform(inject("{\"mode\":\"oom\",\"duration_seconds\":6}"))
                .andExpect(status().isAccepted())
                .andExpect(jsonPath("$.effect").value("pending"))
                .andExpect(jsonPath("$.allocation_cap_mib").value(448));
        awaitTrue(() -> eventNames().contains("oom_not_killed"), 3000);
        assertTrue(eventNames().contains("oom_alloc_start"));
        assertEquals(0, HOG.liveBytes(), "native memory freed");
        JsonNode stored = JSON.readTree(Files.readAllBytes(STATE.resolve("intent.json")));
        assertTrue(stored.get("aborted").asBoolean());
        assertEquals("off", faultStatus().get("mode").asText());
        mvc.perform(post("/internal/chaos/v1/reset").header("Authorization", AUTH))
                .andExpect(status().isOk()).andExpect(jsonPath("$.intent_cleared").value(true));
    }

    // ------------------------------------------------------------------------------------------------ secrecy

    @Test
    void tokenNeverAppearsInLogsOrResponses(CapturedOutput output) throws Exception {
        StringBuilder responses = new StringBuilder();
        for (String body : List.of("{\"mode\":\"latency\",\"delay_ms\":5,\"duration_seconds\":1}",
                "{\"mode\":\"crash\",\"duration_seconds\":0}")) {
            responses.append(mvc.perform(inject(body)).andReturn().getResponse().getContentAsString());
        }
        responses.append(faultStatus().toString());
        responses.append(mvc.perform(post("/internal/chaos/v1/faults").header("Authorization", "Bearer wrong")
                .content("{}")).andReturn().getResponse().getContentAsString());
        responses.append(mvc.perform(post("/internal/chaos/v1/reset").header("Authorization", AUTH))
                .andReturn().getResponse().getContentAsString());
        assertFalse(responses.toString().contains(TOKEN));
        assertFalse(output.getAll().contains(TOKEN), "token must never be logged");
        assertFalse(output.getAll().contains("Bearer "), "Authorization header must never be logged");
        assertTrue(output.getAll().contains("chaos_event"), "chaos events are logged");
    }

    @Test
    void weakTokenFailsFastWithoutEchoingIt() {
        String weak = "short-secret-value";
        IllegalStateException e = assertThrows(IllegalStateException.class, () -> new OperatorAuth(weak));
        assertFalse(e.getMessage().contains(weak));
        assertThrows(IllegalStateException.class, () -> new OperatorAuth("   "));
    }

    /** Minimal UUID v4 shape check. */
    static final class UUIDCheck {
        static void assertUuid4(String s) {
            assertTrue(s.matches("[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"), s);
        }
    }
}
