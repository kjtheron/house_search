-- same_house() also matches twins by equal monthly rates (known once details are fetched).
CREATE INDEX IF NOT EXISTS idx_listings_rates ON listings(rates);
