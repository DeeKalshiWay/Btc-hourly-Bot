#!/usr/bin/env python3
"""Build dist/btc_hourly_learning_src.tar.gz from the current tree.

The archive contains source for the shadow learner and the paper files it
depends on. It does not contain config, calendars, databases, logs, or secrets.
"""
from __future__ import annotations

import hashlib
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREFIX = "btc_hourly_learning_src"
MEMBERS = (
    "INTEGRATION.md",
    "btc_hourly/learning.py",
    "btc_hourly/test_learning.py",
    "btc_hourly/broker.py",
    "btc_hourly/db.py",
    "btc_hourly/run.py",
)
OUT = ROOT / "dist" / "btc_hourly_learning_src.tar.gz"


def main() -> None:
    missing = [name for name in MEMBERS if not (ROOT / name).is_file()]
    if missing:
        raise SystemExit("missing bundle members: " + ", ".join(missing))
    lines = []
    OUT.parent.mkdir(parents=True, exist_ok=True)
    temporary = OUT.with_suffix(OUT.suffix + ".tmp")
    with tarfile.open(temporary, "w:gz", format=tarfile.GNU_FORMAT) as archive:
        for name in MEMBERS:
            path = ROOT / name
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            lines.append(f"{digest}  {name}")
            info = tarfile.TarInfo(f"{PREFIX}/{name}")
            data = path.read_bytes()
            info.size = len(data)
            info.mtime = 0
            info.mode = 0o755 if name.endswith("run.py") else 0o644
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            archive.addfile(info, fileobj=__import__("io").BytesIO(data))
        manifest = ("\n".join(lines) + "\n").encode()
        info = tarfile.TarInfo(f"{PREFIX}/SHA256SUMS")
        info.size = len(manifest)
        info.mtime = 0
        info.mode = 0o644
        archive.addfile(info, fileobj=__import__("io").BytesIO(manifest))
    temporary.replace(OUT)
    outer = hashlib.sha256(OUT.read_bytes()).hexdigest()
    (OUT.with_suffix(OUT.suffix + ".sha256")).write_text(f"{outer}  {OUT.name}\n")
    print(f"{outer}  {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
