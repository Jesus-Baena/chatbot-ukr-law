# Flowise chatflow — Ukraine Law Paralegal RAG

The production answer path is a **Flowise Agentflow v2**. The frontend
(`../index.html`) POSTs to its prediction endpoint and renders `text` +
`sourceDocuments`.

`ukr-law-chatflow.json` is a **real, importable Agentflow** (Start → 3
retrievers → Agent), generated to match this Flowise build's node versions
(`startAgentflow` 1.1, `retrieverAgentflow` 1.1, `agentAgentflow` 3.2). It is
**not fully turnkey**: after import you must bind 3 Document Stores and 1
credential in the UI (see below) — those are UI/credential-encrypted entities
that can't live in an exported graph. After finalizing in the UI, use **Export
Agentflow** and overwrite this file so git tracks the real state. Flows live in
the Supabase `flowise` schema and are **lost on schema drop**
(`2025-swarm-infrastructure-deployment/FLOWISE_RUNBOOK.md`).

## Architecture (this Flowise build)

This build's retrievers (`retrieverAgentflow`) read from **Flowise Document
Stores**, not raw Qdrant collections. So each pipeline collection is wrapped in
a thin Qdrant-backed Document Store. The graph is:

```
Start ──▶ Retriever: Primary_Law        (Document Store → rada_legislation)   ─┐
      ──▶ Retriever: Curated_Law        (Document Store → curated_legislation) ─┼─▶ Agent (Gemini, law-first)
      ──▶ Retriever: Secondary_Analysis (Document Store → secondary_reports)   ─┘
```

## ⚠️ Embedding-dimension parity (read first)

Retrieval breaks silently if the query embedding doesn't match the indexed
vectors. Every Document Store below must embed with **Gemini
`gemini-embedding-001`** at the **same dimension** the collections were built
with (`EMBED_DIM` = **3072**), task type **RETRIEVAL_QUERY** on the query side.

## Setup: 3 Qdrant-backed Document Stores (one per collection)

For each of `rada_legislation`, `curated_legislation`, `secondary_reports`:

1. **Document Stores → Add New** → name it after the collection.
2. Add a **Qdrant** record store (node `qdrant` v5):
   - URL `http://storage_qdrant:6333`, API key from the `QDRANT_API_KEY_ASCII` secret.
   - **Collection**: the existing collection name (data is already populated by
     the Python pipeline — do **not** re-upsert).
3. **Embeddings**: Google Generative AI Embeddings, `gemini-embedding-001`,
   `RETRIEVAL_QUERY`, dimension `3072`.
4. Save, then copy the Document Store's **id** from its URL
   (`/document-stores/<id>`).

## Import & bind the Agentflow

1. **Agentflows → Add New → Import**, choose `ukr-law-chatflow.json`.
2. Open each **Retriever** node and select the matching Document Store (or paste
   its id, replacing the `<REPLACE_WITH_*_DOCSTORE_ID>` placeholder). Top-K:
   Primary ≈ 4, Curated ≈ 3, Secondary_Analysis ≈ 2 (commentary — keep small so
   reports never crowd out the law).
3. Open the **Agent** node → select **ChatGoogleGenerativeAI**
   (`gemini-2.0-flash`) and attach your Google credential (replaces
   `<REPLACE_WITH_GOOGLE_AI_CREDENTIAL_ID>`). Keep the law-first system prompt
   (already embedded; mirror of `5_query.py`).
4. Ensure source documents are returned so the frontend renders citations. The
   report Document Store carries `source_type`/`report_title`/`pub_date` metadata
   so `../index.html` can label analyses distinctly.

## System prompt (mirror of `5_query.py`)

```
You are a legal research assistant specializing in Ukrainian legislation.
You answer questions about Ukrainian law based on retrieved legal text excerpts.

The excerpts are grouped by authority:
- "PRIMARY LAW" and "CURATED LAW" are the actual legislative text — treat these
  as authoritative.
- "SECONDARY ANALYSIS" are expert reports/commentary that review the law. Use
  them only for context, interpretation, or to flag proposed reforms. Never
  present a report's claim as the law itself, and note that it is commentary
  (with its date, since analyses go out of date).

Guidelines:
- Base legal conclusions on the primary/curated legal excerpts
- Cite the specific law title and article/section when possible
- When you rely on a secondary analysis, attribute it as commentary and give its date
- If the excerpts don't fully answer the question, say so clearly
- You may answer in English even if the source texts are in Ukrainian
- Note the enactment date of relevant laws, especially for martial law context
- Be precise about legal rights, obligations, and procedures
```

## Secrets & config (Docker Swarm)

Per `DOCKER_SECRET_INJECTION.md`, create one Google AI key secret and add it as a
Flowise credential (encrypted by `FLOWISE_SECRETKEY`):

```bash
ssh sysop@pokrovsk 'printf "<GOOGLE_AI_API_KEY>" | sudo docker secret create GOOGLE_AI_API_KEY -'
```

- Set the chatflow `isPublic = true` (Share → Public) so the public frontend can call it.
- Add the frontend origin to Flowise `CORS_ORIGINS` in
  `2025-swarm-infrastructure-deployment/automation/docker-compose.yml`, then
  `docker service update --force automation_flowise`.

## Wiring the frontend

Capture the prediction URL
`https://flowise.baena.info/api/v1/prediction/<chatflow-id>` and set it in
`../index.html` (`FLOWISE_PREDICTION_URL`, or inject `window.FLOWISE_PREDICTION_URL`
at deploy time).

## Smoke test

```bash
curl -X POST https://flowise.baena.info/api/v1/prediction/<chatflow-id> \
  -H 'Content-Type: application/json' \
  -d '{"question":"IDP rights during martial law"}'
# → { "text": "...", "sourceDocuments": [ { "metadata": { "title": "...", "url": "..." } } ] }
```
