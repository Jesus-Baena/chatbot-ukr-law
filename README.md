# rada-rag: Ukrainian Legislation RAG Pipeline

Updatable RAG knowledge base built from Verkhovna Rada legislation.

## Architecture

```
data.rada.gov.ua (catalogue JSON)
        ↓  [1_fetch_catalogue.py]
    catalogue.json  (law IDs + metadata)
        ↓  [2_scrape_laws.py]
    Postgres `rada_raw_laws` (raw law HTML stored first)
        ↓
    laws/  (raw HTML -> structured JSON per law)
        ↓  [3_chunk_embed.py]
    Qdrant collection  (vectors + payloads)
        ↑  [4_incremental_update.py]  <- run via n8n cron
data.rada.gov.ua (new IDs since last run)
```

## Stack

- Scraper: `requests` + `BeautifulSoup` (`lxml`)
- Extraction: Docling service (`DOCLING_API_URL`) with HTML fallback parser
- Embeddings: `mxbai-embed-large` via Ollama
- Vector store: Qdrant
- Staging store: PostgreSQL (`DATABASE_URL`) for extracted law text + metadata
- RAG query: Ollama chat model
- Orchestration: n8n (incremental updates)

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure environment
cp .env.example .env
# Edit .env: set QDRANT_URL, QDRANT_API_KEY,
# OLLAMA_BASE_URL, OLLAMA_MODEL, EMBED_MODEL, DOCLING_API_URL,
# DATABASE_URL

# 3. Initialize PostgreSQL staging tables
python 0_init_postgres.py

# 4. Backfill staging tables from INDEXED_LAWS.md
python 0_backfill_indexed_laws.py

# 5. (Optional) run local Qdrant
docker compose up -d
```

## Swarm Postgres Tunnel

If this project runs outside the Docker Swarm network, keep `DATABASE_URL` pointed at
`localhost:5432` and start the SSH tunnel before running any Postgres-backed scripts:

```bash
# Terminal 1: start the tunnel and leave it running
bash 0_start_postgres_tunnel.sh
```

This creates a temporary `socat` proxy on the remote Docker host and forwards local
`localhost:5432` to the Swarm service DNS name `storage_postgres:5432` on the
`storage-internal` overlay network.

Notes:
- `0_start_postgres_tunnel.sh` is a blocking process because it keeps the SSH tunnel open.
- Run the ingest scripts in a second terminal after the tunnel prints `Keep this terminal open while using the database.`
- `.env` is ignored by git. Commit only placeholder values in `.env.example`.
- `QDRANT_URL` is the canonical variable name.
- `2_scrape_laws.py` and `4_incremental_update.py` automatically write extracted laws to Postgres staging when `DATABASE_URL` is set.
- With Postgres enabled, raw law payloads are saved first to `rada_raw_laws` before extraction/chunking steps run.
- `0_backfill_indexed_laws.py` imports the already-indexed laws listed in `INDEXED_LAWS.md` and marks their staged chunks as synced.
- `STAGING_STORE_RAW_JSON=0` (default) keeps staging lean by not storing full processed law JSON in `rada_staging_laws.raw_json`.
- When available, the original catalogue/update entry is stored in `rada_staging_laws.source_catalogue_json` for provenance.
- `0_start_postgres_tunnel.sh` is the supported way to reach the Portainer-managed Swarm Postgres from local development.

### Batch Run Example

Use two terminals when `DATABASE_URL` points at the SSH tunnel.

Terminal 1:

```bash
bash 0_start_postgres_tunnel.sh
```

Terminal 2:

```bash
sed -i 's/^CATALOGUE_OFFSET=.*/CATALOGUE_OFFSET=1200/' .env || echo 'CATALOGUE_OFFSET=1200' >> .env
sed -i 's/^MAX_LAWS=.*/MAX_LAWS=300/' .env || echo 'MAX_LAWS=300' >> .env
sed -i 's/^FORCE_RESCRAPE=.*/FORCE_RESCRAPE=1/' .env || echo 'FORCE_RESCRAPE=1' >> .env

/home/jbi/Git/2025-ukraine-law-chatbot/.venv/bin/python 1_fetch_catalogue.py
/home/jbi/Git/2025-ukraine-law-chatbot/.venv/bin/python 2_scrape_laws.py
/home/jbi/Git/2025-ukraine-law-chatbot/.venv/bin/python 6_retry_failed_ingest.py
/home/jbi/Git/2025-ukraine-law-chatbot/.venv/bin/python 3_chunk_embed.py
```

Increase `CATALOGUE_OFFSET` by `300` for the next batch.

Or run the same flow with one command:

```bash
# Terminal 1
bash 0_start_postgres_tunnel.sh

# Terminal 2
bash run_batch.sh 1200

# Optional: allow a no-op run when offset has no remaining rows
bash run_batch.sh 1200 300 --allow-empty
```

`run_batch.sh` sets `CATALOGUE_OFFSET`, `MAX_LAWS` (default `300`), and `FORCE_RESCRAPE=1`, then runs:
`1_fetch_catalogue.py` -> `2_scrape_laws.py` -> `6_retry_failed_ingest.py` -> `3_chunk_embed.py`.
By default it stops early if the filtered catalogue is empty, to avoid silent no-op batches.

## Usage

```bash
# Full bootstrap
python 1_fetch_catalogue.py
python 2_scrape_laws.py
python 3_chunk_embed.py

# Incremental update (daily via n8n)
python 4_incremental_update.py

# Retry only failed law files and vectorize recovered ones
python 6_retry_failed_ingest.py

# Query
python 5_query.py "права внутрішньо переміщених осіб"
python 5_query.py "IDP rights during martial law"
```

## Scope Filtering

Set optional filters in `.env`:
- `CATEGORY_FILTER=humanitarian`
- `DATE_FROM=2022-02-24`
- `MAX_LAWS=5000`
- `CATALOGUE_OFFSET=0` (set `300` to fetch the next 300 after the first batch)

## Indexed Laws Tracker

The list of laws currently embedded in the vector collection is maintained in [INDEXED_LAWS.md](INDEXED_LAWS.md), including English titles, law IDs, section counts, and chunk counts.
Each row also records the UTC date when that law was last embedded/backfilled, and is updated automatically by `3_chunk_embed.py`, `4_incremental_update.py`, and `0_backfill_indexed_laws.py`.

## Files

| File | Purpose |
|------|---------|
| `1_fetch_catalogue.py` | Download law ID catalogue from open data portal |
| `2_scrape_laws.py` | Scrape full text from zakon.rada.gov.ua |
| `3_chunk_embed.py` | Chunk, embed, upsert to Qdrant |
| `4_incremental_update.py` | Delta updates (new laws since last run) |
| `5_query.py` | RAG query interface |
| `config.py` | Shared config and constants |
| `docker-compose.yml` | Qdrant service |
