"""Encoder engine for relevance pruning — local, no LLM call, no training.

The cheap/local/private half of the ensemble (the judge in :mod:`relevance` is
the expensive, reasoning half). It scores each episode's relevance to the
*current* task and maps that score to KEEP/SUMMARIZE/DROP by threshold. It runs
the same in the website backend and the extension.

Backends, picked automatically unless one is forced via ``backend=``:

  * ``onnx``    — a cross-encoder exported to ONNX (e.g. Provence or any
    reranker), run with ``onnxruntime``. The production path: ~10 ms on CPU,
    private, free per call. Point ``model_path`` at the exported model dir.
  * ``embed``   — ``sentence-transformers`` embeddings + cosine similarity.
  * ``lexical`` — dependency-free salient-token overlap (file paths,
    identifiers, error strings). Always available, so the ensemble works out of
    the box; install a real model and set ``model_path`` to upgrade.

Heavy libraries (numpy / onnxruntime / transformers / sentence-transformers) are
imported **lazily**, so importing this module — and re-exporting it from
``acm_engine`` — stays cheap and never fails on a minimal install.
"""

from __future__ import annotations

import logging
import math
import re
from typing import List, Optional

from relevance import Episode, Suggestion

log = logging.getLogger("relevance_encoder")

VALID_BACKENDS = ("auto", "onnx", "embed", "lexical")

# Tokens worth more for code/agent relevance: paths, dotted identifiers,
# snake/camel names, error-ish strings. Used by the lexical backend's weighting.
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_./-]{2,}")
_STOP = {
    "the", "and", "for", "with", "this", "that", "you", "your", "are", "was",
    "but", "not", "have", "has", "will", "can", "should", "would", "from",
    "into", "now", "then", "add", "fix", "build", "test", "make", "use",
    "code", "file", "files", "function", "feature", "bug", "error", "please",
}


def _salient(text: str) -> dict:
    """Map of salient token -> weight. Identifier-ish tokens (a dot, slash or
    underscore, or mixed case) weigh more than plain words."""
    out: dict = {}
    for m in _TOKEN_RE.finditer(text or ""):
        tok = m.group(0)
        low = tok.lower()
        if low in _STOP or len(low) < 3:
            continue
        weight = 1.0
        if any(c in tok for c in "./_-") or (tok != low and tok != tok.upper()):
            weight = 2.5  # paths / identifiers carry the real signal
        out[low] = max(out.get(low, 0.0), weight)
    return out


def _lexical_score(task: str, text: str) -> float:
    """Weighted overlap-coefficient of salient tokens, in [0, 1]. Measures how
    much of the *task's* vocabulary the episode still talks about."""
    a = _salient(task)
    b = _salient(text)
    if not a or not b:
        return 0.0
    shared = set(a) & set(b)
    inter = sum(min(a[t], b[t]) for t in shared)
    norm = math.sqrt(sum(a.values()) * sum(b.values()))
    return max(0.0, min(1.0, inter / norm)) if norm else 0.0


class EncoderSuggester:
    """Score episodes vs the current task and label them by threshold.

    Implements the same ``suggest(episodes, task) -> List[Suggestion]`` contract
    as :class:`relevance.JudgeSuggester`, so :class:`relevance.EnsembleSuggester`
    can hold one of each.
    """

    def __init__(
        self,
        model_path: Optional[str] = None,
        *,
        backend: str = "auto",
        drop_threshold: float = 0.35,
        summarize_threshold: float = 0.6,
    ) -> None:
        self.model_path = model_path
        self.requested_backend = backend if backend in VALID_BACKENDS else "auto"
        self.drop_threshold = float(drop_threshold)
        self.summarize_threshold = float(summarize_threshold)
        self.backend: Optional[str] = None  # resolved on first use
        self._tokenizer = None
        self._session = None
        self._embedder = None

    # ── backend resolution (lazy) ────────────────────────────────────────
    def _ensure_loaded(self) -> None:
        if self.backend is not None:
            return
        order = (
            [self.requested_backend]
            if self.requested_backend != "auto"
            else ["onnx", "embed", "lexical"]
        )
        for b in order:
            try:
                if b == "onnx" and self._try_load_onnx():
                    self.backend = "onnx"
                    return
                if b == "embed" and self._try_load_embed():
                    self.backend = "embed"
                    return
                if b == "lexical":
                    self.backend = "lexical"
                    return
            except Exception as e:  # pragma: no cover - environment dependent
                log.warning("[encoder] backend %s unavailable: %r", b, e)
        self.backend = "lexical"  # always works

    def _try_load_onnx(self) -> bool:
        if not self.model_path:
            return False
        import onnxruntime  # type: ignore
        from transformers import AutoTokenizer  # type: ignore

        self._tokenizer = AutoTokenizer.from_pretrained(self.model_path)
        # Expect a single-file model.onnx inside the dir (the usual export).
        import os

        onnx_file = self.model_path
        if os.path.isdir(self.model_path):
            onnx_file = os.path.join(self.model_path, "model.onnx")
        self._session = onnxruntime.InferenceSession(
            onnx_file, providers=["CPUExecutionProvider"]
        )
        return True

    def _try_load_embed(self) -> bool:
        from sentence_transformers import SentenceTransformer  # type: ignore

        name = self.model_path or "sentence-transformers/all-MiniLM-L6-v2"
        self._embedder = SentenceTransformer(name)
        return True

    # ── scoring ──────────────────────────────────────────────────────────
    def _score_onnx(self, task: str, texts: List[str]) -> List[float]:
        import numpy as np  # type: ignore

        enc = self._tokenizer(
            [task] * len(texts), texts, padding=True, truncation=True,
            max_length=512, return_tensors="np",
        )
        feeds = {k: v for k, v in enc.items() if k in {i.name for i in self._session.get_inputs()}}
        logits = self._session.run(None, feeds)[0]
        logits = np.asarray(logits).reshape(len(texts), -1)
        # Single-logit reranker -> sigmoid; multi-class -> softmax "relevant".
        if logits.shape[1] == 1:
            scores = 1.0 / (1.0 + np.exp(-logits[:, 0]))
        else:
            ex = np.exp(logits - logits.max(axis=1, keepdims=True))
            scores = (ex / ex.sum(axis=1, keepdims=True))[:, -1]
        return [float(s) for s in scores]

    def _score_embed(self, task: str, texts: List[str]) -> List[float]:
        import numpy as np  # type: ignore

        vecs = self._embedder.encode([task] + texts, normalize_embeddings=True)
        q = np.asarray(vecs[0])
        return [float(max(0.0, np.dot(q, np.asarray(v)))) for v in vecs[1:]]

    def _score(self, task: str, texts: List[str]) -> List[float]:
        self._ensure_loaded()
        if self.backend == "onnx":
            return self._score_onnx(task, texts)
        if self.backend == "embed":
            return self._score_embed(task, texts)
        return [_lexical_score(task, t) for t in texts]

    # ── the Suggester contract ───────────────────────────────────────────
    def suggest(self, episodes: List[Episode], task: str) -> List[Suggestion]:
        if not episodes:
            return []
        try:
            scores = self._score(task, [ep.text for ep in episodes])
        except Exception as e:  # never break the caller over a model hiccup
            log.warning("[encoder] scoring failed: %r — abstaining (KEEP)", e)
            return [
                Suggestion(
                    episode_id=ep.id, episode_index=ep.index, label="KEEP",
                    score=0.5, reason=f"encoder unavailable: {type(e).__name__}",
                    source="encoder", freed_tokens=0,
                    member_indices=ep.member_indices, title=ep.title,
                )
                for ep in episodes
            ]
        out: List[Suggestion] = []
        for ep, rel in zip(episodes, scores):
            if rel < self.drop_threshold:
                label, conf, freed = "DROP", 1.0 - rel, ep.tokens
            elif rel < self.summarize_threshold:
                label, conf, freed = "SUMMARIZE", 0.5, int(ep.tokens * 0.7)
            else:
                label, conf, freed = "KEEP", rel, 0
            out.append(
                Suggestion(
                    episode_id=ep.id,
                    episode_index=ep.index,
                    label=label,
                    score=conf,
                    reason=f"relevance {rel:.2f} to current task ({self.backend})",
                    source="encoder",
                    freed_tokens=freed,
                    member_indices=ep.member_indices,
                    title=ep.title,
                )
            )
        return out
