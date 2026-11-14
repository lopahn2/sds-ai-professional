package io.liveprobe.target.order;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.jdbc.core.JdbcTemplate;

import com.zaxxer.hikari.HikariDataSource;
import com.zaxxer.hikari.metrics.micrometer.MicrometerMetricsTrackerFactory;

import io.micrometer.core.instrument.Gauge;
import io.micrometer.core.instrument.MeterRegistry;

/**
 * KAN-96 contract 3: the order_app DataSource, created only when ORDER_DB_ENABLED=true (chaos overlay).
 * The pool is lazily started on first use so Spring's Hikari meter binder can attach
 * (hikaricp_connections_* with pool="order").
 */
@Configuration(proxyBeanMethods = false)
@ConditionalOnProperty(name = "order.db.enabled", havingValue = "true")
public class OrderDbConfig {

    @Bean(destroyMethod = "close")
    public HikariDataSource orderDataSource(@Value("${order.db.url:}") String url,
                                            @Value("${order.db.user:order_app}") String user,
                                            @Value("${order.db.password:}") String password,
                                            @Value("${order.db.pool-max:4}") int poolMax,
                                            @Value("${order.db.connection-timeout-ms:1000}") long connectionTimeoutMs,
                                            MeterRegistry meters) {
        if (url == null || url.isBlank()) {
            throw new IllegalStateException("ORDER_DB_URL is required when ORDER_DB_ENABLED=true");
        }
        if (poolMax < 1) {
            throw new IllegalStateException("ORDER_DB_POOL_MAX must be >= 1");
        }
        HikariDataSource ds = new HikariDataSource();
        ds.setPoolName("order");
        ds.setJdbcUrl(url);
        ds.setUsername(user);
        ds.setPassword(password);
        ds.setMaximumPoolSize(poolMax);
        ds.setMinimumIdle(1);
        ds.setConnectionTimeout(connectionTimeoutMs);
        ds.setInitializationFailTimeout(-1);
        ds.setMetricsTrackerFactory(new MicrometerMetricsTrackerFactory(meters));
        // Hikari's PoolStats caches gauges for 1 s, while connectionTimeout is 1 s. A pending
        // request may disappear before a scrape sees it. Bind this one gauge to the live MXBean
        // before the pool starts; Hikari's later cached duplicate is ignored by the registry.
        Gauge.builder("hikaricp.connections.pending", ds, h -> {
                    var p = h.getHikariPoolMXBean();
                    return p == null ? 0 : p.getThreadsAwaitingConnection();
                })
                .tag("pool", "order").register(meters);
        if (url.startsWith("jdbc:postgresql:")) {
            ds.addDataSourceProperty("connectTimeout", "2");
            ds.addDataSourceProperty("socketTimeout", "5");
        }
        return ds;
    }

    @Bean
    public JdbcTemplate orderJdbcTemplate(HikariDataSource orderDataSource) {
        JdbcTemplate t = new JdbcTemplate(orderDataSource);
        t.setQueryTimeout(1);
        return t;
    }

    @Bean
    public OrderDbLookup orderDbLookup(JdbcTemplate orderJdbcTemplate) {
        return new OrderDbLookup(orderJdbcTemplate);
    }
}
