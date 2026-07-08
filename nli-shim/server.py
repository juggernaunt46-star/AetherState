#!/usr/bin/env python3
"""Local NLI contradiction shim for AetherState's L10 ledger-contradiction check.

A tiny OpenAI-compatible /v1/chat/completions server backed by a 3-way NLI cross-encoder
(entailment / neutral / CONTRADICTION). AetherState's assist.nli_pass posts the premises
(committed ledger facts) and the hypotheses (prose claims) in one chat message; this shim
parses that message, runs the NLI model over the premise x hypothesis matrix, and replies with
ONLY the CONTRADICTIONS as the JSON assist.nli_pass expects:
    {"contradictions": [{"premise": <idx>, "quote": "<hypothesis>", "score": <prob>}]}

Fires on CONTRADICTION only (not neutral / unsupported) — exactly the 'constraint on fact,
freedom of fiction' semantic. Local + model-agnostic: runs on CPU or the 4070, never touches
the narrator backend. Default model roberta-large-mnli uses a BPE tokenizer (no sentencepiece
build headache on Python 3.13).

Run:  python server.py            # loads the model, serves on 127.0.0.1:8199
Env:  NLI_MODEL, NLI_PORT, NLI_FLOOR (min contradiction prob to return; default 0.45)
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:                                    # use the OS trust store when available — helps HuggingFace
    import truststore                   # downloads succeed behind TLS-intercepting proxies / AV.
    truststore.inject_into_ssl()        # Optional: `pip install truststore` if your model download
except Exception:                       # fails SSL verification; otherwise this is a harmless no-op.
    pass

import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer

MODEL = os.environ.get("NLI_MODEL", "roberta-large-mnli")
PORT = int(os.environ.get("NLI_PORT", "8199"))
FLOOR = float(os.environ.get("NLI_FLOOR", "0.45"))   # AetherState re-filters at nli_threshold
MAX_PAIRS_NO_PREFILTER = 80
_LOCK = threading.Lock()

print(f"[nli-shim] loading {MODEL} ...", file=sys.stderr, flush=True)
_TOK = AutoTokenizer.from_pretrained(MODEL)
_MODEL = AutoModelForSequenceClassification.from_pretrained(MODEL)
_MODEL.eval()
_DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
_MODEL.to(_DEVICE)
# read the CONTRADICTION class index from the model's OWN config (label order varies by model)
_ID2LABEL = {int(k): str(v).upper() for k, v in _MODEL.config.id2label.items()}
_CONTRA = next((i for i, lab in _ID2LABEL.items() if "CONTRADICT" in lab), 0)
print(f"[nli-shim] ready on {_DEVICE}; labels={_ID2LABEL}; contradiction idx={_CONTRA}",
      file=sys.stderr, flush=True)


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


@torch.no_grad()
def contradictions(premises, hypotheses):
    """Score the premise x hypothesis matrix; return one best CONTRADICTION hit per premise."""
    if not premises or not hypotheses:
        return []
    # A real contradiction is ABOUT the same subject. Require a shared non-stopword token before
    # scoring a pair — this is the precision lever that stops roberta over-firing on unrelated RP
    # prose ("Kael has 12 silver" vs "your boots scrape stone"). Live-calibrated 2026-07-08.
    ptok = [_tokens(p) for p in premises]
    htok = [_tokens(h) for h in hypotheses]
    pairs = [(i, j) for i in range(len(premises)) for j in range(len(hypotheses))
             if ptok[i] & htok[j]]
    if not pairs:
        return []
    enc = _TOK([premises[i] for i, _ in pairs], [hypotheses[j] for _, j in pairs],
               return_tensors="pt", padding=True, truncation=True, max_length=256).to(_DEVICE)
    probs = torch.softmax(_MODEL(**enc).logits, dim=-1)[:, _CONTRA].tolist()
    best: dict = {}
    for (i, j), p in zip(pairs, probs):
        if p >= FLOOR and p > best.get(i, (0.0, ""))[0]:
            best[i] = (p, hypotheses[j])
    return [{"premise": i, "quote": q[:160], "score": round(float(p), 3)}
            for i, (p, q) in sorted(best.items())]


def run_matrix(content: str):
    with _LOCK:
        prem, hyp = parse_user(content)
        hits = contradictions(prem, hyp)
    print(f"[nli-shim] {len(prem)}x{len(hyp)} pairs -> {len(hits)} contradiction(s)",
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
            self._json({"status": "ok", "model": MODEL, "device": _DEVICE})

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
    print(f"[nli-shim] serving on http://127.0.0.1:{PORT}/v1 (model={MODEL})",
          file=sys.stderr, flush=True)
    srv.serve_forever()
