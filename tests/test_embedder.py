"""Tests for lazy, deterministic semantic-memory embeddings."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Lock

import pytest

from core import embedder


class _Encoded:
    def __init__(self, value):
        self.value = value

    def tolist(self):
        return self.value


class _FakeEmbedder:
    def __init__(self) -> None:
        self.calls: list[object] = []

    def encode(self, sentences, *, normalize_embeddings: bool):
        assert normalize_embeddings is True
        self.calls.append(sentences)
        if isinstance(sentences, str):
            return _Encoded([1.0, 2.0])
        return _Encoded([[float(i), 0.0] for i, _ in enumerate(sentences)])


@pytest.fixture(autouse=True)
def _reset_embedder_factory():
    embedder.set_model_factory_for_testing(None)
    yield
    embedder.set_model_factory_for_testing(None)


def test_embedder_is_initialized_only_on_first_genuine_use() -> None:
    created: list[_FakeEmbedder] = []

    def factory() -> _FakeEmbedder:
        model = _FakeEmbedder()
        created.append(model)
        return model

    embedder.set_model_factory_for_testing(factory)
    assert created == []

    assert embedder.embed("first") == [1.0, 2.0]
    assert embedder.embed_batch(["second", "third"]) == [[0.0, 0.0], [1.0, 0.0]]
    assert len(created) == 1
    assert created[0].calls == ["first", ["second", "third"]]


def test_concurrent_first_use_creates_one_embedding_model() -> None:
    created = 0
    counter_lock = Lock()

    def factory() -> _FakeEmbedder:
        nonlocal created
        with counter_lock:
            created += 1
        return _FakeEmbedder()

    embedder.set_model_factory_for_testing(factory)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(embedder.embed, [f"text-{i}" for i in range(24)]))

    assert results == [[1.0, 2.0]] * 24
    assert created == 1


def test_initialization_failure_is_reported_at_use_with_safe_error() -> None:
    def factory():
        raise OSError("network unavailable")

    embedder.set_model_factory_for_testing(factory)

    with pytest.raises(RuntimeError, match="Semantic memory is unavailable"):
        embedder.embed("only now")
