"""Portable, validated ZIP packages for individual My Islands."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


PACKAGE_TYPE = "english-learning-app-island"
FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"
ISLAND_NAME = "island.json"
SUPPORTED_AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".webm"}

MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024
MAX_MEMBER_COUNT = 10002
MAX_ITEM_COUNT = 10000
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_ISLAND_JSON_BYTES = 20 * 1024 * 1024
MAX_AUDIO_FILE_BYTES = 25 * 1024 * 1024
MAX_COMPRESSION_RATIO = 500


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while True:
            chunk = source.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8")


def _safe_audio_archive_name(name):
    pure = PurePosixPath(str(name or ""))
    return bool(
        len(pure.parts) == 2
        and pure.parts[0] == "audio"
        and pure.parts[1] not in ("", ".", "..")
        and re.fullmatch(r"custom-\d{6}\.(mp3|wav|m4a|aac|ogg|webm)", pure.parts[1], re.I)
    )


def suggested_export_filename(name):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", str(name or "").strip())
    value = value.rstrip(" .") or "My Island"
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if value.upper() in reserved:
        value += " Island"
    return f"{value[:100].rstrip(' .')}.island.zip"


def write_package(output_path, island, audio_files, app_version):
    """Write a complete package to a sibling partial file, then replace output."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    island_raw = _json_bytes(island)
    if len(island_raw) > MAX_ISLAND_JSON_BYTES:
        raise ValueError("Nội dung Island vượt giới hạn an toàn 20 MB")
    files = {
        ISLAND_NAME: {
            "size": len(island_raw),
            "sha256": hashlib.sha256(island_raw).hexdigest(),
        }
    }
    normalized_audio = []
    total_size = len(island_raw)
    for archive_name, source_path in audio_files:
        archive_name = str(archive_name)
        source_path = Path(source_path)
        if not _safe_audio_archive_name(archive_name):
            raise ValueError(f"Tên audio trong package không an toàn: {archive_name}")
        if archive_name in files:
            raise ValueError(f"Audio bị trùng trong package: {archive_name}")
        if not source_path.is_file():
            raise ValueError(f"Không tìm thấy custom audio cần export: {source_path.name}")
        size = source_path.stat().st_size
        if size <= 0 or size > MAX_AUDIO_FILE_BYTES:
            raise ValueError(f"Custom audio vượt giới hạn 25 MB: {source_path.name}")
        total_size += size
        if total_size > MAX_UNCOMPRESSED_BYTES:
            raise ValueError("Island giải nén vượt giới hạn an toàn 1 GB")
        files[archive_name] = {"size": size, "sha256": _sha256_file(source_path)}
        normalized_audio.append((archive_name, source_path))
    if len(files) + 1 > MAX_MEMBER_COUNT:
        raise ValueError("Island có quá nhiều file")
    manifest = {
        "packageType": PACKAGE_TYPE,
        "formatVersion": FORMAT_VERSION,
        "appVersion": str(app_version),
        "createdAtUtc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "islandFile": ISLAND_NAME,
        "itemCount": len(island.get("items") or []),
        "audioFileCount": len(normalized_audio),
        "files": files,
    }
    manifest_raw = _json_bytes(manifest)
    if len(manifest_raw) > MAX_MANIFEST_BYTES:
        raise ValueError("Manifest Island vượt giới hạn 1 MB")
    fd, partial_name = tempfile.mkstemp(
        prefix=f".{output_path.name}.", suffix=".partial", dir=output_path.parent
    )
    os.close(fd)
    partial = Path(partial_name)
    try:
        with zipfile.ZipFile(partial, "w", allowZip64=True) as archive:
            archive.writestr(MANIFEST_NAME, manifest_raw, compress_type=zipfile.ZIP_DEFLATED)
            # Store structured content without compression so a highly repetitive but
            # valid Island cannot look like a ZIP bomb when imported elsewhere.
            archive.writestr(ISLAND_NAME, island_raw, compress_type=zipfile.ZIP_STORED)
            for archive_name, source_path in normalized_audio:
                archive.write(source_path, archive_name, compress_type=zipfile.ZIP_STORED)
        if partial.stat().st_size > MAX_ARCHIVE_BYTES:
            raise ValueError("File Island vượt giới hạn an toàn 512 MB")
        os.replace(partial, output_path)
        return {
            "ok": True,
            "path": str(output_path),
            "filename": output_path.name,
            "size": output_path.stat().st_size,
            "manifest": manifest,
        }
    finally:
        partial.unlink(missing_ok=True)


def _required_string(value, label, maximum, allow_empty=False):
    if not isinstance(value, str):
        raise ValueError(f"{label} phải là chuỗi")
    if not allow_empty and not value.strip():
        raise ValueError(f"{label} không được trống")
    if len(value) > maximum:
        raise ValueError(f"{label} quá dài")
    return value


def _content_object(value, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label} không hợp lệ")
    allowed = {"enUs", "viVn", "usageNote", "literalNote", "audioKey", "audioExpected", "note"}
    if not set(value) <= allowed:
        raise ValueError(f"{label} chứa trường không được hỗ trợ")
    content = {
        "enUs": _required_string(value.get("enUs"), f"{label}.enUs", 1000),
        "viVn": _required_string(value.get("viVn", ""), f"{label}.viVn", 1500, True),
        "usageNote": _required_string(value.get("usageNote", ""), f"{label}.usageNote", 1000, True),
        "literalNote": _required_string(value.get("literalNote", ""), f"{label}.literalNote", 1000, True),
        "audioKey": _required_string(value.get("audioKey", ""), f"{label}.audioKey", 180, True),
        "audioExpected": _required_string(value.get("audioExpected", ""), f"{label}.audioExpected", 180, True),
        "note": _required_string(value.get("note", ""), f"{label}.note", 1000, True),
    }
    return content


def _validated_island(value, available_files):
    if not isinstance(value, dict) or set(value) != {"name", "description", "items"}:
        raise ValueError("island.json không đúng cấu trúc")
    name = _required_string(value.get("name"), "Tên Island", 120).strip()
    description = _required_string(value.get("description"), "Mô tả Island", 500, True)
    items = value.get("items")
    if not isinstance(items, list):
        raise ValueError("Danh sách nội dung Island không hợp lệ")
    if len(items) > MAX_ITEM_COUNT:
        raise ValueError("Mỗi Island tối đa 10.000 câu")
    validated = []
    custom_refs = set()
    referenced_audio = set()
    for index, item in enumerate(items, 1):
        if not isinstance(item, dict):
            raise ValueError(f"Câu #{index} không hợp lệ")
        kind = item.get("kind")
        if kind == "canonical":
            if set(item) != {"kind", "contentId", "content"}:
                raise ValueError(f"Câu canonical #{index} không đúng cấu trúc")
            content_id = item.get("contentId")
            if isinstance(content_id, bool) or not isinstance(content_id, int) or content_id <= 0:
                raise ValueError(f"contentId của câu #{index} không hợp lệ")
            validated.append({"kind": kind, "contentId": content_id, "content": _content_object(item.get("content"), f"Câu #{index}")})
        elif kind == "custom":
            if set(item) != {"kind", "ref", "content", "audio"}:
                raise ValueError(f"Câu custom #{index} không đúng cấu trúc")
            ref = item.get("ref")
            if not isinstance(ref, str) or not re.fullmatch(r"custom-\d{6}", ref):
                raise ValueError(f"ref của câu #{index} không hợp lệ")
            if ref in custom_refs:
                raise ValueError(f"ref custom bị trùng: {ref}")
            custom_refs.add(ref)
            audio = item.get("audio")
            if audio is not None:
                if not isinstance(audio, str) or not _safe_audio_archive_name(audio):
                    raise ValueError(f"Audio của câu #{index} không hợp lệ")
                if audio not in available_files:
                    raise ValueError(f"Package thiếu custom audio: {audio}")
                if audio in referenced_audio:
                    raise ValueError(f"Custom audio bị tham chiếu trùng: {audio}")
                referenced_audio.add(audio)
            validated.append({"kind": kind, "ref": ref, "content": _content_object(item.get("content"), f"Câu #{index}"), "audio": audio})
        else:
            raise ValueError(f"Loại câu #{index} không được hỗ trợ")
    packaged_audio = {name for name in available_files if name.startswith("audio/")}
    if referenced_audio != packaged_audio:
        raise ValueError("Danh sách custom audio không khớp nội dung Island")
    return {"name": name, "description": description, "items": validated}


def _safe_member_name(info):
    name = str(info.filename or "")
    if not name or "\\" in name or name.startswith("/"):
        raise ValueError(f"Đường dẫn trong ZIP Island không hợp lệ: {name or '(trống)'}")
    pure = PurePosixPath(name)
    if pure.is_absolute() or any(part in ("", ".", "..") for part in pure.parts):
        raise ValueError(f"Phát hiện ZIP path traversal: {name}")
    if name not in {MANIFEST_NAME, ISLAND_NAME} and not _safe_audio_archive_name(name):
        raise ValueError(f"File không được hỗ trợ trong ZIP Island: {name}")
    unix_mode = (info.external_attr >> 16) & 0o170000
    if unix_mode == 0o120000:
        raise ValueError(f"ZIP Island không được chứa symbolic link: {name}")
    if info.flag_bits & 0x1:
        raise ValueError("ZIP Island được mã hóa không được hỗ trợ")
    return name


def _manifest_meta(meta, name):
    if not isinstance(meta, dict) or set(meta) != {"size", "sha256"}:
        raise ValueError(f"Manifest không hợp lệ cho {name}")
    size = meta.get("size")
    digest = meta.get("sha256")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ValueError(f"Kích thước trong manifest không hợp lệ: {name}")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
        raise ValueError(f"Checksum trong manifest không hợp lệ: {name}")
    return size, digest.lower()


def extract_and_validate(archive_path, stage):
    archive_path = Path(archive_path)
    size = archive_path.stat().st_size
    if size <= 0:
        raise ValueError("File Island trống")
    if size > MAX_ARCHIVE_BYTES:
        raise ValueError("File Island vượt giới hạn an toàn 512 MB")
    stage = Path(stage)
    extracted = stage / "extracted"
    extracted.mkdir(parents=True, exist_ok=True)
    try:
        archive = zipfile.ZipFile(archive_path, "r")
    except (zipfile.BadZipFile, OSError) as exc:
        raise ValueError("File Island không phải ZIP hợp lệ") from exc
    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_MEMBER_COUNT:
            raise ValueError("File Island chứa quá nhiều thành phần")
        names = set()
        folded = set()
        total_size = 0
        total_compressed = 0
        for info in infos:
            if info.is_dir():
                raise ValueError("ZIP Island không được chứa directory entry rời")
            name = _safe_member_name(info)
            if name in names or name.casefold() in folded:
                raise ValueError(f"ZIP Island chứa tên file trùng hoặc xung đột: {name}")
            names.add(name)
            folded.add(name.casefold())
            member_size = int(info.file_size)
            compressed_size = int(info.compress_size)
            if member_size < 0 or compressed_size < 0:
                raise ValueError(f"Kích thước ZIP không hợp lệ: {name}")
            if name == MANIFEST_NAME and member_size > MAX_MANIFEST_BYTES:
                raise ValueError("Manifest Island vượt giới hạn 1 MB")
            if name == ISLAND_NAME and member_size > MAX_ISLAND_JSON_BYTES:
                raise ValueError("Nội dung Island vượt giới hạn 20 MB")
            if name.startswith("audio/") and (member_size <= 0 or member_size > MAX_AUDIO_FILE_BYTES):
                raise ValueError(f"Custom audio vượt giới hạn 25 MB: {name}")
            total_size += member_size
            total_compressed += compressed_size
            if total_size > MAX_UNCOMPRESSED_BYTES:
                raise ValueError("Island giải nén vượt giới hạn an toàn 1 GB")
            if member_size > 1024 * 1024 and member_size > max(1, compressed_size) * MAX_COMPRESSION_RATIO:
                raise ValueError(f"Tỷ lệ nén bất thường, có thể là ZIP bomb: {name}")
        if total_size > max(1, total_compressed) * MAX_COMPRESSION_RATIO:
            raise ValueError("Tỷ lệ nén tổng thể bất thường, có thể là ZIP bomb")
        if shutil.disk_usage(stage).free < total_size + 64 * 1024 * 1024:
            raise ValueError("Không đủ dung lượng trống để kiểm tra file Island an toàn")
        if {MANIFEST_NAME, ISLAND_NAME} - names:
            raise ValueError("Package thiếu manifest.json hoặc island.json")
        try:
            manifest = json.loads(archive.read(MANIFEST_NAME).decode("utf-8"))
        except Exception as exc:
            raise ValueError("Manifest Island không phải JSON UTF-8 hợp lệ") from exc
        required_manifest = {
            "packageType", "formatVersion", "appVersion", "createdAtUtc", "islandFile",
            "itemCount", "audioFileCount", "files",
        }
        if not isinstance(manifest, dict) or set(manifest) != required_manifest:
            raise ValueError("Manifest Island không đúng cấu trúc")
        if manifest.get("packageType") != PACKAGE_TYPE:
            raise ValueError("File ZIP không phải My Island package")
        version = manifest.get("formatVersion")
        if isinstance(version, bool) or not isinstance(version, int) or version != FORMAT_VERSION:
            raise ValueError(f"Island format {version} không được hỗ trợ; ứng dụng hỗ trợ format {FORMAT_VERSION}")
        if not isinstance(manifest.get("appVersion"), str) or not manifest["appVersion"]:
            raise ValueError("Manifest Island thiếu appVersion")
        if not isinstance(manifest.get("createdAtUtc"), str) or not manifest["createdAtUtc"]:
            raise ValueError("Manifest Island thiếu thời điểm tạo")
        if manifest.get("islandFile") != ISLAND_NAME:
            raise ValueError("Manifest Island trỏ tới nội dung không được hỗ trợ")
        files = manifest.get("files")
        if not isinstance(files, dict) or set(files) != names - {MANIFEST_NAME}:
            raise ValueError("Danh sách file trong manifest không khớp ZIP Island")
        for info in infos:
            if info.filename == MANIFEST_NAME:
                continue
            target = extracted.joinpath(*PurePosixPath(info.filename).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as source, target.open("wb") as destination:
                shutil.copyfileobj(source, destination, 1024 * 1024)
            expected_size, expected_hash = _manifest_meta(files.get(info.filename), info.filename)
            if target.stat().st_size != expected_size:
                raise ValueError(f"Sai kích thước file Island: {info.filename}")
            if _sha256_file(target) != expected_hash:
                raise ValueError(f"Checksum không khớp: {info.filename}")
        try:
            island = json.loads((extracted / ISLAND_NAME).read_text(encoding="utf-8"))
        except Exception as exc:
            raise ValueError("island.json không phải JSON UTF-8 hợp lệ") from exc
        island = _validated_island(island, set(files))
        item_count = manifest.get("itemCount")
        audio_count = manifest.get("audioFileCount")
        if isinstance(item_count, bool) or not isinstance(item_count, int) or item_count != len(island["items"]):
            raise ValueError("Số câu trong manifest không khớp nội dung Island")
        actual_audio_count = sum(1 for name in files if name.startswith("audio/"))
        if isinstance(audio_count, bool) or not isinstance(audio_count, int) or audio_count != actual_audio_count:
            raise ValueError("Số audio trong manifest không khớp nội dung Island")
        return {"manifest": manifest, "island": island, "extracted": extracted}


class IslandImportSessions:
    def __init__(self):
        self._lock = threading.RLock()
        self._sessions = {}

    def _create(self, base_dir):
        base = Path(base_dir).resolve()
        base.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".island_import_", dir=base)).resolve()
        token = uuid.uuid4().hex
        record = {"token": token, "stage": stage}
        with self._lock:
            self._sessions[token] = record
        return record

    def get(self, token):
        with self._lock:
            record = self._sessions.get(str(token or ""))
        if not record:
            raise ValueError("Phiên Import Island không tồn tại hoặc đã kết thúc")
        return record

    def from_path(self, base_dir, selected_path):
        source = Path(selected_path).resolve(strict=True)
        if not source.is_file() or not source.name.lower().endswith(".island.zip"):
            raise ValueError("Hãy chọn file .island.zip hợp lệ")
        size = source.stat().st_size
        if size <= 0 or size > MAX_ARCHIVE_BYTES:
            raise ValueError("File Island trống hoặc vượt giới hạn 512 MB")
        record = self._create(base_dir)
        try:
            incoming = record["stage"] / "incoming.island.zip"
            shutil.copyfile(source, incoming)
            record.update(extract_and_validate(incoming, record["stage"]))
            return record
        except Exception:
            self.cleanup(record["token"])
            raise

    def from_stream(self, base_dir, source, size):
        try:
            size = int(size)
        except (TypeError, ValueError) as exc:
            raise ValueError("Kích thước file Island không hợp lệ") from exc
        if size <= 0 or size > MAX_ARCHIVE_BYTES:
            raise ValueError("File Island trống hoặc vượt giới hạn 512 MB")
        record = self._create(base_dir)
        try:
            incoming = record["stage"] / "incoming.island.zip"
            remaining = size
            with incoming.open("wb") as destination:
                while remaining:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError("File Island tải lên bị thiếu dữ liệu")
                    destination.write(chunk)
                    remaining -= len(chunk)
            record.update(extract_and_validate(incoming, record["stage"]))
            return record
        except Exception:
            self.cleanup(record["token"])
            raise

    def cleanup(self, token):
        with self._lock:
            record = self._sessions.pop(str(token or ""), None)
        if record:
            shutil.rmtree(record["stage"], ignore_errors=True)

    def cleanup_orphans(self, base_dir):
        base = Path(base_dir).resolve()
        if not base.is_dir():
            return 0
        with self._lock:
            active = {Path(record["stage"]).resolve() for record in self._sessions.values()}
        removed = 0
        for entry in base.iterdir():
            if not entry.name.startswith(".island_import_") or entry.resolve() in active:
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
                pass
        return removed


ISLAND_IMPORT_SESSIONS = IslandImportSessions()
