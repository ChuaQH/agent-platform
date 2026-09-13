"""Lazy shared bge-m3 embedding utility.

Importing this module is side-effect free. The model is initialized only when
semantic memory genuinely requests an embedding.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from threading import Lock
from typing import Any, Protocol

logger = logging.getLogger(__name__)

class EmbeddingModel(Protocol):
    def encode(self, sentences: str | list[str], *, normalize_embeddings: bool) -> Any: ...


ModelFactory = Callable[[], EmbeddingModel]
_model: EmbeddingModel | None = None
_model_lock = Lock()
_model_factory: ModelFactory | None = None


def _default_model_factory() -> EmbeddingModel:
    # Keep the sentence-transformers import itself out of application startup.
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer("BAAI/bge-m3")


def _get_model() -> EmbeddingModel:
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        try:
            model = (_model_factory or _default_model_factory)()
        except Exception as exc:  # noqa: BLE001 - safe public error at point of use
            logger.exception("Embedding model initialization failed")
            raise RuntimeError(
                "Semantic memory is unavailable because its embedding model could not be initialized."
            ) from exc
        _model = model
        logger.info("Embedding model initialized on first use.")
        return model


def set_model_factory_for_testing(factory: ModelFactory | None) -> None:
    """Install a deterministic test factory and clear the cached model.

    This narrow injection seam is not configuration-driven production mode.
    Tests must restore ``None`` when complete.
    """
    global _model, _model_factory
    with _model_lock:
        _model = None
        _model_factory = factory


def embed(text: str) -> list[float]:
    return _get_model().encode(text, normalize_embeddings=True).tolist()


def embed_batch(texts: list[str]) -> list[list[float]]:
    return _get_model().encode(texts, normalize_embeddings=True).tolist()
