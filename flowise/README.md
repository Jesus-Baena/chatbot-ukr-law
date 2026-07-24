# Flowise chatflow — Ukraine Law Paralegal RAG

The production answer path is a **Flowise chatflow** (the same pattern as
`chat.baena.ai`). The frontend (`../index.html`) POSTs to its prediction
endpoint and renders `text` + `sourceDocuments`.

Flowise chatflows live only in the Supabase `flowise_db` schema and are **lost on
schema drop** (see `2025-swarm-infrastructure-deployment/FLOWISE_RUNBOOK.md`).
Keep an exported copy here in git: after building/editing the flow in the UI,
use **Export Chatflow** and overwrite `ukr-law-chatflow.json`.
`ukr-law-chatflow.json` in this repo is a **build spec** (node/parameter
reference) until it is replaced by a real export.

## ⚠️ Embedding-dimension parity (read first)

Retrieval breaks silently if the query embedding doesn't match the indexed
vectors. Both must use **Gemini `gemini-embedding-001`** at the **same output
dimension** as the collections were built with (`EMBED_DIM` = **3072**).

- The embeddings node below must produce 3072-d query vectors. 3072 is the
  model's native dimension, so most Flowise builds need no extra config.
- Use task type **RETRIEVAL_QUERY** on the query side (passages were embedded
  with RETRIEVAL_DOCUMENT).

## Nodes

1. **Google Generative AI Embeddings**
   - Model `gemini-embedding-001`, task type `RETRIEVAL_QUERY`, dimension = `EMBED_DIM`.
   - Credential: a Flowise "Google GenerativeAI" credential holding the API key
     (see secrets below).
2. **Qdrant retriever → `rada_legislation`** (auto-scraped corpus), top-K ≈ 4.
   - URL `http://storage_qdrant:6333`, API key from the `QDRANT_API_KEY_ASCII` secret.
3. **Qdrant retriever → `curated_legislation`** (curated humanitarian KB), top-K ≈ 3.
   - Optionally a metadata filter favoring `humanitarian_specific = true` /
     higher `utility_score`.
   - Any **date range** filter must target the numeric `enacted_date_int`
     (`YYYYMMDD`) payload field, not the string `enacted_date` — Qdrant range
     filtering only works on numeric fields.
   - Combine the two retrievers (newer Flowise: a fusion / "compose retrievers"
     node; otherwise a Tool Agent with two retriever tools). If your Flowise
     build has neither, ship MVP with `rada_legislation` as the single retriever
     and add the curated retriever as a fast follow.
4. **ChatGoogleGenerativeAI (Gemini)** — model `gemini-2.0-flash` (configurable).
   System prompt below.
5. **Conversational Retrieval QA Chain** wiring embeddings + retriever(s) + LLM;
   enable **Return Source Documents** so the frontend can render citations.

## System prompt (mirror of `5_query.py`)

```
You are a legal research assistant specializing in Ukrainian legislation.
You answer questions about Ukrainian law based on retrieved legal text excerpts.

Guidelines:
- Base your answer strictly on the provided legal excerpts
- Cite the specific law title and article/section when possible
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
