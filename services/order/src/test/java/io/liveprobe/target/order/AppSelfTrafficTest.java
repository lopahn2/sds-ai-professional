package io.liveprobe.target.order;
import org.junit.jupiter.api.Test;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.beans.factory.annotation.Autowired;
import io.micrometer.core.instrument.MeterRegistry;
import static org.assertj.core.api.Assertions.*;
@SpringBootTest(webEnvironment=SpringBootTest.WebEnvironment.RANDOM_PORT,properties={"CHAOS_OPERATOR_TOKEN=local-self-traffic-test-0123456789", "SELF_TRAFFIC_INTERVAL_SECONDS=0.1", "management.metrics.distribution.percentiles-histogram.http.server.requests=true","inventory.base-url=http://127.0.0.1:1","inventory.max-retries=0"})
@ActiveProfiles("chaos")
class AppSelfTrafficTest {
 @Autowired MeterRegistry meters;
 @Test void configuredTrafficTraversesRealHttpRoutes() throws Exception {
  long end=System.nanoTime()+3_000_000_000L;
  while(System.nanoTime()<end && meters.find("http.server.requests").timers().stream().mapToLong(t->t.count()).sum()<5)Thread.sleep(50);
  var uris=meters.find("http.server.requests").timers().stream().map(t->t.getId().getTag("uri")).toList();
  assertThat(uris).contains("/api/orders/check","/api/orders","/api/orders/{id}");
 }
}
