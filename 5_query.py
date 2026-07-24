"""
Step 5: RAG query interface

Retrieves relevant law chunks from Qdrant and uses Ollama to answer.

Cross-lingual: query in English → retrieves Ukrainian law text → Ollama answers.
Or query in Ukrainian directly.

Usage:
  python 5_query.py "права внутрішньо переміщених осіб"
  python 5_query.py "IDP rights during martial law"
  python 5_query.py --filter-date 2022-02-24 "compensation for destroyed housing"
"""

import sys
import argparse
import requests
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, Range

from config import (
    QDRANT_COLLECTION, CURATED_COLLECTION, REPORTS_COLLECTION,
    GEMINI_API_KEY, GEMINI_API_BASE, GEMINI_CHAT_MODEL, REQUEST_TIMEOUT,
)
from service_clients import get_qdrant_client
from embedding_pipeline import embed_query
from date_utils import date_to_int, normalize_date


TOP_K = 6  # number of chunks to retrieve

SYSTEM_PROMPT = """You are a legal research assistant specializing in Ukrainian legislation.
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
- Be precise about legal rights, obligations, and procedures"""


def preflight_gemini() -> None:
    """Confirm a Gemini API key is configured before issuing requests."""
    if not GEMINI_API_KEY:
        raise RuntimeError(
            "Gemini API key missing — set GOOGLE_AI_API_KEY (or GEMINI_API_KEY) "
            "in your environment / .env."
        )


def _search_collection(client: QdrantClient, query_vector: list[float],
                       collection_name: str, search_filter, top_k: int) -> list[dict]:
    """Run a single vector search against one collection and normalize hits."""
    if hasattr(client, "query_points"):
        results = client.query_points(
            collection_name=collection_name,
            query=query_vector,
            query_filter=search_filter,
            limit=top_k,
            with_payload=True,
        ).points
    else:
        results = client.search(
            collection_name=collection_name,
            query_vector=query_vector,
            query_filter=search_filter,
            limit=top_k,
            with_payload=True,
        )

    def _source_type(payload) -> str:
        explicit = payload.get("source_type")
        if explicit:
            return explicit
        if collection_name == REPORTS_COLLECTION:
            return "secondary_report"
        if collection_name == CURATED_COLLECTION or str(payload.get("source", "")).startswith("curated"):
            return "curated_law"
        return "primary_law"

    chunks = []
    for hit in results:
        p = hit.payload
        chunks.append({
            "score": round(hit.score, 3),
            "text": p.get("text", ""),
            "title": p.get("report_title") or p.get("title", ""),
            "law_id": p.get("law_id", ""),
            "url": p.get("url", ""),
            "enacted_date": p.get("pub_date") or p.get("enacted_date", ""),
            "section_heading": p.get("section_heading", ""),
            "source": p.get("source", collection_name),
            "source_type": _source_type(p),
            "reviews_law_refs": p.get("reviews_law_refs", []),
        })
    return chunks


def retrieve(query: str,
             client: QdrantClient, date_from: str = None,
             category: str = None, top_k: int = TOP_K,
             collections: list[str] | None = None) -> list[dict]:
    """
    Retrieve relevant chunks across the law and report collections.

    Embeds the query once via Gemini (RETRIEVAL_QUERY task), searches each
    collection, then composes results **law-first**: up to ``top_k`` primary/
    curated law chunks, plus a small, capped set of secondary-report chunks as
    commentary (never crowding out the law). Missing collections are skipped
    gracefully. Optional filters: date_from, category.
    """
    if collections is None:
        collections = [QDRANT_COLLECTION, CURATED_COLLECTION, REPORTS_COLLECTION]

    query_vector = embed_query(query)

    # Build optional filters
    filters = []
    if date_from:
        date_from_int = date_to_int(normalize_date(date_from))
        if date_from_int is not None:
            # Range filters need the numeric enacted_date_int field. Points with
            # an unknown/unparseable date lack the field and are excluded, which
            # is the desired behaviour for a "since date" filter.
            filters.append(FieldCondition(
                key="enacted_date_int",
                range=Range(gte=date_from_int)
            ))
        else:
            print(f"  (ignoring unparseable --filter-date: {date_from!r})")
    if category:
        from qdrant_client.models import MatchValue
        filters.append(FieldCondition(
            key="category",
            match=MatchValue(value=category)
        ))

    search_filter = Filter(must=filters) if filters else None

    merged: list[dict] = []
    for collection_name in collections:
        try:
            merged.extend(
                _search_collection(client, query_vector, collection_name, search_filter, top_k)
            )
        except Exception as exc:  # collection may not exist yet
            print(f"  (skipping collection '{collection_name}': {exc})")

    # Law-first composition: law chunks fill up to top_k; reports are additive
    # but capped so commentary never crowds out the actual legislation.
    report_budget = max(1, top_k // 3)
    law = sorted((c for c in merged if c["source_type"] != "secondary_report"),
                 key=lambda c: c["score"], reverse=True)
    reports = sorted((c for c in merged if c["source_type"] == "secondary_report"),
                     key=lambda c: c["score"], reverse=True)
    return law[:top_k] + reports[:report_budget]


_SOURCE_TYPE_LABELS = {
    "primary_law": "PRIMARY LAW",
    "curated_law": "CURATED LAW",
    "secondary_report": "SECONDARY ANALYSIS",
}


def format_context(chunks: list[dict]) -> str:
    """Format retrieved chunks into authority-grouped blocks for the LLM."""
    groups: dict[str, list[dict]] = {"primary_law": [], "curated_law": [], "secondary_report": []}
    for c in chunks:
        groups.setdefault(c.get("source_type", "primary_law"), []).append(c)

    blocks = []
    idx = 1
    for source_type in ("primary_law", "curated_law", "secondary_report"):
        group = groups.get(source_type) or []
        if not group:
            continue
        is_report = source_type == "secondary_report"
        date_label = "published" if is_report else "enacted"
        parts = [f"=== {_SOURCE_TYPE_LABELS[source_type]} ==="]
        for c in group:
            heading = f" — {c['section_heading']}" if c['section_heading'] else ""
            reviews = ""
            if is_report and c.get("reviews_law_refs"):
                reviews = f", reviews: {', '.join(c['reviews_law_refs'])}"
            src = (f"[{idx}] {c['title']}{heading} "
                   f"({date_label}: {c['enacted_date'] or 'n/a'}{reviews}, score: {c['score']})")
            parts.append(f"{src}\n{c['text']}\nURL: {c['url']}")
            idx += 1
        blocks.append("\n\n".join(parts))
    return "\n\n---\n\n".join(blocks)


def ask_gemini(query: str, context: str) -> str:
    """Send query + retrieved context to Gemini for answer generation."""
    prompt = f"""Based on the following excerpts from Ukrainian legislation, please answer this question:

Question: {query}

Retrieved legal excerpts:

{context}

Please provide a clear, accurate answer citing the relevant laws and articles."""

    base_url = GEMINI_API_BASE.rstrip("/")
    url = f"{base_url}/models/{GEMINI_CHAT_MODEL}:generateContent"
    request_timeout = max(60, REQUEST_TIMEOUT * 3)

    payload = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
    }
    response = requests.post(
        url,
        params={"key": GEMINI_API_KEY},
        json=payload,
        timeout=request_timeout,
    )
    response.raise_for_status()
    data = response.json()
    try:
        answer = data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError):
        answer = ""
    if not answer:
        raise RuntimeError(f"Gemini returned an empty response: {data}")
    return answer


def main():
    parser = argparse.ArgumentParser(description="Query Ukrainian legislation RAG")
    parser.add_argument("query", nargs="+", help="Your legal question")
    parser.add_argument("--filter-date", help="Only laws enacted after this date (YYYY-MM-DD)")
    parser.add_argument("--category", help="Filter by law category")
    parser.add_argument("--top-k", type=int, default=TOP_K, help="Number of chunks to retrieve")
    parser.add_argument("--show-sources", action="store_true", help="Print retrieved chunks")
    args = parser.parse_args()

    query = " ".join(args.query)
    print(f"\n🔍 Query: {query}")
    if args.filter_date:
        print(f"   Date filter: ≥ {args.filter_date}")

    # Preflight check
    preflight_gemini()
    print(f"Using Gemini chat model: {GEMINI_CHAT_MODEL}")

    # Client
    client = get_qdrant_client()

    # Retrieve
    print("Retrieving relevant law excerpts...")
    chunks = retrieve(
        query, client,
        date_from=args.filter_date,
        category=args.category,
        top_k=args.top_k
    )

    if not chunks:
        print("✗ No relevant excerpts found.")
        return

    print(f"Found {len(chunks)} relevant chunks (top score: {chunks[0]['score']})")

    if args.show_sources:
        print("\n--- Retrieved excerpts ---")
        for c in chunks:
            print(f"\n[score={c['score']}] {c['title']}")
            print(f"  {c['text'][:200]}...")

    # Generate answer
    print("\nGenerating answer with Gemini...\n")
    context = format_context(chunks)
    answer = ask_gemini(query, context)

    print("=" * 60)
    print(answer)
    print("=" * 60)

    # Show source URLs
    print("\nSources:")
    seen = set()
    for c in chunks:
        key = c["url"] or c["title"]
        if key in seen:
            continue
        seen.add(key)
        tag = "[analysis] " if c.get("source_type") == "secondary_report" else ""
        suffix = f" — {c['url']}" if c["url"] else ""
        print(f"  • {tag}{c['title']}{suffix}")


if __name__ == "__main__":
    main()
