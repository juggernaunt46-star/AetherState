"""CLI entry: aetherstate [--config path] [--host H] [--port P]"""
from __future__ import annotations

import argparse
from pathlib import Path


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
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.host:
        cfg.server.host = args.host          # CLI > env > file > defaults (12)
    if args.port:
        cfg.server.port = args.port
    Path(cfg.server.data_dir).mkdir(parents=True, exist_ok=True)

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

    uvicorn.run(create_app(cfg), host=cfg.server.host, port=cfg.server.port, log_level="info")


if __name__ == "__main__":
    main()
