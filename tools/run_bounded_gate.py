from __future__ import annotations

import argparse
import ctypes
import json
import os
import signal
import subprocess
import threading
import time
from collections import deque
from ctypes import wintypes
from pathlib import Path
from typing import Any, Sequence

from stage_gate_contract import (
    ALL_GATE_IDS,
    DERIVATION_SOURCES,
    DIRECT_EVIDENCE_FIELDS,
    GATE_EVIDENCE_SCHEMA,
    GATE_STATUS_REASON_CODES,
    elapsed_seconds_is_valid,
)

OUTPUT_TAIL_LINES = 100
SIGNAL_GRACE_SECONDS = 1.0
POLL_SECONDS = 0.02

CREATE_SUSPENDED = 0x00000004
CREATE_NEW_PROCESS_GROUP = 0x00000200
TH32CS_SNAPTHREAD = 0x00000004
THREAD_SUSPEND_RESUME = 0x00000002
PROCESS_TERMINATE = 0x00000001
PROCESS_SET_QUOTA = 0x00000100
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _ThreadEntry32(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD),
        ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", wintypes.LONG),
        ("tpDeltaPri", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
    ]


def _windows_kernel32() -> Any:
    win_dll = getattr(ctypes, "WinDLL", None)
    if win_dll is None:
        raise RuntimeError("ctypes.WinDLL unavailable")
    kernel32 = win_dll("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [
        wintypes.HANDLE,
        wintypes.HANDLE,
    ]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Thread32First.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ThreadEntry32),
    ]
    kernel32.Thread32First.restype = wintypes.BOOL
    kernel32.Thread32Next.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ThreadEntry32),
    ]
    kernel32.Thread32Next.restype = wintypes.BOOL
    kernel32.OpenThread.argtypes = [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    kernel32.OpenThread.restype = wintypes.HANDLE
    kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel32.ResumeThread.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def _windows_error(operation: str) -> OSError:
    get_last_error = getattr(ctypes, "get_last_error", None)
    if get_last_error is None:
        return OSError(f"{operation}: ctypes.get_last_error unavailable")
    return OSError(get_last_error(), operation)


def _close_windows_handle(kernel32: Any, handle: int) -> None:
    if not kernel32.CloseHandle(handle):
        raise _windows_error("CloseHandle")


def _primary_thread_handle(kernel32: Any, pid: int) -> int:
    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if not snapshot or snapshot == INVALID_HANDLE_VALUE:
        raise _windows_error("CreateToolhelp32Snapshot")
    thread_ids: list[int] = []
    try:
        entry = _ThreadEntry32()
        entry.dwSize = ctypes.sizeof(_ThreadEntry32)
        found = bool(kernel32.Thread32First(snapshot, ctypes.byref(entry)))
        while found:
            if entry.th32OwnerProcessID == pid:
                thread_ids.append(int(entry.th32ThreadID))
            found = bool(kernel32.Thread32Next(snapshot, ctypes.byref(entry)))
    finally:
        _close_windows_handle(kernel32, int(snapshot))
    if len(thread_ids) != 1:
        raise OSError("suspended_primary_thread_not_unique")
    thread = kernel32.OpenThread(
        THREAD_SUSPEND_RESUME,
        False,
        thread_ids[0],
    )
    if not thread:
        raise _windows_error("OpenThread")
    return int(thread)


class _WindowsJob:
    def __init__(self) -> None:
        self._kernel32 = _windows_kernel32()
        self._handle: int | None = None
        handle = self._kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise _windows_error("CreateJobObjectW")
        self._handle = int(handle)
        limits = _JobObjectExtendedLimitInformation()
        limits.BasicLimitInformation.LimitFlags = (
            JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        )
        if not self._kernel32.SetInformationJobObject(
            self._handle,
            JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            error = _windows_error("SetInformationJobObject")
            try:
                self.close()
            except OSError:
                pass
            raise error

    def assign_and_resume(self, pid: int) -> None:
        if self._handle is None:
            raise OSError("job_closed")
        process = self._kernel32.OpenProcess(
            PROCESS_SET_QUOTA | PROCESS_TERMINATE,
            False,
            pid,
        )
        if not process:
            raise _windows_error("OpenProcess")
        try:
            if not self._kernel32.AssignProcessToJobObject(
                self._handle,
                process,
            ):
                raise _windows_error("AssignProcessToJobObject")
        finally:
            _close_windows_handle(self._kernel32, int(process))

        thread = _primary_thread_handle(self._kernel32, pid)
        try:
            previous_suspend_count = self._kernel32.ResumeThread(thread)
            if previous_suspend_count != 1:
                raise _windows_error("ResumeThread")
        finally:
            _close_windows_handle(self._kernel32, thread)

    def close(self) -> None:
        handle = self._handle
        if handle is None:
            return
        _close_windows_handle(self._kernel32, handle)
        self._handle = None


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


def _source_is_same_commit_pass(
    target_gate: str,
    value: object,
    head: str,
) -> bool:
    if not isinstance(value, dict):
        return False
    fields = frozenset(value)
    if fields != DIRECT_EVIDENCE_FIELDS:
        return False
    elapsed = value.get("elapsed_seconds")
    return (
        value.get("schema") == GATE_EVIDENCE_SCHEMA
        and value.get("id") in DERIVATION_SOURCES.get(target_gate, frozenset())
        and value.get("status") == "PASS"
        and elapsed_seconds_is_valid(elapsed)
        and value.get("reason_code") == "command_passed"
        and value.get("evidence_commit") == head
    )


def _derive(gate_id: str, source: Path, evidence_path: Path, head: str) -> int:
    try:
        value = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        value = None
    if not _source_is_same_commit_pass(gate_id, value, head):
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


def _wait_full_grace(process: subprocess.Popen[str], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        process.poll()
        time.sleep(min(POLL_SECONDS, max(0.0, deadline - time.monotonic())))


def _posix_group_exists(process_group: int) -> bool:
    kill_group = getattr(os, "killpg")
    try:
        kill_group(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_posix_group_gone(
    process: subprocess.Popen[str],
    process_group: int,
    timeout: float,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        process.poll()
        if not _posix_group_exists(process_group):
            return True
        time.sleep(min(POLL_SECONDS, max(0.0, deadline - time.monotonic())))
    process.poll()
    return not _posix_group_exists(process_group)


def _stop_process_group(
    process: subprocess.Popen[str],
    windows_job: _WindowsJob | None,
) -> None:
    if os.name == "nt":
        if windows_job is None:
            raise RuntimeError("windows_job_missing")
        try:
            ctrl_break_event = getattr(signal, "CTRL_BREAK_EVENT", None)
            if ctrl_break_event is None:
                raise RuntimeError("CTRL_BREAK_EVENT unavailable")
            process.send_signal(ctrl_break_event)
        except (OSError, ValueError):
            pass
        _wait_full_grace(process, SIGNAL_GRACE_SECONDS)
        windows_job.close()
        _wait(process, SIGNAL_GRACE_SECONDS)
        return
    process_group = process.pid
    kill_group = getattr(os, "killpg")
    for group_signal in (
        signal.SIGINT,
        signal.SIGTERM,
        getattr(signal, "SIGKILL"),
    ):
        if _posix_group_exists(process_group):
            try:
                kill_group(process_group, group_signal)
            except ProcessLookupError:
                pass
        if _wait_posix_group_gone(
            process,
            process_group,
            SIGNAL_GRACE_SECONDS,
        ):
            _wait(process, SIGNAL_GRACE_SECONDS)
            return
    _wait(process, SIGNAL_GRACE_SECONDS)
    if _posix_group_exists(process_group):
        raise RuntimeError("process_group_survived")


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
    windows_job: _WindowsJob | None = None
    process: subprocess.Popen[str] | None = None
    try:
        if os.name == "nt":
            windows_job = _WindowsJob()
            process = subprocess.Popen(
                list(command),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=CREATE_NEW_PROCESS_GROUP | CREATE_SUSPENDED,
            )
            windows_job.assign_and_resume(process.pid)
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
        cleanup_failed = False
        if process is not None:
            try:
                process.kill()
            except OSError:
                cleanup_failed = True
        if windows_job is not None:
            try:
                windows_job.close()
            except OSError:
                cleanup_failed = True
        if process is not None and not _wait(process, SIGNAL_GRACE_SECONDS):
            cleanup_failed = True
        if process is not None and process.stdout is not None:
            process.stdout.close()
        if cleanup_failed:
            raise RuntimeError("containment_setup_cleanup_failed")
        _write_evidence(
            evidence_path,
            _evidence(gate_id, "HOLD", 0.0, failure_reason, head),
        )
        print(f"HOLD {gate_id} {failure_reason}")
        return 1
    assert process is not None
    assert process.stdout is not None
    tail: deque[str] = deque(maxlen=OUTPUT_TAIL_LINES)
    reader = threading.Thread(
        target=_reader,
        args=(process.stdout, tail),
        daemon=True,
    )
    reader.start()
    try:
        timed_out = not _wait(process, float(timeout_seconds))
        if timed_out:
            _stop_process_group(process, windows_job)
        elif windows_job is not None:
            windows_job.close()
    finally:
        if windows_job is not None:
            windows_job.close()
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
        failure_reason_is_valid = (
            args.failure_reason == "terminal_serial_budget_exhausted"
            if args.timeout_seconds == 0
            else args.failure_reason in GATE_STATUS_REASON_CODES["HOLD"]
        )
        if not failure_reason_is_valid:
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
