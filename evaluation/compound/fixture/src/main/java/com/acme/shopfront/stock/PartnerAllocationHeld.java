package com.acme.shopfront.stock;

/** The partner has frozen its allocation for this SKU; the reservation is deferred, not failed. */
public class PartnerAllocationHeld extends RuntimeException {
    public PartnerAllocationHeld(String sku) {
        super("partner allocation on hold for " + sku);
    }
}
