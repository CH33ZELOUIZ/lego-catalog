# Deployment

LEGO Catalog is a small Flask app for inventorying sets and minifigures.

## Runtime pieces

- Flask web app
- SQLite database
- local image cache
- optional mounted photo/import directory

## Setup flow

1. Copy `.env.example` to `.env`.
2. Pick persistent paths for database, cache, and imports.
3. Start with Docker Compose.
4. Import CSVs or add items manually.
5. Run metadata enrichment/backfill if configured.
