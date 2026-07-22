"""Run one owned play-stack service while teeing its merged output to a bounded log."""
from __future__ import annotations

import argparse
import ctypes
import os
import subprocess
import sys
from pathlib import Path

_MAX_LOG_BYTES = 8 * 1024 * 1024
_CHUNK_BYTES = 64 * 1024


class _RollingLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.handle = None
        self.size = 0

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.parent.is_symlink() or self.path.is_symlink():
            raise RuntimeError("refusing symlinked process log")
        self.handle = self.path.open("w+b")
        self.size = 0

    def write(self, data: bytes) -> None:
        if self.handle is None:
            raise RuntimeError("process log is not open")
        if self.size + len(data) > _MAX_LOG_BYTES:
            keep = max(0, _MAX_LOG_BYTES - len(data))
            self.handle.seek(max(0, self.size - keep))
            retained = self.handle.read(keep) + data
            retained = retained[-_MAX_LOG_BYTES:]
            self.handle.seek(0)
            self.handle.truncate()
            self.handle.write(retained)
            self.handle.flush()
            self.size = len(retained)
            return
        self.handle.seek(0, os.SEEK_END)
        self.handle.write(data)
        self.handle.flush()
        self.size += len(data)

    def close(self) -> None:
        if self.handle is not None:
            self.handle.close()
            self.handle = None


def _set_console_title(title: str) -> None:
    if os.name == "nt":
        ctypes.windll.kernel32.SetConsoleTitleW(str(title))


def _write_console(data: bytes) -> None:
    stream = getattr(sys.stdout, "buffer", None)
    try:
        if stream is not None:
            stream.write(data)
            stream.flush()
        else:
            sys.stdout.write(data.decode("utf-8", errors="replace"))
            sys.stdout.flush()
    except OSError:
        # Logging remains authoritative if a console host is closed or detached.
        pass


def run(command: list[str], *, log_path: Path, title: str, service: str = "owned service") -> int:
    if not command:
        raise ValueError("owned process command is empty")
    _set_console_title(title)
    log = _RollingLog(log_path)
    log.open()
    process: subprocess.Popen[bytes] | None = None
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )
        if process.stdout is None:
            raise RuntimeError("owned process output pipe was not created")
        while True:
            chunk = process.stdout.read(_CHUNK_BYTES)
            if not chunk:
                break
            log.write(chunk)
            _write_console(chunk)
        return_code = int(process.wait())
        if return_code:
            notice = f"\n[playstack] {service} exited with code {return_code}\n".encode()
            log.write(notice)
            _write_console(notice)
        return return_code
    except KeyboardInterrupt:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
        return 130
    finally:
        log.close()


def _record_wrapper_failure(path: Path, message: bytes) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.parent.is_symlink() or path.is_symlink():
            return
        with path.open("ab") as handle:
            handle.write(message)
            handle.flush()
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tee one owned play-stack service")
    parser.add_argument("--log", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--service", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    log_path = Path(args.log).absolute()
    try:
        return run(command, log_path=log_path, title=args.title, service=args.service)
    except (OSError, RuntimeError, ValueError) as exc:
        message = (
            f"[playstack] {args.service} failed to start: {type(exc).__name__}\n".encode()
        )
        _record_wrapper_failure(log_path, message)
        _write_console(message)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
