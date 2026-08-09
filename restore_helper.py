#!/usr/bin/env python3
"""Out-of-process installer for a validated user-data restore.

The running app prepares and validates everything.  This module only swaps the
validated profile after the app process and all SQLite/WAL handles are gone.
"""
from __future__ import annotations

import argparse
import ctypes
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path


DB_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258
SYNCHRONIZE = 0x00100000
GENERIC_READ = 0x80000000
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class HelperAlreadyRunning(RuntimeError):
    pass


def _state_path(pending_file: Path) -> Path:
    return Path(pending_file).resolve().parent / "restore_state.json"


def _read_state(pending_file: Path) -> dict:
    path = _state_path(pending_file)
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        return state if isinstance(state, dict) else {"status": "prepared"}
    except (OSError, json.JSONDecodeError):
        return {"status": "prepared"}


def _write_state(pending_file: Path, status: str, **values) -> dict:
    path = _state_path(pending_file)
    state = _read_state(pending_file)
    state.update(values)
    state["status"] = status
    state["updatedAt"] = time.time()
    partial = path.with_name(path.name + ".partial")
    partial.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(partial, path)
    return state


class _HelperOwnership:
    """One OS-level lock for all restore helpers in this profile."""

    def __init__(self, pending_file: Path):
        restore_root = Path(pending_file).resolve().parent.parent
        restore_root.mkdir(parents=True, exist_ok=True)
        self.path = restore_root / "restore_helper.lock"
        self.stream = None

    def __enter__(self):
        self.stream = self.path.open("a+b")
        self.stream.seek(0, os.SEEK_END)
        if self.stream.tell() == 0:
            self.stream.write(b"0")
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.stream.close()
            self.stream = None
            raise HelperAlreadyRunning("Một restore helper khác đang xử lý session") from exc
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if self.stream is None:
            return
        try:
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        finally:
            self.stream.close()
            self.stream = None
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                # A newly-started recovery helper may already own the same lock.
                pass


def helper_is_active(pending_file: Path) -> bool:
    try:
        with _HelperOwnership(Path(pending_file)):
            return False
    except HelperAlreadyRunning:
        return True


def _inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _load_pending(pending_file: Path) -> dict:
    pending_file = pending_file.resolve()
    data = json.loads(pending_file.read_text(encoding="utf-8"))
    pending_dir = pending_file.parent
    required = (
        "incomingDb", "incomingAudio", "safetyDb", "safetyAudio",
        "targetDb", "targetAudio", "resultFile", "schemaVersion",
    )
    if not isinstance(data, dict) or any(key not in data for key in required):
        raise ValueError("Pending restore không đầy đủ")
    for key in ("incomingDb", "incomingAudio", "safetyDb", "safetyAudio"):
        if not _inside(Path(data[key]), pending_dir):
            raise ValueError(f"Đường dẫn {key} nằm ngoài staging")
    target_db = Path(data["targetDb"]).resolve()
    target_audio = Path(data["targetAudio"]).resolve()
    if target_db.parent.parent != target_audio.parent:
        raise ValueError("Đích user database/user_audio không cùng profile")
    if Path(data["resultFile"]).resolve().parent != target_audio.parent:
        raise ValueError("Đường dẫn kết quả restore không hợp lệ")
    return data


def _wait_for_pid_windows(pid: int, timeout: float) -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
    open_process.restype = ctypes.c_void_p
    handle = open_process(SYNCHRONIZE, False, int(pid))
    if not handle:
        return True
    try:
        wait_ms = max(0, min(int(timeout * 1000), 0xFFFFFFFE))
        result = kernel32.WaitForSingleObject(handle, wait_ms)
        if result == WAIT_OBJECT_0:
            return True
        if result == WAIT_TIMEOUT:
            return False
        raise OSError(ctypes.get_last_error(), "Không thể chờ process chính thoát")
    finally:
        kernel32.CloseHandle(handle)


def wait_for_process_exit(pid: int, timeout: float = 300.0) -> bool:
    if int(pid or 0) <= 0:
        return True
    if os.name == "nt":
        return _wait_for_pid_windows(int(pid), timeout)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    return False


def _windows_file_is_exclusive(path: Path) -> bool:
    if not path.exists():
        return True
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    handle = create_file(str(path), GENERIC_READ, 0, None, OPEN_EXISTING, 0, None)
    if handle == INVALID_HANDLE_VALUE:
        return False
    kernel32.CloseHandle(handle)
    return True


def _profile_files(target_db: Path, target_audio: Path) -> list[Path]:
    paths = [target_db, *(Path(str(target_db) + suffix) for suffix in DB_SIDECAR_SUFFIXES)]
    if target_audio.exists():
        paths.extend(path for path in target_audio.rglob("*") if path.is_file())
    return paths


def wait_for_profile_unlock(target_db: Path, target_audio: Path, timeout: float = 300.0) -> bool:
    if os.name != "nt":
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(_windows_file_is_exclusive(path) for path in _profile_files(target_db, target_audio)):
            return True
        time.sleep(0.15)
    return False


def _validate_installed_db(path: Path, schema_version: int) -> None:
    con = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True, timeout=20)
    try:
        integrity = con.execute("PRAGMA integrity_check").fetchone()
        if not integrity or str(integrity[0]).lower() != "ok":
            raise RuntimeError("Database sau swap không vượt qua integrity_check")
        actual = int(con.execute("PRAGMA user_version").fetchone()[0])
        if actual != int(schema_version):
            raise RuntimeError(f"Schema sau swap là {actual}, cần {schema_version}")
    finally:
        con.close()


def _clear_profile(target_db: Path, target_audio: Path) -> None:
    target_db.unlink(missing_ok=True)
    for suffix in DB_SIDECAR_SUFFIXES:
        Path(str(target_db) + suffix).unlink(missing_ok=True)
    if target_audio.exists():
        shutil.rmtree(target_audio)


def _copy_profile(source_db: Path, source_audio: Path, target_db: Path, target_audio: Path) -> None:
    target_db.parent.mkdir(parents=True, exist_ok=True)
    target_audio.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_db, target_db)
    shutil.copytree(source_audio, target_audio)


def _write_result(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    partial.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(partial, path)


def apply_pending_restore(
    pending_file: Path,
    wait_pid: int = 0,
    *,
    process_timeout: float = 300.0,
    lock_timeout: float = 300.0,
    after_swap=None,
) -> dict:
    """Apply one prepared restore; exposed separately for integration tests."""
    pending_file = Path(pending_file).resolve()
    pending_dir = pending_file.parent
    data = _load_pending(pending_file)
    previous_state = _read_state(pending_file)
    target_db = Path(data["targetDb"]).resolve()
    target_audio = Path(data["targetAudio"]).resolve()
    result_file = Path(data["resultFile"]).resolve()
    old_dir = pending_dir / "old_profile"
    old_db = old_dir / "learning.sqlite"
    old_audio = old_dir / "user_audio"
    rollback_ok = False
    success = False
    swap_started = False
    result = {}

    if previous_state.get("status") in ("success", "rolled_back", "untouched_failure"):
        result = previous_state.get("result")
        if not isinstance(result, dict):
            result = {
                "ok": previous_state.get("status") == "success",
                "message": "Đã hoàn tất recovery phiên khôi phục trước.",
                "completedAt": time.time(),
            }
        if not wait_for_process_exit(int(wait_pid or 0), process_timeout):
            return result
        _write_result(result_file, result)
        shutil.rmtree(pending_dir, ignore_errors=True)
        return result

    try:
        _write_state(pending_file, "waiting_process", helperPid=os.getpid(), waitPid=int(wait_pid or 0))
        if not wait_for_process_exit(int(wait_pid or 0), process_timeout):
            raise TimeoutError("Process chính chưa thoát sau thời gian chờ")
        _write_state(pending_file, "waiting_locks", helperPid=os.getpid())
        if not wait_for_profile_unlock(target_db, target_audio, lock_timeout):
            raise TimeoutError("SQLite/WAL vẫn đang được process khác sử dụng")

        swap_started = True
        _write_state(pending_file, "swapping", helperPid=os.getpid())
        if previous_state.get("status") in ("swapping", "rollback_failed"):
            raise RuntimeError("Restore helper trước đã dừng trong lúc swap; tự động rollback safety backup")
        old_dir.mkdir(parents=True, exist_ok=True)
        if target_db.exists():
            os.replace(target_db, old_db)
        for suffix in DB_SIDECAR_SUFFIXES:
            sidecar = Path(str(target_db) + suffix)
            if sidecar.exists():
                os.replace(sidecar, old_dir / ("learning.sqlite" + suffix))
        if target_audio.exists():
            os.replace(target_audio, old_audio)

        _copy_profile(
            Path(data["incomingDb"]), Path(data["incomingAudio"]), target_db, target_audio
        )
        _validate_installed_db(target_db, int(data["schemaVersion"]))
        if after_swap is not None:
            after_swap()
        success = True
        result = {
            "ok": True,
            "message": "Khôi phục dữ liệu thành công.",
            "restoredAppVersion": data.get("restoredAppVersion"),
            "completedAt": time.time(),
        }
        _write_state(pending_file, "success", result=result)
    except Exception as original_error:
        rollback_error = None
        if not swap_started:
            # Waiting failed before the first filesystem mutation. Never touch a
            # potentially locked profile; the original data is already intact.
            rollback_ok = True
        else:
            try:
                _clear_profile(target_db, target_audio)
                _copy_profile(
                    Path(data["safetyDb"]), Path(data["safetyAudio"]), target_db, target_audio
                )
                _validate_installed_db(target_db, int(data["schemaVersion"]))
                rollback_ok = True
            except Exception as exc:
                rollback_error = exc
                try:
                    _clear_profile(target_db, target_audio)
                    if old_db.exists():
                        os.replace(old_db, target_db)
                    for suffix in DB_SIDECAR_SUFFIXES:
                        old_sidecar = old_dir / ("learning.sqlite" + suffix)
                        if old_sidecar.exists():
                            os.replace(old_sidecar, Path(str(target_db) + suffix))
                    if old_audio.exists():
                        os.replace(old_audio, target_audio)
                    rollback_ok = target_db.exists()
                except Exception as fallback_error:
                    rollback_error = RuntimeError(f"{rollback_error}; fallback: {fallback_error}")
        result = {
            "ok": False,
            "message": "Khôi phục không thành công. Dữ liệu cũ đã được giữ nguyên."
            if rollback_ok else "Khôi phục và rollback đều thất bại. Staging được giữ để chẩn đoán.",
            "error": str(original_error),
            "rollbackOk": rollback_ok,
            "rollbackError": str(rollback_error) if rollback_error else None,
            "completedAt": time.time(),
        }
        _write_state(
            pending_file,
            "rolled_back" if swap_started and rollback_ok else "untouched_failure" if rollback_ok else "rollback_failed",
            result=result,
        )

    _write_result(result_file, result)
    if success or rollback_ok:
        shutil.rmtree(pending_dir, ignore_errors=True)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pending", required=True)
    parser.add_argument("--wait-pid", type=int, default=0)
    parser.add_argument("--process-timeout", type=float, default=300.0)
    parser.add_argument("--lock-timeout", type=float, default=300.0)
    args = parser.parse_args(argv)
    try:
        with _HelperOwnership(Path(args.pending)):
            result = apply_pending_restore(
                Path(args.pending),
                wait_pid=args.wait_pid,
                process_timeout=max(0.01, args.process_timeout),
                lock_timeout=max(0.01, args.lock_timeout),
            )
        return 0 if result.get("ok") else 2
    except HelperAlreadyRunning:
        return 4
    except Exception as exc:
        try:
            data = _load_pending(Path(args.pending))
            _write_result(Path(data["resultFile"]), {
                "ok": False, "message": "Không thể chạy restore helper.", "error": str(exc),
                "rollbackOk": False, "completedAt": time.time(),
            })
        except Exception:
            pass
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
