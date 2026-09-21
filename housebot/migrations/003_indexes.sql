-- pending_notifications() looks up notifications by fingerprint (+price) for every listing;
-- show/delete_town look up price history by listing. Without these, each is a full table scan.
CREATE INDEX IF NOT EXISTS idx_notifications_fp ON notifications(fingerprint, price_notified);
CREATE INDEX IF NOT EXISTS idx_price_history_listing ON price_history(listing_id);
