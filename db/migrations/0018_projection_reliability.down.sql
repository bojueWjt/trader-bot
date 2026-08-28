-- Revert 0018: drop the projection reliability tables. Grants and indexes are
-- dropped with their tables.
DROP TABLE IF EXISTS projection_watermarks;
DROP TABLE IF EXISTS projection_failures;
