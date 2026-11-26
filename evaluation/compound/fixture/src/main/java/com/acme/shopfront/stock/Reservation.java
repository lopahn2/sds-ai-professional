package com.acme.shopfront.stock;

public record Reservation(String sku, String outcome, int remaining) {
    static Reservation reserved(String sku, int remaining) {
        return new Reservation(sku, remaining >= 0 ? "reserved" : "sold_out", remaining);
    }

    static Reservation deferred(String sku) {
        return new Reservation(sku, "deferred", -1);
    }
}
