package io.liveprobe.target.order;

import static org.assertj.core.api.Assertions.assertThat;
import static org.springframework.test.web.client.match.MockRestRequestMatchers.requestTo;
import static org.springframework.test.web.client.response.MockRestResponseCreators.withResourceNotFound;
import static org.springframework.test.web.client.response.MockRestResponseCreators.withServerError;
import static org.springframework.test.web.client.response.MockRestResponseCreators.withServiceUnavailable;
import static org.springframework.test.web.client.response.MockRestResponseCreators.withSuccess;
import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.springframework.http.MediaType;
import org.springframework.test.web.client.ExpectedCount;
import org.springframework.test.web.client.MockRestServiceServer;
import org.springframework.test.web.servlet.MockMvc;
import org.springframework.test.web.servlet.setup.MockMvcBuilders;
import org.springframework.web.client.RestClient;

import io.micrometer.core.instrument.simple.SimpleMeterRegistry;

class OrderControllerTest {
    MockRestServiceServer server;
    MockMvc mvc;

    @BeforeEach
    void setUp() {
        RestClient.Builder builder = RestClient.builder().baseUrl("http://inventory");
        server = MockRestServiceServer.bindTo(builder).build();
        mvc = MockMvcBuilders.standaloneSetup(new OrderController(builder.build())).build();
    }

    @Test
    void returnsInventoryData() throws Exception {
        server.expect(requestTo("http://inventory/api/stock/a"))
                .andRespond(withSuccess("{\"item\":\"a\",\"quantity\":42,\"available\":true}", MediaType.APPLICATION_JSON));
        mvc.perform(get("/api/orders/check").param("item", "a"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.orderable").value(true))
                .andExpect(jsonPath("$.inventory.quantity").value(42));
    }

    @Test
    void inventoryFailureIs502() throws Exception {
        // one bounded retry, then 502
        server.expect(requestTo("http://inventory/api/stock/a")).andRespond(withServerError());
        server.expect(requestTo("http://inventory/api/stock/a")).andRespond(withServerError());
        mvc.perform(get("/api/orders/check").param("item", "a"))
                .andExpect(status().isBadGateway());
        server.verify();
    }

    @Test
    void transientFailureIsRetriedOnce() throws Exception {
        server.expect(requestTo("http://inventory/api/stock/a")).andRespond(withServiceUnavailable());
        server.expect(requestTo("http://inventory/api/stock/a"))
                .andRespond(withSuccess("{\"item\":\"a\",\"quantity\":42,\"available\":true}", MediaType.APPLICATION_JSON));
        mvc.perform(get("/api/orders/check").param("item", "a"))
                .andExpect(status().isOk());
        server.verify();
    }

    @Test
    void notFoundIsNotRetried() throws Exception {
        server.expect(ExpectedCount.once(), requestTo("http://inventory/api/stock/x")).andRespond(withResourceNotFound());
        mvc.perform(get("/api/orders/check").param("item", "x"))
                .andExpect(status().isNotFound());
        server.verify();
    }

    @Test
    void retriesAreCappedAtOne() {
        OrderController c = new OrderController(RestClient.create(), new SimpleMeterRegistry(), 99, 99_999);
        assertThat(c).extracting("maxRetries").isEqualTo(1);
        assertThat(c).extracting("retryBackoffMs").isEqualTo(500L);
    }
}
