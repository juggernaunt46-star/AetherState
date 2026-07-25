from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Sequence

from stage_gate_contract import (
    ALL_GATE_IDS,
    DERIVED_EVIDENCE_FIELDS,
    DIRECT_EVIDENCE_FIELDS,
    GATE_EVIDENCE_SCHEMA,
    STABLE_REASON_CODES,
)

OUTPUT_TAIL_LINES = 100
SIGNAL_GRACE_SECONDS = 1.0


def _head() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("git_unavailable") from exc


def _write_evidence(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _evidence(
    gate_id: str,
    status: str,
    elapsed_seconds: float,
    reason_code: str,
    evidence_commit: str,
    *,
    source_gate: str | None = None,
) -> dict[str, object]:
    value: dict[str, object] = {
        "schema": GATE_EVIDENCE_SCHEMA,
        "id": gate_id,
        "status": status,
        "elapsed_seconds": round(max(0.0, elapsed_seconds), 3),
        "reason_code": reason_code,
        "evidence_commit": evidence_commit,
    }
    if source_gate is not None:
        value["source_gate"] = source_gate
    return value


def _source_is_same_commit_pass(value: object, head: str) -> bool:
    if not isinstance(value, dict):
        return False
    fields = frozenset(value)
    if fields not in {DIRECT_EVIDENCE_FIELDS, DERIVED_EVIDENCE_FIELDS}:
        return False
    return (
        value.get("schema") == GATE_EVIDENCE_SCHEMA
        and value.get("id") in ALL_GATE_IDS
        and value.get("status") == "PASS"
        and isinstance(value.get("elapsed_seconds"), (int, float))
        and not isinstance(value.get("elapsed_seconds"), bool)
        and value.get("reason_code") in STABLE_REASON_CODES
        and value.get("evidence_commit") == head
        and (
            "source_gate" not in value
            or isinstance(value.get("source_gate"), str)
            and value.get("source_gate") in ALL_GATE_IDS
        )
    )


def _derive(gate_id: str, source: Path, evidence_path: Path, head: str) -> int:
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        value = None
    if not _source_is_same_commit_pass(value, head):
        _write_evidence(
            evidence_path,
            _evidence(
                gate_id,
                "INVALID",
                0.0,
                "source_gate_invalid",
                head,
            ),
        )
        print(f"INVALID {gate_id} source_gate_invalid")
        return 1
    assert isinstance(value, dict)
    source_gate = str(value["id"])
    _write_evidence(
        evidence_path,
        _evidence(
            gate_id,
            "PASS",
            0.0,
            "covered_by_source_gate",
            head,
            source_gate=source_gate,
        ),
    )
    print(f"PASS {gate_id}")
    return 0


def _reader(
    stream,
    tail: deque[str],
) -> None:
    try:
        for line in iter(stream.readline, ""):
            tail.append(line)
            print(line, end="", flush=True)
    finally:
        stream.close()


def _wait(process: subprocess.Popen[str], timeout: float) -> bool:
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    return True


def _stop_process_group(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT)
        except (OSError, ValueError):
            pass
        if _wait(process, SIGNAL_GRACE_SECONDS):
            return
        try:
            process.terminate()
        except OSError:
            pass
        if _wait(process, SIGNAL_GRACE_SECONDS):
            return
        try:
            process.kill()
        except OSError:
            pass
        _wait(process, SIGNAL_GRACE_SECONDS)
        return
    kill_group = getattr(os, "killpg")
    try:
        kill_group(process.pid, signal.SIGINT)
    except (OSError, ProcessLookupError):
        pass
    if _wait(process, SIGNAL_GRACE_SECONDS):
        return
    try:
        kill_group(process.pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass
    if _wait(process, SIGNAL_GRACE_SECONDS):
        return
    try:
        kill_group(process.pid, getattr(signal, "SIGKILL"))
    except (OSError, ProcessLookupError):
        pass
    _wait(process, SIGNAL_GRACE_SECONDS)


def _execute(
    gate_id: str,
    timeout_seconds: int,
    failure_reason: str,
    evidence_path: Path,
    command: Sequence[str],
    head: str,
) -> int:
    if timeout_seconds == 0:
        _write_evidence(
            evidence_path,
            _evidence(
                gate_id,
                "TEST_BUDGET_HOLD",
                0.0,
                "terminal_serial_budget_exhausted",
                head,
            ),
        )
        print(f"TEST_BUDGET_HOLD {gate_id} terminal_serial_budget_exhausted")
        return 1
    if not command:
        print("INVALID command")
        return 2
    started = time.monotonic()
    try:
        if os.name == "nt":
            process = subprocess.Popen(
                list(command),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=getattr(
                    subprocess,
                    "CREATE_NEW_PROCESS_GROUP",
                    0x00000200,
                ),
            )
        else:
            process = subprocess.Popen(
                list(command),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                start_new_session=True,
            )
    except OSError:
        _write_evidence(
            evidence_path,
            _evidence(gate_id, "HOLD", 0.0, failure_reason, head),
        )
        print(f"HOLD {gate_id} {failure_reason}")
        return 1
    assert process.stdout is not None
    tail: deque[str] = deque(maxlen=OUTPUT_TAIL_LINES)
    reader = threading.Thread(
        target=_reader,
        args=(process.stdout, tail),
        daemon=True,
    )
    reader.start()
    timed_out = not _wait(process, float(timeout_seconds))
    if timed_out:
        _stop_process_group(process)
    reader.join(timeout=SIGNAL_GRACE_SECONDS)
    elapsed = time.monotonic() - started
    if timed_out:
        status = "TEST_BUDGET_HOLD"
        reason = "gate_timeout"
    elif process.returncode == 0:
        status = "PASS"
        reason = "command_passed"
    else:
        status = "HOLD"
        reason = failure_reason
    _write_evidence(
        evidence_path,
        _evidence(gate_id, status, elapsed, reason, head),
    )
    print(f"{status} {gate_id} {reason}")
    return 0 if status == "PASS" else 1


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--gate-id", required=True)
    parser.add_argument("--timeout-seconds", type=int)
    parser.add_argument("--failure-reason")
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--derive-from", type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if args.gate_id not in ALL_GATE_IDS:
        print("INVALID gate_id")
        return 2
    if args.derive_from is None:
        if args.timeout_seconds is None or args.timeout_seconds < 0:
            print("INVALID timeout_seconds")
            return 2
        if args.failure_reason not in STABLE_REASON_CODES:
            print("INVALID reason_code")
            return 2
    elif (
        args.timeout_seconds is not None
        or args.failure_reason is not None
        or args.command
    ):
        print("INVALID derive_arguments")
        return 2
    try:
        head = _head()
        if args.derive_from is not None:
            return _derive(args.gate_id, args.derive_from, args.evidence, head)
        command = list(args.command)
        if command[:1] == ["--"]:
            command = command[1:]
        assert isinstance(args.timeout_seconds, int)
        assert isinstance(args.failure_reason, str)
        return _execute(
            args.gate_id,
            args.timeout_seconds,
            args.failure_reason,
            args.evidence,
            command,
            head,
        )
    except (OSError, RuntimeError):
        print("INVALID gate_runner_failed")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
