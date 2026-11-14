package io.liveprobe.target.inventory;

import static org.mockito.ArgumentMatchers.anyString;
import static org.mockito.Mockito.when;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.post;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import java.util.Optional;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.WebMvcTest;
import org.springframework.boot.test.context.TestConfiguration;
import org.springframework.context.annotation.Bean;
import org.springframework.dao.DataAccessResourceFailureException;
import org.springframework.test.context.bean.override.mockito.MockitoBean;
import org.springframework.test.web.servlet.MockMvc;

import io.micrometer.core.instrument.MeterRegistry;
import io.micrometer.core.instrument.simple.SimpleMeterRegistry;

@WebMvcTest(StockController.class)
class StockControllerTest {
    @TestConfiguration
    static class Meters {
        @Bean
        MeterRegistry meterRegistry() {
            return new SimpleMeterRegistry();
        }
    }

    @Autowired
    MockMvc mvc;

    @MockitoBean
    StockRepository repository;

    @Test
    void knownItemReturnsStock() throws Exception {
        when(repository.quantity("a")).thenReturn(Optional.of(42));
        mvc.perform(get("/api/stock/a"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.item").value("a"))
                .andExpect(jsonPath("$.quantity").value(42))
                .andExpect(jsonPath("$.available").value(true));
    }

    @Test
    void zeroStockIsUnavailable() throws Exception {
        when(repository.quantity("c")).thenReturn(Optional.of(0));
        mvc.perform(get("/api/stock/c"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.available").value(false));
    }

    @Test
    void unknownItemIs404() throws Exception {
        when(repository.quantity(anyString())).thenReturn(Optional.empty());
        mvc.perform(get("/api/stock/nope")).andExpect(status().isNotFound());
    }

    @Test
    void chaosEndpointAbsentWithoutProfile() throws Exception {
        mvc.perform(post("/internal/chaos/reset")).andExpect(status().isNotFound());
    }

    @Test
    void databaseFailureIs503NeverFakeStock() throws Exception {
        when(repository.quantity(anyString())).thenThrow(new DataAccessResourceFailureException("down"));
        mvc.perform(get("/api/stock/a")).andExpect(status().isServiceUnavailable());
    }
}
