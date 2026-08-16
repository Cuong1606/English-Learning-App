#!/usr/bin/env python3
"""Count the audio assets present in this checkout/build without hard-coded totals."""

import argparse
import json
from pathlib import Path


SUPPORTED = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".webm"}


def count_audio(root):
    root = Path(root)
    return sum(1 for path in root.rglob("*") if path.is_file() and path.suffix.lower() in SUPPORTED) if root.is_dir() else 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    core = count_audio(args.root / "audio")
    courses = count_audio(args.root / "course_audio")
    result = {"core_audio": core, "course_audio": courses, "bundled_audio": core + courses}
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"core audio: {core:,}")
        print(f"course audio: {courses:,}")
        print(f"bundled audio: {core + courses:,}")


if __name__ == "__main__":
    main()
