"""A picture per person: a head-and-shoulders crop the enrolling page cuts
from the first accepted photo. Kept on the app's data volume, outside the
adapter's gallery; deleted with the faces."""
from __future__ import annotations

import pathlib
import re
from typing import Optional

from .config import get_settings

_CODE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def picture_path(code: str) -> Optional[pathlib.Path]:
    if not _CODE.match(code or ""):
        return None
    return pathlib.Path(get_settings().data_dir) / "pictures" / f"{code}.jpg"


def has_picture(code: str) -> bool:
    p = picture_path(code)
    return bool(p and p.is_file())


def save_picture(code: str, data: bytes) -> None:
    p = picture_path(code)
    if p is None:
        raise ValueError("bad code")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def drop_picture(code: str) -> None:
    p = picture_path(code)
    if p and p.is_file():
        try:
            p.unlink()
        except OSError:
            pass
