package com.acme.shopfront.order;

import org.springframework.boot.context.properties.ConfigurationProperties;

/** Client policy for the stock service: one attempt plus `retries`, each bounded by the two timeouts. */
@ConfigurationProperties(prefix = "shopfront.order.stock")
public record OrderSettings(String url, int connectTimeoutMs, int readTimeoutMs, int retries, int backoffMs) {
    public OrderSettings {
        if (url == null || url.isBlank()) {
            url = "http://stock:8080";
        }
        if (connectTimeoutMs <= 0) {
            connectTimeoutMs = 500;
        }
        if (readTimeoutMs <= 0) {
            readTimeoutMs = 1800;
        }
        if (retries < 0) {
            retries = 0;
        }
        if (backoffMs < 0) {
            backoffMs = 0;
        }
    }
}
