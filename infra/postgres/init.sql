-- PostgreSQL bootstrap script.
-- Runs once, when the postgres container initializes an empty data volume
-- (mounted into /docker-entrypoint-initdb.d/ by docker-compose.yml).
-- To re-run it: docker compose down -v && docker compose up -d postgres
--
-- The application schema is NOT created here: tables, indexes and
-- constraints are managed exclusively by Alembic migrations (migrations/).

-- Separate database for integration tests run against the compose stack,
-- so tests can freely truncate tables without touching local dev data.
-- The owner is the main application user (POSTGRES_USER from .env).
CREATE DATABASE billing_test;

-- Extensions available to migrations in both databases.
-- pgcrypto provides gen_random_uuid() for server-side UUID defaults
-- (built into PostgreSQL 13+, kept explicit for clarity).
\connect billing
CREATE EXTENSION IF NOT EXISTS pgcrypto;

\connect billing_test
CREATE EXTENSION IF NOT EXISTS pgcrypto;
