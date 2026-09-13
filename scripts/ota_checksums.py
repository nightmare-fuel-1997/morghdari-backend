#!/usr/bin/env python3
"""M8 C5 — OTA artifact checksum generator (firmware-spec §2.6, topic T6).

Emits the exact `{version, url, checksum_sha256}` object the backend publishes
on the OTA topic and a sidecar manifest for the artifact store. Pure stdlib.

Usage:
  python scripts/ota_checksums.py firmware-v2.1.0.bin \\
      --version 2.1.0 --url https://mqtt.morghdari.com/ota/v2.1.0/firmware.bin

Prints the JSON and (with --out) writes the manifest next to the artifact.

فارسی: تولیدِ چکسامِ SHA-256 و مانیفستِ OTA برای بارگذاریِ فریم‌ویر.
"""

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def sha256_hex(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact", type=Path, help="path to the firmware .bin")
    parser.add_argument("--version", required=True, help="semver, e.g. 2.1.0")
    parser.add_argument("--url", required=True, help="public https URL of the artifact")
    parser.add_argument("--out", type=Path, default=None,
                        help="write {artifact}.json manifest next to the file")
    args = parser.parse_args(argv)

    if not args.artifact.is_file():
        print(f"error: artifact not found: {args.artifact}", file=sys.stderr)
        return 2

    checksum = sha256_hex(args.artifact)
    manifest = {
        "version": args.version,
        "url": args.url,
        "checksum_sha256": checksum,
        "size_bytes": args.artifact.stat().st_size,
        "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    print(json.dumps(manifest, indent=2))

    if args.out is not None:
        args.out.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        print(f"manifest written → {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())