package com.acme.shopfront.order;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.context.annotation.Profile;
import org.springframework.http.client.SimpleClientHttpRequestFactory;
import org.springframework.stereotype.Component;
import org.springframework.web.client.HttpStatusCodeException;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientException;

@Component
@Profile("order")
class StockClient {
    private static final Logger log = LoggerFactory.getLogger(StockClient.class);
    private final RestClient client;
    private final OrderSettings settings;

    StockClient(OrderSettings settings) {
        this.settings = settings;
        var factory = new SimpleClientHttpRequestFactory();
        factory.setConnectTimeout(settings.connectTimeoutMs());
        factory.setReadTimeout(settings.readTimeoutMs());
        this.client = RestClient.builder().baseUrl(settings.url()).requestFactory(factory).build();
    }

    /** Returns the stock HTTP status, or -1 when every attempt failed. */
    int reserve(String orderId, String sku, String channel, int quantity) {
        int attempts = settings.retries() + 1;
        for (int attempt = 1; attempt <= attempts; attempt++) {
            try {
                var response = client.post()
                        .uri("/api/stock/{sku}/reservations?channel={channel}&quantity={quantity}", sku, channel, quantity)
                        .retrieve().toBodilessEntity();
                return response.getStatusCode().value();
            } catch (HttpStatusCodeException status) {
                if (status.getStatusCode().is4xxClientError()) {
                    return status.getStatusCode().value();
                }
                log.warn("order {} stock reservation attempt {}/{} failed: {} {}", orderId, attempt, attempts,
                        status.getStatusCode().value(), status.getResponseBodyAsString());
            } catch (RestClientException failure) {
                log.warn("order {} stock reservation attempt {}/{} failed: {}", orderId, attempt, attempts,
                        failure.getMessage());
            }
            if (attempt < attempts && settings.backoffMs() > 0) {
                try {
                    Thread.sleep(settings.backoffMs() * (1L << (attempt - 1)));
                } catch (InterruptedException interrupted) {
                    Thread.currentThread().interrupt();
                    return -1;
                }
            }
        }
        return -1;
    }
}
