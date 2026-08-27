#!/usr/bin/env python3
"""Synchronize the workflow-factory mirror from the canonical Registry definition."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import shutil


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("canonical", type=Path)
    parser.add_argument("--destination", type=Path, default=Path("contracts/system-definition.json"))
    parser.add_argument("--checksum", type=Path, default=Path("contracts/system-definition.sha256"))
    args = parser.parse_args()

    if not args.canonical.is_file():
        parser.error(f"canonical definition does not exist: {args.canonical}")
    args.destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(args.canonical, args.destination)
    digest = hashlib.sha256(args.destination.read_bytes()).hexdigest()
    args.checksum.write_text(f"{digest}  system-definition.json\n", encoding="ascii")
    print(f"Synchronized system definition ({digest[:12]}...)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
