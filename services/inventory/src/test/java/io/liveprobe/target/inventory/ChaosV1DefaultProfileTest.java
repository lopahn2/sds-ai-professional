package io.liveprobe.target.inventory;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.http.MediaType;
import org.springframework.test.web.servlet.MockMvc;

/** KAN-96 §1.1: without profile `chaos` none of the fault routes exist, even with a token configured. */
@SpringBootTest(properties = {
        "spring.datasource.url=jdbc:h2:mem:inv-default;DB_CLOSE_DELAY=-1",
        "spring.datasource.username=sa",
        "spring.datasource.password=",
        "CHAOS_OPERATOR_TOKEN=default-profile-token-that-is-long-enough-000",
})
@AutoConfigureMockMvc
class ChaosV1DefaultProfileTest {
    private static final String AUTH = "Bearer default-profile-token-that-is-long-enough-000";

    @Autowired
    MockMvc mvc;

    @Test
    void allChaosRoutesAre404InDefaultProfile() throws Exception {
        String body = "{\"mode\":\"latency\",\"delay_ms\":100,\"duration_seconds\":5}";
        mvc.perform(post("/internal/chaos/v1/faults").header("Authorization", AUTH)
                .contentType(MediaType.APPLICATION_JSON).content(body)).andExpect(status().isNotFound());
        mvc.perform(get("/internal/chaos/v1/faults").header("Authorization", AUTH))
                .andExpect(status().isNotFound());
        mvc.perform(post("/internal/chaos/v1/reset").header("Authorization", AUTH))
                .andExpect(status().isNotFound());
        mvc.perform(post("/internal/chaos/inject").header("Authorization", AUTH)
                .contentType(MediaType.APPLICATION_JSON).content("{\"duration_seconds\":1,\"delay_ms\":1}"))
                .andExpect(status().isNotFound());
        mvc.perform(post("/internal/chaos/oom").header("Authorization", AUTH)).andExpect(status().isNotFound());
        mvc.perform(post("/internal/chaos/reset").header("Authorization", AUTH)).andExpect(status().isNotFound());
    }
}
