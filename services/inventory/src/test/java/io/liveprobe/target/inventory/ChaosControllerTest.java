package io.liveprobe.target.inventory;

import static org.junit.jupiter.api.Assertions.*;
import static org.mockito.Mockito.*;

import java.sql.Connection;
import javax.sql.DataSource;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import org.springframework.http.HttpStatus;
import org.springframework.mock.web.MockHttpServletRequest;
import org.springframework.web.server.ResponseStatusException;

class ChaosControllerTest {
    private static final String TOKEN = "a-strong-operator-token-for-unit-tests-only";
    private final DataSource datasource = mock(DataSource.class);
    private final ChaosController chaos = new ChaosController(datasource, TOKEN);

    @AfterEach void cleanup() { chaos.close(); }

    private MockHttpServletRequest request(String token) {
        MockHttpServletRequest request = new MockHttpServletRequest();
        if (token != null) request.addHeader("Authorization", "Bearer " + token);
        return request;
    }

    @Test void refusesWeakToken() {
        assertThrows(IllegalStateException.class, () -> new ChaosController(datasource, ""));
    }

    @Test void authBoundsResetAndExpiry() throws Exception {
        var latency = new ChaosController.FaultRequest(1, 1, null, null, null);
        assertEquals(HttpStatus.UNAUTHORIZED, chaos.inject(request(null), latency).getStatusCode());
        assertEquals(HttpStatus.UNAUTHORIZED, chaos.inject(request("wrong"), latency).getStatusCode());
        assertEquals(HttpStatus.UNAUTHORIZED, chaos.reset(request(null)).getStatusCode());
        assertEquals(HttpStatus.BAD_REQUEST, chaos.inject(request(TOKEN),
                new ChaosController.FaultRequest(31, 1, null, null, null)).getStatusCode());
        assertEquals(HttpStatus.BAD_REQUEST, chaos.inject(request(TOKEN),
                new ChaosController.FaultRequest(1, 1501, null, null, null)).getStatusCode());
        assertEquals(HttpStatus.BAD_REQUEST, chaos.inject(request(TOKEN),
                new ChaosController.FaultRequest(1, null, 6, null, null)).getStatusCode());
        assertEquals(HttpStatus.BAD_REQUEST, chaos.inject(request(TOKEN),
                new ChaosController.FaultRequest(1, 1, 1, null, null)).getStatusCode());
        assertEquals("latency", chaos.inject(request(TOKEN), latency).getBody().get("mode"));
        Thread.sleep(1200);
        assertEquals("off", chaos.reset(request(TOKEN)).getBody().get("mode"));
        assertEquals(HttpStatus.UNAUTHORIZED, chaos.oom(request(null)).getStatusCode());
    }

    @Test void connectionsAreReleasedByResetAndTimeout() throws Exception {
        Connection c = mock(Connection.class);
        when(datasource.getConnection()).thenReturn(c);
        var hold = new ChaosController.FaultRequest(1, null, 1, null, null);
        assertEquals(HttpStatus.OK, chaos.inject(request(TOKEN), hold).getStatusCode());
        chaos.reset(request(TOKEN));
        verify(c).close();
        chaos.inject(request(TOKEN), hold);
        Thread.sleep(1200);
        verify(c, times(2)).close();
    }

    @Test void injectedErrorIsClearedOnReset() {
        var errors = new ChaosController.FaultRequest(1, null, null, null, 100);
        chaos.inject(request(TOKEN), errors);
        assertThrows(ResponseStatusException.class, chaos::beforeLookup);
        chaos.reset(request(TOKEN));
        assertDoesNotThrow(chaos::beforeLookup);
    }
}
