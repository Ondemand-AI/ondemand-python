"""
OpenAI-compatible inference client for fine-tuned classification models.

This is the serving-side counterpart to the training pipeline: given a vLLM
endpoint (an OpenAI-compatible server, e.g. a RunPod pod serving base + LoRA
adapter), it sends completion requests and derives a **confidence** for each
prediction so the caller can gate on it (auto-post vs send to human review).

Ported from the fine-tuning spike (`run_llm_arm.py`, `eval_margin.py`,
`calibrate.py`), generalized: nothing here knows about a particular chart of
accounts or client. The prompt template, the label set and the name->code map
belong to the caller — this module only talks HTTP and does the confidence math.

Confidence signals (the spike found the first-token margin the strongest, AUC
0.88 vs 0.75 for the geometric mean):
  - ``margin_first``: P(top1) - P(top2) at the first generated token
  - ``margin_min`` / ``margin_mean``: min / mean of that margin over all tokens
  - ``geomean``: exp(mean(token logprobs)), the original signal

The endpoint is plain vLLM ``/v1/completions`` with ``logprobs`` requested; no
Valkyrie, no gateway. Point ``base_url`` straight at the box.
"""

import logging
import math
import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# httpx is a base dependency of ondemand-ai, but guard anyway so import of this
# module never hard-fails a robot that only needed something else from shared.
try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:  # pragma: no cover
    HTTPX_AVAILABLE = False
    logger.warning("httpx not installed. LLM inference will not be available.")


# --------------------------------------------------------------------------- #
# Pure helpers (no I/O) — safe to unit-test on their own.
# --------------------------------------------------------------------------- #

def normalize_text(s: Optional[str]) -> str:
    """Strip accents, collapse whitespace, lowercase. Matches the spike's `_norm`."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", s.strip().lower())


def build_alpaca_prompt(instruction: str, input_text: str) -> str:
    """Reconstruct the alpaca training format the LoRA was fine-tuned on.

    The fine-tune saw ``### Instruction / ### Input / ### Response`` blocks, so
    inference must send the same shape or accuracy collapses.
    """
    return f"### Instruction:\n{instruction}\n\n### Input:\n{input_text}\n\n### Response:\n"


def map_name_to_code(text: str, name_to_code: Dict[str, str]) -> Optional[str]:
    """Map a model-emitted account name back to its code.

    Exact normalized match first, then a best-effort substring match either way
    (a known name contained in the output, or the output contained in a name).
    ``name_to_code`` keys must already be normalized (see ``normalize_text``).
    """
    if not text:
        return None
    nt = normalize_text(text)
    if nt in name_to_code:
        return name_to_code[nt]
    for name, code in name_to_code.items():
        if name and (name in nt or nt in name):
            return code
    return None


def prediction_from_choice(
    choice: Dict[str, Any], name_to_code: Optional[Dict[str, str]] = None
) -> "Prediction":
    """Build a Prediction from a vLLM/OpenAI ``choices[0]`` object.

    Shared by the dedicated HTTP path (``LLMInferenceClient.classify``) and the
    serverless path (the RunPod worker returns this same choice shape), so the
    margin/confidence math is identical wherever the model is served.
    """
    text = (choice.get("text") or "").strip()
    conf = margins_from_logprobs(choice.get("logprobs"))
    code = map_name_to_code(text, name_to_code) if name_to_code else None
    return Prediction(raw=text, code=code, confidence=conf)


def predictions_from_choices(choices, name_to_code=None):
    """Batch counterpart of ``prediction_from_choice``: one Prediction per choice
    (the RunPod worker returns ``{"choices": [...]}`` for a batched request)."""
    return [prediction_from_choice(c, name_to_code) for c in (choices or [])]


def precision_coverage_curve(scored, cutoffs=None):
    """Auto-post precision vs coverage across margin cutoffs.

    ``scored``: iterable of ``(margin_first, correct_bool)``. For each cutoff,
    precision = fraction correct among rows with margin >= cutoff, coverage =
    fraction of rows at/above it. Empirical (no sklearn) — the operating-point
    view the spike derived via calibration. margin None counts as 0.
    """
    pts = [(float(m) if m is not None else 0.0, bool(c)) for m, c in scored]
    n = len(pts) or 1
    cutoffs = cutoffs if cutoffs is not None else [0.90, 0.95, 0.97, 0.98, 0.99, 0.995, 0.999]
    out = []
    for cut in cutoffs:
        auto = [c for m, c in pts if m >= cut]
        out.append({
            "cutoff": cut,
            "auto": len(auto),
            "coverage": len(auto) / n,
            "precision": (sum(auto) / len(auto)) if auto else 0.0,
        })
    return out


def threshold_for_target_precision(scored, target, grid_step=0.001):
    """Smallest margin cutoff whose empirical auto-post precision >= ``target``.

    Returns ``(cutoff, coverage, precision)`` or ``None`` when unreachable (the
    model's confidence ceiling is below the target — no cutoff delivers it).
    """
    pts = [(float(m) if m is not None else 0.0, bool(c)) for m, c in scored]
    n = len(pts) or 1
    cut = 0.0
    while cut <= 1.0:
        auto = [c for m, c in pts if m >= cut]
        if auto and (sum(auto) / len(auto)) >= target:
            return (round(cut, 4), len(auto) / n, sum(auto) / len(auto))
        cut += grid_step
    return None


def margins_from_logprobs(logprobs: Optional[Dict[str, Any]]) -> Dict[str, Optional[float]]:
    """Compute confidence signals from a vLLM ``choices[0].logprobs`` object.

    Returns a dict with ``geomean``, ``margin_first``, ``margin_min`` and
    ``margin_mean``. Any signal is ``None`` when there are no usable tokens.
    """
    logprobs = logprobs or {}
    tok_lps = [x for x in (logprobs.get("token_logprobs") or []) if isinstance(x, (int, float))]
    tops = logprobs.get("top_logprobs") or []

    geomean = math.exp(sum(tok_lps) / len(tok_lps)) if tok_lps else None

    margins: List[float] = []
    for i, _chosen in enumerate(tok_lps):
        if i >= len(tops) or not tops[i]:
            continue
        vals = sorted(tops[i].values(), reverse=True)  # logprobs of the top-k here
        p1 = math.exp(vals[0])
        p2 = math.exp(vals[1]) if len(vals) > 1 else 0.0
        margins.append(p1 - p2)

    return {
        "geomean": geomean,
        "margin_first": margins[0] if margins else None,
        "margin_min": min(margins) if margins else None,
        "margin_mean": (sum(margins) / len(margins)) if margins else None,
    }


# --------------------------------------------------------------------------- #
# Result type
# --------------------------------------------------------------------------- #

@dataclass
class Prediction:
    """One classification result plus its confidence signals."""

    raw: str                                   # model text, stripped
    code: Optional[str] = None                 # mapped label (via name_to_code), if any
    confidence: Dict[str, Optional[float]] = field(default_factory=dict)
    error: Optional[str] = None                # set when the call never landed

    @property
    def margin_first(self) -> Optional[float]:
        return self.confidence.get("margin_first")

    def gate(self, threshold: float, signal: str = "margin_first") -> str:
        """Return "auto" if the confidence signal clears the threshold, else "review".

        The spike's recommended operating point is margin_first >= 0.99
        (auto-post ~57% of rows at ~98.6% accuracy). Own/deterministic accounts
        should be resolved before reaching the model, not gated here.
        """
        value = self.confidence.get(signal)
        if value is None:
            return "review"
        return "auto" if value >= threshold else "review"


# --------------------------------------------------------------------------- #
# Client
# --------------------------------------------------------------------------- #

class LLMInferenceClient:
    """Thin OpenAI-compatible client for a vLLM box serving base + LoRA.

    Args:
        base_url: e.g. ``http://<pod-ip>:8000`` (no trailing ``/v1``).
        model: the vLLM model id (for multi-LoRA, the adapter name it was loaded under).
        api_key: optional bearer token; omit for an unauthenticated pod on a private network.
        timeout: per-request timeout in seconds.
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: Optional[str] = None,
        timeout: float = 60.0,
    ):
        if not HTTPX_AVAILABLE:
            raise RuntimeError("httpx is not installed. Run: pip install httpx")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.timeout = timeout

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def wait_ready(self, timeout: float = 300.0, interval: float = 3.0, heartbeat=None) -> bool:
        """Poll ``/v1/models`` until the server answers 200 or the timeout elapses.

        vLLM serves ``/v1/models`` only once the model is loaded, so this is the
        honest readiness probe for a pod that just booted. Pass ``heartbeat`` (e.g.
        ``activity.heartbeat``) — loading a 7B base can exceed a Temporal activity
        heartbeat timeout, so we ping each poll.
        """
        deadline = time.time() + timeout
        url = f"{self.base_url}/v1/models"
        while time.time() < deadline:
            if heartbeat:
                try:
                    heartbeat("waiting for vLLM /v1/models")
                except Exception:
                    pass
            try:
                with httpx.Client(timeout=10.0) as client:
                    r = client.get(url, headers=self._headers())
                    if r.status_code == 200:
                        return True
            except Exception:
                pass
            time.sleep(interval)
        return False

    def classify(
        self,
        prompt: str,
        name_to_code: Optional[Dict[str, str]] = None,
        max_tokens: int = 24,
        logprobs: int = 5,
        stop: Optional[List[str]] = None,
        retries: int = 4,
    ) -> Prediction:
        """Send one completion, capturing logprobs, and build a Prediction.

        ``prompt`` should already be the full alpaca-format string (use
        ``build_alpaca_prompt``). ``name_to_code`` maps the emitted name back to
        a code; omit it if the model emits the label directly.
        """
        stop = stop if stop is not None else ["\n", "###", "<|"]
        body = {
            "model": self.model,
            "prompt": prompt,
            "max_tokens": max_tokens,
            "temperature": 0,
            "logprobs": logprobs,
            "stop": stop,
        }
        url = f"{self.base_url}/v1/completions"

        last_err: Optional[str] = None
        for attempt in range(retries):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    r = client.post(url, json=body, headers=self._headers())
                    r.raise_for_status()
                    choice = r.json()["choices"][0]
                return prediction_from_choice(choice, name_to_code)
            except Exception as e:  # noqa: BLE001 - report, retry, then surface
                last_err = str(e)
                if attempt < retries - 1:
                    time.sleep(min(5.0, 1.0 + attempt * 0.5))

        logger.warning("classify failed after %d attempts: %s", retries, last_err)
        return Prediction(raw="", code=None, confidence={}, error=last_err)


# --------------------------------------------------------------------------- #
# Optional calibration (isotonic / Platt) — needs scikit-learn.
# --------------------------------------------------------------------------- #

class MarginCalibrator:
    """Turn a raw margin score into a calibrated probability + a precision dial.

    Isotonic regression by default (monotonic, preserves AUC), Platt (logistic)
    as an alternative. Optional: only import this if you have labelled
    predictions to fit on. Requires scikit-learn and numpy.
    """

    def __init__(self, method: str = "isotonic"):
        if method not in ("isotonic", "platt"):
            raise ValueError("method must be 'isotonic' or 'platt'")
        self.method = method
        self._model = None

    def fit(self, scores: List[float], correct: List[bool]) -> "MarginCalibrator":
        """Fit on margin scores and whether each prediction was correct."""
        try:
            import numpy as np
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("MarginCalibrator needs numpy + scikit-learn") from e

        x = np.asarray(scores, dtype=float)
        y = np.asarray([1 if c else 0 for c in correct], dtype=float)

        if self.method == "isotonic":
            from sklearn.isotonic import IsotonicRegression
            m = IsotonicRegression(out_of_bounds="clip")
            m.fit(x, y)
        else:
            from sklearn.linear_model import LogisticRegression
            m = LogisticRegression()
            m.fit(x.reshape(-1, 1), y)
        self._model = m
        return self

    def probability(self, score: float) -> float:
        """Calibrated P(correct) for a single margin score."""
        if self._model is None:
            raise RuntimeError("call fit() first")
        if self.method == "isotonic":
            return float(self._model.predict([score])[0])
        return float(self._model.predict_proba([[score]])[0][1])

    def threshold_for_precision(
        self, target: float, grid_step: float = 0.005
    ) -> float:
        """Smallest margin score whose calibrated probability >= target precision."""
        if self._model is None:
            raise RuntimeError("call fit() first")
        s = 0.0
        while s <= 1.0:
            if self.probability(s) >= target:
                return round(s, 4)
            s += grid_step
        return 1.0
