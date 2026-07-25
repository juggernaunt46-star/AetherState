from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "tools/run_bounded_gate.py"
WHEEL_SMOKE = ROOT / "tools/smoke_clean_wheel.py"
EVIDENCE_FIELDS = {
    "schema",
    "id",
    "status",
    "elapsed_seconds",
    "reason_code",
    "evidence_commit",
}


def test_process_control_tools_typecheck_against_linux_stubs(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "mypy",
            "--platform",
            "linux",
            "--cache-dir",
            str(tmp_path / "mypy-cache"),
            "--follow-imports=skip",
            "--ignore-missing-imports",
            str(RUNNER),
            str(WHEEL_SMOKE),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def _head(cwd: Path = ROOT) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _run(
    tmp_path: Path,
    *,
    gate_id: str = "manifest",
    timeout: int = 10,
    failure_reason: str = "manifest_failed",
    command: list[str] | None = None,
    derive_from: Path | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    evidence = tmp_path / f"{gate_id}.json"
    args = [
        sys.executable,
        str(RUNNER),
        "--gate-id",
        gate_id,
        "--evidence",
        str(evidence),
    ]
    if derive_from is not None:
        args.extend(["--derive-from", str(derive_from)])
    else:
        args.extend(
            [
                "--timeout-seconds",
                str(timeout),
                "--failure-reason",
                failure_reason,
            ]
        )
        if command is not None:
            args.extend(["--", *command])
    return (
        subprocess.run(args, cwd=ROOT, capture_output=True, text=True, check=False),
        evidence,
    )


def _load(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_success_writes_exact_content_free_pass_evidence_and_streams_output(
    tmp_path: Path,
) -> None:
    result, path = _run(
        tmp_path,
        command=[sys.executable, "-c", "print('bounded-output')"],
    )

    assert result.returncode == 0
    assert "bounded-output" in result.stdout
    evidence = _load(path)
    assert set(evidence) == EVIDENCE_FIELDS
    assert evidence["schema"] == "aetherstate-hardening-gate-evidence/1"
    assert evidence["id"] == "manifest"
    assert evidence["status"] == "PASS"
    assert evidence["reason_code"] == "command_passed"
    assert evidence["evidence_commit"] == _head()
    assert isinstance(evidence["elapsed_seconds"], float)
    assert 0.0 <= evidence["elapsed_seconds"] < 10.0
    assert evidence["elapsed_seconds"] == round(evidence["elapsed_seconds"], 3)
    serialized = path.read_text(encoding="utf-8").lower()
    for forbidden in (
        "command_line",
        "output",
        "exception",
        "traceback",
        "password",
        "credential",
    ):
        assert forbidden not in serialized
    assert str(ROOT).lower() not in serialized


def test_nonzero_writes_hold_with_the_supplied_stable_reason(tmp_path: Path) -> None:
    result, path = _run(
        tmp_path,
        command=[sys.executable, "-c", "raise SystemExit(23)"],
    )

    assert result.returncode != 0
    evidence = _load(path)
    assert set(evidence) == EVIDENCE_FIELDS
    assert evidence["status"] == "HOLD"
    assert evidence["reason_code"] == "manifest_failed"
    assert evidence["evidence_commit"] == _head()


def test_zero_timeout_never_spawns_and_needs_no_child_arguments(tmp_path: Path) -> None:
    marker = tmp_path / "must-not-exist"
    result, path = _run(
        tmp_path,
        gate_id="local-terminal-budget",
        timeout=0,
        failure_reason="terminal_serial_budget_exhausted",
        command=None,
    )

    assert result.returncode != 0
    assert not marker.exists()
    assert _load(path) == {
        "schema": "aetherstate-hardening-gate-evidence/1",
        "id": "local-terminal-budget",
        "status": "TEST_BUDGET_HOLD",
        "elapsed_seconds": 0.0,
        "reason_code": "terminal_serial_budget_exhausted",
        "evidence_commit": _head(),
    }


@pytest.mark.parametrize(
    ("timeout", "failure_reason"),
    [
        (1, "terminal_serial_budget_exhausted"),
        (0, "manifest_failed"),
    ],
)
def test_timeout_budget_and_hold_reasons_cannot_cross_statuses(
    tmp_path: Path,
    timeout: int,
    failure_reason: str,
) -> None:
    result, path = _run(
        tmp_path,
        timeout=timeout,
        failure_reason=failure_reason,
        command=[sys.executable, "-c", "raise SystemExit(7)"],
    )

    assert result.returncode != 0
    assert result.stdout.strip() == "INVALID reason_code"
    assert not path.exists()


def test_timeout_stops_resistant_grandchild_after_leader_exits_first(
    tmp_path: Path,
) -> None:
    sentinel = tmp_path / "grandchild-survived"
    ready = tmp_path / "grandchild-ready"
    leader_break = tmp_path / "leader-break"
    leader_exited = tmp_path / "leader-exited"
    grandchild_break = tmp_path / "grandchild-break"
    survived_leader_exit = tmp_path / "survived-leader-exit"
    grandchild = (
        "import pathlib,signal,time\n"
        "break_received=False\n"
        "def on_break(signum,frame):\n"
        " global break_received\n"
        " break_received=True\n"
        f" pathlib.Path({str(grandchild_break)!r}).write_text('break',encoding='utf-8')\n"
        "signal.signal(getattr(signal,'SIGBREAK',signal.SIGINT),on_break)\n"
        f"pathlib.Path({str(ready)!r}).write_text('ready',encoding='utf-8')\n"
        "deadline=time.monotonic()+10\n"
        "while not break_received and time.monotonic()<deadline: time.sleep(0.01)\n"
        "if break_received:\n"
        f" leader_exited=pathlib.Path({str(leader_exited)!r})\n"
        " deadline=time.monotonic()+1\n"
        " while not leader_exited.exists() and time.monotonic()<deadline: time.sleep(0.01)\n"
        " if leader_exited.exists():\n"
        f"  pathlib.Path({str(survived_leader_exit)!r}).write_text('survived',encoding='utf-8')\n"
        " time.sleep(1.5)\n"
        f" pathlib.Path({str(sentinel)!r}).write_text('survived',encoding='utf-8')\n"
    )
    child = (
        "import atexit,pathlib,signal,subprocess,sys,time\n"
        f"atexit.register(lambda:pathlib.Path({str(leader_exited)!r}).write_text('exited',encoding='utf-8'))\n"
        "def on_break(signum,frame):\n"
        f" pathlib.Path({str(leader_break)!r}).write_text('break',encoding='utf-8')\n"
        " raise SystemExit(0)\n"
        "signal.signal(getattr(signal,'SIGBREAK',signal.SIGINT),on_break)\n"
        f"subprocess.Popen([sys.executable,'-c',{grandchild!r}])\n"
        f"ready=pathlib.Path({str(ready)!r})\n"
        "deadline=time.monotonic()+10\n"
        "while not ready.exists() and time.monotonic()<deadline: time.sleep(0.01)\n"
        "while True: time.sleep(0.1)\n"
    )

    result, path = _run(
        tmp_path,
        timeout=2,
        command=[sys.executable, "-c", child],
    )

    assert result.returncode != 0
    assert leader_break.is_file()
    assert leader_exited.is_file()
    assert grandchild_break.is_file()
    assert survived_leader_exit.is_file()
    time.sleep(2.0)
    assert not sentinel.exists()
    evidence = _load(path)
    assert evidence["status"] == "TEST_BUDGET_HOLD"
    assert evidence["reason_code"] == "gate_timeout"
    assert evidence["elapsed_seconds"] >= 1.0
    assert set(evidence) == EVIDENCE_FIELDS


@pytest.mark.parametrize(
    ("target_gate", "source_gate"),
    [
        ("installer-linux", "linux-py310-full"),
        ("installer-windows", "windows-py312-full"),
        ("installer-windows", "local-windows-py310-full"),
    ],
)
def test_same_commit_direct_pass_derives_only_an_allowed_covered_gate(
    tmp_path: Path,
    target_gate: str,
    source_gate: str,
) -> None:
    source = tmp_path / f"{source_gate}.json"
    source.write_text(
        json.dumps(
            {
                "schema": "aetherstate-hardening-gate-evidence/1",
                "id": source_gate,
                "status": "PASS",
                "elapsed_seconds": 12.345,
                "reason_code": "command_passed",
                "evidence_commit": _head(),
            }
        ),
        encoding="utf-8",
    )

    result, path = _run(
        tmp_path,
        gate_id=target_gate,
        derive_from=source,
    )

    assert result.returncode == 0
    assert _load(path) == {
        "schema": "aetherstate-hardening-gate-evidence/1",
        "id": target_gate,
        "status": "PASS",
        "elapsed_seconds": 0.0,
        "reason_code": "covered_by_source_gate",
        "evidence_commit": _head(),
        "source_gate": source_gate,
    }


@pytest.mark.parametrize("source_status", ["HOLD", "TEST_BUDGET_HOLD", "INVALID"])
def test_derivation_never_promotes_non_pass_source(
    tmp_path: Path,
    source_status: str,
) -> None:
    source = tmp_path / "linux-py310-full.json"
    source.write_text(
        json.dumps(
            {
                "schema": "aetherstate-hardening-gate-evidence/1",
                "id": "linux-py310-full",
                "status": source_status,
                "elapsed_seconds": 1.0,
                "reason_code": "full_suite_failed",
                "evidence_commit": _head(),
            }
        ),
        encoding="utf-8",
    )

    result, path = _run(tmp_path, gate_id="installer-linux", derive_from=source)

    assert result.returncode != 0
    evidence = _load(path)
    assert evidence["status"] == "INVALID"
    assert evidence["reason_code"] == "source_gate_invalid"
    assert "source_gate" not in evidence


def test_derivation_rejects_a_different_commit(tmp_path: Path) -> None:
    parent = subprocess.run(
        ["git", "rev-parse", "HEAD^"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    source = tmp_path / "windows-py312-full.json"
    source.write_text(
        json.dumps(
            {
                "schema": "aetherstate-hardening-gate-evidence/1",
                "id": "windows-py312-full",
                "status": "PASS",
                "elapsed_seconds": 1.0,
                "reason_code": "command_passed",
                "evidence_commit": parent,
            }
        ),
        encoding="utf-8",
    )

    result, path = _run(tmp_path, gate_id="installer-windows", derive_from=source)

    assert result.returncode != 0
    assert _load(path)["reason_code"] == "source_gate_invalid"


@pytest.mark.parametrize(
    "source_gate",
    ["javascript", "local-terminal-budget", "stage-2-cumulative"],
)
def test_derivation_rejects_wrong_required_diagnostic_or_handoff_source(
    tmp_path: Path,
    source_gate: str,
) -> None:
    source = tmp_path / f"{source_gate}.json"
    source.write_text(
        json.dumps(
            {
                "schema": "aetherstate-hardening-gate-evidence/1",
                "id": source_gate,
                "status": "PASS",
                "elapsed_seconds": 1.0,
                "reason_code": "command_passed",
                "evidence_commit": _head(),
            }
        ),
        encoding="utf-8",
    )

    result, path = _run(tmp_path, gate_id="installer-linux", derive_from=source)

    assert result.returncode != 0
    assert _load(path)["status"] == "INVALID"
    assert _load(path)["reason_code"] == "source_gate_invalid"


def test_derivation_rejects_pass_with_timeout_reason(tmp_path: Path) -> None:
    source = tmp_path / "linux-py310-full.json"
    source.write_text(
        json.dumps(
            {
                "schema": "aetherstate-hardening-gate-evidence/1",
                "id": "linux-py310-full",
                "status": "PASS",
                "elapsed_seconds": 1.0,
                "reason_code": "gate_timeout",
                "evidence_commit": _head(),
            }
        ),
        encoding="utf-8",
    )

    result, path = _run(tmp_path, gate_id="installer-linux", derive_from=source)

    assert result.returncode != 0
    assert _load(path)["status"] == "INVALID"
    assert _load(path)["reason_code"] == "source_gate_invalid"


def test_derivation_rejects_non_finite_elapsed_seconds(tmp_path: Path) -> None:
    source = tmp_path / "linux-py310-full.json"
    source.write_text(
        json.dumps(
            {
                "schema": "aetherstate-hardening-gate-evidence/1",
                "id": "linux-py310-full",
                "status": "PASS",
                "elapsed_seconds": float("inf"),
                "reason_code": "command_passed",
                "evidence_commit": _head(),
            }
        ),
        encoding="utf-8",
    )

    result, path = _run(tmp_path, gate_id="installer-linux", derive_from=source)

    assert result.returncode != 0
    assert _load(path)["status"] == "INVALID"
    assert _load(path)["reason_code"] == "source_gate_invalid"


def test_stage_2_handoff_gate_and_reason_are_normal_runner_inputs(tmp_path: Path) -> None:
    result, path = _run(
        tmp_path,
        gate_id="stage-2-cumulative",
        failure_reason="stage_2_cumulative_failed",
        command=[sys.executable, "-c", "raise SystemExit(7)"],
    )

    assert result.returncode != 0
    evidence = _load(path)
    assert evidence["id"] == "stage-2-cumulative"
    assert evidence["status"] == "HOLD"
    assert evidence["reason_code"] == "stage_2_cumulative_failed"


def test_unknown_gate_or_free_form_reason_is_rejected_without_payload_leak(
    tmp_path: Path,
) -> None:
    result, path = _run(
        tmp_path,
        gate_id="made-up",
        failure_reason="arbitrary prose",
        command=[sys.executable, "-c", "print('secret payload')"],
    )

    assert result.returncode != 0
    assert not path.exists()
    combined = (result.stdout + result.stderr).strip()
    assert combined in {"INVALID gate_id", "INVALID reason_code"}
    assert "secret payload" not in combined
