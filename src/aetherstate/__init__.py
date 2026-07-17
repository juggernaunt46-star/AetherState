"""AetherState — transparent OpenAI-compatible narrative state proxy. MIT, credit Bean."""
# The REAL version (pyproject.toml is the single source of truth). The old hardcoded
# "1.0.0" made /aether/status lie forever — the exact "is my proxy stale?" confusion this
# project keeps getting burned by (2026-07-09). A source checkout reads pyproject directly
# (editable installs freeze dist metadata at install time); a wheel install falls back to
# importlib metadata.
__version__ = "unknown"
try:
    import pathlib
    import re as _re
    _pp = pathlib.Path(__file__).resolve().parents[2] / "pyproject.toml"
    _m = _re.search(r'^version\s*=\s*"([^"]+)"', _pp.read_text(encoding="utf-8"),
                    _re.MULTILINE)
    if _m:
        __version__ = _m.group(1)
except Exception:
    pass
if __version__ == "unknown":
    try:
        from importlib.metadata import version as _pkg_version
        __version__ = _pkg_version("aetherstate")
    except Exception:
        pass
