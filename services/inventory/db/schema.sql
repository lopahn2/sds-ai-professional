-- Inventory (service B) schema + deterministic baseline fixture (KAN-67).
-- Applied by scripts/provision-inventory-db.sh as the dedicated `inventory` role inside the dedicated
-- `inventory` database. Idempotent: re-running restores the baseline quantities.
CREATE SCHEMA IF NOT EXISTS inventory;

CREATE TABLE IF NOT EXISTS inventory.stock (
    item     text PRIMARY KEY CHECK (length(item) BETWEEN 1 AND 64),
    quantity integer NOT NULL CHECK (quantity >= 0)
);

INSERT INTO inventory.stock (item, quantity) VALUES
    ('a', 42), ('b', 7), ('c', 0), ('widget', 120), ('gadget', 15)
ON CONFLICT (item) DO UPDATE SET quantity = EXCLUDED.quantity;
