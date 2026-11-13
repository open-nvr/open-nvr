# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Where a notification actually goes.

Ten destinations, one interface. Everything here is HTTP through the
``httpx`` the app already had, or stdlib ``smtplib`` — no channel adds a
dependency, because a notifier that cannot start is a notifier that does
not notify.

The interface is deliberately wider than "POST a string", because the
differences between these services are exactly the differences that
matter at 3am:

``send``      deliver, and return a handle if the service lets us edit
              the message later
``edit``      replace a message in place (Telegram, Discord, Matrix,
              Gotify-no, Slack-no) — the difference between one
              notification that updates and nine that stack
``probe``     ask the service whether our credentials still work,
              WITHOUT sending anything. This is how a revoked bot token
              is found on a Tuesday afternoon instead of at 3am.
``can_*``     honest capability flags. The page shows what a channel can
              and cannot do rather than pretending they are equivalent.

Severity maps to each service's own urgency scheme here and nowhere
else. An operator picks a severity; they never hand-set ``X-Priority``
and a Pushover ``priority`` and a Telegram ``disable_notification`` and
keep them consistent.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import smtplib
import ssl
import time
import uuid
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any
from urllib.parse import quote, urlsplit

import httpx  # module attribute on purpose: tests monkeypatch channels.httpx

logger = logging.getLogger("alert-notifier.channels")

#: Per-attempt HTTP budget. Short: a channel that is down must not hold
#: the delivery of the other channels behind it.
TIMEOUT_SECONDS = 8.0

#: Uploading a photo is slower than posting a line of text.
UPLOAD_TIMEOUT_SECONDS = 20.0

#: Severity ladder. Anything unrecognised ranks as "low" rather than
#: "critical": an unknown severity must not be able to ring a phone at
#: 3am, and the operator sees it in the inbox either way.
SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


def severity_rank(value: Any) -> int:
    return SEVERITY_RANK.get(str(value or "").lower().strip(), 1)


def severity_name(value: Any) -> str:
    name = str(value or "").lower().strip()
    return name if name in SEVERITY_RANK else "low"


# ── Errors ──────────────────────────────────────────────────────────


class DeliveryError(RuntimeError):
    """Delivery failed. Retriable unless a subclass says otherwise."""


class AuthFailed(DeliveryError):
    """The service rejected our credentials.

    Kept distinct from every other failure because it is the failure
    that does not heal: a revoked Telegram token, a deleted Discord
    webhook and a rotated Slack URL all return here, and all of them
    stay broken until a human fixes the config. Retrying is pointless
    and the channel must be marked failing immediately — this is the
    exact shape of "push quietly stopped working three weeks ago".
    """


class Throttled(DeliveryError):
    """The service asked us to slow down (HTTP 429)."""

    def __init__(self, message: str, retry_after: float = 0.0) -> None:
        super().__init__(message)
        self.retry_after = max(0.0, float(retry_after or 0.0))


class NotSupported(DeliveryError):
    """This channel cannot do that (edit a sent message, attach bytes)."""


# ── The message ─────────────────────────────────────────────────────


@dataclass
class Action:
    """A button on the notification.

    ``url`` only. An HTTP-callback button (ntfy's ``http`` action,
    Telegram's ``callback_data``) needs the phone to reach this app,
    and this app sits on an internal Docker network with no inbound
    route — so a callback button would render, be tapped, and silently
    do nothing. The app offers callback buttons only when the operator
    supplies a reachable base URL, and builds them as plain URLs even
    then. See CHANNELS.md → "Buttons that actually work".
    """

    label: str
    url: str


@dataclass
class Message:
    """One notification, before any channel has had an opinion about it.

    ``group`` is the count of alerts collapsed into this message (1 for
    a plain one). ``dedup_key`` is stable across an alert's updates so a
    channel that can edit replaces rather than stacks.
    """

    title: str
    body: str = ""
    severity: str = "high"
    camera: str = ""
    when: str = ""
    url: str = ""
    image: bytes | None = None
    image_url: str = ""
    actions: list[Action] = field(default_factory=list)
    dedup_key: str = ""
    #: Unique to THIS message, stable across its retries. ``dedup_key``
    #: identifies the situation and is reused for the next alert on the
    #: same camera; anything that must not collide between two separate
    #: notifications uses this instead.
    message_id: str = ""
    group: int = 1
    #: The alerts behind a grouped message, newest first — used for the
    #: "and 3 more" lines. Each is ``(severity, title, camera)``.
    members: list[tuple[str, str, str]] = field(default_factory=list)

    def without_actions(self) -> "Message":
        """A copy with the buttons removed, for a channel that cannot
        render them. Cheaper and more honest than sending a payload the
        service will drop."""
        import dataclasses

        return dataclasses.replace(self, actions=[])

    def text(self, *, limit: int = 4096) -> str:
        """The plain-text rendering every channel falls back to."""
        lines = [f"[{severity_name(self.severity).upper()}] {self.title}"]
        if self.body:
            lines.append(self.body)
        meta = [p for p in (self.camera, self.when) if p]
        if meta:
            lines.append(" · ".join(meta))
        if self.group > 1:
            extra = [f"  • {t}" for _s, t, _c in self.members[1:4]]
            lines.append(f"+{self.group - 1} more in this burst:")
            lines.extend(extra)
        if self.url:
            lines.append(self.url)
        out = "\n".join(lines)
        return out if len(out) <= limit else out[: limit - 1] + "…"

    def subject(self) -> str:
        """A one-line summary for channels with a title field."""
        head = f"{self.title}"
        if self.camera:
            head = f"{head} — {self.camera}"
        if self.group > 1:
            head = f"{head} (+{self.group - 1})"
        return head[:240]


# ── Base ────────────────────────────────────────────────────────────


class Channel:
    """One destination. Subclasses implement ``_send`` and ``_probe``."""

    kind = "channel"
    #: Can this channel replace a message it already sent?
    can_edit = False
    #: Can it carry the JPEG itself (rather than a link to one)?
    can_attach = False
    #: Can it render tappable buttons?
    can_act = False
    #: Can ``probe()`` verify credentials without sending anything?
    can_probe = True

    def __init__(self, name: str, spec: dict[str, Any]) -> None:
        self.name = name
        self.spec = spec

    # -- the surface --------------------------------------------------

    def send(self, msg: Message) -> str | None:
        """Deliver. Returns a handle for :meth:`edit`, or ``None`` when
        this channel cannot edit. Raises a :class:`DeliveryError`."""
        raise NotImplementedError

    def edit(self, msg: Message, handle: str) -> None:
        raise NotSupported(f"{self.kind} cannot edit a sent message")

    def probe(self) -> None:
        """Verify credentials without notifying anyone. Raises on
        failure, returns None on success."""
        raise NotSupported(f"{self.kind} has no silent credential check")

    @property
    def address(self) -> str:
        """Where this channel points, with secrets removed. Shown in the
        UI and written to the delivery log, so it must never carry a
        token — a support thread is the last place a bot token should
        turn up."""
        return "—"

    def close(self) -> None:
        pass

    # -- helpers ------------------------------------------------------

    @staticmethod
    def _check(resp: Any, *, what: str, gone_is_fatal: bool = True) -> None:
        code = getattr(resp, "status_code", 0)
        if 200 <= code < 300:
            return
        detail = _body_hint(resp)
        if code in (401, 403):
            raise AuthFailed(f"{what} rejected our credentials (HTTP {code}){detail}")
        if code == 404:
            # A deleted webhook is a 404 and is just as permanent as a
            # 401. But an EDIT's 404 usually means the message is gone
            # — somebody deleted the notification, or it aged out — and
            # condemning a healthy channel for that would take the
            # operator's alerting down because they tidied a chat.
            if not gone_is_fatal:
                raise NotSupported(
                    f"{what}: the message we would edit is gone{detail}")
            raise AuthFailed(f"{what} returned 404 — the target is gone{detail}")
        if code == 429:
            raise Throttled(f"{what} rate-limited us{detail}",
                            _retry_after(resp))
        raise DeliveryError(f"{what} returned HTTP {code}{detail}")

    @staticmethod
    def _post(url: str, *, what: str, timeout: float = TIMEOUT_SECONDS,
              **kwargs: Any) -> Any:
        try:
            resp = httpx.post(url, timeout=timeout, **kwargs)
        except Exception as exc:  # noqa: BLE001 — every transport fault
            raise DeliveryError(f"{what}: {exc}{_egress_hint(exc, url)}") from exc
        Channel._check(resp, what=what)
        return resp


def _retry_after(resp: Any) -> float:
    headers = getattr(resp, "headers", None) or {}
    try:
        return float(headers.get("Retry-After") or headers.get("retry-after") or 0)
    except (TypeError, ValueError):
        return 0.0


def _body_hint(resp: Any) -> str:
    """A short, safe excerpt of an error body.

    Truncated hard: some services echo the request back, and the request
    contains the token."""
    try:
        text = (resp.text or "").strip().replace("\n", " ")
    except Exception:  # noqa: BLE001
        return ""
    return f": {text[:160]}" if text else ""


def _egress_hint(exc: Exception, url: str) -> str:
    """Name the shipped compose's egress rule when a host is unreachable.

    Apps run on an internal network whose only way out is a proxy that
    asks core whether THIS app may reach THIS host. A channel the
    operator has not allowed does not fail with "forbidden" — it fails
    with a connection error, which reads like the service being down.
    """
    text = f"{exc}".lower()
    unreachable = any(
        s in text for s in ("connect", "name or service", "resolve",
                            "unreachable", "timed out", "timeout", "refused",
                            "proxy"))
    if not unreachable:
        return ""
    host = urlsplit(url).hostname or url
    return (f" — if {host} is correct, the app may not be allowed to reach "
            f"it: add it to this app's allowed hosts in the App Catalog "
            f"(shipped compose routes app egress through a proxy)")


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ── ntfy ────────────────────────────────────────────────────────────

#: ntfy's own scale is 1..5; ours is five names. Mapped once, here.
_NTFY_PRIORITY = {"info": "2", "low": "2", "medium": "3",
                  "high": "4", "critical": "5"}
_NTFY_TAGS = {"info": "information_source", "low": "information_source",
              "medium": "warning", "high": "rotating_light",
              "critical": "rotating_light"}


class NtfyChannel(Channel):
    """ntfy — the default, and the only channel with no account at all.

    A topic name IS the credential on the public server, which is why
    the app generates a long random one rather than letting someone pick
    ``home``: an alert stream is readable by anyone who guesses it.
    """

    kind = "ntfy"
    can_attach = True
    can_act = True

    def __init__(self, name: str, spec: dict[str, Any]) -> None:
        super().__init__(name, spec)
        self.server = str(spec.get("server") or "https://ntfy.sh").rstrip("/")
        self.topic = str(spec.get("topic") or "").strip().strip("/")
        self.token = str(spec.get("token") or "").strip()
        self.user = str(spec.get("username") or "").strip()
        self.password = str(spec.get("password") or "")
        if not self.topic:
            raise ValueError("ntfy: topic is required")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.topic):
            raise ValueError(
                "ntfy: topic must be 1-64 characters of A-Z a-z 0-9 _ -")

    @property
    def address(self) -> str:
        """The server, and only a hint of the topic.

        On the public server the topic name IS the credential — anyone
        who reads it off a screenshot receives every alert from this
        site, including the snapshots. It is exactly as sensitive as a
        webhook URL and is redacted the same way."""
        return f"{self.server}/{self.topic[:4]}…"

    def _fields(self, msg: Message) -> dict[str, Any]:
        """The message as ntfy's JSON publish body.

        JSON, not headers, for one blunt reason: httpx encodes header
        values as ASCII and RAISES on anything else. A camera called
        "Cámara Patio" would take the recommended default channel down
        permanently — and so would every ordinary message, because the
        title this app composes contains an em dash. Headers are used
        only where the protocol forces them (the file upload below),
        and then RFC 2047-encoded.
        """
        body: dict[str, Any] = {
            "topic": self.topic,
            "title": _truncate(msg.subject(), 250),
            "message": msg.text(limit=3500),
            "priority": int(_NTFY_PRIORITY.get(severity_name(msg.severity), "3")),
            "tags": [_NTFY_TAGS.get(severity_name(msg.severity), "warning")],
        }
        if msg.url:
            body["click"] = msg.url
        if msg.image_url and not msg.image:
            body["attach"] = msg.image_url
        # ntfy takes at most three actions and silently drops the rest,
        # so truncate here rather than letting the fourth vanish.
        if msg.actions:
            body["actions"] = [
                {"action": "view", "label": a.label[:40], "url": a.url}
                for a in msg.actions[:3]]
        return body

    def _auth(self) -> dict[str, str]:
        if self.token:
            return {"Authorization": f"Bearer {self.token}"}
        if self.user:
            raw = f"{self.user}:{self.password}".encode()
            return {"Authorization": "Basic " + base64.b64encode(raw).decode()}
        return {}

    def send(self, msg: Message) -> str | None:
        if msg.image:
            return self._send_with_photo(msg)
        self._post(self.server, what="ntfy", json=self._fields(msg),
                   headers=self._auth())
        return None

    def _send_with_photo(self, msg: Message) -> None:
        """A JPEG as the body is ntfy's file-upload form, and the only
        shape that takes one — so here the text really does have to
        travel in headers, and every one of them is RFC 2047-encoded
        because a plain non-ASCII header value raises before the request
        is even built."""
        url = f"{self.server}/{self.topic}"
        headers = {
            "X-Title": _rfc2047(_truncate(msg.subject(), 250)),
            "X-Priority": _NTFY_PRIORITY.get(severity_name(msg.severity), "3"),
            "X-Tags": _NTFY_TAGS.get(severity_name(msg.severity), "warning"),
            "X-Message": _rfc2047(_truncate(
                msg.text(limit=2000).replace("\n", "\\n"), 2000)),
            "X-Filename": "snapshot.jpg",
            "Content-Type": "image/jpeg",
        }
        if msg.url:
            headers["X-Click"] = msg.url
        if msg.actions:
            headers["X-Actions"] = "; ".join(
                f"view, {_ntfy_escape(a.label)}, {a.url}"
                for a in msg.actions[:3])
        headers.update(self._auth())
        try:
            resp = httpx.put(url, content=msg.image, headers=headers,
                             timeout=UPLOAD_TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001
            raise DeliveryError(f"ntfy: {exc}{_egress_hint(exc, url)}") from exc
        self._check(resp, what="ntfy")

    @property
    def can_probe(self) -> bool:
        """Only when there is a credential to check.

        On the public server the topic name IS the credential: there is
        no token to revoke and nothing that can silently stop working,
        so there is nothing a silent check could tell us. Publishing a
        "checking this still works" message twice a day to prove it
        would be notification spam from the app whose entire purpose is
        not sending notification spam — so this says it cannot check,
        which is the truth, and Send a test covers it.
        """
        return bool(self.token or self.user)

    def probe(self) -> None:
        if not (self.token or self.user):
            raise NotSupported(
                "an ntfy topic with no auth has no credential to check — "
                "use Test")
        # Reading the topic's own auth endpoint proves the credential
        # and publishes nothing.
        url = f"{self.server}/{self.topic}/auth"
        try:
            resp = httpx.get(url, headers=self._auth(),
                             timeout=TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001
            raise DeliveryError(f"ntfy: {exc}{_egress_hint(exc, url)}") from exc
        self._check(resp, what="ntfy")


def _rfc2047(value: str) -> str:
    """A header value httpx will not refuse.

    httpx encodes header values as ASCII and raises on anything else,
    so a non-ASCII camera name is not a mangled notification, it is a
    channel that fails for ever. ntfy decodes RFC 2047 in its own
    headers, so anything outside ASCII is encoded and anything inside
    is left alone (encoding everything would be unreadable in any
    client that did not decode it).
    """
    if value.isascii():
        return value
    encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
    return f"=?UTF-8?B?{encoded}?="


def _ntfy_escape(text: str) -> str:
    """ntfy's header action syntax is comma- and semicolon-separated, so
    a label containing either would silently split the action."""
    return text.replace(",", " ").replace(";", " ").strip()[:40] or "Open"


# ── Telegram ────────────────────────────────────────────────────────


class TelegramChannel(Channel):
    """Telegram — free, instant, and the one people already have.

    ``thread_id`` targets a forum topic, which makes "one group, one
    topic per camera" a clean routing model without ten bots.
    """

    kind = "telegram"
    can_edit = True
    can_attach = True
    can_act = True

    #: Telegram's own limits. A caption is a quarter the length of a
    #: message, which is exactly the trap: attach the photo and the text
    #: silently truncates unless we send the long form separately.
    TEXT_LIMIT = 4096
    CAPTION_LIMIT = 1024

    def __init__(self, name: str, spec: dict[str, Any]) -> None:
        super().__init__(name, spec)
        self.token = str(spec.get("bot_token") or "").strip()
        self.chat_id = str(spec.get("chat_id") or "").strip()
        self.thread_id = str(spec.get("thread_id") or "").strip()
        if not self.token:
            raise ValueError("telegram: bot_token is required")
        if not self.chat_id:
            raise ValueError("telegram: chat_id is required (use Link a chat)")

    @property
    def address(self) -> str:
        chat = self.chat_id
        if self.thread_id:
            chat = f"{chat} / topic {self.thread_id}"
        return f"chat {chat}"

    def _api(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    def _base(self, msg: Message) -> dict[str, Any]:
        data: dict[str, Any] = {"chat_id": self.chat_id}
        if self.thread_id:
            data["message_thread_id"] = self.thread_id
        # Below the bar the operator set, deliver silently rather than
        # not at all: the message is in the chat in the morning.
        if severity_rank(msg.severity) <= SEVERITY_RANK["low"]:
            data["disable_notification"] = True
        if msg.actions:
            data["reply_markup"] = json.dumps({
                "inline_keyboard": [[{"text": a.label[:40], "url": a.url}]
                                    for a in msg.actions[:3]]})
        return data

    def send(self, msg: Message) -> str | None:
        if msg.image:
            data = self._base(msg)
            data["caption"] = msg.text(limit=self.CAPTION_LIMIT)
            resp = self._post(self._api("sendPhoto"), what="telegram",
                              data=data,
                              files={"photo": ("snapshot.jpg", msg.image,
                                               "image/jpeg")},
                              timeout=UPLOAD_TIMEOUT_SECONDS)
        else:
            data = self._base(msg)
            data["text"] = msg.text(limit=self.TEXT_LIMIT)
            data["disable_web_page_preview"] = True
            resp = self._post(self._api("sendMessage"), what="telegram",
                              data=data)
        return _telegram_message_id(resp)

    def edit(self, msg: Message, handle: str) -> None:
        """Replace the message in place.

        A photo message's text lives in ``caption``, a plain one's in
        ``text``, and calling the wrong editor is a 400 — so the handle
        records which kind it was.
        """
        kind, _, mid = handle.partition(":")
        if not mid:
            kind, mid = "text", handle
        # No message_thread_id: the edit methods address a message by
        # id and do not take it.
        data: dict[str, Any] = {"chat_id": self.chat_id, "message_id": mid}
        if msg.actions:
            data["reply_markup"] = json.dumps({
                "inline_keyboard": [[{"text": a.label[:40], "url": a.url}]
                                    for a in msg.actions[:3]]})
        if kind == "photo":
            data["caption"] = msg.text(limit=self.CAPTION_LIMIT)
            resp = httpx.post(self._api("editMessageCaption"), data=data,
                              timeout=TIMEOUT_SECONDS)
        else:
            data["text"] = msg.text(limit=self.TEXT_LIMIT)
            # Kept from the original send, or the edit grows a link
            # preview the message did not have.
            data["disable_web_page_preview"] = True
            resp = httpx.post(self._api("editMessageText"), data=data,
                              timeout=TIMEOUT_SECONDS)
        self._check(resp, what="telegram", gone_is_fatal=False)

    def probe(self) -> None:
        """``getMe`` proves the token, ``getChat`` proves the chat is
        still reachable — a bot removed from a group has a perfectly
        valid token and cannot deliver a thing."""
        self._post(self._api("getMe"), what="telegram")
        self._post(self._api("getChat"), what="telegram",
                   data={"chat_id": self.chat_id})


def _telegram_message_id(resp: Any) -> str | None:
    try:
        result = (resp.json() or {}).get("result") or {}
    except Exception:  # noqa: BLE001
        return None
    mid = result.get("message_id")
    if mid is None:
        return None
    kind = "photo" if result.get("photo") else "text"
    return f"{kind}:{mid}"


# ── Pushover ────────────────────────────────────────────────────────

_PUSHOVER_PRIORITY = {"info": -1, "low": -1, "medium": 0,
                      "high": 1, "critical": 2}


class PushoverChannel(Channel):
    """Pushover — the cheapest real escalation in this list.

    ``critical`` maps to Pushover's emergency priority, which re-alerts
    every ``retry`` seconds until a human acknowledges it on the device
    or ``expire`` runs out. That is "keep waking someone up until they
    tap", which no NVR ships and which costs a one-off $5.
    """

    kind = "pushover"
    can_attach = True
    can_act = True

    API = "https://api.pushover.net/1/messages.json"
    VALIDATE = "https://api.pushover.net/1/users/validate.json"
    #: Pushover's own floor and ceiling for emergency re-alerting.
    MIN_RETRY = 30
    MAX_EXPIRE = 10800

    def __init__(self, name: str, spec: dict[str, Any]) -> None:
        super().__init__(name, spec)
        self.token = str(spec.get("token") or "").strip()
        self.user = str(spec.get("user_key") or "").strip()
        self.device = str(spec.get("device") or "").strip()
        self.retry = _clamp_int(spec.get("retry"), self.MIN_RETRY,
                                self.MIN_RETRY, self.MAX_EXPIRE)
        self.expire = _clamp_int(spec.get("expire"), 600,
                                 self.MIN_RETRY, self.MAX_EXPIRE)
        if not self.token:
            raise ValueError("pushover: token (the application token) is required")
        if not self.user:
            raise ValueError("pushover: user_key is required")

    @property
    def address(self) -> str:
        return f"user …{self.user[-4:]}" + (f" / {self.device}" if self.device else "")

    def send(self, msg: Message) -> str | None:
        priority = _PUSHOVER_PRIORITY.get(severity_name(msg.severity), 0)
        data: dict[str, Any] = {
            "token": self.token,
            "user": self.user,
            "title": _truncate(msg.subject(), 250),
            "message": msg.text(limit=1024),
            "priority": priority,
            "timestamp": int(time.time()),
        }
        if self.device:
            data["device"] = self.device
        if msg.url:
            data["url"] = msg.url
            data["url_title"] = "Open in OpenNVR"
        if priority == 2:
            # Emergency REQUIRES both, and Pushover rejects the message
            # outright if either is missing or out of range.
            data["retry"] = self.retry
            data["expire"] = self.expire
        files = None
        if msg.image:
            files = {"attachment": ("snapshot.jpg", msg.image, "image/jpeg")}
        resp = self._post(
            self.API, what="pushover", data=data, files=files,
            timeout=UPLOAD_TIMEOUT_SECONDS if files else TIMEOUT_SECONDS)
        try:
            return (resp.json() or {}).get("receipt") or None
        except Exception:  # noqa: BLE001
            return None

    def probe(self) -> None:
        self._post(self.VALIDATE, what="pushover",
                   data={"token": self.token, "user": self.user})


def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        num = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, num))


# ── Discord ─────────────────────────────────────────────────────────

_DISCORD_COLOUR = {"info": 0x5865F2, "low": 0x5865F2, "medium": 0xFEE75C,
                   "high": 0xED4245, "critical": 0x992D22}


class DiscordChannel(Channel):
    """Discord incoming webhook.

    Buttons are deliberately absent: ``components`` are restricted for
    non-application webhooks, so a button here would not render.
    """

    kind = "discord"
    can_edit = True
    can_attach = True

    def __init__(self, name: str, spec: dict[str, Any]) -> None:
        super().__init__(name, spec)
        self.url = str(spec.get("webhook_url") or "").strip()
        self.thread_id = str(spec.get("thread_id") or "").strip()
        if not self.url.startswith("https://"):
            raise ValueError("discord: webhook_url must be an https:// URL")

    @property
    def address(self) -> str:
        return _redact_url(self.url)

    def _embed(self, msg: Message) -> dict[str, Any]:
        fields = []
        if msg.camera:
            fields.append({"name": "Camera", "value": msg.camera[:1024],
                           "inline": True})
        fields.append({"name": "Severity",
                       "value": severity_name(msg.severity).upper(),
                       "inline": True})
        if msg.group > 1:
            fields.append({
                "name": f"{msg.group} alerts in this burst",
                "value": "\n".join(f"• {t}" for _s, t, _c in msg.members[:6])[:1024],
                "inline": False})
        embed: dict[str, Any] = {
            "title": _truncate(msg.subject(), 250),
            "description": _truncate(msg.body or msg.title, 4000),
            "color": _DISCORD_COLOUR.get(severity_name(msg.severity), 0xED4245),
            "fields": fields,
        }
        if msg.when:
            embed["footer"] = {"text": msg.when[:2048]}
        if msg.url:
            embed["url"] = msg.url
        if msg.image:
            embed["image"] = {"url": "attachment://snapshot.jpg"}
        elif msg.image_url:
            embed["image"] = {"url": msg.image_url}
        return embed

    @property
    def _base(self) -> str:
        """The webhook URL with any query string removed.

        A pasted URL can carry one, and appending ``/messages/{id}``
        after a query produces ``…/tok?thread_id=5/messages/m1``, which
        is a 404 — and a 404 used to condemn the whole channel."""
        parts = urlsplit(self.url)
        return f"{parts.scheme}://{parts.netloc}{parts.path}"

    def _query(self, *, wait: bool) -> str:
        parts = []
        if wait:
            parts.append("wait=true")
        if self.thread_id:
            parts.append(f"thread_id={quote(self.thread_id)}")
        return "?" + "&".join(parts) if parts else ""

    def _target(self, *, wait: bool) -> str:
        return self._base + self._query(wait=wait)

    def send(self, msg: Message) -> str | None:
        payload = {"embeds": [self._embed(msg)]}
        if msg.image:
            resp = self._post(
                self._target(wait=True), what="discord",
                data={"payload_json": json.dumps(payload)},
                files={"files[0]": ("snapshot.jpg", msg.image, "image/jpeg")},
                timeout=UPLOAD_TIMEOUT_SECONDS)
        else:
            resp = self._post(self._target(wait=True), what="discord",
                              json=payload)
        try:
            return (resp.json() or {}).get("id") or None
        except Exception:  # noqa: BLE001
            return None

    def edit(self, msg: Message, handle: str) -> None:
        # Editing drops the attachment rather than re-uploading it: the
        # original photo stays on the message, and re-POSTing bytes on
        # every update is how you get rate-limited.
        payload = {"embeds": [self._embed(msg)]}
        if msg.image:
            payload["embeds"][0].pop("image", None)
        # thread_id belongs on the edit too: without it Discord cannot
        # find a message that lives inside a thread, and answers 404.
        url = (f"{self._base}/messages/{quote(handle)}"
               + self._query(wait=False))
        try:
            resp = httpx.patch(url, json=payload, timeout=TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001
            raise DeliveryError(f"discord: {exc}{_egress_hint(exc, url)}") from exc
        self._check(resp, what="discord", gone_is_fatal=False)

    def probe(self) -> None:
        """GET on a webhook URL returns its metadata and sends nothing —
        the cleanest silent check of the ten."""
        try:
            resp = httpx.get(self._base, timeout=TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001
            raise DeliveryError(
                f"discord: {exc}{_egress_hint(exc, self.url)}") from exc
        self._check(resp, what="discord")


def _redact_url(url: str) -> str:
    """A URL safe to show and to log.

    Webhook URLs ARE credentials — a Slack or Discord hook in a
    screenshot is a working send-anything token."""
    try:
        parts = urlsplit(url)
    except Exception:  # noqa: BLE001
        return "(invalid url)"
    host = parts.hostname or "?"
    segments = [s for s in (parts.path or "").split("/") if s]
    if not segments:
        return host
    # Every segment is redacted, not just the last one. The secret is
    # not reliably at the end: a relay whose path is
    # /<token>/send has its token FIRST, and printing "the first
    # segment" there puts a working credential in a screenshot.
    return f"{host}/…({len(segments)} path segments hidden)"


# ── Slack ───────────────────────────────────────────────────────────

_SLACK_EMOJI = {"info": ":information_source:", "low": ":information_source:",
                "medium": ":warning:", "high": ":rotating_light:",
                "critical": ":rotating_light:"}


class SlackChannel(Channel):
    """Slack incoming webhook.

    Three hard limits worth knowing before you wire this up as your only
    channel: an incoming webhook cannot upload a file, cannot receive a
    button press, and cannot change the channel it posts to. The image
    therefore has to be a URL Slack's servers can fetch, which on a
    home NVR usually means no image at all — the app says so on the page
    rather than showing a broken thumbnail.
    """

    kind = "slack"
    can_act = True  # url buttons render; clicks just open a link

    def __init__(self, name: str, spec: dict[str, Any]) -> None:
        super().__init__(name, spec)
        self.url = str(spec.get("webhook_url") or "").strip()
        if not self.url.startswith("https://"):
            raise ValueError("slack: webhook_url must be an https:// URL")

    @property
    def address(self) -> str:
        return _redact_url(self.url)

    def send(self, msg: Message) -> str | None:
        emoji = _SLACK_EMOJI.get(severity_name(msg.severity), ":warning:")
        blocks: list[dict[str, Any]] = [
            {"type": "section",
             "text": {"type": "mrkdwn",
                      "text": f"{emoji} *{_slack_escape(msg.subject())}*"}},
            {"type": "context", "elements": [
                {"type": "mrkdwn",
                 "text": _slack_escape(
                     " · ".join(p for p in (
                         severity_name(msg.severity).upper(),
                         msg.camera, msg.when) if p))}]},
        ]
        if msg.body:
            blocks.insert(1, {"type": "section",
                              "text": {"type": "mrkdwn",
                                       "text": _slack_escape(
                                           _truncate(msg.body, 2900))}})
        if msg.image_url:
            blocks.append({"type": "image", "image_url": msg.image_url,
                           "alt_text": "snapshot"})
        if msg.actions:
            blocks.append({"type": "actions", "elements": [
                {"type": "button",
                 "text": {"type": "plain_text", "text": a.label[:75]},
                 "url": a.url} for a in msg.actions[:5]]})
        self._post(self.url, what="slack",
                   json={"text": _slack_escape(msg.text(limit=1500)),
                         "blocks": blocks})
        return None

    def probe(self) -> None:
        raise NotSupported(
            "Slack incoming webhooks have no check that sends nothing — "
            "use Test, which posts a real message")

    can_probe = False


def _slack_escape(text: str) -> str:
    """Slack eats ``&``, ``<`` and ``>``. A camera called "Gate <East>"
    otherwise renders as an empty string."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ── Microsoft Teams ─────────────────────────────────────────────────

_TEAMS_COLOUR = {"info": "good", "low": "good", "medium": "warning",
                 "high": "attention", "critical": "attention"}


class TeamsChannel(Channel):
    """Microsoft Teams, via Power Automate Workflows.

    The Office 365 connector this used to mean was retired on
    22 May 2026 and legacy connector URLs no longer deliver. The
    replacement is a Workflows webhook, created from the "Post to a
    channel when a webhook request is received" template, which takes
    an Adaptive Card. This channel emits an Adaptive Card; a legacy
    connector URL will simply fail, which is the truth rather than a
    message nobody receives.
    """

    kind = "teams"
    can_attach = False

    def __init__(self, name: str, spec: dict[str, Any]) -> None:
        super().__init__(name, spec)
        self.url = str(spec.get("webhook_url") or "").strip()
        if not self.url.startswith("https://"):
            raise ValueError("teams: webhook_url must be an https:// URL")

    @property
    def address(self) -> str:
        return _redact_url(self.url)

    def send(self, msg: Message) -> str | None:
        facts = [{"title": "Severity",
                  "value": severity_name(msg.severity).upper()}]
        if msg.camera:
            facts.append({"title": "Camera", "value": msg.camera})
        if msg.when:
            facts.append({"title": "When", "value": msg.when})
        if msg.group > 1:
            facts.append({"title": "In this burst", "value": str(msg.group)})
        body: list[dict[str, Any]] = [
            {"type": "TextBlock", "text": _truncate(msg.subject(), 250),
             "weight": "Bolder", "size": "Large", "wrap": True,
             "color": _TEAMS_COLOUR.get(severity_name(msg.severity), "attention")},
            {"type": "FactSet", "facts": facts},
        ]
        if msg.body:
            body.insert(1, {"type": "TextBlock",
                            "text": _truncate(msg.body, 2000), "wrap": True})
        if msg.image_url:
            body.append({"type": "Image", "url": msg.image_url,
                         "altText": "snapshot"})
        card: dict[str, Any] = {
            "type": "AdaptiveCard",
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "version": "1.4",
            "body": body,
        }
        if msg.actions:
            card["actions"] = [{"type": "Action.OpenUrl", "title": a.label[:60],
                                "url": a.url} for a in msg.actions[:5]]
        self._post(self.url, what="teams", json={
            "type": "message",
            "attachments": [{
                "contentType": "application/vnd.microsoft.card.adaptive",
                "contentUrl": None,
                "content": card}]})
        return None

    def probe(self) -> None:
        raise NotSupported(
            "A Teams Workflows webhook has no silent check — use Test")

    can_probe = False


# ── Gotify ──────────────────────────────────────────────────────────

_GOTIFY_PRIORITY = {"info": 2, "low": 2, "medium": 5, "high": 8, "critical": 9}


class GotifyChannel(Channel):
    """Gotify — self-hosted, no account, no third party.

    Gotify has no file upload, so a snapshot has to be a URL its client
    can fetch; on a LAN-only install that is usually fine and is exactly
    the case the other hosted channels cannot serve.
    """

    kind = "gotify"

    def __init__(self, name: str, spec: dict[str, Any]) -> None:
        super().__init__(name, spec)
        self.server = str(spec.get("server") or "").strip().rstrip("/")
        self.token = str(spec.get("token") or "").strip()
        if not self.server.startswith(("http://", "https://")):
            raise ValueError("gotify: server must be an http(s):// URL")
        if not self.token:
            raise ValueError("gotify: token (an application token) is required")

    @property
    def address(self) -> str:
        return self.server

    def send(self, msg: Message) -> str | None:
        extras: dict[str, Any] = {
            "client::display": {"contentType": "text/plain"}}
        if msg.url:
            extras["client::notification"] = {"click": {"url": msg.url}}
        if msg.image_url:
            extras.setdefault("client::notification", {})
            extras["client::notification"]["bigImageUrl"] = msg.image_url
        resp = self._post(
            f"{self.server}/message", what="gotify",
            headers={"X-Gotify-Key": self.token},
            json={"title": _truncate(msg.subject(), 250),
                  "message": msg.text(limit=3000),
                  "priority": _GOTIFY_PRIORITY.get(
                      severity_name(msg.severity), 5),
                  "extras": extras})
        try:
            mid = (resp.json() or {}).get("id")
            return str(mid) if mid is not None else None
        except Exception:  # noqa: BLE001
            return None

    def probe(self) -> None:
        """``/current/application`` identifies the app token itself and
        sends no message."""
        url = f"{self.server}/current/application"
        try:
            resp = httpx.get(url, headers={"X-Gotify-Key": self.token},
                             timeout=TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001
            raise DeliveryError(f"gotify: {exc}{_egress_hint(exc, url)}") from exc
        self._check(resp, what="gotify")


# ── Matrix ──────────────────────────────────────────────────────────


class MatrixChannel(Channel):
    """Matrix — the only channel here whose send is idempotent.

    Matrix requires a client-chosen transaction id and guarantees the
    same id never posts twice, so a retry after a timeout cannot produce
    a duplicate. We feed the dedup key into it and get exactly-once for
    free.
    """

    kind = "matrix"
    can_edit = True
    can_attach = True

    def __init__(self, name: str, spec: dict[str, Any]) -> None:
        super().__init__(name, spec)
        self.server = str(spec.get("server") or "").strip().rstrip("/")
        self.token = str(spec.get("access_token") or "").strip()
        self.room = str(spec.get("room_id") or "").strip()
        if not self.server.startswith(("http://", "https://")):
            raise ValueError("matrix: server must be an http(s):// URL")
        if not self.token:
            raise ValueError("matrix: access_token is required")
        if not self.room:
            raise ValueError("matrix: room_id is required (the !abc:server form)")

    @property
    def address(self) -> str:
        return f"{self.server} {self.room}"

    def _head(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def _room_path(self, txn: str) -> str:
        return (f"{self.server}/_matrix/client/v3/rooms/"
                f"{quote(self.room, safe='')}/send/m.room.message/{quote(txn)}")

    def _put(self, url: str, body: dict[str, Any], *, what: str) -> Any:
        try:
            resp = httpx.put(url, json=body, headers=self._head(),
                             timeout=TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001
            raise DeliveryError(f"{what}: {exc}{_egress_hint(exc, url)}") from exc
        self._check(resp, what=what)
        return resp

    def send(self, msg: Message) -> str | None:
        # message_id, not dedup_key. dedup_key names the SITUATION and
        # is reused for the next alert on the same camera, so seeding
        # the transaction id from it makes Matrix recognise a brand-new
        # alert as a replay of an old one: it returns the original event
        # id, posts nothing, and this app records a successful delivery
        # that never happened.
        txn = _txn_id(msg.message_id or msg.dedup_key)
        if msg.image:
            mxc = self._upload(msg.image)
            if mxc:
                self._put(self._room_path(txn + "-img"),
                          {"msgtype": "m.image", "body": "snapshot.jpg",
                           "url": mxc}, what="matrix")
        resp = self._put(self._room_path(txn),
                         {"msgtype": "m.text", "body": msg.text(limit=3000)},
                         what="matrix")
        try:
            return (resp.json() or {}).get("event_id") or None
        except Exception:  # noqa: BLE001
            return None

    def _upload(self, jpeg: bytes) -> str | None:
        url = f"{self.server}/_matrix/media/v3/upload?filename=snapshot.jpg"
        try:
            resp = httpx.post(url, content=jpeg,
                              headers={**self._head(),
                                       "Content-Type": "image/jpeg"},
                              timeout=UPLOAD_TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001
            logger.warning("matrix media upload failed: %s", exc)
            return None
        if resp.status_code in (401, 403):
            raise AuthFailed("matrix rejected our access token on media upload")
        if resp.status_code >= 400:
            # Losing the photo must not lose the alert.
            logger.warning("matrix media upload → HTTP %d", resp.status_code)
            return None
        try:
            return (resp.json() or {}).get("content_uri") or None
        except Exception:  # noqa: BLE001
            return None

    def edit(self, msg: Message, handle: str) -> None:
        text = msg.text(limit=3000)
        self._put(self._room_path(
            _txn_id(f"{msg.message_id or msg.dedup_key}:e{msg.group}")), {
            "msgtype": "m.text",
            "body": "* " + text,
            "m.new_content": {"msgtype": "m.text", "body": text},
            "m.relates_to": {"rel_type": "m.replace", "event_id": handle},
        }, what="matrix")

    def probe(self) -> None:
        url = f"{self.server}/_matrix/client/v3/account/whoami"
        try:
            resp = httpx.get(url, headers=self._head(), timeout=TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001
            raise DeliveryError(f"matrix: {exc}{_egress_hint(exc, url)}") from exc
        self._check(resp, what="matrix")


def _txn_id(seed: str) -> str:
    """A Matrix transaction id: stable for the same message, unique
    otherwise. Sanitised because it goes in a URL path.

    Hashed rather than truncated. A long rule name plus a camera id
    runs past any sane length limit, and truncating collides — two
    different messages would get one id, and Matrix would post only the
    first while reporting success for both.
    """
    import hashlib

    clean = re.sub(r"[^A-Za-z0-9_-]", "_", seed)
    if not clean:
        return uuid.uuid4().hex
    if len(clean) <= 48:
        return clean
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]
    return f"{clean[:32]}-{digest}"


# ── Email ───────────────────────────────────────────────────────────


class EmailChannel(Channel):
    """SMTP — the universal fallback, and the only one that still works
    when every SaaS in this file has had a bad day.

    The snapshot is attached inline (``cid:``) so it renders in the
    client rather than arriving as a file nobody opens, and an alert's
    updates thread into one conversation via ``References``.
    """

    kind = "email"
    can_attach = True

    def __init__(self, name: str, spec: dict[str, Any]) -> None:
        super().__init__(name, spec)
        self.host = str(spec.get("host") or "").strip()
        self.port = _clamp_int(spec.get("port"), 587, 1, 65535)
        self.username = str(spec.get("username") or "").strip()
        self.password = str(spec.get("password") or "")
        self.sender = str(spec.get("from") or self.username).strip()
        raw = spec.get("to") or []
        self.to = [str(x).strip() for x in
                   (raw if isinstance(raw, list) else str(raw).split(","))
                   if str(x).strip()]
        mode = str(spec.get("encryption") or "starttls").lower().strip()
        if mode not in ("starttls", "ssl", "none"):
            raise ValueError("email: encryption must be starttls, ssl or none")
        self.encryption = mode
        if not self.host:
            raise ValueError("email: host is required")
        if not self.sender:
            raise ValueError("email: from (or username) is required")
        if not self.to:
            raise ValueError("email: to is required")

    @property
    def address(self) -> str:
        return f"{self.host}:{self.port} → {', '.join(self.to[:3])}" + (
            f" +{len(self.to) - 3}" if len(self.to) > 3 else "")

    def _connect(self) -> Any:
        server = None
        try:
            if self.encryption == "ssl":
                server = smtplib.SMTP_SSL(self.host, self.port,
                                          timeout=TIMEOUT_SECONDS,
                                          context=ssl.create_default_context())
            else:
                server = smtplib.SMTP(self.host, self.port,
                                      timeout=TIMEOUT_SECONDS)
                if self.encryption == "starttls":
                    server.starttls(context=ssl.create_default_context())
        except Exception as exc:  # noqa: BLE001
            # The socket is already open when starttls fails — an
            # expired certificate would otherwise leak one file
            # descriptor per attempt, three per alert with retries, and
            # the eventual EMFILE surfaces as failures in UNRELATED
            # channels.
            if server is not None:
                _close_quietly(server)
            raise DeliveryError(f"email: cannot reach {self.host}: {exc}") from exc
        if self.username:
            try:
                server.login(self.username, self.password)
            except smtplib.SMTPAuthenticationError as exc:
                _close_quietly(server)
                raise AuthFailed(f"email: {self.host} rejected the login: {exc}") from exc
            except Exception as exc:  # noqa: BLE001
                _close_quietly(server)
                raise DeliveryError(f"email: login failed: {exc}") from exc
        return server

    def send(self, msg: Message) -> str | None:
        mail = EmailMessage()
        mail["Subject"] = f"[{severity_name(msg.severity).upper()}] {msg.subject()}"
        mail["From"] = self.sender
        mail["To"] = ", ".join(self.to)
        thread = f"<{_txn_id(msg.dedup_key or uuid.uuid4().hex)}@opennvr>"
        mail["Message-ID"] = f"<{uuid.uuid4().hex}@opennvr>"
        # Threading on the DEDUP key, not the message id: an alert's
        # updates land in one conversation instead of nine.
        mail["References"] = thread
        mail["In-Reply-To"] = thread
        text = msg.text(limit=8000)
        mail.set_content(text)
        if msg.image:
            # A cid on a plain-text message is just an attachment
            # nobody opens. An HTML alternative that REFERENCES the cid
            # is what makes the snapshot render in the message body,
            # which at 3am is the whole point of attaching it.
            import html as _html

            body = _html.escape(text).replace("\n", "<br>")
            mail.add_alternative(
                f"<html><body><p style=\"font-family:sans-serif\">{body}</p>"
                f"<img src=\"cid:snapshot\" alt=\"snapshot\" "
                f"style=\"max-width:100%\"></body></html>",
                subtype="html")
            mail.get_payload()[1].add_related(
                msg.image, maintype="image", subtype="jpeg",
                filename="snapshot.jpg", cid="<snapshot>")
        server = self._connect()
        try:
            server.send_message(mail)
        except Exception as exc:  # noqa: BLE001
            raise DeliveryError(f"email: send failed: {exc}") from exc
        finally:
            _close_quietly(server)
        return None

    def probe(self) -> None:
        """Connect, negotiate TLS, authenticate, hang up. Sends no mail
        and is the single best early warning of an expired app password."""
        _close_quietly(self._connect())


def _close_quietly(server: Any) -> None:
    try:
        server.quit()
    except Exception:  # noqa: BLE001
        try:
            server.close()
        except Exception:  # noqa: BLE001
            pass


# ── Generic webhook ─────────────────────────────────────────────────


class WebhookChannel(Channel):
    """Anything with a URL: Home Assistant, a siren relay, a SIEM, an
    SMS gateway's HTTP API, n8n, Node-RED, an Apprise API instance.

    Sends the whole alert plus a ready-made ``message`` string, so the
    receiving end can use either without parsing ours.
    """

    kind = "webhook"

    def __init__(self, name: str, spec: dict[str, Any]) -> None:
        super().__init__(name, spec)
        self.url = str(spec.get("url") or "").strip()
        self.method = str(spec.get("method") or "POST").upper().strip()
        headers = spec.get("headers") or {}
        self.headers = {str(k): str(v) for k, v in headers.items()} \
            if isinstance(headers, dict) else {}
        if not self.url.startswith(("http://", "https://")):
            raise ValueError("webhook: url must be an http(s):// URL")
        if self.method not in ("POST", "PUT", "GET"):
            raise ValueError("webhook: method must be POST, PUT or GET")

    @property
    def address(self) -> str:
        return _redact_url(self.url)

    def payload(self, msg: Message) -> dict[str, Any]:
        return {
            "message": msg.text(),
            "title": msg.title,
            "severity": severity_name(msg.severity),
            "camera": msg.camera,
            "when": msg.when,
            "url": msg.url,
            "group_size": msg.group,
            "dedup_key": msg.dedup_key,
            "alerts": [{"severity": s, "title": t, "camera": c}
                       for s, t, c in msg.members],
        }

    def send(self, msg: Message) -> str | None:
        if self.method == "GET":
            # A dumb relay that switches on whatever request arrives.
            try:
                resp = httpx.get(self.url, headers=self.headers,
                                 timeout=TIMEOUT_SECONDS)
            except Exception as exc:  # noqa: BLE001
                raise DeliveryError(
                    f"webhook: {exc}{_egress_hint(exc, self.url)}") from exc
            self._check(resp, what="webhook")
            return None
        body = self.payload(msg)
        if self.method == "PUT":
            try:
                resp = httpx.put(self.url, json=body, headers=self.headers,
                                 timeout=TIMEOUT_SECONDS)
            except Exception as exc:  # noqa: BLE001
                raise DeliveryError(
                    f"webhook: {exc}{_egress_hint(exc, self.url)}") from exc
            self._check(resp, what="webhook")
            return None
        self._post(self.url, what="webhook", json=body, headers=self.headers)
        return None

    def probe(self) -> None:
        raise NotSupported(
            "A generic webhook has no credential check — use Test")

    can_probe = False


# ── Building them ───────────────────────────────────────────────────

CHANNEL_TYPES: dict[str, type[Channel]] = {
    "ntfy": NtfyChannel,
    "telegram": TelegramChannel,
    "pushover": PushoverChannel,
    "discord": DiscordChannel,
    "slack": SlackChannel,
    "teams": TeamsChannel,
    "gotify": GotifyChannel,
    "matrix": MatrixChannel,
    "email": EmailChannel,
    "webhook": WebhookChannel,
}

#: Which config keys each type accepts. Used to reject a typo at load
#: time — ``bot_token`` misspelled is otherwise a channel that builds
#: fine, reports healthy, and 401s at 3am.
CHANNEL_FIELDS: dict[str, tuple[str, ...]] = {
    "ntfy": ("server", "topic", "token", "username", "password"),
    "telegram": ("bot_token", "chat_id", "thread_id"),
    "pushover": ("token", "user_key", "device", "retry", "expire"),
    "discord": ("webhook_url", "thread_id"),
    "slack": ("webhook_url",),
    "teams": ("webhook_url",),
    "gotify": ("server", "token"),
    "matrix": ("server", "access_token", "room_id"),
    "email": ("host", "port", "username", "password", "from", "to",
              "encryption"),
    "webhook": ("url", "method", "headers"),
}

#: Keys that are not a channel's own configuration.
_COMMON_FIELDS = ("type", "name", "enabled")

#: Hosts each channel type needs to reach, for the catalog's egress
#: declaration. Self-hosted types are absent on purpose: their host is
#: whatever the operator typed, and the catalog cannot know it.
CHANNEL_EGRESS: dict[str, tuple[str, ...]] = {
    "ntfy": ("ntfy.sh",),
    "telegram": ("api.telegram.org",),
    "pushover": ("api.pushover.net",),
    "discord": ("discord.com",),
    "slack": ("hooks.slack.com",),
    "teams": ("prod-*.logic.azure.com",),
}


def build_channel(name: str, spec: Any) -> Channel:
    """One channel from its config block. Raises ``ValueError`` with a
    message an operator can act on."""
    if isinstance(spec, str):
        # A bare URL is a webhook — the 1.0 config shape.
        spec = {"type": "webhook", "url": spec}
    if not isinstance(spec, dict):
        raise ValueError(f"channel {name!r}: expected a mapping")
    kind = str(spec.get("type") or "").lower().strip()
    if not kind:
        raise ValueError(
            f"channel {name!r}: type is required "
            f"(one of {', '.join(sorted(CHANNEL_TYPES))})")
    cls = CHANNEL_TYPES.get(kind)
    if cls is None:
        raise ValueError(
            f"channel {name!r}: unknown type {kind!r} — "
            f"expected one of {', '.join(sorted(CHANNEL_TYPES))}")
    allowed = set(CHANNEL_FIELDS.get(kind, ())) | set(_COMMON_FIELDS)
    unknown = sorted(set(spec) - allowed)
    if unknown:
        raise ValueError(
            f"channel {name!r} ({kind}): unknown setting(s) "
            f"{', '.join(unknown)} — accepts "
            f"{', '.join(sorted(CHANNEL_FIELDS.get(kind, ())))}")
    return cls(name, spec)


def suggested_topic() -> str:
    """A random ntfy topic.

    Long on purpose. On the public server the topic name is the only
    thing standing between an alert stream and anyone who guesses it, so
    an operator must never be invited to type ``home``.
    """
    return "opennvr-" + uuid.uuid4().hex[:16]
