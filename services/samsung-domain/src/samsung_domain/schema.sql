CREATE SCHEMA IF NOT EXISTS samsung_domain;
SET search_path TO samsung_domain;
CREATE TABLE IF NOT EXISTS identity (
 singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
 environment text NOT NULL, system_name text NOT NULL
);
CREATE TABLE IF NOT EXISTS members (
 id text PRIMARY KEY, name text NOT NULL UNIQUE, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS entities (
 entity_type text NOT NULL, entity_id text NOT NULL, entity_version bigint NOT NULL,
 data jsonb NOT NULL, content text NOT NULL DEFAULT '', PRIMARY KEY(entity_type, entity_id)
);
CREATE TABLE IF NOT EXISTS target_state (
 target text PRIMARY KEY, last_committed_seq bigint NOT NULL DEFAULT 0,
 watermark_at timestamptz, watermark_wire text, watermark_barrier bigint NOT NULL DEFAULT 0
);
INSERT INTO target_state(target) VALUES ('samsung-delivery'),('samsung-trade') ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS projections (
 target text NOT NULL, entity_type text NOT NULL, entity_id text NOT NULL,
 entity_version bigint NOT NULL, target_seq bigint NOT NULL, operation text NOT NULL,
 payload jsonb NOT NULL, PRIMARY KEY(target, entity_type, entity_id)
);
CREATE TABLE IF NOT EXISTS outbox (
 event_id text PRIMARY KEY, target text NOT NULL, target_seq bigint NOT NULL,
 event_key text NOT NULL, wire text NOT NULL, acked_at timestamptz,
 created_at timestamptz NOT NULL DEFAULT now(), UNIQUE(target, target_seq)
);
CREATE INDEX IF NOT EXISTS outbox_pending ON outbox(target, target_seq) WHERE acked_at IS NULL;
CREATE TABLE IF NOT EXISTS exports (
 export_id text PRIMARY KEY, target text NOT NULL, metadata jsonb NOT NULL, items jsonb NOT NULL,
 expires_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS generations (
 generation text PRIMARY KEY, boundary_seq bigint, applied_seq bigint,
 start_offset bigint NOT NULL, checkpoint bigint NOT NULL, end_offset bigint,
 snapshot_expires_at timestamptz, watermark_seq bigint, watermark_at timestamptz,
 state text NOT NULL DEFAULT 'loading', created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS replica_state (
 singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton), active_generation text,
 rebuilding_generation text, dependency_error boolean NOT NULL DEFAULT false
);
INSERT INTO replica_state(singleton) VALUES (true) ON CONFLICT DO NOTHING;
ALTER TABLE replica_state ADD COLUMN IF NOT EXISTS observed_end_offset bigint;
CREATE TABLE IF NOT EXISTS replicas (
 generation text NOT NULL REFERENCES generations(generation), entity_type text NOT NULL, entity_id text NOT NULL,
 entity_version bigint NOT NULL, target_seq bigint NOT NULL, operation text NOT NULL, payload jsonb NOT NULL,
 PRIMARY KEY(generation,entity_type,entity_id)
);
CREATE TABLE IF NOT EXISTS inbox (
 generation text NOT NULL REFERENCES generations(generation), event_id text NOT NULL, content_hash text NOT NULL,
 PRIMARY KEY(generation,event_id)
);
CREATE TABLE IF NOT EXISTS applied_sequences (
 generation text NOT NULL REFERENCES generations(generation), target_seq bigint NOT NULL,
 PRIMARY KEY(generation,target_seq)
);
CREATE TABLE IF NOT EXISTS quarantine (
 generation text NOT NULL REFERENCES generations(generation), topic text NOT NULL, partition integer NOT NULL,
 kafka_offset bigint NOT NULL, content_hash text NOT NULL, diagnostic text NOT NULL,
 resolved boolean NOT NULL DEFAULT false, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(generation,topic,partition,kafka_offset)
);
