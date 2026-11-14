package io.liveprobe.target.order;

import java.time.Duration;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.autoconfigure.jdbc.DataSourceAutoConfiguration;
import org.springframework.context.annotation.Bean;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import org.springframework.web.client.RestClient;

import io.liveprobe.target.order.chaos.BootstrapGuard;

// DataSource auto-configuration is excluded: the order DB exists only when ORDER_DB_ENABLED=true (OrderDbConfig).
@SpringBootApplication(exclude = DataSourceAutoConfiguration.class)
public class OrderApplication {
    public static void main(String[] args) {
        // KAN-96 contract 1.6: acts on an armed crash/oom intent BEFORE Spring starts. No-op unless
        // SPRING_PROFILES_ACTIVE contains "chaos" and CHAOS_STATE_DIR is set.
        BootstrapGuard.fromEnvironment().run();
        SpringApplication.run(OrderApplication.class, args);
    }

    @Bean
    RestClient inventoryClient(RestClient.Builder builder,
                               @Value("${inventory.base-url}") String baseUrl,
                               @Value("${inventory.connect-timeout-ms:500}") int connectTimeoutMs,
                               @Value("${inventory.timeout-ms:2000}") int timeoutMs) {
        SimpleClientHttpRequestFactory rf = new SimpleClientHttpRequestFactory();
        rf.setConnectTimeout(Duration.ofMillis(connectTimeoutMs));
        rf.setReadTimeout(Duration.ofMillis(timeoutMs));
        // The auto-configured builder carries Micrometer observation -> http_client_requests_seconds.
        return builder.baseUrl(baseUrl).requestFactory(rf).build();
    }
}
