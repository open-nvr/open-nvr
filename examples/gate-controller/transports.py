# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""
How this app actually touches a barrier.

One question decides everything here: **what does the gate operator
accept as "open now"?** In the field the answer is nearly always a
momentary volt-free contact — the same two terminals a push button, a
key switch or a loop detector is wired to. Everything below is a way
of closing that contact:

====================  ===============================================
``dry_contact``       A GPIO pin on the host driving a relay module.
                      The universal answer: every barrier ever made
                      takes a contact closure, no network required.
``http``              An IP relay (Shelly, Tasmota, ESPHome, most
                      commercial gate relays) with a trigger URL.
``modbus``            Modbus TCP coil — industrial controllers and a
                      lot of parking equipment.
``onvif``             ONVIF Profile C ``AccessDoor`` — any conformant
                      door controller, no vendor code needed.
``mqtt``              A broker topic, for sites already running one.
====================  ===============================================

Two rules shape every transport:

**We request, we do not command.** Under UL 325 and EN 12453 the
entrapment protection lives in the gate operator, which must monitor
its own safety devices every cycle. This app is an *accessory input*
in that model — the same class as the push button. So a transport
offers a momentary ``pulse()``; a latching ``hold()`` is optional and
a transport that cannot truly latch says so rather than faking it by
re-pulsing, because re-pulsing a momentary input is how you make a
barrier close on a car.

**Knowing is better than assuming.** ``read_state()`` returns the real
position when the wiring can report it (a limit switch on a GPIO input,
a Modbus discrete input, an ONVIF door monitor) and ``None`` when it
cannot. The page shows "not reported" rather than a confident "closed"
in that case, because a barrier that failed to close is exactly the
situation where a made-up state does harm.

Dependencies: none beyond ``httpx``, which the app already has. Modbus
TCP is 12 bytes on a socket and ONVIF is a SOAP POST, so both are
written out here rather than pulling a library in for one frame each.
MQTT is the exception — it needs a client, so that transport is
available only when ``paho-mqtt`` is installed and says so clearly
when it is not.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
import secrets
import socket
import struct
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger("gate-controller.transport")

#: Barriers are near-field devices. If the relay has not answered in
#: three seconds it is not going to, and a car is waiting.
DEFAULT_TIMEOUT = 3.0

#: How long the contact stays closed. 500 ms is the value gate
#: operators document for a button press; too short and some operators
#: debounce it away, too long and a few read it as "hold".
DEFAULT_PULSE_MS = 500


class OpenerError(Exception):
    """The barrier could not be actuated. Always a fault, never a crash."""


class NotSupported(OpenerError):
    """This transport genuinely cannot do that (e.g. latch open)."""


class StuckClosed(OpenerError):
    """The contact closed and did not release.

    Worse than a failed open and the opposite shape: the barrier is
    probably UP and staying up, not down with a car waiting. Callers
    say so rather than reporting "did not open".
    """


# ── Base ────────────────────────────────────────────────────────────


@dataclass
class Opener:
    """One barrier's wiring. Subclasses implement ``_pulse``."""

    kind: str = "none"
    #: Human-readable wiring, shown on the page so an installer can
    #: check the right gate against the right terminals.
    address: str = ""
    #: The product this was configured as, when a profile was used.
    vendor: str = ""
    pulse_ms: int = DEFAULT_PULSE_MS
    timeout: float = DEFAULT_TIMEOUT
    #: True when this wiring can latch open and be released again.
    can_hold: bool = False

    def pulse(self) -> None:
        """Close the contact for ``pulse_ms``, then release it."""
        raise NotSupported(f"{self.kind} cannot pulse")

    def hold(self, on: bool) -> None:
        """Latch open (``on``) or release. Only when ``can_hold``."""
        raise NotSupported(
            f"{self.kind} is momentary-only: it can ask the operator to open, "
            f"but it cannot hold the barrier up. Wire a latching output "
            f"(a second relay channel, a GPIO line, a Modbus coil) to hold.")

    def read_state(self) -> str | None:
        """``"open"`` / ``"closed"``, or None when nothing reports it."""
        return None

    def close(self) -> None:
        """Release any resource held for the lifetime of the app."""


# ── Dry contact (GPIO) ──────────────────────────────────────────────


@dataclass
class DryContactOpener(Opener):
    """A GPIO line on the host driving a relay module.

    The most universal option and the cheapest: a two-quid opto-isolated
    relay board between a Pi/mini-PC header and the operator's button
    terminals opens literally any barrier on the market, with no network
    between the decision and the gate.

    Uses the kernel's libgpiod character device when the ``gpiod``
    Python binding is installed, and falls back to the legacy sysfs
    interface otherwise — sysfs is deprecated upstream but still present
    on the SBC images most sites actually run.

    ``sense_line`` is an optional input wired to the operator's limit
    switch or its "gate open" auxiliary contact. With it, the gate is
    monitored and the page tells the truth about the barrier's real
    position; without it, the position is assumed from the last command.
    """

    chip: str = "gpiochip0"
    line: int = -1
    active_low: bool = False
    sense_line: int | None = None
    sense_active_low: bool = False
    hold_line: int | None = None
    kind: str = "dry_contact"
    can_hold: bool = False

    _gpiod: Any = field(default=None, repr=False)
    #: line offset -> the live libgpiod request/line object. HELD OPEN
    #: for the life of the app on purpose: releasing a libgpiod request
    #: reverts the line to its default state, so a driven value only
    #: persists while the request does. Letting it go after set_value
    #: made hold() a no-op that still reported success, and shrank
    #: pulse() to microseconds regardless of pulse_ms.
    _lines: dict = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if self.line < 0:
            raise OpenerError("dry_contact needs a 'line' (BCM/offset number)")
        self.can_hold = self.hold_line is not None
        self.address = f"{self.chip} · line {self.line}" + (
            f" · sense {self.sense_line}" if self.sense_line is not None else "")
        try:
            import gpiod  # type: ignore
            self._gpiod = gpiod
        except Exception:
            self._gpiod = None

    # sysfs fallback ------------------------------------------------

    @staticmethod
    def _sysfs_export(line: int) -> str:
        path = f"/sys/class/gpio/gpio{line}"
        if not os.path.isdir(path):
            with open("/sys/class/gpio/export", "w") as fh:
                fh.write(str(line))
            # The udev rule that chowns the new node is not instant.
            for _ in range(20):
                if os.path.isdir(path):
                    break
                time.sleep(0.01)
        return path

    def _sysfs_write(self, line: int, value: int, direction: str = "out") -> None:
        path = self._sysfs_export(line)
        with open(f"{path}/direction", "w") as fh:
            fh.write(direction)
        with open(f"{path}/value", "w") as fh:
            fh.write("1" if value else "0")

    def _sysfs_read(self, line: int) -> int:
        path = self._sysfs_export(line)
        with open(f"{path}/direction", "w") as fh:
            fh.write("in")
        with open(f"{path}/value") as fh:
            return int(fh.read().strip() or "0")

    # ---------------------------------------------------------------

    def _set(self, line: int, asserted: bool, active_low: bool) -> None:
        value = (0 if asserted else 1) if active_low else (1 if asserted else 0)
        if self._gpiod is not None:
            try:
                self._write_gpiod(line, value)
                return
            except Exception as exc:
                logger.debug("gpiod write failed, falling back to sysfs: %s", exc)
        try:
            self._sysfs_write(line, value)
        except OSError as exc:
            raise OpenerError(
                f"GPIO line {line} on {self.chip} is not writable ({exc}). "
                f"Run the app with access to the GPIO device, or use an "
                f"HTTP/Modbus relay instead.") from exc

    def _write_gpiod(self, line: int, value: int) -> None:
        """Drive ``line`` and KEEP the request, so the value persists.

        libgpiod hands a line back to the kernel when its request is
        released, and the kernel restores the line's default state. A
        request/set/release cycle therefore drives the pin for
        microseconds — which silently turned hold() into a no-op and
        made pulse_ms meaningless. The request is cached per line and
        released only in close().
        """
        gpiod = self._gpiod
        entry = self._lines.get(line)
        if entry is None:
            if hasattr(gpiod, "request_lines"):      # 2.x
                settings = gpiod.LineSettings(
                    direction=gpiod.line.Direction.OUTPUT)
                entry = ("v2", gpiod.request_lines(
                    f"/dev/{self.chip}", consumer="opennvr-gate",
                    config={line: settings}))
            else:                                     # 1.x
                chip = gpiod.Chip(self.chip)
                ln = chip.get_line(line)
                ln.request(consumer="opennvr-gate",
                           type=gpiod.LINE_REQ_DIR_OUT)
                entry = ("v1", (chip, ln))
            self._lines[line] = entry
        api, handle = entry
        if api == "v2":
            handle.set_value(line, gpiod.line.Value(value))
        else:
            handle[1].set_value(value)

    def pulse(self) -> None:
        self._set(self.line, True, self.active_low)
        time.sleep(max(0.02, self.pulse_ms / 1000.0))
        try:
            self._set(self.line, False, self.active_low)
        except OpenerError as exc:
            # The contact closed and did not re-open. That is a gate
            # stuck OPEN, not a gate that failed to open, and the two
            # need different words in the alert.
            raise StuckClosed(
                f"contact closed but did not release ({exc}) — the gate may "
                f"be held open") from exc

    def hold(self, on: bool) -> None:
        if self.hold_line is None:
            raise NotSupported(
                "no 'hold_line' configured — wire a second relay channel to "
                "the operator's hold-open / free-exit input to hold this gate.")
        self._set(self.hold_line, on, self.active_low)

    def read_state(self) -> str | None:
        if self.sense_line is None:
            return None
        try:
            raw = self._sysfs_read(self.sense_line)
        except OSError:
            return None
        asserted = (raw == 0) if self.sense_active_low else (raw == 1)
        return "open" if asserted else "closed"

    def close(self) -> None:
        """Hand every held line back to the kernel."""
        for api, handle in self._lines.values():
            try:
                if api == "v2":
                    handle.release()
                else:
                    handle[1].release()
                    handle[0].close()
            except Exception:  # pragma: no cover - shutdown is best effort
                logger.debug("releasing GPIO line failed", exc_info=True)
        self._lines.clear()


# ── HTTP relay ──────────────────────────────────────────────────────


@dataclass
class HttpOpener(Opener):
    """An IP relay reachable over HTTP.

    ``url`` is the trigger. ``off_url`` makes the transport latching —
    with both, "hold open" is honest rather than a repeated pulse. Many
    relays can do the momentary part themselves (Shelly's ``timer``,
    Tasmota's ``PulseTime``), which is better than us sleeping between
    two calls, so the profiles below use that where it exists.

    ``status_url`` + ``status_open_when`` make the gate monitored: the
    response body is searched for that string.
    """

    url: str = ""
    method: str = "GET"
    off_url: str = ""
    status_url: str = ""
    status_open_when: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    verify_tls: bool = True
    #: True when the relay does the timing itself; we then skip our own
    #: off-call after the pulse.
    self_timed: bool = False
    kind: str = "http"

    #: Query parameters that mean "the relay times its own pulse".
    _SELF_TIMING_HINTS = ("timer=", "toggle_after=", "pulsetime", "pulse_time",
                          "relay1=2", "duration=")

    def __post_init__(self) -> None:
        if not self.url:
            raise OpenerError("http transport needs a 'url'")
        self.can_hold = bool(self.off_url)
        self.address = _host_of(self.url)
        if not self.self_timed and any(hint in self.url.lower()
                                       for hint in self._SELF_TIMING_HINTS):
            # A 1.0-style bare URL that already carries the relay's own
            # auto-off parameter is genuinely self-timing; believe it.
            self.self_timed = True
        if not self.self_timed and not self.off_url:
            logger.warning(
                "gate relay %s has no off_url and no auto-off parameter in "
                "its URL: nothing will release the contact after the pulse, "
                "so the barrier may stay open. Add off_url, or use the "
                "relay's own timer (Shelly timer=/toggle_after=, Tasmota "
                "PulseTime). See HARDWARE.md.", self.address)

    def _call(self, url: str) -> None:
        try:
            fn = httpx.post if self.method.upper() == "POST" else httpx.get
            resp = fn(url, timeout=self.timeout, headers=self.headers or None,
                      verify=self.verify_tls)
        except Exception as exc:  # noqa: BLE001 — a relay problem is a fault
            raise OpenerError(f"{_host_of(url)} did not answer: {exc}") from exc
        if not 200 <= resp.status_code < 300:
            raise OpenerError(
                f"{_host_of(url)} answered HTTP {resp.status_code}")

    def pulse(self) -> None:
        # Device-side timing is always preferred: Shelly's toggle_after,
        # Tasmota's PulseTime and an ESPHome on_turn_on automation all
        # survive the network dropping mid-pulse. The two-call path
        # below does not — if the off-call is lost the contact stays
        # closed — so it shouts rather than returning quietly.
        self._call(self.url)
        if self.self_timed or not self.off_url:
            return
        time.sleep(max(0.02, self.pulse_ms / 1000.0))
        try:
            self._call(self.off_url)
        except OpenerError as exc:
            raise StuckClosed(
                f"contact closed but did not release ({exc}) — the gate may "
                f"be held open") from exc

    def hold(self, on: bool) -> None:
        if not self.off_url:
            raise NotSupported(
                "no 'off_url' configured — this relay is momentary-only, so "
                "the app cannot hold it open.")
        self._call(self.url if on else self.off_url)

    def read_state(self) -> str | None:
        if not self.status_url:
            return None
        try:
            resp = httpx.get(self.status_url, timeout=self.timeout,
                             headers=self.headers or None, verify=self.verify_tls)
        except Exception:
            return None
        if not 200 <= resp.status_code < 300:
            return None
        needle = self.status_open_when or '"output":true'
        return "open" if needle.lower() in resp.text.lower() else "closed"


# ── Modbus TCP ──────────────────────────────────────────────────────


@dataclass
class ModbusOpener(Opener):
    """A Modbus TCP coil.

    Industrial gate controllers, parking equipment and most PLC-fronted
    barriers speak this. Write Single Coil (function 0x05) is the whole
    of the write path and Read Discrete Inputs (0x02) is the whole of
    the read path, so the frames are built here rather than adding a
    Modbus library for twelve bytes.
    """

    host: str = ""
    port: int = 502
    unit: int = 1
    coil: int = 0
    hold_coil: int | None = None
    sense_input: int | None = None
    kind: str = "modbus"

    def __post_init__(self) -> None:
        if not self.host:
            raise OpenerError("modbus transport needs a 'host'")
        self.can_hold = self.hold_coil is not None
        self.address = f"{self.host}:{self.port} · unit {self.unit} · coil {self.coil}"

    def _txn(self, pdu: bytes) -> bytes:
        """One command, one connection. Embedded Modbus slaves cap
        concurrent connections hard (ControlByWeb allows two, and drops
        an idle one after 50 s), so a long-lived socket per barrier is
        how a site runs out of them."""
        tid = secrets.randbelow(0xFFFF)
        frame = struct.pack(">HHHB", tid, 0, len(pdu) + 1, self.unit) + pdu
        try:
            with socket.create_connection((self.host, self.port), self.timeout) as sock:
                sock.settimeout(self.timeout)
                sock.sendall(frame)
                header = _recv_exactly(sock, 8)
                rx_tid, proto, length, unit, fc = struct.unpack(">HHHBB", header)
                # Validate before trusting the wire. A garbage or hostile
                # peer advertising length=0xFFFF would otherwise have us
                # block for the whole socket timeout reading 65 KB —
                # which, on the event loop, stalls every other gate too.
                if proto != 0 or rx_tid != tid or unit != self.unit:
                    raise OpenerError(
                        f"{self.host}:{self.port} answered a frame that does "
                        f"not match the request (transaction {rx_tid} vs "
                        f"{tid}, unit {unit} vs {self.unit})")
                if not 2 <= length <= 260:
                    raise OpenerError(
                        f"{self.host}:{self.port} declared an implausible "
                        f"frame length ({length})")
                body = _recv_exactly(sock, length - 2)
        except OpenerError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise OpenerError(
                f"{self.host}:{self.port} did not answer: {exc}"
                + _egress_hint(exc)) from exc
        if fc & 0x80:
            code = body[0] if body else 0
            raise OpenerError(
                f"Modbus exception {code} from {self.host} "
                f"(function {fc & 0x7F:#04x})")
        return body

    def _write_coil(self, coil: int, on: bool) -> None:
        self._txn(struct.pack(">BHH", 0x05, coil, 0xFF00 if on else 0x0000))

    def pulse(self) -> None:
        self._write_coil(self.coil, True)
        time.sleep(max(0.02, self.pulse_ms / 1000.0))
        try:
            self._write_coil(self.coil, False)
        except OpenerError as exc:
            raise StuckClosed(
                f"coil {self.coil} was set but could not be cleared ({exc}) — "
                f"the gate may be held open") from exc

    def hold(self, on: bool) -> None:
        if self.hold_coil is None:
            raise NotSupported(
                "no 'hold_coil' configured — point it at the controller's "
                "hold-open coil to hold this gate.")
        self._write_coil(self.hold_coil, on)

    def read_state(self) -> str | None:
        if self.sense_input is None:
            return None
        try:
            body = self._txn(struct.pack(">BHH", 0x02, self.sense_input, 1))
        except OpenerError:
            return None
        if len(body) < 2:
            return None
        return "open" if body[1] & 0x01 else "closed"


# ── ONVIF Profile C ─────────────────────────────────────────────────


_ONVIF_ENV = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">'
    "{header}"
    "<s:Body>{body}</s:Body></s:Envelope>"
)
_ONVIF_DC = "http://www.onvif.org/ver10/doorcontrol/wsdl"


@dataclass
class OnvifOpener(Opener):
    """An ONVIF Profile C door controller.

    Profile C is the vendor-neutral access-control profile: site
    information, door access control, and event/alarm management. A
    conformant controller accepts ``AccessDoor`` (momentary release),
    ``LockDoor`` / ``UnlockDoor`` (latching) and answers
    ``GetDoorState`` with the real physical position — which makes an
    ONVIF gate the only transport here that is monitored and holdable
    with no extra wiring.

    Worth preferring where a site already has a door controller: no
    vendor integration, no second credential store, and the barrier
    shows up as a door in whatever else speaks Profile C.
    """

    url: str = ""
    username: str = ""
    password: str = ""
    door_token: str = ""
    verify_tls: bool = True
    kind: str = "onvif"
    can_hold: bool = True

    def __post_init__(self) -> None:
        if not self.url or not self.door_token:
            raise OpenerError("onvif transport needs 'url' and 'door_token'")
        self.address = f"{_host_of(self.url)} · door {self.door_token}"

    def _security_header(self) -> str:
        if not self.username:
            return ""
        nonce = secrets.token_bytes(16)
        created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        digest = base64.b64encode(hashlib.sha1(
            nonce + created.encode() + self.password.encode()).digest()).decode()
        return (
            "<s:Header><Security s:mustUnderstand=\"1\" xmlns=\"http://docs."
            "oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0"
            ".xsd\"><UsernameToken><Username>" + _xml(self.username) +
            "</Username><Password Type=\"http://docs.oasis-open.org/wss/2004/"
            "01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest\">" +
            digest + "</Password><Nonce EncodingType=\"http://docs.oasis-open."
            "org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0"
            "#Base64Binary\">" + base64.b64encode(nonce).decode() +
            "</Nonce><Created xmlns=\"http://docs.oasis-open.org/wss/2004/01/"
            "oasis-200401-wss-wssecurity-utility-1.0.xsd\">" + created +
            "</Created></UsernameToken></Security></s:Header>")

    def _soap(self, body: str) -> str:
        envelope = _ONVIF_ENV.format(header=self._security_header(), body=body)
        try:
            resp = httpx.post(
                self.url, content=envelope.encode(), timeout=self.timeout,
                headers={"Content-Type": "application/soap+xml; charset=utf-8"},
                verify=self.verify_tls)
        except Exception as exc:  # noqa: BLE001
            raise OpenerError(f"{_host_of(self.url)} did not answer: {exc}") from exc
        if resp.status_code == 401:
            raise OpenerError(
                f"{_host_of(self.url)} rejected the credentials — check the "
                f"ONVIF user has the Operator role for door control")
        if not 200 <= resp.status_code < 300:
            fault = re.search(r"<[^>]*Text[^>]*>(.*?)</", resp.text, re.S)
            raise OpenerError(
                f"{_host_of(self.url)} answered HTTP {resp.status_code}"
                + (f": {fault.group(1).strip()[:120]}" if fault else ""))
        return resp.text

    def pulse(self) -> None:
        self._soap(
            f'<AccessDoor xmlns="{_ONVIF_DC}"><Token>{_xml(self.door_token)}'
            f"</Token><UseExtendedTime>false</UseExtendedTime></AccessDoor>")

    def hold(self, on: bool) -> None:
        verb = "UnlockDoor" if on else "LockDoor"
        self._soap(f'<{verb} xmlns="{_ONVIF_DC}"><Token>'
                   f"{_xml(self.door_token)}</Token></{verb}>")

    def read_state(self) -> str | None:
        """Profile C makes ``DoorMode`` mandatory but ``DoorPhysicalState``
        CONDITIONAL — only devices with a door monitor report it. So a
        fully conformant controller may still not know whether the
        barrier moved, and this correctly answers None there rather than
        reporting the mode we asked for as though it were the position."""
        try:
            text = self._soap(f'<GetDoorState xmlns="{_ONVIF_DC}"><Token>'
                              f"{_xml(self.door_token)}</Token></GetDoorState>")
        except OpenerError:
            return None
        match = re.search(r"<[^>]*DoorPhysicalState[^>]*>\s*(\w+)", text)
        if not match:
            return None
        physical = match.group(1).lower()
        if physical == "open":
            return "open"
        if physical == "closed":
            return "closed"
        return None


# ── MQTT ────────────────────────────────────────────────────────────


@dataclass
class MqttOpener(Opener):
    """A broker topic, for sites already running MQTT.

    Needs ``paho-mqtt``. Unlike the other transports this one cannot be
    written out in a few lines, so when the client is missing the
    transport refuses at config time with a message that says what to
    install rather than failing at the first car.
    """

    host: str = ""
    port: int = 1883
    topic: str = ""
    payload_on: str = "ON"
    payload_off: str = "OFF"
    state_topic: str = ""
    username: str = ""
    password: str = ""
    kind: str = "mqtt"

    _client: Any = field(default=None, repr=False)
    _last_state: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if not self.host or not self.topic:
            raise OpenerError("mqtt transport needs 'host' and 'topic'")
        try:
            import paho.mqtt.client as mqtt  # type: ignore
        except Exception as exc:
            raise OpenerError(
                "the mqtt transport needs paho-mqtt: pip install paho-mqtt "
                "(or use the http transport, which most MQTT relays also "
                "expose)") from exc
        self._mqtt = mqtt
        self.can_hold = True
        self.address = f"{self.host}:{self.port} · {self.topic}"

    def _new_client(self) -> Any:
        """paho 2.x requires a callback API version; 1.x has no such
        argument. Construct for whichever is installed — getting this
        wrong meant the gate built and booted fine and then failed on
        every single open, which is the worst time to find out."""
        mqtt = self._mqtt
        version = getattr(mqtt, "CallbackAPIVersion", None)
        if version is not None:                    # paho-mqtt 2.x
            return mqtt.Client(version.VERSION1)
        return mqtt.Client()                       # paho-mqtt 1.x

    def _publish(self, payload: str) -> None:
        client = None
        try:
            client = self._new_client()
            if self.username:
                client.username_pw_set(self.username, self.password)
            # The third positional argument is KEEPALIVE, not a timeout.
            # Passing our 3-second budget there set a 3-second keepalive
            # and churned the broker, while leaving the connect itself
            # on paho's default.
            client.connect(self.host, self.port, keepalive=60)
            if self.state_topic:
                client.on_message = self._on_message
                client.subscribe(self.state_topic)
            info = client.publish(self.topic, payload, qos=1)
            info.wait_for_publish(self.timeout)
            client.loop(timeout=0.2)
        except Exception as exc:  # noqa: BLE001
            raise OpenerError(f"{self.host}:{self.port} — {exc}"
                              + _egress_hint(exc)) from exc
        finally:
            # Without this every failed open leaks a socket: a broker
            # that is down while cars keep arriving leaks one fd a car.
            if client is not None:
                try:
                    client.disconnect()
                except Exception:  # pragma: no cover - teardown only
                    pass

    def _on_message(self, _client: Any, _userdata: Any, message: Any) -> None:
        try:
            self._last_state = message.payload.decode().strip()
        except Exception:
            pass

    def pulse(self) -> None:
        self._publish(self.payload_on)
        time.sleep(max(0.02, self.pulse_ms / 1000.0))
        try:
            self._publish(self.payload_off)
        except OpenerError as exc:
            raise StuckClosed(
                f"{self.payload_on} was published but {self.payload_off} was "
                f"not ({exc}) — the gate may be held open") from exc

    def hold(self, on: bool) -> None:
        self._publish(self.payload_on if on else self.payload_off)

    def read_state(self) -> str | None:
        if not self.state_topic or self._last_state is None:
            return None
        return "open" if self._last_state.upper() == self.payload_on.upper() else "closed"


# ── Vendor profiles ─────────────────────────────────────────────────
#
# A profile is the difference between "read your relay's HTTP manual"
# and "say which product you bought". Each one fills in the URL shape,
# the latching pair and the status probe for a product we have the
# documented API for; ``host`` (and sometimes ``channel``) is all the
# operator supplies.
#
# These describe how to talk to a RELAY or CONTROLLER, not to a barrier:
# the barrier is whatever the relay's contacts are wired to, and any of
# them drives any barrier. The vendor list for the barriers themselves
# is in HARDWARE.md, because the answer there is always "dry contact".

PROFILES: dict[str, dict[str, Any]] = {
    # ── IP relays ───────────────────────────────────────────────────
    "shelly_gen2": {
        "transport": "http", "vendor": "Shelly (Gen2+ / Plus / Pro)",
        "url": "http://{host}/rpc/Switch.Set?id={channel}&on=true&toggle_after={pulse_s}",
        "off_url": "http://{host}/rpc/Switch.Set?id={channel}&on=false",
        "status_url": "http://{host}/rpc/Switch.GetStatus?id={channel}",
        "status_open_when": '"output":true',
        "self_timed": True,
        "note": "toggle_after does the momentary timing in the relay itself.",
    },
    "shelly_gen1": {
        "transport": "http", "vendor": "Shelly (Gen1)",
        "url": "http://{host}/relay/{channel}?turn=on&timer={pulse_s}",
        "off_url": "http://{host}/relay/{channel}?turn=off",
        "status_url": "http://{host}/relay/{channel}",
        "status_open_when": '"ison":true',
        "self_timed": True,
    },
    "tasmota": {
        "transport": "http", "vendor": "Tasmota (Sonoff and friends)",
        "url": "http://{host}/cm?cmnd=Power{channel1}%20ON",
        "off_url": "http://{host}/cm?cmnd=Power{channel1}%20OFF",
        # Deliberately NO status_url. Tasmota has a long-standing report
        # that querying Power DURING an active PulseTime window can
        # defeat the auto-off and leave the relay latched on
        # (arendst/Tasmota#7810, #4093). On a lamp that is a curiosity;
        # on a barrier it means holding the boom up. So a Tasmota gate
        # is unmonitored by choice — set PulseTime on the device and let
        # it do the timing.
        "note": "Set PulseTime<n> on the device (e.g. PulseTime1 105 = "
                "500 ms) so the contact is timed in hardware.",
    },
    "esphome": {
        "transport": "http", "vendor": "ESPHome",
        "url": "http://{host}/switch/{switch}/turn_on",
        "off_url": "http://{host}/switch/{switch}/turn_off",
        "status_url": "http://{host}/switch/{switch}",
        "status_open_when": '"state":"ON"',
        "method": "POST",
        "note": "Give the switch an on_turn_on/delay/turn_off automation so "
                "the contact is timed on the device, then set self_timed: "
                "true here. See HARDWARE.md.",
    },
    "generic_http": {
        "transport": "http", "vendor": "Generic HTTP relay",
        "note": "Give url, and off_url/status_url when the relay has them.",
    },
    # ── Dry contact ─────────────────────────────────────────────────
    "raspberry_pi": {
        "transport": "dry_contact", "vendor": "Raspberry Pi GPIO + relay board",
        "chip": "gpiochip0",
        "note": "BCM numbering. Most relay boards are active-low.",
        "active_low": True,
    },
    "generic_gpio": {
        "transport": "dry_contact", "vendor": "Host GPIO + relay board",
    },
    # ── Industrial ──────────────────────────────────────────────────
    "modbus_tcp": {
        "transport": "modbus", "vendor": "Modbus TCP controller",
        "note": "Coil = the operator's open input; sense_input = its open contact.",
    },
    "waveshare_modbus": {
        "transport": "modbus", "vendor": "Waveshare Modbus POE relay",
        "port": 502, "unit": 1,
    },
    # ── Access control ──────────────────────────────────────────────
    "onvif_door": {
        "transport": "onvif", "vendor": "ONVIF Profile C door controller",
        "url": "http://{host}/onvif/door_control",
        "note": "Monitored and holdable with no extra wiring. Axis A1601, "
                "2N, and any Profile C conformant controller.",
    },
    "axis_a1601": {
        "transport": "onvif", "vendor": "Axis A1601 Network Door Controller",
        "url": "http://{host}/vapix/doorcontrol",
    },
    # ── MQTT ────────────────────────────────────────────────────────
    "mqtt": {
        "transport": "mqtt", "vendor": "MQTT relay",
    },
}

#: Which config keys each transport accepts, so a typo is caught at
#: startup with a list of what was meant instead of ignored silently.
_FIELDS: dict[str, set[str]] = {
    "http": {"url", "method", "off_url", "status_url", "status_open_when",
             "headers", "verify_tls", "self_timed", "pulse_ms", "timeout"},
    "dry_contact": {"chip", "line", "active_low", "sense_line",
                    "sense_active_low", "hold_line", "pulse_ms", "timeout"},
    "modbus": {"host", "port", "unit", "coil", "hold_coil", "sense_input",
               "pulse_ms", "timeout"},
    "onvif": {"url", "username", "password", "door_token", "verify_tls",
              "pulse_ms", "timeout"},
    "mqtt": {"host", "port", "topic", "payload_on", "payload_off",
             "state_topic", "username", "password", "pulse_ms", "timeout"},
}

_CLASSES: dict[str, type[Opener]] = {
    "http": HttpOpener,
    "dry_contact": DryContactOpener,
    "modbus": ModbusOpener,
    "onvif": OnvifOpener,
    "mqtt": MqttOpener,
}


def build_opener(spec: Any) -> Opener:
    """Config for one gate → a ready transport.

    ``spec`` is a bare URL string (the 1.0 shape, still honoured) or a
    dict with ``transport`` and/or ``profile``.
    """
    if isinstance(spec, str):
        spec = {"transport": "http", "url": spec.strip()}
    if not isinstance(spec, dict):
        raise OpenerError("gate config must be a URL or a table")

    spec = {str(k): v for k, v in spec.items()}
    profile_name = str(spec.pop("profile", "") or "").strip().lower()
    vendor = ""
    if profile_name:
        profile = PROFILES.get(profile_name)
        if profile is None:
            raise OpenerError(
                f"unknown profile {profile_name!r}. Known profiles: "
                + ", ".join(sorted(PROFILES)))
        profile = dict(profile)
        profile.pop("note", None)
        vendor = str(profile.pop("vendor", ""))
        transport = str(spec.pop("transport", "") or profile.pop("transport"))
        merged = {**profile, **spec}
    else:
        transport = str(spec.pop("transport", "http"))
        merged = spec

    cls = _CLASSES.get(transport)
    if cls is None:
        raise OpenerError(
            f"unknown transport {transport!r}. Supported: "
            + ", ".join(sorted(_CLASSES)))

    merged.pop("transport", None)
    merged.pop("name", None)
    merged.pop("schedule", None)

    # Templated profile URLs: {host}, {channel}, {channel1}, {switch},
    # {pulse_s}. Substituted before the keys are handed to the class.
    host = str(merged.pop("host", "") or "")
    channel = merged.pop("channel", 0)
    switch = str(merged.pop("switch", "gate") or "gate")
    pulse_ms = int(merged.get("pulse_ms", DEFAULT_PULSE_MS) or DEFAULT_PULSE_MS)
    subs = {
        "host": host,
        "channel": channel,
        "channel1": (int(channel) + 1) if str(channel).isdigit() else channel,
        "switch": switch,
        "pulse_s": max(1, round(pulse_ms / 1000)),
    }
    for key in ("url", "off_url", "status_url", "topic", "state_topic"):
        value = merged.get(key)
        if isinstance(value, str) and "{" in value:
            # An empty substitution is not a substitution: it would
            # quietly produce http:///rpc/... and fail at the first car
            # instead of at startup, where it is cheap to fix.
            for field_name in re.findall(r"{(\w+)}", value):
                if not str(subs.get(field_name, "")).strip():
                    raise OpenerError(
                        f"profile {profile_name or transport!r} needs "
                        f"{field_name!r} (for example {field_name}: "
                        f"192.168.1.50)")
            try:
                merged[key] = value.format(**subs)
            except KeyError as exc:
                raise OpenerError(
                    f"profile {profile_name!r} needs {exc.args[0]!r} "
                    f"(for example host: 192.168.1.50)") from exc
    # Transports that take host directly rather than in a URL.
    if transport in ("modbus", "mqtt") and host:
        merged["host"] = host

    known = _FIELDS[transport]
    unknown = set(merged) - known
    if unknown:
        raise OpenerError(
            f"{transport}: unknown option(s) {', '.join(sorted(unknown))}. "
            f"Accepted: {', '.join(sorted(known))}")

    opener = cls(**merged)  # type: ignore[arg-type]
    if vendor:
        opener.vendor = vendor
    return opener


# ── Small helpers ───────────────────────────────────────────────────


def _recv_exactly(sock: socket.socket, count: int) -> bytes:
    chunks = b""
    while len(chunks) < count:
        piece = sock.recv(count - len(chunks))
        if not piece:
            raise OpenerError("connection closed mid-frame")
        chunks += piece
    return chunks


#: Errors that, inside the shipped compose, almost always mean the
#: apps network is ``internal`` rather than that the device is down.
_UNREACHABLE = ("unreachable", "no route", "name or service not known",
                "temporary failure in name resolution", "timed out")


def _egress_hint(exc: Exception) -> str:
    """Modbus and MQTT are raw TCP, and the app-egress proxy is HTTP
    CONNECT — it cannot carry them. On a default compose install the
    apps network is ``internal``, so these transports have no route to
    the LAN at all and the first symptom is an unhelpful socket error.
    Name the real cause rather than making somebody bisect it."""
    if any(needle in str(exc).lower() for needle in _UNREACHABLE):
        return (". If this app runs in the shipped compose stack, note that "
                "the apps network is internal and the egress proxy only "
                "carries HTTP — raw TCP transports (modbus, mqtt) need "
                "APPS_EGRESS_ENFORCED=false. See HARDWARE.md → Running in "
                "the shipped compose")
    return ""


def _host_of(url: str) -> str:
    match = re.match(r"^[a-z]+://([^/:]+)(:\d+)?", url or "", re.I)
    return (match.group(1) + (match.group(2) or "")) if match else (url or "")[:40]


def _xml(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))
