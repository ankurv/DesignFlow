"""Authoritative DesignFlow release version."""

from __future__ import annotations

import re
from pathlib import Path


VERSION_FILE = Path(__file__).resolve().parents[1] / "VERSION"
VERSION_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


def read_version() -> str:
    version = VERSION_FILE.read_text(encoding="utf-8").strip()
    if not VERSION_PATTERN.fullmatch(version):
        raise RuntimeError(f"Invalid DesignFlow VERSION: {version!r}; expected x.y.z")
    return version


__version__ = read_version()
