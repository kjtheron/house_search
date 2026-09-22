-- same_house() looks for a twin listing by equal price or a nearby erf/floor size.
CREATE INDEX IF NOT EXISTS idx_listings_price ON listings(price);
CREATE INDEX IF NOT EXISTS idx_listings_erf ON listings(erf_m2);
CREATE INDEX IF NOT EXISTS idx_listings_floor ON listings(floor_m2);
