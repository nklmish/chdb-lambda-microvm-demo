-- Seed the NYC TLC taxi-zone lookup into Postgres.
--
-- This is the federation demo's third-party RDBMS leg (the paper's Fig 3 +
-- hero-SQL `postgresql()`): chDB joins local taxi trips against this Postgres
-- table in one statement to answer "which zones tip best?". Columns are
-- lower-cased (the source CSV header is "LocationID","Borough",...) so the
-- chDB postgresql() join needs no quoted identifiers.
--
-- Runs once on first container start via /docker-entrypoint-initdb.d.

CREATE TABLE IF NOT EXISTS taxi_zones (
    location_id  INTEGER PRIMARY KEY,
    borough      TEXT,
    zone         TEXT,
    service_zone TEXT
);

COPY taxi_zones (location_id, borough, zone, service_zone)
FROM '/docker-entrypoint-initdb.d/taxi_zone_lookup.csv'
WITH (FORMAT csv, HEADER true);
