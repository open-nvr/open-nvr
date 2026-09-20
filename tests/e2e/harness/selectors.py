# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every selector the GUI tests use, in one editable place.

**If a GUI test breaks because the UI changed, fix it here.** One line, one
file, no test touched. That is the whole point of this module: selectors are
the part of a browser suite that rots, so they are kept out of the tests and
out of the page objects, in a list a tester can edit without reading any
Playwright.

## Why each selector is a chain

The app currently has **no** ``data-testid`` attributes. They are being added
on a separate branch (`feat/ui-test-hooks`), which must be able to land — or
not land — without breaking anything here. So every selector lists several
ways to find the same element, and resolves to whichever exists:

    testid  ->  title  ->  role+name  ->  placeholder  ->  label  ->  text

That ordering reflects what the app actually offers. There are ~145 ``title``
attributes and they are the most reliable hook that exists today, especially
for the many icon-only buttons. Implicit roles (``button``, ``heading``,
``row``, ``textbox``) come next. ``get_by_label`` works in only a handful of
places — AddCameraDialog, Login, and two RecordingSettings checkboxes — because
elsewhere the ``<label>`` is a sibling with no ``htmlFor``, so text inputs are
addressed by placeholder instead.

The chain is built with Playwright's ``Locator.or_()``, so it is one locator
that matches whichever strategy the current build supports. When the testid
branch lands, the testid arm starts matching and the rest become dead weight
that costs nothing.

## Adding or fixing one

Add a ``Selector`` to the registry below and reference it from a page object.
Give it every hook the element genuinely has; the chain does the rest. If an
element has no usable hook at all, that is a signal to add a ``data-testid``
to the product rather than to reach for a Tailwind class chain here — class
chains are the one strategy guaranteed to break on a redesign.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _fill(template: str | None, values: dict[str, Any]) -> str | None:
    """Substitute ``{}`` placeholders, or return None if a value is missing.

    Returning None rather than raising is deliberate -- see ``_candidates``.
    """
    if not template:
        return None
    try:
        return template.format(**values)
    except (KeyError, IndexError):
        return None


@dataclass(frozen=True)
class Selector:
    """One element, described several ways.

    Every field is optional; supply whichever hooks the element really has.
    ``{}``-style placeholders in ``title``, ``text`` and ``name`` are filled by
    keyword arguments to :meth:`locate`, so one entry covers a whole family of
    rows or buttons.
    """

    #: Human name, used in failure messages. Make it read like the thing.
    name: str
    #: ``data-testid``, once feat/ui-test-hooks lands. Preferred when present.
    testid: str | None = None
    #: ``title`` attribute — the most widely available hook in this app.
    title: str | None = None
    #: ``(role, accessible_name)``; name may be None to match any.
    role: tuple[str, str | None] | None = None
    placeholder: str | None = None
    label: str | None = None
    #: Visible text. Substring match by default, so keep it distinctive.
    text: str | None = None
    #: Last resort. A class chain WILL break on a redesign — prefer a testid.
    css: str | None = None
    #: True when the element legitimately appears more than once and the
    #: caller is expected to narrow it (e.g. "Add Camera" exists 2-3 times).
    ambiguous: bool = False
    #: Free-text note surfaced in failure messages, for traps worth recalling.
    note: str = ""

    def locate(self, scope: Any, **fmt: Any) -> Any:
        """Return a Playwright locator matching any of this selector's hooks.

        Args:
            scope: a ``Page`` or a ``Locator`` to search within. Scoping to a
                locator is how the ambiguous ones are disambiguated.
            **fmt: values for ``{}`` placeholders in title/text/name.

        Raises:
            LookupError: if the selector describes no hooks at all.
        """
        candidates = self._candidates(scope, fmt)
        if not candidates:
            raise LookupError(
                f"selector {self.name!r} describes no way to find the element; "
                "give it a testid, title, role, placeholder, label or text in "
                "harness/selectors.py"
            )
        located = candidates[0]
        for other in candidates[1:]:
            located = located.or_(other)

        # Name the locator so the TRACE says which selector each step used.
        # Measured, because it is easy to assume more than it does: this does
        # NOT change the pytest assertion message, which still prints the raw
        # chain (`waiting for get_by_test_id("alert-bell").or_(...)`). What it
        # changes is the trace, which this suite captures on every failure --
        # each step there is labelled "<name> [selectors.py]", so opening the
        # trace tells you which entry in this file to edit rather than leaving
        # you to match a locator chain back to it by eye. Guarded because
        # describe() arrived in a recent Playwright.
        try:
            return located.describe(f"{self.name} [selectors.py]")
        except AttributeError:  # pragma: no cover - older Playwright
            return located

    def _candidates(self, scope: Any, fmt: dict[str, Any]) -> list[Any]:
        """Build one locator per usable hook, skipping those we cannot fill in.

        A hook that needs a value the caller did not supply is **dropped**, not
        an error. This is what lets one entry serve both "the row for camera 7"
        and "any row": ``CAMERA_ROW`` carries a ``camera-row-{camera_id}``
        testid for the specific case and a bare ``row`` role for the general
        one, and asking for rows without an id simply uses the latter.

        Getting this wrong the first time raised ``KeyError: 'camera_id'`` from
        deep inside a locator, which reads as a harness crash rather than as
        "that selector wanted an argument".
        """
        out: list[Any] = []
        if (value := _fill(self.testid, fmt)) is not None:
            out.append(scope.get_by_test_id(value))
        if (value := _fill(self.title, fmt)) is not None:
            out.append(scope.get_by_title(value, exact=False))
        if self.role:
            role, name = self.role
            if name:
                if (value := _fill(name, fmt)) is not None:
                    out.append(scope.get_by_role(role, name=value))
            else:
                out.append(scope.get_by_role(role))
        if (value := _fill(self.placeholder, fmt)) is not None:
            # exact=True, because Playwright matches placeholders as
            # SUBSTRINGS by default and this app has pairs where one is
            # contained in the other on the same form: the Add Camera dialog's
            # IP field says "192.168.1.100" and its RTSP field says
            # "rtsp://192.168.1.100:554/stream1", so the loose form resolves to
            # two elements and every fill() dies on a strict-mode violation.
            # Settings has "admin" against "admin@example.com, ...". Every
            # placeholder declared below is a whole literal copied from the
            # markup, so exact costs nothing and removes the whole class.
            out.append(scope.get_by_placeholder(value, exact=True))
        if (value := _fill(self.label, fmt)) is not None:
            out.append(scope.get_by_label(value))
        if (value := _fill(self.text, fmt)) is not None:
            out.append(scope.get_by_text(value))
        if self.css:
            out.append(scope.locator(self.css))
        return out

    def describe(self) -> str:
        """A one-line summary for failure messages."""
        hooks = [
            f"testid={self.testid!r}" if self.testid else "",
            f"title={self.title!r}" if self.title else "",
            f"role={self.role!r}" if self.role else "",
            f"placeholder={self.placeholder!r}" if self.placeholder else "",
            f"label={self.label!r}" if self.label else "",
            f"text={self.text!r}" if self.text else "",
            f"css={self.css!r}" if self.css else "",
        ]
        joined = ", ".join(h for h in hooks if h)
        suffix = f" -- {self.note}" if self.note else ""
        return f"{self.name} ({joined}){suffix}"


# ===========================================================================
# Application shell
# ===========================================================================
NAV_LINKS = Selector(
    name="sidebar navigation links",
    css="nav a, aside a",
    note="entries are permission-gated and appear after first paint, so poll",
)
NAV_GROUP_HEADERS = Selector(
    name="sidebar navigation group headers",
    css="nav button[aria-expanded], aside button[aria-expanded]",
    note=(
        "the collapsible groups (AI, Security, Governance, Administration). "
        "Only the pinned NVR group renders its links directly, so everything "
        "else is behind one of these and is invisible to NAV_LINKS while the "
        "group is collapsed -- which it is by default. A group with no "
        "permitted items is dropped entirely, so THESE are where nav gating "
        "actually shows"
    ),
)
ALERT_BELL = Selector(
    name="alert bell",
    testid="alert-bell",
    role=("button", "Alarms"),
    title="Alarms",
)
SNACKBAR = Selector(
    name="snackbar",
    role=("alert", None),
    note="auto-dismisses after 5s -- assert promptly or use the network response",
)
MODAL_HEADING = Selector(
    name="modal heading",
    role=("heading", "{title}"),
    note="Modal has no role=dialog until feat/ui-test-hooks lands",
)
MODAL_CLOSE = Selector(
    name="modal close button",
    role=("button", "Close"),
    note="'Close' also matches the snackbar and PlaybackConsole -- scope it",
    ambiguous=True,
)

# ===========================================================================
# Login / MFA
# ===========================================================================
LOGIN_USERNAME = Selector(name="username field", placeholder="admin", label="Username")
LOGIN_PASSWORD = Selector(name="password field", placeholder="●●●●●●●●")
LOGIN_SUBMIT = Selector(name="sign-in button", role=("button", "Sign in"))
MFA_CODE = Selector(name="TOTP field", placeholder="000000")
MFA_SUBMIT = Selector(name="verify button", role=("button", "Verify"))

# ===========================================================================
# Cameras
# ===========================================================================
CAMERAS_HEADING = Selector(name="Cameras heading", role=("heading", "Cameras"))
CAMERAS_SEARCH = Selector(name="camera search", placeholder="Search name or IP")
ADD_CAMERA_OPEN = Selector(
    name="Add Camera button",
    testid="cameras-add",
    role=("button", "Add Camera"),
    ambiguous=True,
    note="matches the header button, the empty-state button AND the dialog CTA",
)
ADD_CAMERA_DIALOG = Selector(
    name="Add Camera dialog",
    role=("heading", "Add New Camera"),
    note="the dialog has no role=dialog; its <h2> is the scoping handle",
)
TAB_MANUAL = Selector(name="Manual tab", role=("tab", "Manual"))
FIELD_CAMERA_NAME = Selector(
    name="camera name field",
    placeholder="e.g., Front Door",
    label="Camera Name",
)
FIELD_IP = Selector(name="IP address field", placeholder="192.168.1.100")
FIELD_RTSP = Selector(
    name="RTSP URL field",
    placeholder="rtsp://192.168.1.100:554/stream1",
    note="blurring another field rewrites this via syncIdentity -- fill it LAST",
)
ADD_CAMERA_SUBMIT = Selector(
    name="dialog Add Camera button",
    role=("button", "Add Camera"),
    note="scope to the dialog; disabled until name and IP are both filled",
)
DUPLICATE_PROMPT = Selector(
    name="duplicate-camera prompt",
    text="already added",
    note="every fake camera shares one IP, so this always appears",
)
ADD_ANYWAY = Selector(name="Add Anyway button", role=("button", "Add Anyway"))
CAMERA_ROW = Selector(
    name="camera row",
    testid="camera-row-{camera_id}",
    role=("row", None),
    note="filter by camera name; the table is a real <table>, not virtualised",
)
CAMERA_ROW_EDIT = Selector(name="edit camera", title="Edit camera", role=("button", "Edit {name}"))
CAMERA_ROW_DELETE = Selector(
    name="delete camera",
    title="Delete camera",
    role=("button", "Delete {name}"),
    note="triggers native window.confirm -- register a dialog handler first",
)
CAMERA_ROW_LIVE = Selector(name="view live", title="View live", role=("button", "View {name} live"))
CAMERAS_EMPTY = Selector(name="no cameras empty state", text="No cameras")

# ===========================================================================
# Live view
# ===========================================================================
LIVE_HEADING = Selector(name="Live View heading", role=("heading", "Live View"))
VIDEO = Selector(
    name="video element",
    css="video",
    note="'playing' is readyState >= 2 and currentTime > 0, not a DOM class",
)
LIVE_BADGE = Selector(name="LIVE badge", text="LIVE")
TRANSPORT_CHIP = Selector(
    name="transport chip",
    title="Streaming over",
    note="a button only when both transports exist; otherwise a plain span",
)
LIVE_TILE = Selector(
    name="live tile",
    testid="live-tile-{index}",
    css="[data-camera-id]",
    note="tiles come from localStorage, not ?camera= -- seed the display order",
)
NO_CAMERA_CHIP = Selector(name="empty tile chip", text="NO CAMERA")

# ===========================================================================
# Playback
# ===========================================================================
PLAYBACK_HEADING = Selector(name="Recordings heading", role=("heading", "Recordings"))
PLAYBACK_CAMERA_GROUP = Selector(
    name="recordings camera group header",
    role=("button", "{camera}"),
    note=(
        "the Recordings list groups footage under one collapsible button per "
        "camera, and only auto-expands when exactly ONE camera has footage. "
        "With two or more, every Play button is out of the DOM until this is "
        "clicked -- so this must be clicked by name, never guessed at"
    ),
)
PLAY_RECORDING = Selector(
    name="Play button",
    title="Play recording",
    role=("button", "Play"),
    note=(
        "the title is 'Playback unavailable - Media Server offline' when the "
        "day has no playback_url, so a miss here can mean MediaMTX is down "
        "rather than that the selector is wrong"
    ),
)
PLAYBACK_CLOSE = Selector(name="close playback console", role=("button", "Close"))
TIMELINE_TRACK = Selector(
    name="timeline scrub track",
    testid="playback-timeline-track",
    css="div.h-9.bg-neutral-800",
    note="bare divs with no role; the class chain is a stopgap until the testid lands",
)
TIMELINE_PLAYHEAD = Selector(
    name="playhead readout",
    testid="playback-timeline-playhead",
    css="div.h-5 span",
)
CLIP_MODE = Selector(name="clip/export toggle", title="Clip / export")
EXPORT_CLIP = Selector(name="Export clip button", role=("button", "Export clip"))
PLAYBACK_EMPTY = Selector(name="no recordings", text="No recordings found")

# ===========================================================================
# Vehicles
# ===========================================================================
VEHICLES_HEADING = Selector(name="Vehicles heading", role=("heading", "Vehicles"))
PLATE_CELL = Selector(
    name="plate button",
    role=("button", "{plate}"),
    note="the plate cell is a button, so it has a role",
)
PLATE_SEARCH = Selector(
    name="plate search",
    # The full literal, including the ellipsis and the example: matching
    # is exact now, and this one used to lean on the substring behaviour.
    placeholder="Plate contains… (e.g. 1234)",
)
EXPORT_CSV = Selector(name="Export CSV button", role=("button", "Export CSV"))
VEHICLES_EMPTY = Selector(name="no plate reads", text="No plate reads in this window")

# ===========================================================================
# Alerts
# ===========================================================================
ALERTS_HEADING = Selector(
    name="Alerts heading",
    role=("heading", "Alerts & Incidents"),
    note="the heading renders &amp; -- the accessible name is the decoded &",
)
FIRE_TEST_ALARM = Selector(name="Test high button", role=("button", "Test high"))
ACK_ROW = Selector(
    name="row Ack button",
    role=("button", "Ack"),
    note="rendered only while unacknowledged; disappears after a successful ack",
)
ACK_ALL = Selector(name="Acknowledge all button", text="Acknowledge all")
UNACKED_STATUS = Selector(name="unacked status cell", text="unacked")
ALERTS_EMPTY = Selector(name="no alarms", text="No alarms yet")

# ===========================================================================
# Settings
# ===========================================================================
RETENTION_DAYS = Selector(
    name="retention days field",
    testid="retention-days",
    placeholder="30",
    note="its <label> is a sibling with no htmlFor, so get_by_label fails today",
)
MIN_FREE_SPACE = Selector(
    name="minimum free space field",
    testid="min-free-space",
    placeholder="Leave empty to disable",
)
PROTECT_FLAGGED = Selector(
    name="protect flagged checkbox",
    label="Protect Flagged Recordings",
    note="one of the few properly labelled controls in the app",
)
SAVE_RETENTION = Selector(
    name="Save Retention Settings button",
    role=("button", "Save Retention Settings"),
)

API_TOKEN_NEW = Selector(name="New token button", testid="api-token-new", role=("button", "New token"))
API_TOKEN_NAME = Selector(name="token name field", testid="api-token-name", placeholder="Home Assistant")
API_TOKEN_CREATE = Selector(name="Create token button", testid="api-token-create", role=("button", "Create token"))
API_TOKEN_SECRET = Selector(
    name="one-time token secret",
    testid="api-token-secret",
    note="shown once, right after create; gone after Done or a reload",
)
API_TOKEN_ROW = Selector(
    name="API token row", testid="api-token-row", role=("row", None), ambiguous=True,
    note="one per token; the page object narrows it with .filter(has_text=name)",
)
API_TOKEN_REVOKE = Selector(name="Revoke button", role=("button", "Revoke"))

# ===========================================================================
# Access control / audit
# ===========================================================================
AUDIT_HEADING = Selector(name="audit log heading", role=("heading", None))
OCCUPANCY_HEADING = Selector(name="Occupancy heading", role=("heading", "Occupancy"))


def all_selectors() -> dict[str, Selector]:
    """Every selector in this module, by name.

    Used by the self-test that asserts each one describes at least one
    hook -- an empty Selector is a silent no-op that would fail much later
    with a confusing timeout.
    """
    return {
        name: value
        for name, value in globals().items()
        if name.isupper() and isinstance(value, Selector)
    }


__all__ = ["Selector", "all_selectors"]
