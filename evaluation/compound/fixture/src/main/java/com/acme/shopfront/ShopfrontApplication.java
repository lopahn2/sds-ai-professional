package com.acme.shopfront;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.boot.context.properties.ConfigurationPropertiesScan;

@SpringBootApplication
@ConfigurationPropertiesScan
public class ShopfrontApplication {
    public static void main(String[] args) {
        SpringApplication.run(ShopfrontApplication.class, args);
    }
}
