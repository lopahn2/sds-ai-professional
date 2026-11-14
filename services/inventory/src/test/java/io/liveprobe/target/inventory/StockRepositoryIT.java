package io.liveprobe.target.inventory;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.actuate.observability.AutoConfigureObservability;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.web.servlet.MockMvc;

/**
 * Integration test against a real PostgreSQL provisioned with services/inventory/db/schema.sql.
 * Run by the failsafe plugin in `mvn verify` (not in the image build). Requires INVENTORY_DB_URL,
 * INVENTORY_DB_USER and INVENTORY_DB_PASSWORD; see scripts/verify-target-services.sh.
 */
@SpringBootTest
@AutoConfigureMockMvc
@AutoConfigureObservability
class StockRepositoryIT {
    @Autowired
    StockRepository repository;

    @Autowired
    MockMvc mvc;

    @Test
    void readsBaselineFixtureFromDatabase() {
        assertThat(System.getenv("INVENTORY_DB_URL")).as("INVENTORY_DB_URL must be set for IT").isNotBlank();
        assertThat(repository.quantity("a")).contains(42);
        assertThat(repository.quantity("c")).contains(0);
        assertThat(repository.quantity("nope")).isEmpty();
    }

    @Test
    void httpLookupGoesThroughDatabase() throws Exception {
        mvc.perform(get("/api/stock/widget"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.quantity").value(120));
        mvc.perform(get("/actuator/health"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.status").value("UP"));
    }

    @Test
    void hikariMetricsAreExported() throws Exception {
        mvc.perform(get("/api/stock/a")).andExpect(status().isOk());
        String body = mvc.perform(get("/actuator/prometheus")).andReturn().getResponse().getContentAsString();
        assertThat(body).contains("hikaricp_connections_max{").contains("pool=\"inventory\"");
        assertThat(body).contains("inventory_stock_lookups_total{");
    }
}
