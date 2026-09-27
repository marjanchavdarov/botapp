-- ============================================================
-- price_cache — one row per barcode, holding the nationwide branch price list
--
-- Why this works:
--   GET /v1/prices/?eans=X&lat=..&lon=..&d=500 returns every branch in the
--   country, and the result is *identical* from Zagreb and from Split
--   (verified 2026-09-27: 493 branches, byte-identical from both). So the
--   cache needs no location key — one row serves every user everywhere.
--
--   Distance is still computed per request from the branch coordinates in the
--   stored payload, so each user sees their own distances.
--
-- Effect on the rate limit:
--   Today every scan costs TWO cijene.dev calls (/prices/ then /products/ for
--   the name), against a ~1000/day allowance — roughly 500 scans a day.
--   With this cache the cost becomes distinct BARCODES per day, not scans.
--   A barcode scanned 500 times costs one call.
--
-- Freshness:
--   Prices refresh at 08:00 Europe/Zagreb. price_day is stored from the
--   upstream response's own price_date, so the cache expires when the data
--   changes rather than on an arbitrary timer.
--
-- Run in the Supabase SQL editor.
-- ============================================================

CREATE TABLE IF NOT EXISTS price_cache (
    barcode     text PRIMARY KEY,
    price_day   date NOT NULL,            -- price_date reported by cijene.dev
    prices      jsonb NOT NULL,           -- nationwide store_prices array
    meta        jsonb,                    -- name, brand, quantity, unit
    fetched_at  timestamptz DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_price_cache_day ON price_cache (price_day);

ALTER TABLE price_cache ENABLE ROW LEVEL SECURITY;

-- The backend reads and writes this with the service key, which bypasses RLS.
-- Nothing in the app touches it, so no anon policy is granted on purpose:
-- leaving it closed means a leaked publishable key cannot poison the cache.
COMMENT ON TABLE price_cache IS
    'Nationwide per-barcode price payloads, one row per barcode. Refresh at 08:00 Europe/Zagreb.';
