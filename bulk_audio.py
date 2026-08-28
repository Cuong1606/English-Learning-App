"""Safe disk-backed staging for large ZIP/folder audio imports."""

import shutil
import tempfile
import threading
import uuid
import zipfile
from pathlib import Path, PurePosixPath


ALLOWED_AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".webm"}
MAX_AUDIO_FILE_BYTES = 50 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_FILE_COUNT = 10000


class BulkAudioSessions:
    def __init__(self):
        self._lock = threading.RLock()
        self._sessions = {}

    def create(self, base_dir, scope, target):
        base = Path(base_dir).resolve()
        base.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".bulk_audio_", dir=base)).resolve()
        token = uuid.uuid4().hex
        record = {"token": token, "scope": str(scope), "target": str(target), "stage": stage, "files": [], "matches": []}
        with self._lock:
            self._sessions[token] = record
        return record

    def get(self, token):
        with self._lock:
            record = self._sessions.get(str(token or ""))
        if not record:
            raise ValueError("Phiên Bulk Audio không tồn tại hoặc đã kết thúc")
        return record

    @staticmethod
    def _safe_name(name):
        name = Path(str(name or "").replace("\\", "/")).name
        if not name or Path(name).suffix.lower() not in ALLOWED_AUDIO_EXTENSIONS:
            raise ValueError("File audio không được hỗ trợ")
        return name[:180]

    def add_stream(self, token, name, source, size):
        record = self.get(token)
        name = self._safe_name(name)
        size = int(size)
        if size <= 0 or size > MAX_AUDIO_FILE_BYTES:
            raise ValueError(f"File audio vượt giới hạn 50 MB: {name}")
        if len(record["files"]) >= MAX_FILE_COUNT:
            raise ValueError("Mỗi lần Bulk Audio tối đa 10.000 file")
        total = sum(item["size"] for item in record["files"]) + size
        if total > MAX_TOTAL_BYTES:
            raise ValueError("Tổng audio vượt giới hạn 1 GB")
        destination = record["stage"] / f"{len(record['files']):05d}{Path(name).suffix.lower()}"
        remaining = size
        with destination.open("wb") as output:
            while remaining:
                chunk = source.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError(f"File audio tải lên bị thiếu dữ liệu: {name}")
                output.write(chunk)
                remaining -= len(chunk)
        record["files"].append({"name": name, "path": destination, "size": size})
        return {"ok": True, "uploaded": len(record["files"]), "bytes": total}

    def from_folder(self, base_dir, scope, target, selected_folder):
        root = Path(selected_folder).resolve(strict=True)
        if not root.is_dir():
            raise ValueError("Folder audio không hợp lệ")
        record = self.create(base_dir, scope, target)
        try:
            paths = [path for path in root.rglob("*") if path.is_file() and path.suffix.lower() in ALLOWED_AUDIO_EXTENSIONS]
            if len(paths) > MAX_FILE_COUNT:
                raise ValueError("Mỗi lần Bulk Audio tối đa 10.000 file")
            for path in paths:
                resolved = path.resolve(strict=True)
                if root not in resolved.parents:
                    raise ValueError("Đường dẫn audio vượt ra ngoài folder đã chọn")
                with resolved.open("rb") as source:
                    self.add_stream(record["token"], resolved.name, source, resolved.stat().st_size)
            if not record["files"]:
                raise ValueError("Folder không có file audio được hỗ trợ")
            return record
        except Exception:
            self.cleanup(record["token"])
            raise

    def from_zip(self, base_dir, scope, target, selected_zip):
        source_path = Path(selected_zip).resolve(strict=True)
        if not source_path.is_file() or source_path.suffix.lower() != ".zip":
            raise ValueError("ZIP audio không hợp lệ")
        size = source_path.stat().st_size
        if size <= 0 or size > MAX_TOTAL_BYTES:
            raise ValueError("ZIP audio vượt giới hạn 1 GB")
        record = self.create(base_dir, scope, target)
        try:
            archive_copy = record["stage"] / "source.zip"
            shutil.copyfile(source_path, archive_copy)
            self._extract_zip(record, archive_copy)
            return record
        except Exception:
            self.cleanup(record["token"])
            raise

    def from_uploaded_zip(self, base_dir, scope, target, source, size):
        size = int(size)
        if size <= 0 or size > MAX_TOTAL_BYTES:
            raise ValueError("ZIP audio vượt giới hạn 1 GB")
        record = self.create(base_dir, scope, target)
        try:
            archive_copy = record["stage"] / "source.zip"
            remaining = size
            with archive_copy.open("wb") as output:
                while remaining:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError("ZIP audio tải lên bị thiếu dữ liệu")
                    output.write(chunk)
                    remaining -= len(chunk)
            self._extract_zip(record, archive_copy)
            return record
        except Exception:
            self.cleanup(record["token"])
            raise

    def _extract_zip(self, record, archive_path):
        total = 0
        with zipfile.ZipFile(archive_path) as archive:
            infos = []
            for info in archive.infolist():
                if info.is_dir():
                    continue
                pure = PurePosixPath(info.filename.replace("\\", "/"))
                if pure.is_absolute() or ".." in pure.parts:
                    raise ValueError("ZIP audio chứa đường dẫn không an toàn")
                if pure.suffix.lower() not in ALLOWED_AUDIO_EXTENSIONS:
                    continue
                if info.file_size <= 0 or info.file_size > MAX_AUDIO_FILE_BYTES:
                    raise ValueError(f"File audio vượt giới hạn 50 MB: {pure.name}")
                total += info.file_size
                if total > MAX_TOTAL_BYTES:
                    raise ValueError("ZIP giải nén vượt giới hạn 1 GB")
                infos.append(info)
            if not infos:
                raise ValueError("ZIP không có file audio được hỗ trợ")
            if len(infos) > MAX_FILE_COUNT:
                raise ValueError("Mỗi lần Bulk Audio tối đa 10.000 file")
            for info in infos:
                with archive.open(info) as source:
                    self.add_stream(record["token"], PurePosixPath(info.filename).name, source, info.file_size)

    def cleanup(self, token):
        with self._lock:
            record = self._sessions.pop(str(token or ""), None)
        if record:
            shutil.rmtree(record["stage"], ignore_errors=True)

    def cleanup_orphans(self, base_dir):
        """Remove staging left by an earlier process without touching live sessions."""
        base = Path(base_dir).resolve()
        if not base.is_dir():
            return 0
        with self._lock:
            active = {Path(record["stage"]).resolve() for record in self._sessions.values()}
        removed = 0
        for entry in base.iterdir():
            if not entry.name.startswith(".bulk_audio_") or entry.resolve() in active:
                continue
            try:
                if entry.is_symlink() or getattr(entry, "is_junction", lambda: False)():
                    entry.unlink()
                elif entry.is_dir():
                    shutil.rmtree(entry)
                else:
                    entry.unlink()
                removed += 1
            except FileNotFoundError:
                continue
        return removed


BULK_AUDIO_SESSIONS = BulkAudioSessions()
