# rada-rag: Ukrainian Legislation RAG Pipeline

Updatable RAG knowledge base built from Verkhovna Rada legislation, plus a
hand-curated humanitarian knowledgebase, served to a chat UI via Flowise.

## Architecture

```
INGEST (Rada corpus)                         INGEST (curated humanitarian KB)
data.rada.gov.ua (catalogue JSON)            2025-ukraine-law-knowledgebase/
   ↓ [1_fetch_catalogue.py]                     ↓ [7_ingest_knowledgebase.py]
   ↓ [2_scrape_laws.py] → Postgres staging      Input/*.htm + metadata CSV
   ↓ [3_chunk_embed.py]                          ↓ (Gemini embeddings)
   ↓ (Gemini embeddings)                    Qdrant: curated_legislation
Qdrant: rada_legislation
   ↑ [4_incremental_update.py] (n8n cron)
   ↺ [8_reembed_to_gemini.py] (migration backfill from Postgres staging)

SERVE
index.html  →  Flowise chatflow (Gemini embeddings → Qdrant ×2 → Gemini answer)
            →  { text, sourceDocuments }
```

## Stack

- Scraper: `requests` + `BeautifulSoup` (`lxml`)
- Extraction: Docling service (`DOCLING_API_URL`) with HTML fallback parser
- **Embeddings: Google Gemini `gemini-embedding-001`** (migrated from Ollama
  `mxbai-embed-large`; query/passage handled by `task_type`, not a text prefix)
- Vector store: Qdrant — two collections: `rada_legislation` (auto-scraped) and
  `curated_legislation` (curated humanitarian KB with UtilityScore/Topics metadata)
- Staging store: PostgreSQL (`DATABASE_URL`) for extracted law text + metadata
- **Serving / generation: Flowise chatflow with Gemini** (`flowise/`), called by `index.html`
- Orchestration: n8n (incremental updates)

> **Embedding migration:** the corpus was originally embedded with Ollama
> `mxbai-embed-large` (1024-d). It is being rebuilt with Gemini at `EMBED_DIM`
> (3072) — see `8_reembed_to_gemini.py`. Query and passage embeddings
> must use the **same model and dimension**, including inside the Flowise flow
> (`flowise/README.md`).

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

# Query (CLI spot-check — Gemini end-to-end across both collections)
python 5_query.py "права внутрішньо переміщених осіб"
python 5_query.py "IDP rights during martial law"
```

## Embedding migration & curated knowledgebase

```bash
# Re-embed the Rada corpus with Gemini, sourced from Postgres staging (no re-scrape).
# Build into a versioned collection for zero-downtime switch-over, or --recreate in place.
python 8_reembed_to_gemini.py --limit 5            # smoke test first
python 8_reembed_to_gemini.py                      # full backfill into rada_legislation

# Ingest the curated humanitarian KB into its own collection
python 7_ingest_knowledgebase.py --kb-path ../2025-ukraine-law-knowledgebase/004_Knowledge_Base
```

Both require `GOOGLE_AI_API_KEY` and Qdrant access. The Flowise serving layer is
documented in [`flowise/README.md`](flowise/README.md).

## Data quality & incremental updates

- **Extraction quality gate.** `2_scrape_laws.py`, `4_incremental_update.py`, and
  `6_retry_failed_ingest.py` now assess every extracted law
  (`law_processing.assess_law_quality`) and print a per-run section-count
  histogram plus `ok` / `thin` / `suspect` counts. A law with substantial text
  but no article segmentation is flagged `suspect` (a collapsed extraction). The
  quality signals are persisted into each `data/laws/*.json`. When
  `DOCLING_API_URL` is unset, the scripts emit a loud warning because the HTML
  fallback is the main cause of collapsed extractions.
- **Audit the corpus** at any time without re-scraping:

  ```bash
  python corpus_quality_report.py            # distribution + extraction-mode mix
  python corpus_quality_report.py --worst 25 # largest suspect laws
  ```

- **Hold back collapsed laws.** Pass `--skip-suspect` to keep `suspect` laws out
  of the index until they are re-extracted, on both the incremental embed and
  the full rebuild:

  ```bash
  python 3_chunk_embed.py --skip-suspect
  python 8_reembed_to_gemini.py --recreate --skip-suspect
  ```

- **Fix collapsed laws (targeted re-extract).** With `DOCLING_API_URL` set,
  re-extract only the suspect subset — far cheaper than re-scraping everything.
  It refreshes the on-disk JSON + Postgres staging and reports how many laws
  actually improved:

  ```bash
  python 9_reextract_suspect.py --dry-run   # list what would be re-extracted
  python 9_reextract_suspect.py --limit 5   # smoke test on 5 laws
  python 9_reextract_suspect.py             # re-extract the whole suspect subset
  # then rebuild so chunking is consistent across the collection:
  python 8_reembed_to_gemini.py --recreate --skip-suspect
  ```

- **Incremental updates** (`4_incremental_update.py`) fetch through the same
  robust catalogue source as the bootstrap (`catalogue_source.py`) — the old
  path assumed `zak.json` returned a flat law list and silently ingested
  nothing. The watermark in `data/state.json` now advances to *today* only after
  a fully-processed run, to the newest processed enactment date on a run capped
  by `INCREMENTAL_MAX_LAWS`, and **not at all** when every live source is
  unavailable — so no update window is silently skipped.

## Chunking

Chunking is tuned for Gemini `gemini-embedding-001` (2048-token input):
`CHUNK_SIZE=1200`, `CHUNK_OVERLAP=200` characters (word-boundary aligned), both
overridable via `.env`. The earlier 400-char window was a leftover from the
mxbai-embed-large 512-token limit and heavily over-fragmented legal text.

**Changing chunk size requires a full re-embed** so the whole collection is
chunked consistently (mixing sizes degrades retrieval). Rebuild from Postgres
staging — no re-scrape needed:

```bash
python 8_reembed_to_gemini.py --recreate                 # apply new chunk size in place
python 8_reembed_to_gemini.py --recreate --skip-suspect  # + drop collapsed laws
```

`--recreate` drops and rebuilds the collection, so no stale chunks are left
behind from the previous (smaller) chunking.

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
| `catalogue_source.py` | Shared catalogue fetch/fallback chain (feed → doc.txt → seed) |
| `1_fetch_catalogue.py` | Download law ID catalogue from open data portal |
| `2_scrape_laws.py` | Scrape full text from zakon.rada.gov.ua |
| `corpus_quality_report.py` | Audit scraped laws for collapsed extraction / missing titles |
| `9_reextract_suspect.py` | Re-extract the suspect subset through Docling |
| `3_chunk_embed.py` | Chunk, embed, upsert to Qdrant |
| `4_incremental_update.py` | Delta updates (new laws since last run) |
| `5_query.py` | RAG query interface (CLI, Gemini end-to-end) |
| `6_retry_failed_ingest.py` | Retry failed law files and vectorize recovered ones |
| `7_ingest_knowledgebase.py` | Ingest curated humanitarian KB → `curated_legislation` |
| `8_reembed_to_gemini.py` | Re-embed Rada corpus with Gemini from Postgres staging |
| `index.html` | Chat frontend (calls the Flowise prediction endpoint) |
| `flowise/` | Flowise chatflow build spec + export (serving layer) |
| `config.py` | Shared config and constants |
| `docker-compose.yml` | Qdrant service |
