package io.liveprobe.target.inventory;
import org.junit.jupiter.api.Test;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.test.context.ActiveProfiles;
import org.springframework.beans.factory.annotation.Autowired;
import io.micrometer.core.instrument.MeterRegistry;
import static org.assertj.core.api.Assertions.*;
@SpringBootTest(webEnvironment=SpringBootTest.WebEnvironment.RANDOM_PORT,properties={"CHAOS_OPERATOR_TOKEN=local-self-traffic-test-0123456789","SELF_TRAFFIC_INTERVAL_SECONDS=0.1","spring.datasource.url=jdbc:h2:mem:selftraffic;DB_CLOSE_DELAY=-1","spring.datasource.username=sa","spring.datasource.password=","spring.sql.init.mode=always","spring.sql.init.schema-locations=classpath:chaos-test-schema.sql"})
@ActiveProfiles("chaos")
class AppSelfTrafficTest {
 @Autowired MeterRegistry meters;
 @Test void configuredTrafficTraversesRealHttpRoutes() throws Exception {
  long end=System.nanoTime()+3_000_000_000L;
  while(System.nanoTime()<end && meters.find("http.server.requests").timers().stream().mapToLong(t->t.count()).sum()<5)Thread.sleep(50);
  var uris=meters.find("http.server.requests").timers().stream().map(t->t.getId().getTag("uri")).toList();
  assertThat(uris).contains("/api/stock/{item}","/api/stock/{item}/reserve","/api/stock/{item}/summary");
 }
}
