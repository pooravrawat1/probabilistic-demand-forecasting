"""Download the source without modifying its bytes."""

from __future__ import annotations

import hashlib
from pathlib import Path
from urllib.request import urlopen
import zipfile


def download_source(url: str, destination: Path) -> dict[str, str | int | bool]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if not zipfile.is_zipfile(destination):
            raise ValueError(f"Existing source is not a ZIP archive: {destination}")
        return {"bytes": destination.stat().st_size, "sha256": _sha256(destination), "already_present": True}
    partial = destination.with_name(destination.name + ".part")
    digest = hashlib.sha256()
    byte_count = 0
    try:
        with urlopen(url, timeout=60) as response, partial.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
                byte_count += len(chunk)
        if not zipfile.is_zipfile(partial):
            raise ValueError("Download is not a ZIP archive")
        with zipfile.ZipFile(partial) as archive:
            if archive.testzip() is not None:
                raise ValueError("Downloaded ZIP failed its CRC check")
        partial.replace(destination)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return {"bytes": byte_count, "sha256": digest.hexdigest(), "already_present": False}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
