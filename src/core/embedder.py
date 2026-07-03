"""Shared embedding utility using sentence-transformers bge-m3.

Eagerly loads the model at import time so the first embedding call is fast.
"""

from __future__ import annotations

import logging

from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

logger.info("Loading embedding model: BAAI/bge-m3")
_model = SentenceTransformer("BAAI/bge-m3")
logger.info("Embedding model loaded.")


def embed(text: str) -> list[float]:
    return _model.encode(text, normalize_embeddings=True).tolist()


def embed_batch(texts: list[str]) -> list[list[float]]:
    embeddings = _model.encode(texts, normalize_embeddings=True)
    return embeddings.tolist()
