"""Cameras in and out of a spreadsheet: the Cameras page's Import and Export.

One row is one camera, or one NVR/DVR expanded into its channels: a row
with ``channels`` ("1-16", "1,3,5-8") and a ``vendor`` (or an ``rtsp_url``
holding ``{ch}``) becomes a camera per channel, named "<name> Ch <n>", each
with that channel's RTSP URL. That is what makes a 350-recorder site 350
rows instead of a thousand.

This module is the pure part: reading CSV and Excel, checking every row,
matching rows to cameras already on the system, and writing the export and
the template. It touches the database only to read. Creating and updating
cameras is services/camera_import.py, through the same code paths as the
Cameras page.

Matching an existing camera, so a file can be imported again:

* a row with an RTSP URL (given, or built from the vendor's channel
  pattern) matches the camera with that URL, logins ignored;
* a row with only an address matches the one camera at that address; with
  several there, the row has to give the RTSP URL to say which.

A file never deletes a camera: one missing from the file is left alone.
"""

from __future__ import annotations

import csv
import io
import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse, urlunparse

from core.camera_source import is_publisher

#: Columns in export and template order. ``channels`` and ``vendor`` are
#: blank on an export (one row per camera) and are there so an exported file
#: and the template read the same way.
COLUMNS = [
    "name", "location", "ip_address", "port", "username", "password",
    "rtsp_url", "substream_url", "description", "vlan", "active",
    "channels", "vendor",
]

#: What each column means, for the template's "How to fill" sheet and docs.
COLUMN_HELP = {
    "name": "Camera name (required). For an NVR row, the recorder's name: each channel becomes '<name> Ch <n>'.",
    "location": "Where it is, e.g. North Zone, Branch 12. The Cameras page and tours filter by it.",
    "ip_address": "The camera's or recorder's address. Optional when rtsp_url is given.",
    "port": "RTSP port, 554 if blank.",
    "username": "Login for the camera or recorder.",
    "password": "Its password. Blank on update keeps the stored one.",
    "rtsp_url": "Main stream, rtsp://host:port/path. Blank: found by ONVIF from the address and login. May hold {ch} for an NVR row.",
    "substream_url": "Optional low-resolution stream for live walls. May hold {ch}.",
    "description": "Free text.",
    "vlan": "Optional VLAN label.",
    "active": "yes or no (paused). Blank: yes for a new camera, unchanged for an existing one.",
    "channels": "NVR/DVR only: the channels to add, e.g. 1-16 or 1,3,5-8.",
    "vendor": "NVR/DVR only, with channels: hikvision, uniview, dahua or cpplus. Not needed when rtsp_url holds {ch}.",
}

#: The most rows a file may have, and cameras an import may make (an NVR row
#: makes one per channel). Generous for one site, a stop for a wrong file.
MAX_ROWS = 5000
MAX_CAMERAS = 10000
MAX_CHANNELS = 256
MAX_FILE_BYTES = 5 * 1024 * 1024

# Main and sub stream path per vendor, {ch} the 1-based channel. Hikvision and
# Uniview number streams <channel><stream>; Dahua and CP Plus take a query.
_VENDOR_PATHS = {
    "hikvision": ("/Streaming/Channels/{ch}01", "/Streaming/Channels/{ch}02"),
    "uniview": ("/Streaming/Channels/{ch}01", "/Streaming/Channels/{ch}02"),
    "dahua": ("/cam/realmonitor?channel={ch}&subtype=0", "/cam/realmonitor?channel={ch}&subtype=1"),
    "cpplus": ("/cam/realmonitor?channel={ch}&subtype=0", "/cam/realmonitor?channel={ch}&subtype=1"),
}
_VENDOR_ALIASES = {
    "hik": "hikvision", "hikvision": "hikvision", "unv": "uniview", "uniview": "uniview",
    "dahua": "dahua", "cpplus": "cpplus", "cp plus": "cpplus", "cp-plus": "cpplus",
}

# Header spellings people use for each column.
_HEADER_ALIASES = {
    "name": "name", "camera": "name", "camera name": "name",
    "location": "location", "zone": "location", "site": "location",
    "ip": "ip_address", "ip address": "ip_address", "ip_address": "ip_address", "address": "ip_address", "host": "ip_address",
    "port": "port", "rtsp port": "port",
    "username": "username", "user": "username", "login": "username",
    "password": "password", "pass": "password",
    "rtsp": "rtsp_url", "rtsp url": "rtsp_url", "rtsp_url": "rtsp_url", "stream": "rtsp_url", "main stream": "rtsp_url",
    "substream": "substream_url", "sub stream": "substream_url", "substream url": "substream_url", "substream_url": "substream_url",
    "description": "description", "notes": "description",
    "vlan": "vlan",
    "active": "active", "enabled": "active",
    "channels": "channels", "channel": "channels",
    "vendor": "vendor", "brand": "vendor", "make": "vendor",
}

_YES = {"yes", "y", "true", "1", "on", "active"}
_NO = {"no", "n", "false", "0", "off", "paused", "inactive"}
_HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
# A cell a spreadsheet would run as a formula; written with a leading quote.
_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


class ImportFileError(ValueError):
    """The file as a whole cannot be read: a message for the operator."""


# ── Reading ───────────────────────────────────────────────────────────


def read_table(data: bytes, filename: str) -> list[tuple[int, dict[str, str]]]:
    """The file's rows as (row number in the file, {column: text}), blank
    rows dropped. Headers are matched loosely ("IP Address", "ip"), unknown
    columns ignored. Raises ImportFileError for a file it cannot use."""
    if len(data) > MAX_FILE_BYTES:
        raise ImportFileError("The file is larger than 5 MB.")
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")) or data[:2] == b"PK":
        grid = _read_xlsx(data)
    elif name.endswith((".csv", ".txt")) or not name:
        grid = _read_csv(data)
    else:
        raise ImportFileError("Use a .csv or .xlsx file.")
    if not grid:
        raise ImportFileError("The file is empty.")

    header_at = next((i for i, cells in enumerate(grid) if any(c.strip() for c in cells)), None)
    if header_at is None:
        raise ImportFileError("The file is empty.")
    columns = [_HEADER_ALIASES.get(_norm_header(c)) for c in grid[header_at]]
    if "name" not in columns:
        raise ImportFileError("No 'name' column. Start from the template, or name the first row's columns as it does.")

    rows: list[tuple[int, dict[str, str]]] = []
    for offset, cells in enumerate(grid[header_at + 1:], start=header_at + 2):
        if not any(c.strip() for c in cells):
            continue
        row: dict[str, str] = {}
        for col, cell in zip(columns, cells):
            if col and col not in row:
                row[col] = _unquote(cell.strip())
        rows.append((offset, row))
        if len(rows) > MAX_ROWS:
            raise ImportFileError(f"More than {MAX_ROWS} rows. Split the file.")
    if not rows:
        raise ImportFileError("The file has a header but no cameras.")
    return rows


def _read_csv(data: bytes) -> list[list[str]]:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:  # pragma: no cover - cp1252 decodes nearly anything
        raise ImportFileError("The file is not text. Save it as CSV (UTF-8) or as .xlsx.")
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    return [list(r) for r in csv.reader(io.StringIO(text), dialect)]


def _read_xlsx(data: bytes) -> list[list[str]]:
    from openpyxl import load_workbook

    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise ImportFileError("Could not read the Excel file. Save it again as .xlsx.") from exc
    try:
        ws = wb.worksheets[0]
        grid: list[list[str]] = []
        for values in ws.iter_rows(values_only=True):
            grid.append([_cell_text(v) for v in values])
            if len(grid) > MAX_ROWS + 50:
                break
        return grid
    finally:
        wb.close()


def _cell_text(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _norm_header(text: str) -> str:
    return re.sub(r"[\s_]+", " ", (text or "").strip().lower().lstrip("﻿"))


def _unquote(value: str) -> str:
    """Undo the quote an export puts before a formula-like cell."""
    if len(value) > 1 and value[0] == "'" and value[1] in "=+-@":
        return value[1:]
    return value


# ── Checking and expanding ─────────────────────────────────────────────


@dataclass
class PlannedCamera:
    """One camera a row asks for, after expanding NVR channels."""

    row: int
    channel: int | None
    fields: dict[str, Any]
    errors: list[str] = field(default_factory=list)
    #: new, update, unchanged, duplicate, error.
    status: str = "new"
    camera_id: int | None = None
    #: The fields an update would change.
    changes: list[str] = field(default_factory=list)

    @property
    def key(self) -> str | None:
        url = self.fields.get("rtsp_url")
        return stream_key(url) if url else None


def parse_channels(text: str) -> list[int]:
    """'1-16' / '1,3,5-8' as a sorted list of channels; ValueError if bad."""
    out: set[int] = set()
    for part in re.split(r"[,\s;]+", text.strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", part)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            if lo < 1 or hi < lo:
                raise ValueError(f"'{part}' is not a channel range")
            out.update(range(lo, hi + 1))
        elif part.isdigit() and int(part) >= 1:
            out.add(int(part))
        else:
            raise ValueError(f"'{part}' is not a channel")
        if len(out) > MAX_CHANNELS:
            raise ValueError(f"more than {MAX_CHANNELS} channels")
    if not out:
        raise ValueError("no channel given")
    return sorted(out)


def plan_rows(rows: list[tuple[int, dict[str, str]]]) -> list[PlannedCamera]:
    """Every camera the rows ask for, each with its own errors."""
    planned: list[PlannedCamera] = []
    for row_no, raw in rows:
        planned.extend(_plan_row(row_no, raw))
        if len(planned) > MAX_CAMERAS:
            raise ImportFileError(f"The file adds more than {MAX_CAMERAS} cameras. Split it.")
    return planned


def _plan_row(row_no: int, raw: dict[str, str]) -> list[PlannedCamera]:
    errors: list[str] = []
    name = raw.get("name", "").strip()
    if not name:
        errors.append("name is missing")
    elif len(name) > 100:
        errors.append("name is longer than 100 characters")

    fields: dict[str, Any] = {"name": name}
    for col, limit in (("location", 200), ("description", 2000), ("vlan", 50), ("username", 50), ("password", 255)):
        value = raw.get(col, "")
        if col not in ("password",):
            value = value.strip()
        if value:
            if len(value) > limit:
                errors.append(f"{col} is longer than {limit} characters")
            fields[col] = value

    port_text = raw.get("port", "").strip()
    port: int | None = None
    if port_text:
        if port_text.isdigit() and 1 <= int(port_text) <= 65535:
            port = int(port_text)
        else:
            errors.append(f"port '{port_text}' is not a number from 1 to 65535")

    active_text = raw.get("active", "").strip().lower()
    if active_text in _YES:
        fields["is_active"] = True
    elif active_text in _NO:
        fields["is_active"] = False
    elif active_text:
        errors.append(f"active '{raw.get('active')}' should be yes or no")

    rtsp = raw.get("rtsp_url", "").strip()
    sub = raw.get("substream_url", "").strip()
    for label, url in (("rtsp_url", rtsp), ("substream_url", sub)):
        if url and not re.match(r"^rtsps?://", url, re.I):
            errors.append(f"{label} should start with rtsp:// or rtsps://")
        elif url and len(url) > 500:
            errors.append(f"{label} is longer than 500 characters")

    ip = raw.get("ip_address", "").strip()
    if not ip and rtsp:
        ip = (urlparse(rtsp.replace("{ch}", "1")).hostname or "")
    if not ip:
        errors.append("ip_address is missing (give it, or an rtsp_url)")
    elif not _valid_host(ip):
        errors.append(f"ip_address '{ip}' is not an address or host name")
    fields["ip_address"] = ip
    if port is None and rtsp:
        port = urlparse(rtsp.replace("{ch}", "1")).port
    # Only a port the row gives (or its URL does): a blank cell must not move
    # an existing camera to 554. A new camera gets 554 when it is created.
    if port is not None:
        fields["port"] = port

    channels_text = raw.get("channels", "").strip()
    vendor_text = raw.get("vendor", "").strip().lower()
    vendor = _VENDOR_ALIASES.get(vendor_text) if vendor_text else None
    if vendor_text and not vendor:
        errors.append(f"vendor '{raw.get('vendor')}' is not one of hikvision, uniview, dahua, cpplus")

    if not channels_text:
        if "{ch}" in rtsp or "{ch}" in sub:
            errors.append("rtsp_url has {ch} but no channels are given")
        if rtsp:
            fields["rtsp_url"] = rtsp
        if sub:
            fields["substream_url"] = sub
        return [PlannedCamera(row=row_no, channel=None, fields=fields, errors=errors,
                              status="error" if errors else "new")]

    try:
        channels = parse_channels(channels_text)
    except ValueError as exc:
        errors.append(f"channels: {exc}")
        channels = []
    if rtsp and "{ch}" not in rtsp:
        errors.append("an NVR row's rtsp_url needs {ch} where the channel goes, or leave it blank and give the vendor")
    if not rtsp and not vendor and not errors:
        errors.append("an NVR row needs a vendor (hikvision, uniview, dahua, cpplus) or an rtsp_url with {ch}")
    if errors:
        return [PlannedCamera(row=row_no, channel=None, fields=fields, errors=errors, status="error")]

    base = f"rtsp://{_url_host(ip)}:{fields.get('port', 554)}"
    main_t, sub_t = (rtsp, sub) if rtsp else (base + _VENDOR_PATHS[vendor][0], sub or base + _VENDOR_PATHS[vendor][1])
    out = []
    for ch in channels:
        f = dict(fields)
        f["name"] = f"{name} Ch {ch}"[:100]
        f["rtsp_url"] = main_t.replace("{ch}", str(ch))
        if sub_t:
            f["substream_url"] = sub_t.replace("{ch}", str(ch))
        out.append(PlannedCamera(row=row_no, channel=ch, fields=f))
    return out


def _valid_host(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        return bool(_HOST_RE.match(text)) and len(text) <= 45


def _url_host(host: str) -> str:
    try:
        return f"[{host}]" if ipaddress.ip_address(host).version == 6 else host
    except ValueError:
        return host


def strip_userinfo(url: str | None) -> str | None:
    """``url`` without its user:password@, the query kept (Dahua's channel
    lives in it). Unlike utils.url_redaction, which also drops the query."""
    if not url:
        return url
    try:
        p = urlparse(url)
    except ValueError:
        return url
    if not (p.username or p.password):
        return url
    host = p.hostname or ""
    netloc = f"[{host}]" if ":" in host else host
    if p.port is not None:
        netloc = f"{netloc}:{p.port}"
    return urlunparse((p.scheme, netloc, p.path, p.params, p.query, p.fragment))


def stream_key(url: str) -> str:
    """What makes two RTSP URLs the same stream: no login, scheme and host
    in lower case, the default port spelled out."""
    p = urlparse(strip_userinfo(url) or "")
    host = (p.hostname or "").lower()
    port = p.port or (322 if p.scheme.lower() == "rtsps" else 554)
    q = f"?{p.query}" if p.query else ""
    return f"{p.scheme.lower()}://{host}:{port}{p.path}{q}"


# ── Matching against the cameras already here ──────────────────────────

#: Fields a matched row can change, and how they compare with the camera.
_UPDATABLE = ("name", "location", "description", "vlan", "username", "password",
              "rtsp_url", "substream_url", "ip_address", "port", "is_active")


def match_existing(planned: list[PlannedCamera], cameras: list[Any], manageable: set[int] | None) -> None:
    """Mark each planned camera new, update, unchanged, duplicate (twice in
    the file) or error, against ``cameras`` (every non-deleted camera on
    the system), ``manageable`` the ids the importer may change (None: all).
    """
    by_url: dict[str, list[Any]] = {}
    by_ip: dict[str, list[Any]] = {}
    for cam in cameras:
        # A webcam is not in any file (export leaves it out) and must never
        # be matched by its placeholder address and turned into an IP camera.
        if is_publisher(cam):
            continue
        if cam.rtsp_url:
            by_url.setdefault(stream_key(cam.rtsp_url), []).append(cam)
        by_ip.setdefault((cam.ip_address or "").lower(), []).append(cam)

    seen: dict[str, int] = {}
    for p in planned:
        if p.status == "error":
            continue
        key = p.key or f"ip:{p.fields['ip_address'].lower()}:{p.fields['name'].lower()}"
        if key in seen:
            p.status = "duplicate"
            p.errors.append(f"the same camera as row {seen[key]}")
            continue
        seen[key] = p.row

        if p.key:
            matches = by_url.get(p.key, [])
        else:
            matches = by_ip.get(p.fields["ip_address"].lower(), [])
            if len(matches) > 1:
                p.status = "error"
                p.errors.append(f"{len(matches)} cameras already use this address; give the rtsp_url to say which")
                continue
        if not matches:
            p.status = "new"
            continue
        cam = matches[0]
        p.camera_id = cam.id
        if manageable is not None and cam.id not in manageable:
            p.status = "error"
            p.errors.append("this camera is already on the system and you may not change it")
            continue
        p.changes = _changes(p.fields, cam)
        p.status = "update" if p.changes else "unchanged"


def _changes(fields: dict[str, Any], cam: Any) -> list[str]:
    changed = []
    for name in _UPDATABLE:
        if name not in fields:
            continue
        want = fields[name]
        have = getattr(cam, name, None)
        if name in ("rtsp_url", "substream_url"):
            if stream_key(want) != (stream_key(have) if have else None):
                changed.append(name)
        elif name == "password":
            if want and want != have:
                changed.append(name)
        elif name == "ip_address":
            if str(want).lower() != str(have or "").lower():
                changed.append(name)
        elif want != have:
            changed.append(name)
    return changed


def summary(planned: list[PlannedCamera]) -> dict[str, int]:
    out = {"total": len(planned), "new": 0, "update": 0, "unchanged": 0, "duplicate": 0, "error": 0}
    for p in planned:
        out[p.status] = out.get(p.status, 0) + 1
    return out


def preview_row(p: PlannedCamera) -> dict[str, Any]:
    """A planned camera as the preview shows it: never a password."""
    f = p.fields
    return {
        "row": p.row,
        "channel": p.channel,
        "status": p.status,
        "name": f.get("name"),
        "location": f.get("location"),
        "ip_address": f.get("ip_address"),
        "port": f.get("port"),
        "rtsp_url": strip_userinfo(f.get("rtsp_url")),
        "has_password": bool(f.get("password")),
        "camera_id": p.camera_id,
        "changes": [c for c in p.changes],
        "errors": p.errors,
    }


# ── Writing ────────────────────────────────────────────────────────────


def export_rows(cameras: list[Any], include_passwords: bool) -> list[list[Any]]:
    """One row per camera in COLUMNS order. Without passwords, logins are
    taken out of the RTSP URLs too: the stored URL carries them. Webcams
    are left out: they have no address or URL, so a row for one could not
    be imported back."""
    out = []
    for c in cameras:
        if is_publisher(c):
            continue
        rtsp = c.rtsp_url if include_passwords else strip_userinfo(c.rtsp_url)
        sub = c.substream_url if include_passwords else strip_userinfo(c.substream_url)
        out.append([
            c.name, c.location or "", c.ip_address or "", c.port or "",
            c.username or "", (c.password or "") if include_passwords else "",
            rtsp or "", sub or "", c.description or "", c.vlan or "",
            "yes" if c.is_active else "no", "", "",
        ])
    return out


TEMPLATE_EXAMPLES = [
    ["Gate camera", "North Zone", "192.168.1.64", "554", "admin", "", "", "", "Main gate, facing the road", "", "yes", "", ""],
    ["Front desk", "North Zone", "", "", "admin", "", "rtsp://192.168.1.65:554/Streaming/Channels/101", "", "", "", "yes", "", ""],
    ["NVR South 1", "South Zone", "10.1.2.20", "554", "admin", "", "", "", "16-channel recorder: one camera per channel", "", "yes", "1-16", "hikvision"],
]


def to_csv(rows: list[list[Any]]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(COLUMNS)
    for r in rows:
        w.writerow([_guard(v) for v in r])
    # A BOM, so Excel opens it as UTF-8 rather than mangling names.
    return ("﻿" + buf.getvalue()).encode("utf-8")


def to_xlsx(rows: list[list[Any]], *, with_help: bool = False) -> bytes:
    from openpyxl import Workbook
    from openpyxl.cell import WriteOnlyCell
    from openpyxl.styles import Font

    wb = Workbook(write_only=True)
    ws = wb.create_sheet("Cameras")
    ws.freeze_panes = "A2"
    widths = {"name": 28, "location": 18, "ip_address": 16, "port": 7, "username": 12, "password": 14,
              "rtsp_url": 48, "substream_url": 48, "description": 32, "vlan": 8, "active": 8,
              "channels": 10, "vendor": 12}
    for i, col in enumerate(COLUMNS, start=1):
        ws.column_dimensions[_col_letter(i)].width = widths.get(col, 14)
    header = []
    for col in COLUMNS:
        cell = WriteOnlyCell(ws, value=col)
        cell.font = Font(bold=True)
        header.append(cell)
    ws.append(header)
    for r in rows:
        line = []
        for v in r:
            cell = WriteOnlyCell(ws, value=v if v != "" else None)
            if isinstance(v, str) and v:
                # Text, always: a name starting with "=" must not become a formula.
                cell.data_type = "s"
            line.append(cell)
        ws.append(line)
    if with_help:
        hs = wb.create_sheet("How to fill")
        hs.column_dimensions["A"].width = 16
        hs.column_dimensions["B"].width = 110
        hs.append([WriteOnlyCell(hs, value="Column"), WriteOnlyCell(hs, value="What to put in it")])
        for col in COLUMNS:
            hs.append([col, COLUMN_HELP[col]])
        hs.append([])
        hs.append(["", "One row per camera, or one row per NVR/DVR with channels and vendor. Importing the same file again changes nothing; a camera left out of the file is not deleted."])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _col_letter(i: int) -> str:
    from openpyxl.utils import get_column_letter

    return get_column_letter(i)


def _guard(v: Any) -> Any:
    """A CSV cell a spreadsheet would run as a formula, made plain text."""
    if isinstance(v, str) and v.startswith(_FORMULA_START):
        return "'" + v
    return v
