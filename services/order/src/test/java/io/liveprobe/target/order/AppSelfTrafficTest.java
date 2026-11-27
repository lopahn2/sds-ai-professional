package io.liveprobe.target.order;
import java.util.Set;
import java.util.stream.Collectors;
import org.junit.jupiter.api.Test;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.DynamicPropertyRegistry;
import org.springframework.test.context.DynamicPropertySource;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.beans.factory.annotation.Autowired;
import io.micrometer.core.instrument.MeterRegistry;
import static org.assertj.core.api.Assertions.*;
@SpringBootTest(webEnvironment=SpringBootTest.WebEnvironment.RANDOM_PORT,properties={"SELF_TRAFFIC_INTERVAL_SECONDS=0.1", "management.metrics.distribution.percentiles-histogram.http.server.requests=true","inventory.base-url=http://127.0.0.1:1","inventory.max-retries=0"})
@ActiveProfiles("chaos")
class AppSelfTrafficTest {
 static final String TOKEN = java.util.UUID.randomUUID().toString();

 @DynamicPropertySource
 static void faultToken(DynamicPropertyRegistry registry) {
     registry.add("CHAOS_OPERATOR_TOKEN", () -> TOKEN);
 }

 @Autowired MeterRegistry meters;
 @Test void configuredTrafficTraversesRealHttpRoutes() throws Exception {
  Set<String> expected = Set.of("/api/orders/check", "/api/orders", "/api/orders/{id}");
  long deadline = System.nanoTime() + 12_000_000_000L;
  Set<String> observed = completedRoutes();
  while (!observed.containsAll(expected) && System.nanoTime() < deadline) {
   Thread.sleep(50);
   observed = completedRoutes();
  }
  assertThat(observed).containsAll(expected);
 }

 private Set<String> completedRoutes() {
  return meters.find("http.server.requests").timers().stream().filter(timer -> timer.count() > 0)
      .map(timer -> timer.getId().getTag("uri")).collect(Collectors.toSet());
 }
}
