import os
import importlib
from pathlib import Path


def load_dotenv():
    try:
        return importlib.import_module("dotenv").load_dotenv()
    except ModuleNotFoundError:
        return False

load_dotenv()


def _first_env(*names: str, default: str = "") -> str:
    """Return the first non-empty environment variable from a list of names."""
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    """Read an int env var, falling back to default when unset/empty/invalid."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default

# Paths
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
LAWS_DIR = DATA_DIR / "laws"
CATALOGUE_PATH = DATA_DIR / "catalogue.json"
STATE_PATH = DATA_DIR / "state.json"  # tracks last_updated for incremental runs

DATA_DIR.mkdir(exist_ok=True)
LAWS_DIR.mkdir(exist_ok=True)

# Rada open data portal — legislation catalogue (CSV/JSON)
# Full catalogue of all law IDs, titles, dates, categories
CATALOGUE_URL = "https://data.rada.gov.ua/open/data/zak"

# Full-text law base URL
LAW_BASE_URL = "https://zakon.rada.gov.ua/laws/show/{law_id}"

# Qdrant
QDRANT_URL = _first_env("QDRANT_URL", "QDRANT_API_URL", "QDRANT_UR", default="http://localhost:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY", "").strip()
# Auto-scraped Rada corpus (rebuilt with Gemini embeddings — see 8_reembed_to_gemini.py)
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "rada_legislation")
# Hand-curated humanitarian knowledgebase, kept in a separate collection
CURATED_COLLECTION = os.getenv("CURATED_COLLECTION", "curated_legislation")

# Postgres (staging layer before Qdrant)
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
STAGING_STORE_RAW_JSON = _env_bool("STAGING_STORE_RAW_JSON", default=False)

# Docling
DOCLING_API_URL = os.getenv("DOCLING_API_URL", "").strip()

# Embedding model — Google Gemini gemini-embedding-001
# Migrated from Ollama mxbai-embed-large (1024-d); see 8_reembed_to_gemini.py for the rebuild.
EMBED_MODEL = os.getenv("EMBED_MODEL", "gemini-embedding-001").strip()
# gemini-embedding-001 supports Matryoshka output dims 3072 (default) / 1536 / 768.
# Using the full 3072 dims for maximum retrieval quality (vectors are already normalized).
EMBED_DIM = int(os.getenv("EMBED_DIM", "3072"))
# Gemini handles query/passage asymmetry via task types, not text prefixes.
EMBED_TASK_DOCUMENT = "RETRIEVAL_DOCUMENT"
EMBED_TASK_QUERY = "RETRIEVAL_QUERY"
# Legacy mxbai prefixes — retained (empty) for backward compatibility; superseded by task types.
QUERY_PREFIX = ""
PASSAGE_PREFIX = ""

# Google AI (Gemini) — one API key serves both embeddings and generation
GEMINI_API_KEY = _first_env("GOOGLE_AI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY")
GEMINI_API_BASE = os.getenv("GEMINI_API_BASE", "https://generativelanguage.googleapis.com/v1beta").strip()
GEMINI_CHAT_MODEL = os.getenv("GEMINI_CHAT_MODEL", "gemini-2.0-flash").strip()

# Chunking (character-based; splits are word-boundary aligned).
# Sized for Gemini gemini-embedding-001 (2048-token input). The old 400-char
# window was a leftover from mxbai-embed-large's 512-token limit and produced
# heavy over-fragmentation of legal text; ~1200 chars keeps most individual
# articles whole while staying well within Gemini's input budget.
# NOTE: changing these requires re-embedding the corpus to keep chunking
# consistent — rebuild with `8_reembed_to_gemini.py --recreate`.
CHUNK_SIZE = env_int("CHUNK_SIZE", 1200)       # characters per chunk
CHUNK_OVERLAP = env_int("CHUNK_OVERLAP", 200)  # character overlap between chunks

# Scraping
REQUEST_DELAY = 1.2     # seconds between requests — be polite
REQUEST_TIMEOUT = 20
MAX_RETRIES = 3
BATCH_SIZE = 50         # laws per batch before saving progress

# Scope filters (from .env)
DATE_FROM = os.getenv("DATE_FROM", "2000-01-01")   # filter by enactment date
MAX_LAWS = int(os.getenv("MAX_LAWS", "999999"))     # cap for testing
CATALOGUE_OFFSET = int(os.getenv("CATALOGUE_OFFSET", "0"))
CATEGORY_FILTER = os.getenv("CATEGORY_FILTER", "")  # optional keyword filter
FORCE_RESCRAPE = _env_bool("FORCE_RESCRAPE", default=False)

# Ollama
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").strip()
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma4:e4b").strip()

# Humanitarian-relevant law keywords for category filtering
HUMANITARIAN_KEYWORDS = [
    "внутрішньо переміщен",   # internally displaced
    "біженц",                  # refugees
    "гуманітарн",              # humanitarian
    "воєнний стан",            # martial law
    "соціальний захист",       # social protection
    "допомога",                # assistance
    "евакуац",                 # evacuation
    "цивільн",                 # civilian
    "медичн",                  # medical
    "захист населення",        # population protection
]
