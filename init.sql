-- Run this once against the Railway Postgres database before first use.
-- Enables trigram similarity matching used in app/search.py

CREATE EXTENSION IF NOT EXISTS pg_trgm;
