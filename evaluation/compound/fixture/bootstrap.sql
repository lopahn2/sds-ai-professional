-- Shop database used by the stock service. Run as the postgres superuser on a fresh server.
-- The application role has a per-role connection cap, as the shared platform database does.
CREATE ROLE shop_app LOGIN PASSWORD 'shop-app-local' CONNECTION LIMIT 8;
CREATE DATABASE shop OWNER shop_app;
\connect shop
SET ROLE shop_app;
CREATE TABLE stock_item (sku text PRIMARY KEY, available integer NOT NULL);
CREATE TABLE reservation_log (
    id bigserial PRIMARY KEY, sku text NOT NULL, channel text NOT NULL, quantity integer NOT NULL,
    plan bigint NOT NULL, at timestamptz NOT NULL DEFAULT now());
CREATE TABLE partner_allocation (sku text PRIMARY KEY, status text NOT NULL, allocatable integer NOT NULL,
    synced_at timestamptz NOT NULL DEFAULT now());
INSERT INTO stock_item SELECT 'SKU-' || lpad(i::text, 4, '0'), 1000000 FROM generate_series(1, 40) AS i;
INSERT INTO partner_allocation (sku, status, allocatable)
    SELECT 'SKU-' || lpad(i::text, 4, '0'), 'OPEN', 1000000 FROM generate_series(1, 10) AS i;
