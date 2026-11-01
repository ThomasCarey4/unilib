from __future__ import annotations

import threading
import time
from typing import Callable, Protocol

RETRY_AFTER_S = 600.0  # after a failed model load, do not touch the network again for this long


class Reranker(Protocol):
    model: str

    def load(self) -> None: ...  # make `score` ready; may be slow, and may raise

    def score(self, query: str, texts: list[str]) -> list[float]: ...


class GuardedReranker:
    """Lazy model loading shared by the rerankers. The first `load`/`score` call loads.

    A failed load is remembered: for RETRY_AFTER_S seconds later calls raise at once
    instead of retrying the download. Subclasses implement `_load_model` and `_score`.
    """

    def __init__(self, model: str, clock: Callable[[], float] = time.monotonic):
        self.model = model
        self._clock = clock
        self._model = None
        self._failed_at: float | None = None
        self._error = ""
        self._lock = threading.Lock()  # server tools run in worker threads

    def _load_model(self):
        raise NotImplementedError

    def _score(self, query: str, texts: list[str]) -> list[float]:
        raise NotImplementedError

    def load(self) -> None:
        if self._model is not None:
            return
        with self._lock:
            if self._model is not None:
                return
            if self._failed_at is not None:
                if self._clock() - self._failed_at < RETRY_AFTER_S:
                    raise RuntimeError(f"model load failed earlier ({self._error}); not retrying yet")
            try:
                self._model = self._load_model()
            except Exception as e:
                self._failed_at, self._error = self._clock(), f"{type(e).__name__}: {e}"
                raise
            self._failed_at = None

    def score(self, query: str, texts: list[str]) -> list[float]:
        if not texts:
            return []
        self.load()
        return self._score(query, texts)
