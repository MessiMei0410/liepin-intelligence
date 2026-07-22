#!/usr/bin/env python3
"""Export accepted Liepin reply assistant samples.

The browser extension stores samples in chrome.storage.local, backed by Chrome's
Local Extension Settings LevelDB. Page JavaScript cannot directly read that
extension storage, so this script reads the local LevelDB log/ldb files.

It does not click, type, or send anything in Liepin.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path


DEFAULT_PORT = 9223
DEFAULT_EXTENSION_ID = "aihpahceageafhjhedhmeikhcfbfoffn"
DEFAULT_PROFILE_DIR = Path.home() / ".hermes" / "chrome_profile_xhs" / "Default"
DEFAULT_OUTPUT_DIR = Path.home() / "Documents" / "Codex" / "2026-06-18" / "liepin-intelligence" / "outputs"
SAMPLE_KEY = b"liepinReplyAssistantAcceptedSamples"


def extension_storage_dir(profile_dir: Path, extension_id: str) -> Path:
    return profile_dir / "Local Extension Settings" / extension_id


def decode_arrays_from_bytes(data: bytes) -> list[list[dict]]:
    decoder = json.JSONDecoder()
    arrays: list[list[dict]] = []
    offset = 0
    while True:
        key_pos = data.find(SAMPLE_KEY, offset)
        if key_pos < 0:
            break
        json_start = data.find(b"[", key_pos + len(SAMPLE_KEY), key_pos + len(SAMPLE_KEY) + 200)
        offset = key_pos + len(SAMPLE_KEY)
        if json_start < 0:
            continue
        text = data[json_start:].decode("utf-8", errors="ignore")
        try:
            value, _ = decoder.raw_decode(text)
        except json.JSONDecodeError:
            continue
        if isinstance(value, list):
            arrays.append(value)
    return arrays


def newest_sample_time(samples: list[dict]) -> str:
    times = [str(item.get("acceptedAt", "")) for item in samples if item.get("acceptedAt")]
    return max(times) if times else ""


def read_samples_from_leveldb(storage_dir: Path) -> list[dict]:
    if not storage_dir.exists():
      raise SystemExit(f"没有找到插件本地存储目录: {storage_dir}")

    candidates: list[list[dict]] = []
    for path in storage_dir.iterdir():
        if path.suffix not in {".log", ".ldb"}:
            continue
        try:
            candidates.extend(decode_arrays_from_bytes(path.read_bytes()))
        except OSError:
            continue

    if not candidates:
        return []
    return max(candidates, key=lambda arr: (len(arr), newest_sample_time(arr)))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="保留参数，兼容旧用法；当前导出不依赖 CDP")
    parser.add_argument("--profile-dir", type=Path, default=DEFAULT_PROFILE_DIR)
    parser.add_argument("--extension-id", default=DEFAULT_EXTENSION_ID)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    samples = read_samples_from_leveldb(extension_storage_dir(args.profile_dir, args.extension_id))
    value = {
        "exportedAt": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "count": len(samples),
        "samples": samples,
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    output = args.output_dir / f"猎聘话术采纳样本_{ts}.json"
    output.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已导出 {value.get('count', 0)} 条样本: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
