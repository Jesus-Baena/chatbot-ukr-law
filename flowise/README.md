# Flowise chatflow — Ukraine Law Paralegal RAG

> **Status / source of truth (verified 2026-07-24, live inspection).**
> `ukr-law-chatflow.json` in this folder is the **real exported live graph** for
> chatflow id `6b3a8804-5200-428f-afd0-abedc9c1f49c`. Re-export it from the
> Flowise UI (or `GET /api/v1/chatflows/<id>`) whenever you change the flow —
> flows are **lost on schema drop** (`2025-swarm-infrastructure-deployment/FLOWISE_RUNBOOK.md`),
> so keep the export in git.

## Where the live flow actually runs (corrects earlier notes)

The live paralegal flow is a **classic chatflow** (`type: CHATFLOW`), **not** an
Agentflow, and it runs on the **`automation_flowise`** service — the same
instance whose admin UI is `flowise.baena.info`. The portfolio demo never calls
`flowise.baena.info` directly: `demos/paralegal-advisor.vue` POSTs to the
**same-origin** path `https://baena.ai/api/v1/prediction/6b3a8804-…`, and the
gateway Caddy (`2025-swarm-infrastructure-deployment/gateway/Caddyfile`, `baena.ai`
block) reverse-proxies `/api/v1/prediction/*` to `automation_flowise:3000` and
injects the `Authorization: Bearer` header.

`flowise.baena.site` is a **separate, currently-down (HTTP 525) legacy** instance
and is **not** in the demo's request path. Earlier docs that named `.site` as the
live target were written without live access — treat this note as the correction.

## Architecture (as deployed)

Classic `conversationalRetrievalQAChain` (v3) with a **single** vector-store
retriever:

```
GoogleGenerativeAI Embeddings ──▶ Qdrant retriever ──▶ Conversational Retrieval QA Chain ──▶ answer + sourceDocuments
  gemini-embedding-001              rada_plus_reports        Gemini 2.5 Flash (temp 0.4)
  RETRIEVAL_QUERY, 3072-d           topK 8, Cosine           returnSourceDocuments = true
```

### Why one collection (`rada_plus_reports`) instead of three retrievers

A classic `conversationalRetrievalQAChain` binds **exactly one** retriever, and
this Flowise build has **no retriever-merger node** (the LOTR *Merger Retriever*
is not installed; *Reciprocal Rank Fusion* is single-source query-expansion, and
*Multi-Retrieval QA* **routes** to one retriever rather than merging). So a single
flow cannot query `rada_legislation` and `secondary_reports` simultaneously.

To serve law + commentary from one retriever **without re-embedding**, we build a
combined **serving** collection `rada_plus_reports` = a pure vector copy of
`rada_legislation` (primary law) + the `secondary_reports` chunks. The canonical
collections are untouched and remain the sources of truth:

- `rada_legislation` — primary law, maintained by the n8n incremental job.
- `secondary_reports` — expert analyses, built by `10_ingest_reports.py`.
- `rada_plus_reports` — **derived, rebuildable** serving index, built by
  `11_build_serving_collection.py` (idempotent; re-run to re-sync after law
  updates).

Report chunks in the serving copy get their `text` payload prefixed with
`[SECONDARY ANALYSIS — "<title>", commentary dated <pub_date>; not primary law]`
so the LLM (which only sees `pageContent` in `{context}`) can tell dated
commentary from statute. Only the **display text** is annotated — the stored
vector is the original, so retrieval ranking is unchanged. The nested `metadata`
payload (`source_type` / `report_title` / `pub_date` / `url`) is what Flowise
returns as `sourceDocuments[].metadata`, and what the frontend "Analysis" badge
keys on.

> A future migration to an **Agentflow** with three independent retriever tools
> (one per collection, each with its own small top-K: law≈4, curated≈3,
> analysis≈2) would remove the need for this duplicate and restore per-source
> top-K control. It is a paradigm change to a live flow (different response
> shape, manual Document-Store/credential binding) and was intentionally not
> attempted here.

## ⚠️ Embedding-dimension parity (read first)

Retrieval breaks **silently** if the query embedding doesn't match the indexed
vectors. The retriever must embed with **Gemini `gemini-embedding-001`** at
**3072-d**, task type **RETRIEVAL_QUERY**, Cosine — matching how the collections
were built (`RETRIEVAL_DOCUMENT`, 3072-d). This is already set in the exported
graph; keep it in sync if you edit nodes.

## Data-protection corpus gap (known)

For data-protection / GDPR questions, the `secondary_reports` chunks legitimately
dominate the top-K because the underlying **Personal Data Protection Law
(No. 2297-VI)** is weak/absent in `rada_legislation`. The answer stays honest —
it attributes claims to the dated secondary analysis and never presents them as
statute — but it is *commentary-led* rather than *law-led* for that topic. To make
it law-led, add 2297-VI to the corpus and re-embed (TASK C), then re-run
`11_build_serving_collection.py`. For law-strong topics (e.g. IDP rights) the flow
is correctly law-first (verified: 7 law + 1 report source).

## System prompt (law-first; lives in the exported graph)

```
You are a legal research assistant specializing in Ukrainian legislation. You answer questions about Ukrainian law based on the retrieved excerpts below.

The excerpts may include two kinds of material:
- PRIMARY LAW — the actual legislative text (statutes, codes, regulations). Treat this as authoritative. Any excerpt that is NOT explicitly marked as analysis is primary law.
- SECONDARY ANALYSIS — expert reports/commentary that review or compare the law. Each such excerpt begins with a line like: [SECONDARY ANALYSIS — "Title", commentary dated YYYY-MM-DD; not primary law]. Use these ONLY for context, interpretation, or to flag proposed reforms. NEVER present a report's claim as the law itself.

Guidelines:
- Base every legal conclusion on the PRIMARY LAW excerpts. Cite the specific law title and article/section when possible.
- When you rely on a SECONDARY ANALYSIS, explicitly attribute it as commentary and state its date, e.g. "According to a secondary analysis (Ukraine Data Protection vs. GDPR, dated 2025-09-03), ...". Remember analyses go out of date.
- If the excerpts don't fully answer the question, say so clearly.
- You may answer in English even if the source texts are in Ukrainian.
- Note the enactment date of relevant laws, especially for martial law context.
- Be precise about legal rights, obligations, and procedures.
------------
{context}
------------
```

## Frontend contract

`demos/paralegal-advisor.vue` POSTs `{ "question": ... }` to
`https://baena.ai/api/v1/prediction/6b3a8804-…` and renders `text` +
`sourceDocuments`. Each source's `metadata.source_type === "secondary_report"`
triggers the "Analysis"/"Аналіз" badge and prefers `metadata.report_title` /
`metadata.pub_date`. Keep the chatflow `isPublic = true`.

## Smoke test

```bash
curl -X POST https://baena.ai/api/v1/prediction/6b3a8804-5200-428f-afd0-abedc9c1f49c \
  -H 'Content-Type: application/json' \
  -d '{"question":"How does Ukraine data protection law compare to the EU GDPR?"}'
# → { "text": "...(cites 'Secondary Analysis, \"Ukraine Data Protection vs. GDPR\", dated 2025-09-03')...",
#     "sourceDocuments": [ { "metadata": { "source_type": "secondary_report",
#                                          "report_title": "Ukraine Data Protection vs. GDPR",
#                                          "pub_date": "2025-09-03" } }, ... ] }
```
