#!/usr/bin/env python3
"""Local grounded-factuality / NLI contradiction shim for AetherState's L10 ledger check.

A tiny OpenAI-compatible /v1/chat/completions server that scores whether the narrator's prose
CONTRADICTS a committed ledger fact, and replies with ONLY the contradictions in the shape
AetherState's assist.nli_pass expects:
    {"contradictions": [{"premise": <idx>, "quote": "<hypothesis>", "score": <prob>}]}

Three selectable backends (env NLI_BACKEND):
  * factcg    - FactCG-DeBERTa-v3-Large (0.4B, MIT, NAACL 2025, yaxili96/FactCG-DeBERTa-v3-Large).
                A grounded-factuality checker: it scores P(SUPPORTED) for a (document, claim) pair.
                A LOW support score on a subject the ledger already tracks is the contradiction
                signal (contradiction confidence = 1 - support). Top sub-1B model on the
                LLM-AggreFact leaderboard. **Bean's default.**
  * minicheck - MiniCheck-Flan-T5-Large (770M, MIT, EMNLP 2024) via the `minicheck` package.
                Same support semantic; GPT-4-level fact-checking at ~400x lower cost.
  * nli       - roberta-large-mnli (or any 3-way MNLI checkpoint). The classic
                entailment/neutral/CONTRADICTION cross-encoder; scores P(contradiction) directly.
                The original shim path, kept for a pure-contradiction (not support) semantic.

Support backends (factcg / minicheck) check each prose claim against the WHOLE ledger slice that
shares a subject with it (the model's intended (document, claim) contract), so a claim grounded by
ANY relevant fact stays SILENT and only a genuinely unsupported claim about a tracked fact fires -
and it fires as a SOFT flag (AetherState's L10 routes a corrective note, never blocks). The nli
backend scores the premise x hypothesis matrix pairwise, exactly as the original shim did.

Local + model-agnostic: runs on CPU or the GPU, never touches the narrator backend, needs no API
key. Fail-open: ANY error returns an EMPTY contradiction list, so a missing or broken judge
degrades silently to AetherState's rules floor (invariant 1).

Run:  python server.py               # loads the chosen model, serves on 127.0.0.1:8199
Env:  NLI_BACKEND  factcg | minicheck | nli      (default: factcg)
      NLI_MODEL    override the HuggingFace id (factcg / nli backends)
      NLI_PORT     port to serve on               (default: 8199)
      NLI_FLOOR    min contradiction confidence the shim returns
                   (default: 0.5 for support backends, 0.45 for nli). AetherState re-filters at
                   [linter].nli_threshold (default 0.85) - keep that high; a raw checker over-fires.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))

try:                                    # use the OS trust store when available - helps HuggingFace
    import truststore                   # downloads succeed behind TLS-intercepting proxies / AV.
    truststore.inject_into_ssl()        # Optional: `pip install truststore` if your model download
except Exception:                       # fails SSL verification; otherwise this is a harmless no-op.
    pass

import torch  # noqa: E402 - truststore must be injected before model imports
from transformers.utils import logging as _hf_logging  # noqa: E402 - same import ordering contract

# Some sequence-classification checkpoints (e.g. roberta-large-mnli) carry a `pooler.*` the head
# never uses, and transformers prints an alarming "UNEXPECTED" load table about it on every start -
# 100% benign here. Quiet the load-time report (and the HF-hub 'unauthenticated' notice) so the
# console isn't scary; genuine load failures still raise.
_hf_logging.set_verbosity_error()
logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

# ----------------------------------------------------------------------------- config / selection
BACKEND = os.environ.get("NLI_BACKEND", "factcg").strip().lower()
_DEFAULT_MODEL = {
    "factcg": "yaxili96/FactCG-DeBERTa-v3-Large",
    "minicheck": "lytang/MiniCheck-Flan-T5-Large",
    "nli": "roberta-large-mnli",
}
MODEL = os.environ.get("NLI_MODEL", _DEFAULT_MODEL.get(BACKEND, _DEFAULT_MODEL["factcg"]))
PORT = int(os.environ.get("NLI_PORT", "8199"))
_DEFAULT_FLOOR = {"factcg": 0.5, "minicheck": 0.5, "nli": 0.45}
FLOOR = float(os.environ.get("NLI_FLOOR", _DEFAULT_FLOOR.get(BACKEND, 0.5)))
MAX_PAIRS = 96                          # hard cap on scored pairs per turn (runaway guard)
_LOCK = threading.Lock()


# FactCG renders each (document, claim) into a single instruction-templated string and reads
# softmax index 1 as P(SUPPORTED) — the official inference contract (github.com/derenlei/FactCG,
# factcg/inference.py + utils.py). Feeding the pair as two segments (the generic NLI way) yields
# garbage, so this backend follows FactCG's template exactly.
_FACTCG_TEMPLATE = ('{text_a}\n\nChoose your answer: based on the paragraph above can we conclude '
                    'that "{text_b}"?\n\nOPTIONS:\n- Yes\n- No\nI think the answer is ')


class _HFSupportBackend:
    """FactCG-DeBERTa-v3-Large grounded-factuality checker (0.4B, MIT). Scores P(supported) for a
    (document, claim) pair via FactCG's own instruction template + softmax index 1."""

    mode = "support"

    def __init__(self, model_id: str):
        from transformers import (AutoConfig, AutoModelForSequenceClassification,
                                  AutoTokenizer)
        cache = os.path.join(_HERE, "cache")
        cfg = AutoConfig.from_pretrained(model_id, num_labels=2,
                                         finetuning_task="text-classification", cache_dir=cache)
        cfg.problem_type = "single_label_classification"
        self.tok = AutoTokenizer.from_pretrained(model_id, use_fast=True, cache_dir=cache)
        if self.tok.pad_token is None:                  # match FactCG's inferencer
            self.tok.pad_token = self.tok.eos_token
        self.model = AutoModelForSequenceClassification.from_pretrained(
            model_id, config=cfg, ignore_mismatched_sizes=False, cache_dir=cache)
        self.model.eval()
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model.to(self.device)

    @torch.no_grad()
    def support(self, docs: list, claims: list) -> list:
        out: list = []
        for k in range(0, len(docs), 16):
            texts = [_FACTCG_TEMPLATE.format(text_a=d, text_b=c)
                     for d, c in zip(docs[k:k + 16], claims[k:k + 16])]
            enc = self.tok(texts, return_tensors="pt", padding="longest",
                           truncation="only_first", max_length=1024).to(self.device)
            probs = torch.softmax(self.model(**enc).logits, dim=-1)[:, 1]   # index 1 = SUPPORTED
            out.extend(float(p) for p in probs.tolist())
        return out


class _MiniCheckBackend:
    """MiniCheck-Flan-T5-Large via the official `minicheck` package. Returns P(SUPPORTED) as
    raw_prob; the package owns chunking, the prompt format, and device placement."""

    mode = "support"
    device = "auto (minicheck)"

    def __init__(self):
        from minicheck.minicheck import MiniCheck
        self.scorer = MiniCheck(model_name="flan-t5-large",
                                cache_dir=os.path.join(_HERE, "ckpts"))

    def support(self, docs: list, claims: list) -> list:
        _label, raw_prob, _a, _b = self.scorer.score(docs=docs, claims=claims)
        return [float(p) for p in raw_prob]


class _NLIBackend:
    """Classic 3-way MNLI cross-encoder (entailment / neutral / CONTRADICTION). Scores
    P(contradiction) directly; the contradiction index is read from the model's own config."""

    mode = "nli"

    def __init__(self, model_id: str):
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
        cache = os.path.join(_HERE, "cache")
        self.tok = AutoTokenizer.from_pretrained(model_id, cache_dir=cache)
        self.model = AutoModelForSequenceClassification.from_pretrained(model_id, cache_dir=cache)
        self.model.eval()
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model.to(self.device)
        id2label = {int(k): str(v).upper() for k, v in self.model.config.id2label.items()}
        self.contra_idx = next((i for i, lab in id2label.items() if "CONTRADICT" in lab), 0)

    @torch.no_grad()
    def contradiction(self, prems: list, hyps: list) -> list:
        out: list = []
        for k in range(0, len(prems), 32):
            enc = self.tok(prems[k:k + 32], hyps[k:k + 32], return_tensors="pt",
                           padding=True, truncation=True, max_length=256).to(self.device)
            probs = torch.softmax(self.model(**enc).logits, dim=-1)[:, self.contra_idx]
            out.extend(float(p) for p in probs.tolist())
        return out


def _load_backend():
    if BACKEND == "minicheck":
        return _MiniCheckBackend()
    if BACKEND == "nli":
        return _NLIBackend(MODEL)
    return _HFSupportBackend(MODEL)      # factcg (default)


print(f"[nli-shim] backend={BACKEND} loading {MODEL} ...", file=sys.stderr, flush=True)
_BACKEND = _load_backend()
_MODE = _BACKEND.mode
print(f"[nli-shim] ready on {getattr(_BACKEND, 'device', '?')}; backend={BACKEND} mode={_MODE}; "
      f"model={MODEL}; floor={FLOOR}", file=sys.stderr, flush=True)


# ------------------------------------------------------------------------------- subject overlap
# common words that must NOT count as a shared SUBJECT (else unrelated lines "overlap")
_STOP = {"your", "yours", "with", "this", "that", "they", "them", "then", "there", "here",
         "have", "from", "into", "onto", "over", "under", "back", "down", "away", "toward",
         "towards", "take", "takes", "took", "taken", "come", "comes", "came", "goes", "went",
         "gone", "look", "looks", "looked", "like", "still", "just", "only", "some", "your",
         "something", "nothing", "around", "against", "before", "after", "while", "where",
         "when", "what", "which", "would", "could", "should", "been", "being", "game", "master",
         "narrator", "turn", "scene", "moment", "thing", "things", "hand", "hands", "eyes"}


def _tokens(s: str) -> set:
    return {w for w in re.findall(r"[a-z0-9']{4,}", s.lower()) if w not in _STOP}


def parse_user(content: str):
    """Recover (premises, hypotheses) from assist.nli_pass's fixed message layout:
    'Established facts:\\n0: ...\\n1: ...\\n\\nPassage claims:\\n- ...\\n- ...'."""
    head, _, tail = content.partition("Passage claims:")
    numbered = sorted((int(m.group(1)), m.group(2).strip())
                      for m in re.finditer(r"^\s*(\d+):\s*(.+)$", head, re.MULTILINE))
    prem = [p for _, p in numbered]
    hyp = [m.group(1).strip() for m in re.finditer(r"^\s*-\s*(.+)$", tail, re.MULTILINE)]
    return prem, hyp


def _support_contradictions(premises, hypotheses, ptok, htok):
    """Support backends: check each claim against the WHOLE ledger slice that shares a subject
    with it (the (document, claim) contract). A claim grounded by any relevant fact scores high
    and stays silent; only a claim UNSUPPORTED by everything known about that subject fires.
    contradiction confidence = 1 - P(supported); the hit keys to the most-overlapping premise."""
    docs, claims, keys = [], [], []
    for j, h in enumerate(hypotheses):
        overlap = [i for i in range(len(premises)) if ptok[i] & htok[j]]
        if not overlap:
            continue
        doc = " ".join(premises[i] for i in overlap)
        best_i = max(overlap, key=lambda i: len(ptok[i] & htok[j]))
        docs.append(doc)
        claims.append(h)
        keys.append((best_i, h))
        if len(docs) >= MAX_PAIRS:
            break
    if not docs:
        return []
    support = _BACKEND.support(docs, claims)
    best: dict = {}
    for (i, q), s in zip(keys, support):
        contra = 1.0 - float(s)
        if contra >= FLOOR and contra > best.get(i, (0.0, ""))[0]:
            best[i] = (contra, q)
    return [{"premise": i, "quote": q[:160], "score": round(float(p), 3)}
            for i, (p, q) in sorted(best.items())]


def _nli_contradictions(premises, hypotheses, ptok, htok):
    """3-way NLI: score the premise x hypothesis matrix pairwise, keep one best CONTRADICTION per
    premise. A pair is only scored when it shares a non-stopword subject token (the precision
    lever that stops the model over-firing on unrelated RP prose). Live-calibrated 2026-07-08."""
    pairs = [(i, j) for i in range(len(premises)) for j in range(len(hypotheses))
             if ptok[i] & htok[j]][:MAX_PAIRS]
    if not pairs:
        return []
    probs = _BACKEND.contradiction([premises[i] for i, _ in pairs],
                                   [hypotheses[j] for _, j in pairs])
    best: dict = {}
    for (i, j), p in zip(pairs, probs):
        if p >= FLOOR and p > best.get(i, (0.0, ""))[0]:
            best[i] = (p, hypotheses[j])
    return [{"premise": i, "quote": q[:160], "score": round(float(p), 3)}
            for i, (p, q) in sorted(best.items())]


def contradictions(premises, hypotheses):
    """Return one best CONTRADICTION hit per premise (support or nli backend, chosen at load)."""
    if not premises or not hypotheses:
        return []
    ptok = [_tokens(p) for p in premises]
    htok = [_tokens(h) for h in hypotheses]
    if _MODE == "support":
        return _support_contradictions(premises, hypotheses, ptok, htok)
    return _nli_contradictions(premises, hypotheses, ptok, htok)


def run_matrix(content: str):
    with _LOCK:
        prem, hyp = parse_user(content)
        hits = contradictions(prem, hyp)
    print(f"[nli-shim] {len(prem)}x{len(hyp)} claims -> {len(hits)} contradiction(s)",
          file=sys.stderr, flush=True)
    return hits


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):                      # silence default access logging
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):   # resolve_endpoint model detection
            self._json({"object": "list", "data": [{"id": MODEL, "object": "model"}]})
        else:
            self._json({"status": "ok", "backend": BACKEND, "model": MODEL,
                        "device": getattr(_BACKEND, "device", "?")})

    def do_POST(self):
        n = int(self.headers.get("content-length", 0) or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            req = json.loads(raw or b"{}")
            msgs = req.get("messages") or []
            user = next((m.get("content", "") for m in reversed(msgs)
                         if m.get("role") == "user"), "")
            hits = run_matrix(user)
        except Exception as exc:                        # fail-open: empty contradictions
            print(f"[nli-shim] error: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            hits = []
        content = json.dumps({"contradictions": hits})
        self._json({"id": "nli-shim", "object": "chat.completion", "model": MODEL,
                    "choices": [{"index": 0, "finish_reason": "stop",
                                 "message": {"role": "assistant", "content": content}}]})


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[nli-shim] serving on http://127.0.0.1:{PORT}/v1 "
          f"(backend={BACKEND}, model={MODEL})", file=sys.stderr, flush=True)
    srv.serve_forever()
