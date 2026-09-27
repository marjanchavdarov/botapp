-- ============================================================
-- store_branches — physical shop locations, one row per branch
--
-- Why this is separate from store_prices:
--   store_prices is keyed on the *chain* (barcode, store, price_date).
--   The app shows "Spar, Savska 58 · 1.1km" — a specific branch, with a
--   distance. That can't be derived from a chain-level price row, which is
--   why barcode_lookup.py currently calls api.cijene.dev live on every scan
--   and is bounded by their request limit.
--
-- Branch locations are reference data: they change a few times a year, not
-- every day. Import once from GET /v1/{chain}/stores/, geocode the ones that
-- arrive without coordinates, then distances are computed locally.
--
-- Run in the Supabase SQL editor.
-- ============================================================

CREATE TABLE IF NOT EXISTS store_branches (
    id          uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    chain_code  text NOT NULL,            -- konzum, lidl, spar, ...
    code        text NOT NULL,            -- branch code from the chain, e.g. 87079
    type        text,                     -- supermarket, hypermarket, ...
    address     text,                     -- street and number, cleaned
    city        text,
    lat         double precision,
    lon         double precision,
    phone       text,
    -- How lat/lon were obtained, so a bad geocode can be found later:
    -- 'source' = given by cijene.dev, 'geocoded' = looked up from the address,
    -- 'none' = we have no coordinates for this branch.
    geocode_source text DEFAULT 'none',
    updated_at  timestamptz DEFAULT now(),

    UNIQUE (chain_code, code)
);

-- Join from a chain-level price row to its branches.
CREATE INDEX IF NOT EXISTS idx_store_branches_chain
    ON store_branches (chain_code);

-- Only rows we can actually measure a distance for.
CREATE INDEX IF NOT EXISTS idx_store_branches_geo
    ON store_branches (chain_code) WHERE lat IS NOT NULL;

ALTER TABLE store_branches ENABLE ROW LEVEL SECURITY;

-- Readable by the app; writes happen from the import script with the
-- service key, which bypasses RLS.
CREATE POLICY "Allow reads store_branches"
    ON store_branches FOR SELECT TO anon, authenticated USING (true);

COMMENT ON COLUMN store_branches.geocode_source IS
    'source | geocoded | none — provenance of lat/lon, so bad geocodes are traceable';
