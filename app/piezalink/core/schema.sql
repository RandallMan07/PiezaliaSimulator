-- Shared catalogue and per-account marketplace references.
CREATE TABLE IF NOT EXISTS accounts (
    account_id INTEGER PRIMARY KEY CHECK (account_id > 0),
    token TEXT NOT NULL CHECK (btrim(token) <> '')
);

CREATE TABLE IF NOT EXISTS catalogue (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    product_id TEXT UNIQUE CHECK (btrim(product_id) <> ''),
    title TEXT NOT NULL CHECK (btrim(title) <> ''),
    price NUMERIC(7, 2) NOT NULL CHECK (price > 0 AND price <= 99999.99),
    status TEXT NOT NULL DEFAULT 'unlisted'
        CHECK (status IN ('unlisted', 'listed', 'sold', 'modified')),
    listed_manually BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT transaction_timestamp(),
    modified_at TIMESTAMPTZ NOT NULL DEFAULT transaction_timestamp(),
    CHECK (listed_manually OR product_id IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS catalogue_accounts (
    catalogue_id BIGINT NOT NULL REFERENCES catalogue(id) ON DELETE CASCADE,
    account_id INTEGER NOT NULL REFERENCES accounts(account_id),
    desired BOOLEAN NOT NULL DEFAULT TRUE,
    listing_id TEXT CHECK (btrim(listing_id) <> ''),
    PRIMARY KEY (catalogue_id, account_id),
    UNIQUE (account_id, listing_id)
);

-- Keep full source titles; enforce marketplace title limits only when publishing.
ALTER TABLE catalogue ALTER COLUMN title TYPE TEXT;

-- Upgrade databases created before feed imports were introduced.
ALTER TABLE catalogue ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ
    NOT NULL DEFAULT transaction_timestamp();
ALTER TABLE catalogue ADD COLUMN IF NOT EXISTS modified_at TIMESTAMPTZ
    NOT NULL DEFAULT transaction_timestamp();
ALTER TABLE catalogue_accounts ADD COLUMN IF NOT EXISTS desired BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE catalogue DROP CONSTRAINT IF EXISTS catalogue_status_check;
ALTER TABLE catalogue ADD CONSTRAINT catalogue_status_check
    CHECK (status IN ('unlisted', 'listed', 'sold', 'modified'));

-- Synchronization metadata is kept independently of HTTP requests.
ALTER TABLE catalogue ADD COLUMN IF NOT EXISTS desired_revision BIGINT NOT NULL DEFAULT 1;
ALTER TABLE catalogue_accounts ADD COLUMN IF NOT EXISTS synced_revision BIGINT NOT NULL DEFAULT 0;
ALTER TABLE catalogue_accounts ADD COLUMN IF NOT EXISTS last_synced_at TIMESTAMPTZ;
ALTER TABLE catalogue_accounts ADD COLUMN IF NOT EXISTS last_attempt_at TIMESTAMPTZ;
ALTER TABLE catalogue_accounts ADD COLUMN IF NOT EXISTS last_error TEXT;
ALTER TABLE catalogue_accounts ADD COLUMN IF NOT EXISTS last_warning TEXT;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS last_sync_error TEXT;
ALTER TABLE accounts ADD COLUMN IF NOT EXISTS last_sync_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS accepted_feeds (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    accepted_at TIMESTAMPTZ NOT NULL DEFAULT transaction_timestamp(),
    product_count BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_runs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT transaction_timestamp(),
    finished_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'completed', 'partial', 'interrupted')),
    summary JSONB NOT NULL DEFAULT '{}',
    last_error TEXT
);
CREATE TABLE IF NOT EXISTS sync_operations (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES sync_runs(id),
    -- Keep history even when a completed product is later deleted.
    catalogue_id BIGINT,
    account_id INTEGER NOT NULL REFERENCES accounts(account_id),
    product_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('POST', 'PUT', 'DELETE')),
    listing_id TEXT,
    revision BIGINT,
    payload JSONB,
    warning TEXT,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'unknown', 'confirmed', 'failed', 'superseded')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT transaction_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT transaction_timestamp(),
    last_error TEXT
);
CREATE INDEX IF NOT EXISTS sync_operations_unresolved
    ON sync_operations(catalogue_id, account_id) WHERE state IN ('pending', 'unknown');

-- Deferred checks allow creation/replacement of account links in one transaction.
CREATE OR REPLACE FUNCTION check_catalogue_accounts(target_id BIGINT)
RETURNS VOID LANGUAGE plpgsql AS $$
BEGIN
    PERFORM id FROM catalogue WHERE id = target_id FOR UPDATE;
    IF EXISTS (SELECT 1 FROM catalogue WHERE id = target_id AND status <> 'sold')
       AND NOT EXISTS (SELECT 1 FROM catalogue_accounts WHERE catalogue_id = target_id AND desired)
    THEN
        RAISE EXCEPTION 'Active catalogue item % requires at least one account', target_id
            USING ERRCODE = '23514';
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION check_catalogue_row_accounts()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    PERFORM check_catalogue_accounts(NEW.id);
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION check_removed_catalogue_account()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    PERFORM check_catalogue_accounts(OLD.catalogue_id);
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS catalogue_requires_accounts ON catalogue;
CREATE CONSTRAINT TRIGGER catalogue_requires_accounts
AFTER INSERT OR UPDATE ON catalogue DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION check_catalogue_row_accounts();

DROP TRIGGER IF EXISTS account_removal_preserves_catalogue ON catalogue_accounts;
CREATE CONSTRAINT TRIGGER account_removal_preserves_catalogue
AFTER DELETE OR UPDATE ON catalogue_accounts DEFERRABLE INITIALLY DEFERRED
FOR EACH ROW EXECUTE FUNCTION check_removed_catalogue_account();

-- Preserve creation dates and avoid timestamp churn for identical data.
CREATE OR REPLACE FUNCTION timestamp_catalogue_change()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.created_at := OLD.created_at;
    IF ROW(NEW.product_id, NEW.title, NEW.price, NEW.listed_manually, NEW.status = 'sold')
       IS DISTINCT FROM
       ROW(OLD.product_id, OLD.title, OLD.price, OLD.listed_manually, OLD.status = 'sold')
       OR NEW.desired_revision IS DISTINCT FROM OLD.desired_revision
    THEN
        NEW.desired_revision := OLD.desired_revision + 1;
        IF NEW.status = 'listed' AND OLD.created_at <> transaction_timestamp() THEN
            NEW.status := 'modified';
        END IF;
    ELSE
        NEW.desired_revision := OLD.desired_revision;
    END IF;
    IF ROW(NEW.product_id, NEW.title, NEW.price, NEW.status, NEW.listed_manually, NEW.modified_at)
       IS DISTINCT FROM
       ROW(OLD.product_id, OLD.title, OLD.price, OLD.status, OLD.listed_manually, OLD.modified_at)
    THEN
        NEW.modified_at := transaction_timestamp();
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS catalogue_timestamps ON catalogue;
CREATE TRIGGER catalogue_timestamps BEFORE UPDATE ON catalogue
FOR EACH ROW EXECUTE FUNCTION timestamp_catalogue_change();

CREATE OR REPLACE FUNCTION timestamp_account_change()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF ROW(NEW.catalogue_id, NEW.account_id, NEW.desired)
           IS NOT DISTINCT FROM ROW(OLD.catalogue_id, OLD.account_id, OLD.desired)
        THEN
            RETURN NULL;
        END IF;
    END IF;
    IF TG_OP <> 'INSERT' THEN
        IF OLD.desired THEN
            UPDATE catalogue SET modified_at = transaction_timestamp(), desired_revision = desired_revision + 1
            WHERE id = OLD.catalogue_id;
        END IF;
    END IF;
    IF TG_OP <> 'DELETE' THEN
        IF NEW.desired THEN
            UPDATE catalogue SET modified_at = transaction_timestamp(), desired_revision = desired_revision + 1
            WHERE id = NEW.catalogue_id;
        END IF;
    END IF;
    RETURN NULL;
END;
$$;
DROP TRIGGER IF EXISTS catalogue_account_timestamps ON catalogue_accounts;
CREATE TRIGGER catalogue_account_timestamps AFTER INSERT OR UPDATE OR DELETE
ON catalogue_accounts FOR EACH ROW EXECUTE FUNCTION timestamp_account_change();

-- Temporary structures are created per import transaction and dropped at commit.
CREATE OR REPLACE FUNCTION create_feed_staging()
RETURNS VOID LANGUAGE plpgsql AS $$
BEGIN
    CREATE TEMP TABLE feed_stage (
        record_no BIGINT NOT NULL,
        product_id TEXT,
        title TEXT,
        price NUMERIC(7,2),
        account_ids INTEGER[] NOT NULL,
        valid BOOLEAN NOT NULL
    ) ON COMMIT DROP;
    CREATE INDEX ON feed_stage(product_id);
    CREATE TEMP TABLE feed_accounts (
        product_id TEXT NOT NULL,
        account_id INTEGER NOT NULL,
        PRIMARY KEY (product_id, account_id)
    ) ON COMMIT DROP;
    CREATE TEMP TABLE feed_actions (
        product_id TEXT PRIMARY KEY,
        catalogue_id BIGINT,
        action TEXT NOT NULL
    ) ON COMMIT DROP;
END;
$$;

-- init_db checks this marker to avoid DDL on every CLI invocation.
CREATE TABLE IF NOT EXISTS piezalink_schema (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    version INTEGER NOT NULL
);
INSERT INTO piezalink_schema(singleton, version) VALUES (TRUE, 5)
ON CONFLICT (singleton) DO UPDATE SET version = EXCLUDED.version;
