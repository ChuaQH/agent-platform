"""Shared embedding utility using sentence-transformers bge-m3.

Lazily loads the model on first use to avoid ~2 GB startup cost.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_model = None


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer
        logger.info("Loading embedding model: BAAI/bge-m3")
        _model = SentenceTransformer("BAAI/bge-m3")
        logger.info("Embedding model loaded.")
    return _model


def embed(text: str) -> list[float]:
    model = _get_model()
    return model.encode(text, normalize_embeddings=True).tolist()


def embed_batch(texts: list[str]) -> list[list[float]]:
    model = _get_model()
    embeddings = model.encode(texts, normalize_embeddings=True)
    return embeddings.tolist()
