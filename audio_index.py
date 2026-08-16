"""Thread-safe runtime index for immutable/bundled audio assets."""

import os
import threading
from pathlib import Path


class BundledAudioIndex:
    def __init__(self):
        self._lock = threading.RLock()
        self._signature = None
        self._core = frozenset()
        self._course = frozenset()
        self._build_count = 0

    @staticmethod
    def _root_signature(core_root, course_root):
        return (str(Path(core_root).resolve()), str(Path(course_root).resolve()))

    def _ensure(self, core_root, course_root):
        signature = self._root_signature(core_root, course_root)
        with self._lock:
            if self._signature == signature:
                return
            core_root = Path(core_root)
            course_root = Path(course_root)
            self._core = frozenset(
                entry.name.casefold() for entry in os.scandir(core_root)
                if entry.is_file(follow_symlinks=False)
            ) if core_root.is_dir() else frozenset()
            course_files = set()
            if course_root.is_dir():
                for directory, _subdirs, filenames in os.walk(course_root):
                    relative_dir = Path(directory).relative_to(course_root)
                    for filename in filenames:
                        course_files.add((relative_dir / filename).as_posix().casefold())
            self._course = frozenset(course_files)
            self._signature = signature
            self._build_count += 1

    def has_core(self, filename, core_root, course_root):
        self._ensure(core_root, course_root)
        return str(filename).casefold() in self._core

    def has_course(self, relative_path, core_root, course_root):
        self._ensure(core_root, course_root)
        normalized = str(relative_path).replace("\\", "/").lstrip("/").casefold()
        return normalized in self._course

    def count_course(self, relative_paths, core_root, course_root):
        self._ensure(core_root, course_root)
        return sum(
            1 for path in set(str(value).replace("\\", "/").lstrip("/").casefold() for value in relative_paths)
            if path in self._course
        )

    def invalidate(self):
        with self._lock:
            self._signature = None
            self._core = frozenset()
            self._course = frozenset()

    def diagnostics(self):
        with self._lock:
            return {
                "buildCount": self._build_count,
                "coreFiles": len(self._core),
                "courseFiles": len(self._course),
            }


BUNDLED_AUDIO_INDEX = BundledAudioIndex()
