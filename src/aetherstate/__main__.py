"""CLI entry: aetherstate [--config path] [--host H] [--port P]"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Iterable


def _apply_runtime_overrides(
    cfg: Any,
    *,
    host: str | None = None,
    port: int | None = None,
    cors_origins: Iterable[str] = (),
    assist_endpoint_urls: Iterable[str] = (),
) -> None:
    """Apply process-local launcher overrides without rewriting the user's config file."""
    if host:
        cfg.server.host = host
    if port is not None:
        cfg.server.port = port

    origins = [str(origin).strip() for origin in cors_origins if str(origin).strip()]
    if origins:
        cfg.server.cors_origins = list(dict.fromkeys(origins))

    endpoints = {str(endpoint.name): endpoint for endpoint in cfg.assist.endpoints}
    for raw in assist_endpoint_urls:
        name, separator, url = str(raw).partition("=")
        name = name.strip()
        url = url.strip()
        if not separator or not name or not url:
            raise ValueError("--assist-endpoint-url must be NAME=URL")
        endpoint = endpoints.get(name)
        if endpoint is None:
            raise ValueError(f"configured assist endpoint not found: {name}")
        endpoint.base_url = url


def _configure_turn_trace_file(
    data_dir: str | Path,
    *,
    max_bytes: int = 16 * 1024 * 1024,
    backup_count: int = 3,
):
    """Append structured TURN_TRACE payloads to a bounded local JSONL history."""
    import logging
    import os
    from logging.handlers import RotatingFileHandler

    class _DurableRotatingFileHandler(RotatingFileHandler):
        def flush(self) -> None:
            super().flush()
            stream = self.stream
            if stream is None or stream.closed:
                return
            try:
                os.fsync(stream.fileno())
            except (OSError, ValueError):
                pass

    class _TurnTraceOnly(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            try:
                return record.getMessage().startswith("TURN_TRACE ")
            except Exception:
                return False

    class _TurnTraceJson(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            return record.getMessage().removeprefix("TURN_TRACE ")

    handler = _DurableRotatingFileHandler(
        Path(data_dir) / "turn-trace.jsonl",
        maxBytes=max(1, int(max_bytes)),
        backupCount=max(1, int(backup_count)),
        encoding="utf-8",
    )
    handler.setLevel(logging.INFO)
    handler.addFilter(_TurnTraceOnly())
    handler.setFormatter(_TurnTraceJson())
    logging.getLogger("aetherstate").addHandler(handler)
    return handler


def main() -> None:
    import os
    # AV suites (Avast et al.) set SSLKEYLOGFILE to a filter device Python cannot write;
    # httpx then fails to CREATE every client (PermissionError) and extraction/genesis/
    # assist all fail-open to empty. Drop it before anything imports httpx. (Handoff
    # 2026-07-04 runtime fix — verified live.)
    os.environ.pop("SSLKEYLOGFILE", None)
    try:
        import truststore                    # OS trust store (corp/AV MITM certs)
        truststore.inject_into_ssl()
    except Exception:
        pass                                 # optional dependency — fail open

    # Application loggers (aetherstate.*) propagate to the root logger, which has NO
    # handler by default — every genesis/extraction INFO line was silently dropped
    # (only WARNING+ leaked via logging's last-resort stderr handler). That is why
    # the 2026-07-04 session had zero visibility into Stage B. uvicorn configures
    # its own loggers with propagate=False, so this does not duplicate access logs.
    import logging
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    import uvicorn

    from .app import create_app
    from .config import load_config

    ap = argparse.ArgumentParser(prog="aetherstate")
    ap.add_argument("--config", default="./aetherstate-data/config.toml")
    ap.add_argument(
        "--config-read-only",
        action="store_true",
        help="load --config without backup refresh or Console persistence",
    )
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--cors-origin", action="append", default=[])
    ap.add_argument("--assist-endpoint-url", action="append", default=[])
    args = ap.parse_args()

    cfg = load_config(args.config, read_only=args.config_read_only)
    try:
        _apply_runtime_overrides(
            cfg,
            host=args.host,
            port=args.port,
            cors_origins=args.cors_origin,
            assist_endpoint_urls=args.assist_endpoint_url,
        )
    except ValueError as exc:
        ap.error(str(exc))
    Path(cfg.server.data_dir).mkdir(parents=True, exist_ok=True)

    # Constructing Config applies uvicorn's logging dictionary, which closes handlers
    # installed earlier in startup. Build it first, then attach AetherState's persistent
    # trace handler and polling filter so personal-play evidence survives the launch.
    server_config = uvicorn.Config(
        create_app(cfg),
        host=cfg.server.host,
        port=cfg.server.port,
        log_level="info",
    )
    if getattr(cfg.server, "turn_trace", False):
        _configure_turn_trace_file(
            cfg.server.data_dir,
            max_bytes=max(1, int(getattr(cfg.server, "turn_trace_max_mb", 16)))
            * 1024 * 1024,
            backup_count=max(1, int(getattr(cfg.server, "turn_trace_backups", 3))),
        )

    # 2026-07-09: the ST extension polls hud/status/writeback every few seconds — thousands
    # of identical access-log lines drowned every REAL event (a played session was ~95%
    # polling noise). Drop just those GET-200 lines; anything unusual (errors, POSTs, real
    # routes) still logs. [server].log_polling = true restores the raw firehose.
    if not getattr(cfg.server, "log_polling", False):
        _POLL = ("/hud", "/writeback", "/aether/status", "/aether/specialization")

        class _QuietPolls(logging.Filter):
            def filter(self, record: logging.LogRecord) -> bool:
                try:
                    msg = record.getMessage()
                except Exception:
                    return True
                if '"GET ' not in msg or " 200" not in msg:
                    return True
                return not any(p in msg for p in _POLL)

        logging.getLogger("uvicorn.access").addFilter(_QuietPolls())

    uvicorn.Server(server_config).run()


if __name__ == "__main__":
    main()
