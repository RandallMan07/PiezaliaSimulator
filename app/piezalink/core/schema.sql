-- Shared catalogue and per-account marketplace references.
CREATE TABLE IF NOT EXISTS accounts (
    account_id INTEGER PRIMARY KEY CHECK (account_id > 0),
    token TEXT NOT NULL CHECK (btrim(token) <> '')
);

CREATE TABLE IF NOT EXISTS catalogue (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    product_id TEXT UNIQUE CHECK (btrim(product_id) <> ''),
    title VARCHAR(80) NOT NULL CHECK (btrim(title) <> ''),
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

-- Upgrade databases created before feed imports were introduced.
ALTER TABLE catalogue ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ
    NOT NULL DEFAULT transaction_timestamp();
ALTER TABLE catalogue ADD COLUMN IF NOT EXISTS modified_at TIMESTAMPTZ
    NOT NULL DEFAULT transaction_timestamp();
ALTER TABLE catalogue_accounts ADD COLUMN IF NOT EXISTS desired BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE catalogue DROP CONSTRAINT IF EXISTS catalogue_status_check;
ALTER TABLE catalogue ADD CONSTRAINT catalogue_status_check
    CHECK (status IN ('unlisted', 'listed', 'sold', 'modified'));

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
            UPDATE catalogue SET modified_at = transaction_timestamp()
            WHERE id = OLD.catalogue_id AND modified_at <> transaction_timestamp();
        END IF;
    END IF;
    IF TG_OP <> 'DELETE' THEN
        IF NEW.desired THEN
            UPDATE catalogue SET modified_at = transaction_timestamp()
            WHERE id = NEW.catalogue_id AND modified_at <> transaction_timestamp();
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
        product_id TEXT NOT NULL,
        title TEXT NOT NULL,
        price NUMERIC(7,2) NOT NULL,
        account_ids INTEGER[] NOT NULL
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
INSERT INTO piezalink_schema(singleton, version) VALUES (TRUE, 1)
ON CONFLICT (singleton) DO UPDATE SET version = EXCLUDED.version;
