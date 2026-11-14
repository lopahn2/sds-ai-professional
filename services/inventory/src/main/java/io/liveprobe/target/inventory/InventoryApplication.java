package io.liveprobe.target.inventory;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;

@SpringBootApplication
public class InventoryApplication {
    public static void main(String[] args) {
        // KAN-96 §1.6: act on a durable crash/oom intent before Spring starts (no-op outside the chaos profile).
        ChaosBootstrapGuard.fromEnvironment().run();
        SpringApplication.run(InventoryApplication.class, args);
    }
}
