# AetherState — local NLI contradiction shim

A tiny, self-contained **OpenAI-compatible** endpoint that backs AetherState's **L10
ledger-contradiction check** (`[assist.groups].linter_nli = "assist"`). It runs a small 3-way NLI
model locally (default `roberta-large-mnli`, CPU is fine) and answers one question per turn: *does
the narrator's prose contradict a committed ledger fact?* — firing on **contradiction**, staying
silent on new detail. It never touches your story model and needs no API key.

## Quick start

- **Windows:** run `setup-nli.bat` (double-click, or run it in a terminal).
- **Linux / macOS:** `bash setup-nli.sh`

The script creates a virtual environment, installs a CPU build of `torch` + `transformers`, and
starts the server on `http://127.0.0.1:8199`. The **first** start downloads the model (~1.4 GB) to
your HuggingFace cache; later starts are fast.

Manual equivalent (Windows):

```
python -m venv .venv
.venv\Scripts\python -m pip install torch transformers
.venv\Scripts\python server.py
```

On Linux/macOS install the CPU torch build with
`pip install torch --index-url https://download.pytorch.org/whl/cpu`.

## Point AetherState at it

In the **Console → Connection → Assist endpoints**, add an endpoint: name `nli-local`, URL
`http://127.0.0.1:8199/v1`, any model id, tier `small`. Then under **Assist routing** set
`linter_nli` → mode `assist` → endpoint `nli-local`. Equivalent `config.toml`:

```toml
[[assist.endpoints]]
name = "nli-local"
base_url = "http://127.0.0.1:8199/v1"
model = "roberta-large-mnli"
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
| `NLI_MODEL` | `roberta-large-mnli` | Any 3-way NLI (entailment/neutral/contradiction) sequence-classification model on HuggingFace. `roberta-*` use a BPE tokenizer, so no sentencepiece build is needed. Smaller options exist (e.g. `microsoft/deberta-base-mnli`). |
| `NLI_PORT` | `8199` | Port to serve on. |
| `NLI_FLOOR` | `0.45` | Minimum contradiction probability the shim returns. AetherState re-filters at `[linter].nli_threshold` (default `0.85`); a raw NLI model over-fires, so keep the AetherState threshold high. |

The shim requires a **shared subject word** between a fact and a prose claim before scoring them —
that precision guard is what stops it flagging unrelated sentences.

MIT — part of [AetherState](../README.md).
