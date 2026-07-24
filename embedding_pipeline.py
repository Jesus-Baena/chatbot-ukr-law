import json
import math
import time
import uuid

import re
import requests
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PayloadSchemaType,
    PointStruct,
    TextIndexParams,
    TokenizerType,
    VectorParams,
)

from config import (
    CHUNK_OVERLAP,
    CHUNK_SIZE,
    EMBED_DIM,
    EMBED_MODEL,
    EMBED_TASK_DOCUMENT,
    EMBED_TASK_QUERY,
    GEMINI_API_BASE,
    GEMINI_API_KEY,
    PASSAGE_PREFIX,
    QDRANT_COLLECTION,
    QUERY_PREFIX,
    REQUEST_TIMEOUT,
)
from date_utils import enacted_date_fields


LOW_SIGNAL_CHUNK_SNIPPETS = [
    "верховна рада україни",
    "законодавство україни",
]


def _is_low_information_chunk(text: str) -> bool:
    normalized = " ".join(text.lower().split())
    if len(normalized) < 20:
        return True

    # Common portal boilerplate without legal body.
    if all(snippet in normalized for snippet in LOW_SIGNAL_CHUNK_SNIPPETS) and len(normalized) < 120:
        return True

    return False


def _l2_normalize(vector: list[float]) -> list[float]:
    """L2-normalize a vector (recommended for Gemini dims other than 3072)."""
    norm = math.sqrt(sum(component * component for component in vector))
    if norm == 0.0:
        return vector
    return [component / norm for component in vector]


def embed_via_gemini(
    texts: list[str],
    task_type: str,
    max_retries: int = 10,
    retry_wait: int = 15,
) -> list[list[float]]:
    """Embed a batch of texts with Google Gemini ``gemini-embedding-001``.

    Uses the synchronous ``batchEmbedContents`` endpoint. ``task_type`` selects
    query vs. passage asymmetry (RETRIEVAL_QUERY / RETRIEVAL_DOCUMENT) — this
    replaces the old mxbai text prefix. Retries on rate limits (429) and
    transient server/connection errors. Truncated dims (< 3072) are
    L2-normalized as recommended by Google.
    """
    if not GEMINI_API_KEY:
        raise RuntimeError(
            "Gemini API key missing — set GOOGLE_AI_API_KEY (or GEMINI_API_KEY)."
        )

    model_path = f"models/{EMBED_MODEL}"
    url = f"{GEMINI_API_BASE.rstrip('/')}/{model_path}:batchEmbedContents"
    body = {
        "requests": [
            {
                "model": model_path,
                "content": {"parts": [{"text": text}]},
                "taskType": task_type,
                "outputDimensionality": EMBED_DIM,
            }
            for text in texts
        ]
    }

    for attempt in range(max_retries):
        try:
            response = requests.post(
                url,
                params={"key": GEMINI_API_KEY},
                json=body,
                timeout=120,
            )
            if response.status_code in (429, 500, 503):
                raise requests.exceptions.ConnectionError(
                    f"Gemini transient HTTP {response.status_code}"
                )
            response.raise_for_status()
            embeddings = [item["values"] for item in response.json()["embeddings"]]
            if EMBED_DIM != 3072:
                embeddings = [_l2_normalize(vector) for vector in embeddings]
            return embeddings
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
            if attempt + 1 >= max_retries:
                raise
            print(f"\n  [embed] Gemini unavailable ({exc.__class__.__name__}), "
                  f"retry {attempt + 1}/{max_retries} in {retry_wait}s …", flush=True)
            time.sleep(retry_wait)


def embed_query(query: str) -> list[float]:
    """Embed a single query string for retrieval (RETRIEVAL_QUERY task)."""
    return embed_via_gemini([QUERY_PREFIX + query], EMBED_TASK_QUERY)[0]



def setup_qdrant(
    client: QdrantClient,
    collection_name: str = QDRANT_COLLECTION,
    extra_payload_indexes: dict | None = None,
):
    """Create a Qdrant collection (and its payload indexes) if it doesn't exist.

    ``extra_payload_indexes`` maps additional field names to a Qdrant payload
    schema (e.g. the curated collection's ``utility_score`` / ``topics`` fields).
    """
    existing = [collection.name for collection in client.get_collections().collections]
    if collection_name in existing:
        print(f"✓ Collection '{collection_name}' exists")
        return

    client.create_collection(
        collection_name=collection_name,
        vectors_config=VectorParams(size=EMBED_DIM, distance=Distance.COSINE),
    )

    client.create_payload_index(
        collection_name=collection_name,
        field_name="law_id",
        field_schema=PayloadSchemaType.KEYWORD,
    )
    client.create_payload_index(
        collection_name=collection_name,
        field_name="category",
        field_schema=PayloadSchemaType.KEYWORD,
    )
    client.create_payload_index(
        collection_name=collection_name,
        field_name="enacted_date",
        field_schema=PayloadSchemaType.KEYWORD,
    )
    # Sortable YYYYMMDD integer for date range filtering (Qdrant Range needs a
    # numeric field — the KEYWORD enacted_date above only supports exact match).
    client.create_payload_index(
        collection_name=collection_name,
        field_name="enacted_date_int",
        field_schema=PayloadSchemaType.INTEGER,
    )
    client.create_payload_index(
        collection_name=collection_name,
        field_name="text",
        field_schema=TextIndexParams(
            type="text",
            tokenizer=TokenizerType.WORD,
            min_token_len=2,
            max_token_len=40,
            lowercase=True,
        ),
    )

    for field_name, field_schema in (extra_payload_indexes or {}).items():
        client.create_payload_index(
            collection_name=collection_name,
            field_name=field_name,
            field_schema=field_schema,
        )

    print(f"✓ Created collection '{collection_name}'")


_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")   # [text](url) → text
_MD_STYLE_RE = re.compile(r"[*_]{1,3}([^*_]+)[*_]{1,3}")  # *italic* / **bold** → text
_MD_MISC_RE = re.compile(r"[`~#>]+")


def _strip_markdown(text: str) -> str:
    """Remove markdown syntax so URLs don't inflate the token count."""
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _MD_STYLE_RE.sub(r"\1", text)
    text = _MD_MISC_RE.sub("", text)
    return text.strip()


def chunk_section(heading: str, text: str, chunk_size: int, overlap: int) -> list[str]:
    """Chunk a section's text into overlapping character-based chunks.

    chunk_size and overlap are in characters.  Splits are aligned to the
    nearest word boundary so we never embed half-words.
    """
    text = _strip_markdown(text)
    if not text:
        return []

    # Reserve space for heading so final chunks stay within chunk_size
    heading_prefix = f"{heading}\n" if heading else ""
    effective_size = max(100, chunk_size - len(heading_prefix))

    chunks = []
    start = 0
    while start < len(text):
        end = min(start + effective_size, len(text))
        # Extend to next whitespace boundary (don't cut mid-word)
        if end < len(text) and not text[end].isspace():
            while end < len(text) and not text[end].isspace():
                end += 1
        chunk_text = text[start:end].strip()
        if heading_prefix:
            chunk_text = heading_prefix + chunk_text
        # Hard cap: if still over limit (long headings + wrapping), trim at word boundary
        if len(chunk_text) > chunk_size:
            chunk_text = chunk_text[:chunk_size]
            last_space = chunk_text.rfind(" ")
            if last_space > 0:
                chunk_text = chunk_text[:last_space]
        chunks.append(chunk_text)
        if end >= len(text):
            break
        # Retreat start by overlap, aligning to a word boundary
        next_start = end - overlap
        if next_start > start:
            # Snap to the start of the nearest word
            while next_start < end and not text[next_start].isspace():
                next_start -= 1
            start = max(start + 1, next_start)
        else:
            start = end  # safety: always advance

    return chunks


def law_to_chunks(law: dict) -> list[dict]:
    """Convert a law JSON object into the chunk format used for embedding."""
    chunks = []
    chunk_index = 0

    # Normalize the law's date once (prefer extracted enacted_date, fall back to
    # the catalogue date) into an ISO string + sortable YYYYMMDD int.
    enacted_date, enacted_date_int = enacted_date_fields(
        law.get("enacted_date") or law.get("catalogue_date", "")
    )

    for section in law.get("sections", []):
        heading = section.get("heading", "")
        text = section.get("text", "")
        if not text.strip():
            continue

        section_chunks = chunk_section(heading, text, CHUNK_SIZE, CHUNK_OVERLAP)
        for chunk_text in section_chunks:
            if _is_low_information_chunk(chunk_text):
                continue
            chunk = {
                "text": chunk_text,
                "law_id": law["id"],
                "title": law.get("title", ""),
                "url": law.get("url", ""),
                "category": law.get("category", ""),
                "enacted_date": enacted_date,
                "section_heading": heading,
                "chunk_index": chunk_index,
            }
            if enacted_date_int is not None:
                chunk["enacted_date_int"] = enacted_date_int
            chunks.append(chunk)
            chunk_index += 1

    return chunks


def embed_chunks(chunks: list[dict], batch_size: int = 100) -> list[list[float]]:
    """Embed chunk texts in batches using Gemini (RETRIEVAL_DOCUMENT task)."""
    texts = [PASSAGE_PREFIX + chunk["text"] for chunk in chunks]
    all_embeddings: list[list[float]] = []
    for i in range(0, len(texts), batch_size):
        all_embeddings.extend(
            embed_via_gemini(texts[i : i + batch_size], EMBED_TASK_DOCUMENT)
        )
    return all_embeddings


def upsert_to_qdrant(
    client: QdrantClient,
    chunks: list[dict],
    embeddings: list[list[float]],
    collection_name: str = QDRANT_COLLECTION,
):
    """Upsert chunk vectors and payloads to a Qdrant collection."""
    points = []
    for chunk, vector in zip(chunks, embeddings):
        point_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{chunk['law_id']}:{chunk['chunk_index']}"))
        payload = {key: value for key, value in chunk.items() if key != "text"}
        payload["text"] = chunk["text"]
        points.append(PointStruct(id=point_id, vector=vector, payload=payload))

    # Upload in batches to avoid Qdrant write-timeout on large payloads
    batch_size = 200
    for i in range(0, len(points), batch_size):
        client.upsert(collection_name=collection_name, points=points[i:i + batch_size])


def get_processed_ids(client: QdrantClient, collection_name: str = QDRANT_COLLECTION) -> set[str]:
    """Get the set of law IDs already present in a Qdrant collection."""
    processed = set()
    offset = None

    while True:
        result, next_offset = client.scroll(
            collection_name=collection_name,
            scroll_filter=None,
            limit=1000,
            offset=offset,
            with_payload=["law_id"],
            with_vectors=False,
        )
        for point in result:
            if point.payload and "law_id" in point.payload:
                processed.add(point.payload["law_id"])
        if next_offset is None:
            break
        offset = next_offset

    return processed


def delete_law_from_qdrant(client: QdrantClient, law_id: str, collection_name: str = QDRANT_COLLECTION):
    """Remove all chunks for a law before re-indexing it."""
    client.delete(
        collection_name=collection_name,
        points_selector=Filter(
            must=[FieldCondition(key="law_id", match=MatchValue(value=law_id))]
        ),
    )
