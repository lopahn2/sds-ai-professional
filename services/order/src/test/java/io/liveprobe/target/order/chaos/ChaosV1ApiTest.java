package io.liveprobe.target.order.chaos;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.content;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.attribute.PosixFilePermission;
import java.sql.Connection;
import java.sql.SQLException;
import java.sql.SQLTransientConnectionException;
import java.time.Instant;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;

import org.junit.jupiter.api.AfterAll;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.extension.ExtendWith;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.boot.test.system.CapturedOutput;
import org.springframework.boot.test.system.OutputCaptureExtension;
import org.springframework.context.annotation.Bean;
import org.springframework.http.MediaType;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.MvcResult;
import org.springframework.test.web.servlet.request.MockHttpServletRequestBuilder;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.sun.net.httpserver.HttpServer;
import com.zaxxer.hikari.HikariDataSource;
import com.zaxxer.hikari.HikariPoolMXBean;

import io.micrometer.core.instrument.MeterRegistry;

/**
 * KAN-96 contract 8 minimum tests for order, chaos profile: loopback 404, auth 401, bounds 400, 409, and every
 * order mode's in-process signal, expiry and reset. H2-backed Hikari pool "order" (N=4); a local JDK HTTP server
 * stands in for inventory so the data path returns 200 like the live GET /api/orders/check?item=a.
 * Unit tests prove API behaviour only; the live cases stay UNVERIFIED until observed.
 */
@SpringBootTest(properties = {
        "chaos.actuation-delay-ms=50",
        "order.db.enabled=true",
        "order.db.url=jdbc:h2:mem:order_app_chaos;DB_CLOSE_DELAY=-1",
        "order.db.user=sa",
        "order.db.pool-max=4",
        "order.db.connection-timeout-ms=1000",
})
@ActiveProfiles("chaos")
@AutoConfigureMockMvc
@ExtendWith(OutputCaptureExtension.class)
class ChaosV1ApiTest {
    static final String TOKEN = java.util.UUID.randomUUID().toString();
    static final String WRONG = java.util.UUID.randomUUID().toString();
    static final ObjectMapper JSON = new ObjectMapper();

    static final Path STATE_DIR;
    static final Path CGROUP;
    static final HttpServer INVENTORY;
    static final TestHooks.RecordingExit EXIT = new TestHooks.RecordingExit();
    static final TestHooks.CountingMemory MEMORY = new TestHooks.CountingMemory();

    static {
        try {
            Path root = Files.createTempDirectory("order-chaos-test");
            STATE_DIR = Files.createDirectory(root.resolve("state"));
            CGROUP = Files.createDirectory(root.resolve("cgroup"));
            INVENTORY = HttpServer.create(new InetSocketAddress("127.0.0.1", 0), 0);
            INVENTORY.createContext("/api/stock/", ex -> {
                byte[] b = "{\"item\":\"a\",\"quantity\":42,\"available\":true}".getBytes(StandardCharsets.UTF_8);
                ex.getResponseHeaders().add("Content-Type", "application/json");
                ex.sendResponseHeaders(200, b.length);
                try (OutputStream os = ex.getResponseBody()) { os.write(b); }
            });
            INVENTORY.setExecutor(Executors.newFixedThreadPool(8));
            INVENTORY.start();
        } catch (IOException e) {
            throw new IllegalStateException(e);
        }
    }

    @DynamicPropertySource
    static void props(DynamicPropertyRegistry r) {
        r.add("CHAOS_OPERATOR_TOKEN", () -> TOKEN);
        r.add("chaos.state-dir", STATE_DIR::toString);
        r.add("chaos.cgroup-root", CGROUP::toString);
        r.add("inventory.base-url", () -> "http://127.0.0.1:" + INVENTORY.getAddress().getPort());
    }

    @TestConfiguration
    static class Hooks {
        @Bean ProcessExit processExit() { return EXIT; }
        @Bean NativeAllocator nativeAllocator() { return new NativeAllocator(MEMORY, 16L * MemoryLimitProbe.MIB); }
    }

    @Autowired MockMvc mvc;
    @Autowired HikariDataSource dataSource;
    @Autowired MeterRegistry meters;
    @Autowired ChaosFaultService faults;

    @AfterEach
    void resetAfterEach() throws Exception {
        mvc.perform(auth(post("/internal/chaos/v1/reset"), TOKEN)).andExpect(status().isOk());
        Files.createDirectories(STATE_DIR);
        EXIT.codes.clear();
    }

    @AfterAll
    static void stopInventory() {
        INVENTORY.stop(0);
    }

    // ------------------------------------------------------------------ helpers

    static MockHttpServletRequestBuilder auth(MockHttpServletRequestBuilder b, String token) {
        return token == null ? b : b.header("Authorization", "Bearer " + token);
    }

    MvcResult inject(String body) throws Exception {
        return mvc.perform(auth(post("/internal/chaos/v1/faults"), TOKEN)
                .contentType(MediaType.APPLICATION_JSON).content(body)).andReturn();
    }

    JsonNode json(MvcResult r) throws Exception {
        return JSON.readTree(r.getResponse().getContentAsString());
    }

    JsonNode faultStatus() throws Exception {
        return json(mvc.perform(auth(get("/internal/chaos/v1/faults"), TOKEN))
                .andExpect(status().isOk()).andReturn());
    }

    long timeCheckMs(int expectedStatus) throws Exception {
        long t0 = System.nanoTime();
        mvc.perform(get("/api/orders/check").param("item", "a")).andExpect(status().is(expectedStatus));
        return (System.nanoTime() - t0) / 1_000_000;
    }

    Connection acquireSetupConnection() throws SQLException {
        // Retry fixture setup while keeping the actual application's connection timeout unchanged.
        long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(5);
        while (true) {
            try {
                return dataSource.getConnection();
            } catch (SQLTransientConnectionException unavailable) {
                if (System.nanoTime() >= deadline) throw unavailable;
            }
        }
    }

    static void await(long timeoutMs, java.util.function.BooleanSupplier cond) throws InterruptedException {
        long end = System.currentTimeMillis() + timeoutMs;
        while (!cond.getAsBoolean()) {
            if (System.currentTimeMillis() > end) throw new AssertionError("condition not met in " + timeoutMs + " ms");
            Thread.sleep(20);
        }
    }

    double gauge(String name, String mode) {
        return meters.get(name).tag("mode", mode).gauge().value();
    }

    double counter(String mode, String outcome) {
        var c = meters.find("liveprobe_chaos_requests").tag("mode", mode).tag("outcome", outcome).counter();
        return c == null ? 0 : c.count();
    }

    // ------------------------------------------------------------------ gate: 404 loopback, 401 auth

    @Test
    void nonLoopbackCallerGets404BeforeAuth() throws Exception {
        for (String addr : List.of("10.42.0.7", "192.168.1.5", "fd00::1")) {
            mvc.perform(auth(get("/internal/chaos/v1/faults"), TOKEN).with(r -> { r.setRemoteAddr(addr); return r; }))
                    .andExpect(status().isNotFound()).andExpect(content().string(""));
            mvc.perform(post("/internal/chaos/v1/reset").with(r -> { r.setRemoteAddr(addr); return r; }))
                    .andExpect(status().isNotFound());
        }
        mvc.perform(auth(get("/internal/chaos/v1/faults"), TOKEN).with(r -> { r.setRemoteAddr("::1"); return r; }))
                .andExpect(status().isOk());
        mvc.perform(auth(get("/internal/chaos/v1/faults"), TOKEN).with(r -> { r.setRemoteAddr("127.0.0.9"); return r; }))
                .andExpect(status().isOk());
    }

    @Test
    void missingWrongOrNonBearerTokenIs401EvenWithMalformedBody() throws Exception {
        String good = "{\"mode\":\"latency\",\"delay_ms\":10,\"duration_seconds\":1}";
        for (String body : List.of(good, "{not json", "")) {
            mvc.perform(post("/internal/chaos/v1/faults").contentType(MediaType.APPLICATION_JSON).content(body))
                    .andExpect(status().isUnauthorized()).andExpect(content().string(""));
            mvc.perform(auth(post("/internal/chaos/v1/faults"), WRONG).contentType(MediaType.APPLICATION_JSON).content(body))
                    .andExpect(status().isUnauthorized()).andExpect(content().string(""));
            mvc.perform(post("/internal/chaos/v1/faults").header("Authorization", "Basic " + TOKEN)
                    .contentType(MediaType.APPLICATION_JSON).content(body))
                    .andExpect(status().isUnauthorized());
            mvc.perform(post("/internal/chaos/v1/faults").header("Authorization", TOKEN)
                    .contentType(MediaType.APPLICATION_JSON).content(body))
                    .andExpect(status().isUnauthorized());
        }
        mvc.perform(post("/internal/chaos/v1/faults").header("Authorization", "Bearer " + TOKEN + "x".repeat(4100))
                .contentType(MediaType.APPLICATION_JSON).content(good)).andExpect(status().isUnauthorized());
        mvc.perform(get("/internal/chaos/v1/faults")).andExpect(status().isUnauthorized());
        mvc.perform(auth(get("/internal/chaos/v1/faults"), WRONG)).andExpect(status().isUnauthorized());
        mvc.perform(post("/internal/chaos/v1/reset")).andExpect(status().isUnauthorized());
        mvc.perform(auth(post("/internal/chaos/v1/reset"), WRONG)).andExpect(status().isUnauthorized());
        assertThat(faultStatus().get("mode").asText()).isEqualTo("off");
    }

    @Test
    void weakOrMissingTokenFailsFastWithoutEchoingTheValue() {
        String weak = "short-secret-value";
        var e = org.junit.jupiter.api.Assertions.assertThrows(IllegalStateException.class,
                () -> new ChaosV1Controller(faults, weak));
        assertThat(e.getMessage()).doesNotContain(weak);
        org.junit.jupiter.api.Assertions.assertThrows(IllegalStateException.class, () -> new ChaosV1Controller(faults, ""));
        org.junit.jupiter.api.Assertions.assertThrows(IllegalStateException.class, () -> new ChaosV1Controller(faults, " ".repeat(40)));
    }

    // ------------------------------------------------------------------ bounds

    @Test
    void outOfRangeAndOffSchemaBodiesAre400() throws Exception {
        List<String> bad = new ArrayList<>(List.of(
                "{\"mode\":\"crash\",\"duration_seconds\":0}",
                "{\"mode\":\"crash\",\"duration_seconds\":31}",
                "{\"mode\":\"oom\",\"duration_seconds\":31}",
                "{\"mode\":\"endpoint_off\",\"duration_seconds\":0}",
                "{\"mode\":\"pool_exhaust\",\"duration_seconds\":31}",
                "{\"mode\":\"latency\",\"delay_ms\":0,\"duration_seconds\":5}",
                "{\"mode\":\"latency\",\"delay_ms\":1501,\"duration_seconds\":5}",
                "{\"mode\":\"latency\",\"delay_ms\":100,\"duration_seconds\":0}",
                "{\"mode\":\"latency\",\"delay_ms\":100,\"duration_seconds\":31}",
                "{\"mode\":\"latency\",\"duration_seconds\":5}",
                "{\"mode\":\"latency\",\"delay_ms\":100}",
                "{\"mode\":\"latency\",\"delay_ms\":100.5,\"duration_seconds\":5}",
                "{\"mode\":\"latency\",\"delay_ms\":\"100\",\"duration_seconds\":5}",
                "{\"mode\":\"crash\",\"duration_seconds\":1.0}",
                "{\"mode\":\"crash\",\"duration_seconds\":99999999999}",
                "{\"mode\":\"error_rate\",\"duration_seconds\":5}",
                "{\"mode\":\"CRASH\",\"duration_seconds\":5}",
                "{\"duration_seconds\":5}",
                "{\"mode\":\"crash\",\"duration_seconds\":5,\"extra\":1}",
                "{\"mode\":\"crash\",\"duration_seconds\":5,\"delay_ms\":100}",
                "{\"mode\":\"pool_exhaust\",\"duration_seconds\":10,\"connections\":4}",
                "{\"mode\":\"crash\",\"mode\":\"oom\",\"duration_seconds\":5}",
                "{\"mode\":\"crash\",\"duration_seconds\":5} {}",
                "[{\"mode\":\"crash\",\"duration_seconds\":5}]",
                "{not json",
                ""));
        bad.add("{\"mode\":\"latency\",\"delay_ms\":100,\"duration_seconds\":5,\"pad\":\"" + "x".repeat(1100) + "\"}");
        // Valid JSON but over 1 KiB via whitespace padding is still rejected on size alone.
        bad.add("{\"mode\":\"latency\",\"delay_ms\":100,\"duration_seconds\":5" + " ".repeat(1100) + "}");
        for (String body : bad) {
            MvcResult r = inject(body);
            assertThat(r.getResponse().getStatus()).as(body.length() > 80 ? "oversized body" : body).isEqualTo(400);
            assertThat(json(r).get("error").asText()).isEqualTo("invalid_request");
        }
        mvc.perform(auth(post("/internal/chaos/v1/reset"), TOKEN).contentType(MediaType.APPLICATION_JSON)
                .content("{\"mode\":\"off\"}")).andExpect(status().isBadRequest());
        assertThat(faultStatus().get("mode").asText()).isEqualTo("off");
        assertThat(STATE_DIR.resolve("intent.json")).doesNotExist();
    }

    // ------------------------------------------------------------------ one fault rule

    @Test
    void secondFaultIs409WhileOneIsActive() throws Exception {
        MvcResult first = inject("{\"mode\":\"latency\",\"delay_ms\":5,\"duration_seconds\":30}");
        assertThat(first.getResponse().getStatus()).isEqualTo(202);
        MvcResult second = inject("{\"mode\":\"endpoint_off\",\"duration_seconds\":5}");
        assertThat(second.getResponse().getStatus()).isEqualTo(409);
        JsonNode b = json(second);
        assertThat(b.get("error").asText()).isEqualTo("fault_active");
        assertThat(b.get("mode").asText()).isEqualTo("latency");
        assertThat(b.get("expires_at").asText()).isEqualTo(json(first).get("expires_at").asText());
    }

    // ------------------------------------------------------------------ latency (contract 2.3)

    @Test
    void latencyDelaysTheDataPathThenClearsOnExpiry(CapturedOutput out) throws Exception {
        try (var connection = acquireSetupConnection()) {
            assertThat(connection.isValid(1)).isTrue();
        }
        long baseline = timeCheckMs(200);
        int durationSeconds = 3;
        MvcResult r = inject("{\"mode\":\"latency\",\"delay_ms\":300,\"duration_seconds\":" + durationSeconds + "}");
        assertThat(r.getResponse().getStatus()).isEqualTo(202);
        JsonNode b = json(r);
        assertThat(b.get("mode").asText()).isEqualTo("latency");
        assertThat(b.get("delay_ms").asInt()).isEqualTo(300);
        assertThat(b.get("effect").asText()).isEqualTo("pending");
        assertThat(b.get("operation_id").asText()).matches("[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[0-9a-f]{4}-[0-9a-f]{12}");
        Instant accepted = Instant.parse(b.get("accepted_at").asText());
        assertThat(Instant.parse(b.get("expires_at").asText())).isEqualTo(accepted.plusSeconds(durationSeconds));
        assertThat(b.get("accepted_at").asText()).matches("\\d{4}-\\d\\d-\\d\\dT\\d\\d:\\d\\d:\\d\\d\\.\\d{3}Z");

        JsonNode s = faultStatus();
        assertThat(s.get("mode").asText()).isEqualTo("latency");
        assertThat(s.get("latency_delay_ms").asInt()).isEqualTo(300);
        assertThat(s.get("pool_max").asInt()).isEqualTo(4);
        assertThat(gauge("liveprobe_chaos_active", "latency")).isEqualTo(1.0);

        long active = timeCheckMs(200); // data path still 200, just slower
        assertThat(active).isGreaterThanOrEqualTo(300);
        assertThat(baseline).isLessThan(300);

        await(6000, () -> "off".equals(faults.activeMode()));
        assertThat(faultStatus().get("latency_delay_ms").asInt()).isZero();
        assertThat(timeCheckMs(200)).isLessThan(300);
        assertThat(gauge("liveprobe_chaos_active", "latency")).isZero();
        assertThat(counter("latency", "expired")).isGreaterThanOrEqualTo(1);
        assertThat(out.getOut()).contains("\"chaos_event\":\"accepted\"").contains("\"chaos_event\":\"expired\"")
                .contains("\"operation_id\":\"" + b.get("operation_id").asText() + "\"");
    }

    @Test
    void latencyResetClearsImmediately() throws Exception {
        assertThat(inject("{\"mode\":\"latency\",\"delay_ms\":800,\"duration_seconds\":30}").getResponse().getStatus())
                .isEqualTo(202);
        MvcResult r = mvc.perform(auth(post("/internal/chaos/v1/reset"), TOKEN)).andExpect(status().isOk()).andReturn();
        JsonNode b = json(r);
        assertThat(b.get("mode").asText()).isEqualTo("off");
        assertThat(b.get("previous_mode").asText()).isEqualTo("latency");
        assertThat(b.get("released_connections").asInt()).isZero();
        assertThat(b.get("intent_cleared").asBoolean()).isFalse();
        assertThat(timeCheckMs(200)).isLessThan(800);
        assertThat(faultStatus().get("mode").asText()).isEqualTo("off");
        // Reset is idempotent.
        JsonNode again = json(mvc.perform(auth(post("/internal/chaos/v1/reset"), TOKEN)
                .contentType(MediaType.APPLICATION_JSON).content("{}")).andExpect(status().isOk()).andReturn());
        assertThat(again.get("previous_mode").asText()).isEqualTo("off");
    }

    @Test
    void atMost32RequestsSleepAndTheRestAreBypassedAndCounted() throws Exception {
        assertThat(inject("{\"mode\":\"latency\",\"delay_ms\":600,\"duration_seconds\":10}").getResponse().getStatus())
                .isEqualTo(202);
        double before = meters.get("liveprobe_chaos_latency_bypassed").counter().count();
        ExecutorService pool = Executors.newFixedThreadPool(40);
        CountDownLatch go = new CountDownLatch(1);
        List<Future<?>> fs = new ArrayList<>();
        for (int i = 0; i < 40; i++) fs.add(pool.submit(() -> { go.await(); faults.beforeDataPath(); return null; }));
        go.countDown();
        for (Future<?> f : fs) f.get(5, TimeUnit.SECONDS);
        pool.shutdown();
        assertThat(meters.get("liveprobe_chaos_latency_bypassed").counter().count() - before).isEqualTo(8.0);
    }

    // ------------------------------------------------------------------ pool_exhaust (contract 2.4)

    @Test
    void poolExhaustHoldsAllNConnectionsDataPath503ThenReleasesOnExpiry(CapturedOutput out) throws Exception {
        try (var connection = acquireSetupConnection()) {
            assertThat(connection.isValid(1)).isTrue();
        }
        timeCheckMs(200); // warm the pool
        HikariPoolMXBean pool = dataSource.getHikariPoolMXBean();
        double timeoutsBefore = meters.get("hikaricp.connections.timeout").tag("pool", "order").counter().count();

        int durationSeconds = 6;
        MvcResult r = inject("{\"mode\":\"pool_exhaust\",\"duration_seconds\":" + durationSeconds + "}");
        assertThat(r.getResponse().getStatus()).isEqualTo(202);
        JsonNode b = json(r);
        assertThat(b.get("effect").asText()).isEqualTo("held");
        assertThat(Instant.parse(b.get("expires_at").asText()))
                .isEqualTo(Instant.parse(b.get("accepted_at").asText()).plusSeconds(durationSeconds));
        assertThat(b.get("held_connections").asInt()).isEqualTo(4);
        assertThat(b.get("pool_max").asInt()).isEqualTo(4);
        assertThat(pool.getActiveConnections()).isEqualTo(4);
        assertThat(pool.getIdleConnections()).isZero();
        assertThat(meters.get("hikaricp.connections.active").tag("pool", "order").gauge().value()).isEqualTo(4.0);
        assertThat(meters.get("hikaricp.connections.idle").tag("pool", "order").gauge().value()).isZero();
        JsonNode s = faultStatus();
        assertThat(s.get("mode").asText()).isEqualTo("pool_exhaust");
        assertThat(s.get("held_connections").asInt()).isEqualTo(4);

        // A data-path request in flight waits for a connection (pending > 0), then times out -> 503 after >= 0.9 s.
        ExecutorService ex = Executors.newSingleThreadExecutor();
        Future<MvcResult> inflight = ex.submit(() -> {
            long t0 = System.nanoTime();
            MvcResult res = mvc.perform(get("/api/orders/check").param("item", "a")).andReturn();
            res.getResponse().setHeader("X-Elapsed-Ms", String.valueOf((System.nanoTime() - t0) / 1_000_000));
            return res;
        });
        MvcResult slow;
        try {
            await(900, () -> pool.getThreadsAwaitingConnection() > 0
                    && meters.get("hikaricp.connections.pending").tag("pool", "order").gauge().value() > 0);
            slow = inflight.get(5, TimeUnit.SECONDS);
        } finally {
            ex.shutdownNow();
        }
        assertThat(slow.getResponse().getStatus()).isEqualTo(503);
        assertThat(Long.parseLong(slow.getResponse().getHeader("X-Elapsed-Ms"))).isGreaterThanOrEqualTo(900);
        JsonNode err = JSON.readTree(slow.getResponse().getContentAsString());
        assertThat(err.get("error").asText()).isEqualTo("order database unavailable");
        assertThat(err.get("item").asText()).isEqualTo("a");
        assertThat(meters.get("hikaricp.connections.timeout").tag("pool", "order").counter().count())
                .isGreaterThan(timeoutsBefore);
        assertThat(meters.get("order_db_lookups").tag("outcome", "db_error").counter().count()).isGreaterThanOrEqualTo(1);

        // Expiry releases every held connection; data path is back to 200.
        await(8000, () -> "off".equals(faults.activeMode()));
        await(5000, () -> pool.getActiveConnections() == 0);
        assertThat(faultStatus().get("held_connections").asInt()).isZero();
        timeCheckMs(200);
        assertThat(out.getOut()).contains("\"chaos_event\":\"pool_held\"").contains("\"chaos_event\":\"pool_released\"");
    }

    @Test
    void poolExhaustResetReleasesImmediately() throws Exception {
        assertThat(inject("{\"mode\":\"pool_exhaust\",\"duration_seconds\":30}").getResponse().getStatus()).isEqualTo(202);
        assertThat(dataSource.getHikariPoolMXBean().getActiveConnections()).isEqualTo(4);
        JsonNode b = json(mvc.perform(auth(post("/internal/chaos/v1/reset"), TOKEN)).andExpect(status().isOk()).andReturn());
        assertThat(b.get("previous_mode").asText()).isEqualTo("pool_exhaust");
        assertThat(b.get("released_connections").asInt()).isEqualTo(4);
        assertThat(dataSource.getHikariPoolMXBean().getActiveConnections()).isZero();
        timeCheckMs(200);
    }

    // KAN-119: release must evict, not return N idle connections (contract 2.4 pg_stat_activity <= pre + minimumIdle).

    int h2Sessions() {
        try (var c = dataSource.getConnection(); var st = c.createStatement();
             var rs = st.executeQuery("SELECT COUNT(*) FROM INFORMATION_SCHEMA.SESSIONS")) {
            rs.next();
            return rs.getInt(1);
        } catch (Exception e) {
            return Integer.MAX_VALUE;
        }
    }

    @Test
    void poolExhaustExpiryEvictsHeldConnectionsSoDbSessionsDropToMinimumIdle(CapturedOutput out) throws Exception {
        HikariPoolMXBean pool = dataSource.getHikariPoolMXBean();
        int minIdle = dataSource.getMinimumIdle();
        int preSessions = h2Sessions();
        assertThat(inject("{\"mode\":\"pool_exhaust\",\"duration_seconds\":1}").getResponse().getStatus()).isEqualTo(202);
        assertThat(pool.getActiveConnections()).isEqualTo(4);
        assertThat(pool.getTotalConnections()).isEqualTo(4);
        await(4000, () -> "off".equals(faults.activeMode()));
        assertThat(pool.getActiveConnections()).as("active -> 0 on expiry").isZero();
        assertThat(faults.lastEvictedConnections()).as("every held connection evicted").isEqualTo(4);
        // Physical connections close (async) and the pool refills only to minimumIdle, not back to N idle.
        await(3000, () -> pool.getTotalConnections() <= minIdle);
        await(3000, () -> h2Sessions() <= preSessions + minIdle);
        assertThat(pool.getIdleConnections()).isLessThan(4);
        assertThat(out.getOut()).contains("chaos pool released connections=4 evicted_connections=4");
        timeCheckMs(200);
    }

    @Test
    void poolExhaustResetEvictsAndSecondResetIsIdempotent() throws Exception {
        HikariPoolMXBean pool = dataSource.getHikariPoolMXBean();
        int minIdle = dataSource.getMinimumIdle();
        int preSessions = h2Sessions();
        assertThat(inject("{\"mode\":\"pool_exhaust\",\"duration_seconds\":30}").getResponse().getStatus()).isEqualTo(202);
        assertThat(pool.getActiveConnections()).isEqualTo(4);
        JsonNode b = json(mvc.perform(auth(post("/internal/chaos/v1/reset"), TOKEN)).andExpect(status().isOk()).andReturn());
        assertThat(b.get("released_connections").asInt()).isEqualTo(4);
        assertThat(pool.getActiveConnections()).as("active -> 0 on reset").isZero();
        assertThat(faults.lastEvictedConnections()).isEqualTo(4);
        await(3000, () -> pool.getTotalConnections() <= minIdle);
        await(3000, () -> h2Sessions() <= preSessions + minIdle);
        int totalAfter = pool.getTotalConnections();
        JsonNode again = json(mvc.perform(auth(post("/internal/chaos/v1/reset"), TOKEN)).andExpect(status().isOk()).andReturn());
        assertThat(again.get("released_connections").asInt()).isZero();
        assertThat(faults.lastEvictedConnections()).isZero();
        assertThat(pool.getActiveConnections()).isZero();
        assertThat(pool.getTotalConnections()).isEqualTo(totalAfter);
        timeCheckMs(200);
    }

    @Test
    void evictionHelperFallsBackForNonHikariOrClosedConnections() throws Exception {
        assertThat(HikariEviction.evict(null, null)).isFalse();
        try (var c = dataSource.getConnection()) {
            assertThat(HikariEviction.evict(null, c)).as("no pool -> caller must close").isFalse();
        }
        var closed = dataSource.getConnection();
        closed.close();
        assertThat(HikariEviction.evict(dataSource, closed)).as("already closed -> nothing to evict").isFalse();
        var c = dataSource.getConnection();
        assertThat(dataSource.getHikariPoolMXBean().getActiveConnections()).isEqualTo(1);
        assertThat(HikariEviction.evict(dataSource, c)).isTrue();
        // The physical connection is removed from the pool (the proxy itself is simply discarded, never closed).
        assertThat(dataSource.getHikariPoolMXBean().getActiveConnections()).isZero();
        await(3000, () -> dataSource.getHikariPoolMXBean().getTotalConnections() <= dataSource.getMinimumIdle());
    }

    @Test
    void poolExhaustIs503PoolBusyAndReleasesPartialsWhenSomeoneElseHoldsAConnection() throws Exception {
        try (var foreign = acquireSetupConnection()) {
            MvcResult r = inject("{\"mode\":\"pool_exhaust\",\"duration_seconds\":5}");
            assertThat(r.getResponse().getStatus()).isEqualTo(503);
            assertThat(json(r).get("error").asText()).isEqualTo("precondition_failed");
            assertThat(json(r).get("reason").asText()).isEqualTo("pool_busy");
            assertThat(dataSource.getHikariPoolMXBean().getActiveConnections()).isEqualTo(1);
        }
        assertThat(faultStatus().get("mode").asText()).isEqualTo("off");
    }

    @Test
    void poolOverHardCapIsRejectedWithoutAcquiringConnections() throws Exception {
        int original = dataSource.getMaximumPoolSize();
        try {
            dataSource.setMaximumPoolSize(9);
            MvcResult r = inject("{\"mode\":\"pool_exhaust\",\"duration_seconds\":10}");
            assertThat(r.getResponse().getStatus()).isEqualTo(503);
            assertThat(json(r).get("reason").asText()).isEqualTo("pool_busy");
            assertThat(dataSource.getHikariPoolMXBean().getActiveConnections()).isZero();
            assertThat(faultStatus().get("mode").asText()).isEqualTo("off");
        } finally {
            dataSource.setMaximumPoolSize(original);
        }
    }

    // ------------------------------------------------------------------ endpoint_off (contract 2.5)

    @Test
    void endpointOffRefusesReadinessKeepsLivenessThenRestoresOnExpiry(CapturedOutput out) throws Exception {
        mvc.perform(get("/actuator/health/readiness")).andExpect(status().isOk());
        MvcResult r = inject("{\"mode\":\"endpoint_off\",\"duration_seconds\":1}");
        assertThat(r.getResponse().getStatus()).isEqualTo(202);
        assertThat(json(r).get("readiness").asText()).isEqualTo("REFUSING_TRAFFIC");
        mvc.perform(get("/actuator/health/readiness")).andExpect(status().isServiceUnavailable());
        mvc.perform(get("/actuator/health/liveness")).andExpect(status().isOk());
        timeCheckMs(200); // still serves when called directly
        assertThat(faultStatus().get("readiness").asText()).isEqualTo("REFUSING_TRAFFIC");
        await(3000, () -> "off".equals(faults.activeMode()));
        mvc.perform(get("/actuator/health/readiness")).andExpect(status().isOk());
        mvc.perform(get("/actuator/health/liveness")).andExpect(status().isOk());
        assertThat(faultStatus().get("readiness").asText()).isEqualTo("ACCEPTING_TRAFFIC");
        assertThat(out.getOut()).contains("\"chaos_event\":\"readiness_refusing\"")
                .contains("\"chaos_event\":\"readiness_accepting\"");
    }

    @Test
    void endpointOffResetRestoresReadinessImmediately() throws Exception {
        assertThat(inject("{\"mode\":\"endpoint_off\",\"duration_seconds\":30}").getResponse().getStatus()).isEqualTo(202);
        mvc.perform(get("/actuator/health/readiness")).andExpect(status().isServiceUnavailable());
        mvc.perform(get("/actuator/health/liveness")).andExpect(status().isOk());
        mvc.perform(auth(post("/internal/chaos/v1/reset"), TOKEN)).andExpect(status().isOk())
                .andExpect(jsonPath("$.previous_mode").value("endpoint_off"));
        mvc.perform(get("/actuator/health/readiness")).andExpect(status().isOk());
    }

    @Test
    void oldResetReceiptCannotClearAnotherOperation() throws Exception {
        String fault = "{\"mode\":\"endpoint_off\",\"duration_seconds\":30}";
        String oldId = json(inject(fault)).path("operation_id").asText();
        String oldReset = "{\"expected_operation_id\":\"" + oldId + "\"}";
        mvc.perform(auth(post("/internal/chaos/v1/reset"), TOKEN)
                        .contentType(MediaType.APPLICATION_JSON).content(oldReset))
                .andExpect(status().isOk()).andExpect(jsonPath("$.reset_operation_id").value(oldId));

        String currentId = json(inject(fault)).path("operation_id").asText();
        mvc.perform(auth(post("/internal/chaos/v1/reset"), TOKEN)
                        .contentType(MediaType.APPLICATION_JSON).content(oldReset))
                .andExpect(status().isConflict()).andExpect(jsonPath("$.error").value("operation_not_current"));
        assertThat(faultStatus().path("operation_id").asText()).isEqualTo(currentId);
        mvc.perform(get("/actuator/health/readiness")).andExpect(status().isServiceUnavailable());
    }

    // ------------------------------------------------------------------ crash (contract 2.1)

    @Test
    void crashWritesA0600IntentAndHaltsWith70(CapturedOutput out) throws Exception {
        MvcResult r = inject("{\"mode\":\"crash\",\"duration_seconds\":9}");
        assertThat(r.getResponse().getStatus()).isEqualTo(202);
        JsonNode b = json(r);
        assertThat(b.get("exit_code").asInt()).isEqualTo(70);
        assertThat(b.get("effect").asText()).isEqualTo("pending");
        Path intent = STATE_DIR.resolve("intent.json");
        JsonNode i = JSON.readTree(Files.readAllBytes(intent));
        assertThat(i.get("v").asInt()).isEqualTo(1);
        assertThat(i.get("mode").asText()).isEqualTo("crash");
        assertThat(i.get("operation_id").asText()).isEqualTo(b.get("operation_id").asText());
        assertThat(i.get("armed_until_ms").asLong() - i.get("created_ms").asLong()).isEqualTo(9000);
        assertThat(Files.getPosixFilePermissions(intent))
                .containsExactlyInAnyOrder(PosixFilePermission.OWNER_READ, PosixFilePermission.OWNER_WRITE);
        await(2000, () -> !EXIT.codes.isEmpty());
        assertThat(EXIT.codes).containsExactly(70);
        assertThat(out.getOut()).contains("\"chaos_event\":\"crash_exit\"");
        // crash never appears in GET status.
        assertThat(faultStatus().get("mode").asText()).isEqualTo("off");
        // If the JVM is alive, reset deletes the intent.
        mvc.perform(auth(post("/internal/chaos/v1/reset"), TOKEN)).andExpect(status().isOk())
                .andExpect(jsonPath("$.intent_cleared").value(true));
        assertThat(intent).doesNotExist();
    }

    @Test
    void crashAndOomAre503WhenTheStateDirIsMissing() throws Exception {
        Files.writeString(CGROUP.resolve("memory.max"), String.valueOf(256L * MemoryLimitProbe.MIB));
        Files.delete(STATE_DIR);
        for (String body : List.of("{\"mode\":\"crash\",\"duration_seconds\":5}", "{\"mode\":\"oom\",\"duration_seconds\":5}")) {
            MvcResult r = inject(body);
            assertThat(r.getResponse().getStatus()).isEqualTo(503);
            assertThat(json(r).get("reason").asText()).isEqualTo("state_dir_unavailable");
        }
        assertThat(EXIT.codes).isEmpty();
    }

    // ------------------------------------------------------------------ oom (contract 2.2)

    @Test
    void oomPreconditionsAre503() throws Exception {
        Files.deleteIfExists(CGROUP.resolve("memory.max"));
        assertThat(json(inject("{\"mode\":\"oom\",\"duration_seconds\":8}")).get("reason").asText())
                .isEqualTo("memory_limit_unknown");
        Files.writeString(CGROUP.resolve("memory.max"), "max\n");
        MvcResult r = inject("{\"mode\":\"oom\",\"duration_seconds\":8}");
        assertThat(r.getResponse().getStatus()).isEqualTo(503);
        assertThat(json(r).get("reason").asText()).isEqualTo("memory_limit_unknown");
        Files.writeString(CGROUP.resolve("memory.max"), String.valueOf(512L * MemoryLimitProbe.MIB + 1));
        assertThat(json(inject("{\"mode\":\"oom\",\"duration_seconds\":8}")).get("reason").asText())
                .isEqualTo("memory_limit_too_large");
        assertThat(STATE_DIR.resolve("intent.json")).doesNotExist();
        assertThat(faultStatus().get("mode").asText()).isEqualTo("off");
    }

    @Test
    void oomAllocatesUpToLimitPlus64MiBThenFreesAndAbortsWhenNotKilled(CapturedOutput out) throws Exception {
        Files.writeString(CGROUP.resolve("memory.max"), String.valueOf(32L * MemoryLimitProbe.MIB) + "\n");
        long peakBefore = MEMORY.peak.get();
        MvcResult r = inject("{\"mode\":\"oom\",\"duration_seconds\":8}");
        assertThat(r.getResponse().getStatus()).isEqualTo(202);
        assertThat(json(r).get("allocation_cap_mib").asInt()).isEqualTo(32 + 64);
        await(3000, () -> "off".equals(faults.activeMode()));
        assertThat(MEMORY.peak.get()).isEqualTo(Math.max(peakBefore, 96L * MemoryLimitProbe.MIB));
        assertThat(MEMORY.live.get()).isZero(); // everything freed
        JsonNode intent = JSON.readTree(Files.readAllBytes(STATE_DIR.resolve("intent.json")));
        assertThat(intent.get("aborted").asBoolean()).isTrue();
        assertThat(intent.get("mode").asText()).isEqualTo("oom");
        assertThat(out.getOut()).contains("\"chaos_event\":\"oom_alloc_start\"").contains("\"chaos_event\":\"oom_not_killed\"");
    }

    @Test
    void oomResetStopsAllocationAndFreesEverything() throws Exception {
        Files.writeString(CGROUP.resolve("memory.max"), String.valueOf(256L * MemoryLimitProbe.MIB));
        MEMORY.sleepPerChunkMs = 50;
        try {
            assertThat(inject("{\"mode\":\"oom\",\"duration_seconds\":20}").getResponse().getStatus()).isEqualTo(202);
            await(3000, () -> MEMORY.live.get() > 0);
            JsonNode b = json(mvc.perform(auth(post("/internal/chaos/v1/reset"), TOKEN)).andExpect(status().isOk()).andReturn());
            assertThat(b.get("previous_mode").asText()).isEqualTo("oom");
            assertThat(b.get("intent_cleared").asBoolean()).isTrue();
            await(3000, () -> MEMORY.live.get() == 0);
            assertThat(MEMORY.peak.get()).isLessThanOrEqualTo(320L * MemoryLimitProbe.MIB);
            assertThat(STATE_DIR.resolve("intent.json")).doesNotExist();
        } finally {
            MEMORY.sleepPerChunkMs = 0;
        }
    }

    // ------------------------------------------------------------------ no secret leakage

    @Test
    void tokenNeverAppearsInLogsOrResponses(CapturedOutput out) throws Exception {
        List<String> bodies = new ArrayList<>();
        bodies.add(inject("{\"mode\":\"latency\",\"delay_ms\":5,\"duration_seconds\":1}").getResponse().getContentAsString());
        bodies.add(inject("{\"mode\":\"latency\",\"delay_ms\":5,\"duration_seconds\":1}").getResponse().getContentAsString());
        bodies.add(inject("{\"mode\":\"bogus\"}").getResponse().getContentAsString());
        bodies.add(mvc.perform(auth(get("/internal/chaos/v1/faults"), TOKEN)).andReturn().getResponse().getContentAsString());
        bodies.add(mvc.perform(auth(post("/internal/chaos/v1/reset"), TOKEN)).andReturn().getResponse().getContentAsString());
        bodies.add(mvc.perform(auth(post("/internal/chaos/v1/faults"), WRONG).contentType(MediaType.APPLICATION_JSON)
                .content("{bad")).andReturn().getResponse().getContentAsString());
        String tokenHashHex = java.util.HexFormat.of().formatHex(
                java.security.MessageDigest.getInstance("SHA-256").digest(TOKEN.getBytes(StandardCharsets.UTF_8)));
        for (String s : bodies) {
            assertThat(s).doesNotContain(TOKEN).doesNotContain(WRONG).doesNotContain("Bearer").doesNotContain(tokenHashHex);
        }
        // CapturedOutput may include MockMvc diagnostic dumps from *other* tests in the same context;
        // inspect only actual JSON log lines, not test framework request dumps (which echo request headers).
        String actualLogs = out.getOut().lines().filter(s -> s.startsWith("{") && s.contains("\"@timestamp\""))
                .collect(java.util.stream.Collectors.joining("\n"));
        assertThat(actualLogs).doesNotContain(TOKEN).doesNotContain(WRONG).doesNotContain(tokenHashHex)
                .doesNotContain("Bearer ");
        assertThat(actualLogs).isNotBlank();
    }
}
