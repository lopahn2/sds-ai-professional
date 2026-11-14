CREATE SCHEMA IF NOT EXISTS inventory;
CREATE TABLE IF NOT EXISTS inventory.stock (item VARCHAR(64) PRIMARY KEY, quantity INT NOT NULL);
MERGE INTO inventory.stock (item, quantity) KEY (item) VALUES ('a', 42);
