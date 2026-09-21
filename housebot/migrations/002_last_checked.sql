-- When `housebot check` last opened the listing page to confirm it is still for sale.
ALTER TABLE listings ADD COLUMN last_checked TEXT;
