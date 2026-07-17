# AetherState — local grounded fact-checker / NLI contradiction shim

A tiny, self-contained **OpenAI-compatible** endpoint that backs AetherState's **L10
ledger-contradiction check** (`[assist.groups].linter_nli = "assist"`). It runs a small grounding
model locally and answers one question per turn: *does the narrator's prose contradict a committed
ledger fact?* — firing on **contradiction**, staying silent on new detail. It never touches your
story model and needs no API key. Fail-open: any error returns an empty result, so AetherState
degrades to its deterministic rules floor.

## Pick a model (three backends)

Choose with `NLI_BACKEND`. `setup-nli.bat` / `setup-nli.sh` prompt for it, install the right deps,
save the choice to `selected-backend.txt`, and start the selected model.

| `NLI_BACKEND` | model | size · license | what it is |
|---|---|---|---|
| `factcg` **(default)** | `yaxili96/FactCG-DeBERTa-v3-Large` | 0.4B · MIT | Grounded fact-checker. Scores whether a claim is **supported** by the ledger slice; a low score on a tracked subject is the contradiction signal. Top sub-1B model on LLM-AggreFact (NAACL 2025). CPU or GPU. |
| `minicheck` | `lytang/MiniCheck-Flan-T5-Large` (via the `minicheck` package) | 770M · MIT | Same grounded-support semantic, GPT-4-level accuracy at ~400x lower cost (EMNLP 2024). |
| `nli` | `roberta-large-mnli` (override with `NLI_MODEL`) | 0.4B | Classic 3-way entailment/neutral/**contradiction** cross-encoder; scores P(contradiction) directly — a pure-contradiction (not support) semantic. The original shim path. |

**How the support backends stay precise.** `factcg` / `minicheck` check each prose claim against the
**whole ledger slice that shares a subject with it** (their intended *(document, claim)* contract), so
a claim grounded by *any* relevant fact stays silent, and only a genuinely unsupported claim about a
tracked fact fires — as a **soft** flag (L10 routes a corrective note, it never blocks). Because they
detect a slightly broader class than a 3-way NLI (*unsupported* ⊇ *contradicted*), keep
`[linter].nli_threshold` high (**0.85–0.9**) and raise `NLI_FLOOR` if they nag. The `nli` backend
gives the narrowest, pure-contradiction signal.

## Quick start

- **Windows:** run `setup-nli.bat` — pick a model; it makes the venv, installs deps, downloads the
  model, and starts the shim on `http://127.0.0.1:8199`.
- **Linux / macOS:** `bash setup-nli.sh`

The **first** start downloads the model to your HuggingFace cache; later starts are fast. Keep this
shim running in one terminal and start AetherState separately with the launcher in the repository
root. `selected-backend.txt` records the last choice for local reference.

Manual equivalent (Windows, FactCG default):

```
python -m venv .venv
.venv\Scripts\python -m pip install torch transformers sentencepiece
set NLI_BACKEND=factcg
.venv\Scripts\python server.py
```

MiniCheck instead: `pip install "minicheck @ git+https://github.com/Liyan06/MiniCheck.git@main" accelerate sentencepiece` and `set NLI_BACKEND=minicheck`.
On Linux/macOS install the CPU torch build with `pip install torch --index-url https://download.pytorch.org/whl/cpu`.

## Point AetherState at it

In the **Console → Connection → Assist endpoints**, add an endpoint: name `nli-local`, URL
`http://127.0.0.1:8199/v1`, any model id, tier `small`. Then under **Assist routing** set
`linter_nli` → mode `assist` → endpoint `nli-local`. Equivalent `config.toml`:

```toml
[[assist.endpoints]]
name = "nli-local"
base_url = "http://127.0.0.1:8199/v1"
model = "factcg"          # informational only — the shim serves whatever NLI_BACKEND selected
tier = "small"

[assist.groups]
linter_nli = "assist"

[assist.group_endpoints]
linter_nli = "nli-local"
```

Leave `linter_nli` at its default (`rules`) and nothing here runs — the check is entirely opt-in.

## Configuration (environment variables)

| var | default | meaning |
|---|---|---|
| `NLI_BACKEND` | `factcg` | `factcg` \| `minicheck` \| `nli` — which checker to load. |
| `NLI_MODEL` | per-backend | Override the HuggingFace id (used by `factcg` / `nli`; `minicheck` uses its package). |
| `NLI_PORT` | `8199` | Port to serve on. |
| `NLI_FLOOR` | `0.5` support / `0.45` nli | Minimum contradiction confidence the shim returns. AetherState re-filters at `[linter].nli_threshold` (default `0.85`); a raw checker over-fires, so keep the AetherState threshold high. |

The shim requires a **shared subject word** between a fact and a prose claim before scoring them —
that precision guard is what stops it flagging unrelated sentences.

MIT — part of [AetherState](../README.md).
