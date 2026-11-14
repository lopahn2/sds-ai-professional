package io.liveprobe.target.inventory;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.boot.web.servlet.FilterRegistrationBean;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.context.annotation.Profile;

import io.micrometer.core.instrument.MeterRegistry;

/** Chaos-profile-only wiring (contract §1.1). None of these beans exist in the default profile. */
@Configuration(proxyBeanMethods = false)
@Profile("chaos")
public class ChaosConfiguration {

    /** Fails fast at startup if the token is blank or shorter than 32 chars; the value is never logged. */
    @Bean
    OperatorAuth chaosOperatorAuth(@Value("${CHAOS_OPERATOR_TOKEN:}") String token) {
        return new OperatorAuth(token);
    }

    @Bean
    ChaosEvents chaosEvents(MeterRegistry registry) {
        return new ChaosEvents(registry);
    }

    @Bean
    ChaosLatency chaosLatency(ChaosEvents events) {
        return new ChaosLatency(events);
    }

    @Bean
    ChaosFaultGate chaosFaultGate() {
        return new ChaosFaultGate();
    }

    @Bean
    ChaosRuntime chaosRuntime(@Value("${CHAOS_STATE_DIR:}") String stateDir,
                              @Value("${liveprobe.chaos.cgroup-root:/sys/fs/cgroup}") String cgroupRoot) {
        return ChaosRuntime.production(stateDir, cgroupRoot);
    }

    @Bean
    FilterRegistrationBean<ChaosLoopbackFilter> chaosLoopbackFilter() {
        FilterRegistrationBean<ChaosLoopbackFilter> reg = new FilterRegistrationBean<>(new ChaosLoopbackFilter());
        reg.addUrlPatterns(ChaosLoopbackFilter.PREFIX + "/*");
        reg.setOrder(new ChaosLoopbackFilter().getOrder());
        return reg;
    }
}
