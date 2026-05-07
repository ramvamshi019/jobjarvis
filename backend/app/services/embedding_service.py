"""Embedding service — sentence-transformers (default) with OpenAI fallback.

Backend selection (EMBEDDING_BACKEND env var):
  sentence_transformers  — free, runs locally, 384 dims (default)
  openai                 — text-embedding-3-small, 1536 dims (requires OPENAI_API_KEY)

VECTOR_DIMENSIONS must match the backend:
  sentence_transformers → 384
  openai                → 1536
"""
from __future__ import annotations

import logging
import os
from functools import lru_cache

# Container's HOME (/home/appuser) is read-only — point HF caches at /tmp
# BEFORE any transformers / sentence-transformers / huggingface_hub import.
os.environ.setdefault("HF_HOME", "/tmp/hf_cache")
os.environ.setdefault("TRANSFORMERS_CACHE", "/tmp/hf_cache/transformers")
os.environ.setdefault("SENTENCE_TRANSFORMERS_HOME", "/tmp/st_cache")
os.environ.setdefault("XDG_CACHE_HOME", "/tmp/xdg_cache")
for _d in (os.environ["HF_HOME"], os.environ["SENTENCE_TRANSFORMERS_HOME"]):
    try:
        os.makedirs(_d, exist_ok=True)
    except OSError:
        pass

from app.config import settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lazy model loader — only instantiated on first call
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _get_st_model():
    """Load sentence-transformers model once and cache it in the process."""
    try:
        from sentence_transformers import SentenceTransformer
        model_name = getattr(settings, "ST_MODEL", "all-MiniLM-L6-v2")
        logger.info("embedding_service.loading_model model=%s", model_name)
        model = SentenceTransformer(
            model_name,
            cache_folder=os.environ.get("SENTENCE_TRANSFORMERS_HOME"),
        )
        logger.info("embedding_service.model_ready model=%s dims=%d",
                    model_name, model.get_sentence_embedding_dimension())
        return model
    except Exception as e:
        logger.error("embedding_service.model_load_failed error=%s", e)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_embedding(text: str) -> list[float]:
    """Return a dense embedding vector for *text*.

    Uses sentence-transformers by default (free, local, 384 dims).
    Falls back to OpenAI if EMBEDDING_BACKEND=openai and key is set.
    Returns zero-vector on total failure — never raises.
    """
    if not text or not text.strip():
        return _zero_vector()

    backend = getattr(settings, "EMBEDDING_BACKEND", "sentence_transformers")
    if backend == "openai" and settings.OPENAI_API_KEY:
        return _embed_openai(text)
    return _embed_st(text)


def generate_job_embedding_text(title: str, description: str,
                                skills: list[str] | None = None,
                                location: str = "") -> str:
    """Build canonical text for a job posting.

    Title gets repeated for emphasis; skills listed explicitly; description
    truncated to ~1500 chars to stay within the 512-token model window.
    """
    parts: list[str] = []
    if title:
        # Repeat title for stronger weighting
        parts.append(f"Job title: {title.strip()}")
        parts.append(f"Role: {title.strip()}")
    if location:
        parts.append(f"Location: {location.strip()}")
    if skills:
        parts.append(f"Required skills: {', '.join(skills[:20])}")
    if description:
        parts.append(description.strip()[:1500])
    return "\n".join(parts)


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Pure-Python cosine similarity between two vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _embed_st(text: str) -> list[float]:
    model = _get_st_model()
    if model is None:
        return _zero_vector()
    try:
        vec = model.encode(text, normalize_embeddings=True)
        return vec.tolist()
    except Exception as e:
        logger.error("embedding_service.st_encode_failed error=%s", e)
        return _zero_vector()


def _embed_openai(text: str) -> list[float]:
    try:
        import openai
        client = openai.OpenAI(api_key=settings.OPENAI_API_KEY)
        resp = client.embeddings.create(
            model=settings.AI_EMBEDDING_MODEL,
            input=text[:8000],
        )
        return resp.data[0].embedding
    except Exception as e:
        logger.error("embedding_service.openai_failed error=%s falling_back_to_st", e)
        return _embed_st(text)


def _zero_vector() -> list[float]:
    dims = getattr(settings, "VECTOR_DIMENSIONS", 384)
    return [0.0] * dims
