BEGIN;
CREATE TABLE reservation_stock (sku text NOT NULL, available integer NOT NULL, recorded_at timestamptz NOT NULL);
INSERT INTO reservation_stock VALUES
    ('stock-cedar', 8, '2026-01-01T00:00:00Z'),
    ('stock-cedar', 11, '2026-01-02T00:00:00Z'),
    ('stock-maple', 3, '2026-01-02T00:00:00Z');
CREATE TABLE reservation_partner_feed (sku text PRIMARY KEY, quantity_text text NOT NULL, available integer NOT NULL);
INSERT INTO reservation_partner_feed VALUES ('stock-cedar', 'pending', 11), ('stock-maple', 'pending', 3);
CREATE FUNCTION reservation_available_latest(item text, expected_revision int) RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE current_revision constant integer := 2;
BEGIN
    IF expected_revision <> current_revision THEN
        RAISE EXCEPTION 'reservation snapshot revision changed' USING ERRCODE = '40001';
    END IF;
    RETURN (SELECT available FROM reservation_stock WHERE sku = item ORDER BY recorded_at DESC LIMIT 1);
END;
$$;
COMMIT;
