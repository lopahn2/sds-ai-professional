package io.liveprobe.target.order.chaos;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import javax.sql.DataSource;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.context.ApplicationContext;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;

import io.liveprobe.target.order.DataPathHook;
import io.liveprobe.target.order.OrderDbLookup;

/** Contract 1.1 / 3: default profile has no chaos routes (404), no chaos beans and no DataSource. */
@SpringBootTest(properties = "CHAOS_OPERATOR_TOKEN=default-profile-token-must-be-ignored-0123456789")
@AutoConfigureMockMvc
class ChaosDefaultProfileTest {
    static final String TOKEN = "default-profile-token-must-be-ignored-0123456789";
    static final String BODY = "{\"mode\":\"latency\",\"delay_ms\":100,\"duration_seconds\":1}";

    @Autowired MockMvc mvc;
    @Autowired ApplicationContext ctx;

    @Test
    void allV1RoutesAre404InDefaultProfileEvenWithAValidToken() throws Exception {
        mvc.perform(get("/internal/chaos/v1/faults").header("Authorization", "Bearer " + TOKEN))
                .andExpect(status().isNotFound());
        mvc.perform(post("/internal/chaos/v1/faults").header("Authorization", "Bearer " + TOKEN)
                .contentType(MediaType.APPLICATION_JSON).content(BODY))
                .andExpect(status().isNotFound());
        mvc.perform(post("/internal/chaos/v1/reset").header("Authorization", "Bearer " + TOKEN))
                .andExpect(status().isNotFound());
        mvc.perform(post("/internal/chaos/v1/faults").contentType(MediaType.APPLICATION_JSON).content(BODY))
                .andExpect(status().isNotFound());
    }

    @Test
    void noChaosBeansAndNoDataSourceInDefaultProfile() {
        assertThat(ctx.getBeanNamesForType(ChaosV1Controller.class)).isEmpty();
        assertThat(ctx.getBeanNamesForType(ChaosFaultService.class)).isEmpty();
        assertThat(ctx.getBeanNamesForType(DataPathHook.class)).isEmpty();
        assertThat(ctx.getBeanNamesForType(DataSource.class)).isEmpty();
        assertThat(ctx.getBeanNamesForType(OrderDbLookup.class)).isEmpty();
    }

    @Test
    void probeGroupsExcludeDb() throws Exception {
        mvc.perform(get("/actuator/health/readiness")).andExpect(status().isOk());
        mvc.perform(get("/actuator/health/liveness")).andExpect(status().isOk());
        assertThat(ctx.getEnvironment().getProperty("management.endpoint.health.group.readiness.include"))
                .isEqualTo("readinessState");
        assertThat(ctx.getEnvironment().getProperty("management.endpoint.health.group.liveness.include"))
                .isEqualTo("livenessState");
    }
}
