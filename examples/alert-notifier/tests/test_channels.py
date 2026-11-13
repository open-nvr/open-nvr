# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The ten channels: what goes on the wire, and what a failure means.

These tests are about the parts that only show up in production. That a
POST happens is uninteresting; that a 401 is classified as permanent and
a 429 as retriable, that a webhook URL never reaches a log, and that
severity turns into each service's own urgency scheme without the
operator setting four fields — those are the ones that decide whether a
phone buzzes at 3am.
"""
from __future__ import annotations

import json

import pytest

import channels as ch


# ── A fake httpx ────────────────────────────────────────────────────


class Resp:
    def __init__(self, status_code=200, body=None, headers=None):
        self.status_code = status_code
        self._body = body if body is not None else {}
        self.headers = headers or {}

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body

    @property
    def text(self):
        return json.dumps(self._body) if isinstance(
            self._body, (dict, list)) else str(self._body)


class FakeHttpx:
    """Records every call and replays queued responses."""

    def __init__(self):
        self.calls: list[dict] = []
        self.responses: list = []
        self.default = Resp()

    def _next(self, method, url, kwargs):
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            return self.default
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    def post(self, url, **kw):
        return self._next("POST", url, kw)

    def put(self, url, **kw):
        return self._next("PUT", url, kw)

    def get(self, url, **kw):
        return self._next("GET", url, kw)

    def patch(self, url, **kw):
        return self._next("PATCH", url, kw)


@pytest.fixture()
def http(monkeypatch):
    fake = FakeHttpx()
    monkeypatch.setattr(ch, "httpx", fake)
    return fake


def msg(**over) -> ch.Message:
    base = dict(title="Person at gate", body="loitering 8s", severity="high",
                camera="Front Door", when="02:14:03", dedup_key="k1")
    base.update(over)
    return ch.Message(**base)


JPEG = b"\xff\xd8\xff" + b"\x00" * 64


def _decode_header(value: str) -> str:
    """Undo RFC 2047, the way ntfy does."""
    from email.header import decode_header, make_header

    return str(make_header(decode_header(value)))


# ── Building and validating ─────────────────────────────────────────


class TestBuilding:
    def test_every_type_is_buildable(self):
        specs = {
            "ntfy": {"type": "ntfy", "topic": "opennvr-abcdef"},
            "telegram": {"type": "telegram", "bot_token": "T", "chat_id": "1"},
            "pushover": {"type": "pushover", "token": "a", "user_key": "b"},
            "discord": {"type": "discord",
                        "webhook_url": "https://discord.com/api/webhooks/1/x"},
            "slack": {"type": "slack",
                      "webhook_url": "https://hooks.slack.com/services/A/B/C"},
            "teams": {"type": "teams",
                      "webhook_url": "https://prod-1.logic.azure.com/x"},
            "gotify": {"type": "gotify", "server": "http://g:8080",
                       "token": "t"},
            "matrix": {"type": "matrix", "server": "https://m",
                       "access_token": "t", "room_id": "!r:m"},
            "email": {"type": "email", "host": "smtp.x", "from": "a@x",
                      "to": ["b@x"]},
            "webhook": {"type": "webhook", "url": "https://x/y"},
        }
        # Every advertised type builds; a type in the table with no spec
        # here is a type nobody proved works.
        assert set(specs) == set(ch.CHANNEL_TYPES)
        for name, spec in specs.items():
            assert ch.build_channel(name, spec).kind == name

    def test_a_bare_url_is_still_a_webhook(self):
        """1.0's ``notify_webhook_url`` shape."""
        built = ch.build_channel("hook", "https://example.com/hook")
        assert isinstance(built, ch.WebhookChannel)
        assert built.url == "https://example.com/hook"

    def test_a_typo_is_rejected_at_load_not_at_3am(self):
        with pytest.raises(ValueError, match="bot_toekn"):
            ch.build_channel("t", {"type": "telegram", "bot_toekn": "T",
                                   "chat_id": "1"})

    def test_unknown_type_names_the_alternatives(self):
        with pytest.raises(ValueError, match="pushbullet"):
            ch.build_channel("p", {"type": "pushbullet"})

    def test_missing_type_is_an_error(self):
        with pytest.raises(ValueError, match="type is required"):
            ch.build_channel("x", {"topic": "abc"})

    @pytest.mark.parametrize("topic", ["", "has spaces", "a" * 65, "a/b"])
    def test_ntfy_rejects_an_unusable_topic(self, topic):
        with pytest.raises(ValueError):
            ch.build_channel("n", {"type": "ntfy", "topic": topic})

    def test_suggested_topic_is_not_guessable(self):
        first, second = ch.suggested_topic(), ch.suggested_topic()
        assert first != second
        # On the public server the topic IS the credential.
        assert len(first) >= 20

    def test_webhook_rejects_a_non_http_url(self):
        with pytest.raises(ValueError, match="http"):
            ch.build_channel("w", {"type": "webhook", "url": "ftp://x/y"})


# ── Secrets never reach a log or a screenshot ───────────────────────


class TestRedaction:
    def test_a_discord_webhook_url_is_not_its_own_address(self):
        built = ch.build_channel(
            "d", {"type": "discord",
                  "webhook_url": "https://discord.com/api/webhooks/"
                                 "123/SUPERSECRETTOKENVALUE"})
        assert "SUPERSECRETTOKENVALUE" not in built.address
        assert "discord.com" in built.address

    def test_a_telegram_token_is_not_its_own_address(self):
        built = ch.build_channel("t", {"type": "telegram",
                                       "bot_token": "123:SECRET",
                                       "chat_id": "-100777"})
        assert "SECRET" not in built.address
        assert "-100777" in built.address

    def test_a_pushover_user_key_is_only_shown_by_its_tail(self):
        built = ch.build_channel("p", {"type": "pushover", "token": "T",
                                       "user_key": "uQiRzpo4DXghDmr9QzzfQu27cmVRsG"})
        assert "uQiRzpo4" not in built.address

    def test_an_error_body_cannot_dump_a_whole_request_back(self, http):
        # Some services echo the request — which contains the token.
        http.responses.append(Resp(500, "x" * 5000))
        built = ch.build_channel("w", {"type": "webhook",
                                       "url": "https://x/y"})
        with pytest.raises(ch.DeliveryError) as err:
            built.send(msg())
        assert len(str(err.value)) < 400


# ── Failure classification ──────────────────────────────────────────


class TestFailureClassification:
    """The distinction the whole health model rests on: a broken
    credential never heals, so retrying it is wasted and the channel
    must be condemned at once. Everything else might."""

    @pytest.mark.parametrize("status", [401, 403])
    def test_rejected_credentials_are_permanent(self, http, status):
        http.responses.append(Resp(status))
        built = ch.build_channel("w", {"type": "webhook", "url": "https://x/y"})
        with pytest.raises(ch.AuthFailed):
            built.send(msg())

    def test_a_deleted_webhook_is_permanent_too(self, http):
        """A 404 on a webhook is a webhook somebody deleted. Treating it
        as retriable means retrying for ever."""
        http.responses.append(Resp(404))
        built = ch.build_channel("d", {
            "type": "discord",
            "webhook_url": "https://discord.com/api/webhooks/1/x"})
        with pytest.raises(ch.AuthFailed):
            built.send(msg())

    def test_rate_limiting_carries_the_wait(self, http):
        http.responses.append(Resp(429, headers={"Retry-After": "17"}))
        built = ch.build_channel("w", {"type": "webhook", "url": "https://x/y"})
        with pytest.raises(ch.Throttled) as err:
            built.send(msg())
        assert err.value.retry_after == 17.0

    def test_a_500_is_retriable(self, http):
        http.responses.append(Resp(503))
        built = ch.build_channel("w", {"type": "webhook", "url": "https://x/y"})
        with pytest.raises(ch.DeliveryError) as err:
            built.send(msg())
        assert not isinstance(err.value, (ch.AuthFailed, ch.Throttled))

    def test_an_unreachable_host_names_the_egress_rule(self, http):
        """Apps run on an internal network behind a proxy that asks core
        whether THIS app may reach THIS host. A channel the operator has
        not allowed fails with a connection error, which reads like the
        service being down — so the error says otherwise."""
        http.responses.append(OSError("Name or service not known"))
        built = ch.build_channel("w", {"type": "webhook",
                                       "url": "https://ntfy.example.com/x"})
        with pytest.raises(ch.DeliveryError) as err:
            built.send(msg())
        text = str(err.value)
        assert "ntfy.example.com" in text and "allowed hosts" in text

    def test_an_ordinary_error_does_not_blame_egress(self, http):
        http.responses.append(ValueError("malformed payload"))
        built = ch.build_channel("w", {"type": "webhook", "url": "https://x/y"})
        with pytest.raises(ch.DeliveryError) as err:
            built.send(msg())
        assert "allowed hosts" not in str(err.value)


# ── ntfy ────────────────────────────────────────────────────────────


class TestNtfy:
    def _built(self, **over):
        spec = {"type": "ntfy", "topic": "opennvr-abcdef"}
        spec.update(over)
        return ch.build_channel("phone", spec)

    def test_it_publishes_as_JSON_not_as_headers(self, http):
        """httpx encodes header values as ASCII and RAISES on anything
        else — and the title this app composes contains an em dash, so
        a header-based ntfy channel fails on EVERY ordinary message,
        not just an exotic camera name."""
        self._built().send(msg())
        call = http.calls[0]
        assert call["method"] == "POST"
        assert call["json"]["topic"] == "opennvr-abcdef"
        assert "Person at gate" in call["json"]["title"]

    def test_a_non_ascii_camera_name_does_not_break_the_channel(self, http):
        built = self._built()
        built.send(msg(camera="Cámara Patio"))
        body = http.calls[0]["json"]
        assert "Cámara Patio" in body["title"]
        # And the real httpx would accept this request.
        import httpx as real_httpx

        real_httpx.Request("POST", "https://ntfy.sh", json=body)

    def test_severity_becomes_ntfys_own_priority(self, http):
        for severity, expected in (("low", 2), ("medium", 3),
                                   ("high", 4), ("critical", 5)):
            http.calls.clear()
            self._built().send(msg(severity=severity))
            assert http.calls[0]["json"]["priority"] == expected

    def test_an_unknown_severity_cannot_ring_a_phone_at_max(self, http):
        self._built().send(msg(severity="ARGH"))
        assert http.calls[0]["json"]["priority"] == 2

    def test_at_most_three_actions_because_ntfy_drops_the_rest(self, http):
        actions = [ch.Action(f"A{i}", f"https://x/{i}") for i in range(5)]
        self._built().send(msg(actions=actions))
        assert len(http.calls[0]["json"]["actions"]) == 3

    def test_a_photo_is_PUT_as_the_body_with_the_text_in_a_header(self, http):
        self._built().send(msg(image=JPEG))
        call = http.calls[0]
        assert call["method"] == "PUT"
        assert call["content"] == JPEG
        # The body is the file, so the message has to travel as a
        # header or it is silently lost.
        assert "Person at gate" in _decode_header(call["headers"]["X-Message"])
        assert call["headers"]["X-Filename"] == "snapshot.jpg"

    def test_photo_headers_are_rfc2047_encoded_so_httpx_accepts_them(
            self, http):
        """The one place the protocol forces headers on us."""
        self._built().send(msg(image=JPEG, camera="Cámara Patio"))
        headers = http.calls[0]["headers"]
        assert headers["X-Title"].startswith("=?UTF-8?B?")
        import httpx as real_httpx

        real_httpx.Request("PUT", "https://ntfy.sh/x", headers=headers)

    def test_a_pure_ascii_header_is_left_readable(self, http):
        """Encoding everything would be unreadable in any client that
        did not decode it."""
        self._built().send(msg(image=JPEG, title="Person", body="",
                               camera="", when=""))
        assert http.calls[0]["headers"]["X-Title"] == "Person"

    def test_a_label_with_a_comma_cannot_split_the_action(self, http):
        """ntfy's photo-path action header is comma- and
        semicolon-delimited, so an unescaped label silently becomes two
        broken actions."""
        self._built().send(msg(image=JPEG,
                               actions=[ch.Action("Mute, 1h", "https://x")]))
        header = http.calls[0]["headers"]["X-Actions"]
        assert header.count(",") == 2  # action, label, url — and no more

    def test_an_unauthenticated_topic_admits_it_cannot_self_check(self):
        """On the public server the topic name IS the credential: there
        is nothing to revoke, so there is nothing a silent check could
        tell us — and publishing "checking this works" twice a day
        would be notification spam from the anti-spam app."""
        built = self._built()
        assert built.can_probe is False
        with pytest.raises(ch.NotSupported):
            built.probe()

    def test_an_authenticated_topic_CAN_be_checked_silently(self, http):
        built = self._built(token="tk_abc")
        assert built.can_probe is True
        built.probe()
        assert http.calls[0]["method"] == "GET"
        assert http.calls[0]["url"].endswith("/auth")

    def test_a_token_authenticates_as_bearer(self, http):
        self._built(token="tk_abc").send(msg())
        assert http.calls[0]["headers"]["Authorization"] == "Bearer tk_abc"

    def test_a_username_authenticates_as_basic(self, http):
        self._built(username="u", password="p").send(msg())
        assert http.calls[0]["headers"]["Authorization"].startswith("Basic ")


# ── Telegram ────────────────────────────────────────────────────────


class TestTelegram:
    def _built(self, **over):
        spec = {"type": "telegram", "bot_token": "TOK", "chat_id": "42"}
        spec.update(over)
        return ch.build_channel("tg", spec)

    def test_a_photo_goes_to_sendPhoto_with_a_caption(self, http):
        http.responses.append(Resp(200, {"result": {"message_id": 7,
                                                    "photo": [{}]}}))
        handle = self._built().send(msg(image=JPEG))
        assert http.calls[0]["url"].endswith("/sendPhoto")
        assert "photo" in http.calls[0]["files"]
        # The handle records WHICH editor to use later; calling
        # editMessageText on a photo message is a 400.
        assert handle == "photo:7"

    def test_text_only_goes_to_sendMessage(self, http):
        http.responses.append(Resp(200, {"result": {"message_id": 9}}))
        assert self._built().send(msg()) == "text:9"
        assert http.calls[0]["url"].endswith("/sendMessage")

    def test_a_caption_respects_telegrams_shorter_limit(self, http):
        http.responses.append(Resp(200, {"result": {"message_id": 1,
                                                    "photo": [{}]}}))
        self._built().send(msg(image=JPEG, body="x" * 4000))
        assert len(http.calls[0]["data"]["caption"]) <= 1024

    def test_editing_a_photo_edits_its_caption(self, http):
        self._built().edit(msg(), "photo:7")
        assert http.calls[0]["url"].endswith("/editMessageCaption")
        assert "caption" in http.calls[0]["data"]

    def test_editing_text_edits_the_text(self, http):
        self._built().edit(msg(), "text:7")
        assert http.calls[0]["url"].endswith("/editMessageText")

    def test_a_handle_with_no_kind_is_treated_as_text(self, http):
        self._built().edit(msg(), "7")
        assert http.calls[0]["url"].endswith("/editMessageText")
        assert http.calls[0]["data"]["message_id"] == "7"

    def test_low_severity_arrives_without_a_buzz(self, http):
        self._built().send(msg(severity="low"))
        assert http.calls[0]["data"]["disable_notification"] is True
        http.calls.clear()
        self._built().send(msg(severity="high"))
        assert "disable_notification" not in http.calls[0]["data"]

    def test_a_forum_topic_is_targeted(self, http):
        self._built(thread_id="12").send(msg())
        assert http.calls[0]["data"]["message_thread_id"] == "12"

    def test_the_probe_checks_the_token_AND_the_chat(self, http):
        """A bot removed from a group has a perfectly valid token and
        cannot deliver a thing."""
        self._built().probe()
        assert [c["url"].rsplit("/", 1)[-1] for c in http.calls] == [
            "getMe", "getChat"]


# ── Pushover ────────────────────────────────────────────────────────


class TestPushover:
    def _built(self, **over):
        spec = {"type": "pushover", "token": "APP", "user_key": "USER"}
        spec.update(over)
        return ch.build_channel("po", spec)

    def test_critical_becomes_emergency_with_retry_and_expire(self, http):
        """Pushover REJECTS priority 2 without both, so the message
        would simply never arrive."""
        http.responses.append(Resp(200, {"receipt": "r1"}))
        assert self._built().send(msg(severity="critical")) == "r1"
        data = http.calls[0]["data"]
        assert data["priority"] == 2
        assert data["retry"] >= ch.PushoverChannel.MIN_RETRY
        assert 0 < data["expire"] <= ch.PushoverChannel.MAX_EXPIRE

    def test_non_emergency_does_not_send_retry_or_expire(self, http):
        self._built().send(msg(severity="high"))
        assert "retry" not in http.calls[0]["data"]

    def test_out_of_range_retry_is_clamped_not_passed_through(self, http):
        built = self._built(retry=1, expire=99999)
        assert built.retry == ch.PushoverChannel.MIN_RETRY
        assert built.expire == ch.PushoverChannel.MAX_EXPIRE

    def test_a_nonsense_retry_falls_back(self):
        assert self._built(retry="soon").retry == ch.PushoverChannel.MIN_RETRY

    def test_a_photo_is_attached(self, http):
        self._built().send(msg(image=JPEG))
        assert "attachment" in http.calls[0]["files"]


# ── Discord, Slack, Teams ───────────────────────────────────────────


class TestDiscord:
    def _built(self):
        return ch.build_channel("d", {
            "type": "discord",
            "webhook_url": "https://discord.com/api/webhooks/1/tok"})

    def test_a_photo_is_multipart_and_referenced_from_the_embed(self, http):
        http.responses.append(Resp(200, {"id": "m1"}))
        assert self._built().send(msg(image=JPEG)) == "m1"
        call = http.calls[0]
        assert "files[0]" in call["files"]
        payload = json.loads(call["data"]["payload_json"])
        assert payload["embeds"][0]["image"]["url"] == "attachment://snapshot.jpg"

    def test_wait_is_requested_so_we_get_an_id_to_edit(self, http):
        self._built().send(msg())
        assert "wait=true" in http.calls[0]["url"]

    def test_an_edit_does_not_re_upload_the_photo(self, http):
        """Re-POSTing bytes on every update is how you get rate-limited,
        and the original attachment stays on the message anyway."""
        self._built().edit(msg(image=JPEG), "m1")
        call = http.calls[0]
        assert call["method"] == "PATCH"
        assert "image" not in call["json"]["embeds"][0]

    def test_the_probe_sends_nothing(self, http):
        self._built().probe()
        assert http.calls[0]["method"] == "GET"


class TestSlack:
    def _built(self):
        return ch.build_channel("s", {
            "type": "slack",
            "webhook_url": "https://hooks.slack.com/services/A/B/C"})

    def test_angle_brackets_in_a_camera_name_are_escaped(self, http):
        """Slack eats ``<`` and ``>``; a camera called "Gate <East>"
        otherwise renders as nothing at all."""
        self._built().send(msg(camera="Gate <East>", title="Person & car"))
        body = json.dumps(http.calls[0]["json"])
        assert "&lt;East&gt;" in body and "&amp;" in body

    def test_it_says_plainly_that_it_cannot_check_itself(self):
        assert self._built().can_probe is False
        with pytest.raises(ch.NotSupported):
            self._built().probe()

    def test_it_does_not_claim_to_attach_bytes(self):
        # An incoming webhook cannot upload a file. Claiming otherwise
        # would put a broken thumbnail on the page.
        assert self._built().can_attach is False


class TestTeams:
    def _built(self):
        return ch.build_channel("t", {
            "type": "teams",
            "webhook_url": "https://prod-33.westeurope.logic.azure.com/x"})

    def test_it_emits_an_adaptive_card_not_a_dead_connector_card(self, http):
        """Office 365 connectors were retired on 22 May 2026; the
        replacement is a Power Automate Workflows webhook taking an
        Adaptive Card."""
        self._built().send(msg())
        body = http.calls[0]["json"]
        assert body["type"] == "message"
        attachment = body["attachments"][0]
        assert attachment["contentType"] == (
            "application/vnd.microsoft.card.adaptive")
        assert attachment["content"]["type"] == "AdaptiveCard"


# ── Gotify and Matrix ───────────────────────────────────────────────


class TestGotify:
    def _built(self):
        return ch.build_channel("g", {"type": "gotify",
                                      "server": "http://gotify:8080",
                                      "token": "AtOk"})

    def test_the_token_travels_in_a_header_not_the_url(self, http):
        self._built().send(msg())
        assert http.calls[0]["headers"]["X-Gotify-Key"] == "AtOk"
        assert "AtOk" not in http.calls[0]["url"]

    def test_severity_maps_onto_gotifys_scale(self, http):
        self._built().send(msg(severity="critical"))
        assert http.calls[0]["json"]["priority"] == 9


class TestMatrix:
    def _built(self):
        return ch.build_channel("m", {"type": "matrix",
                                      "server": "https://matrix.example",
                                      "access_token": "tok",
                                      "room_id": "!abc:example"})

    def test_the_transaction_id_is_stable_so_a_retry_cannot_duplicate(
            self, http):
        """Matrix guarantees one txn id posts once. Feeding it the dedup
        key makes the send exactly-once for free."""
        self._built().send(msg(dedup_key="rule|cam3"))
        first = http.calls[0]["url"]
        http.calls.clear()
        self._built().send(msg(dedup_key="rule|cam3"))
        assert http.calls[0]["url"] == first

    def test_a_room_id_is_escaped_into_the_path(self, http):
        self._built().send(msg())
        assert "%21abc%3Aexample" in http.calls[0]["url"]

    def test_a_failed_photo_upload_still_delivers_the_alert(self, http):
        """Losing the picture must never lose the alert."""
        http.responses.append(Resp(500))            # the upload
        http.responses.append(Resp(200, {"event_id": "$e"}))  # the message
        assert self._built().send(msg(image=JPEG)) == "$e"

    def test_a_rejected_token_on_upload_is_still_an_auth_failure(self, http):
        http.responses.append(Resp(401))
        with pytest.raises(ch.AuthFailed):
            self._built().send(msg(image=JPEG))


# ── Email ───────────────────────────────────────────────────────────


class FakeSMTP:
    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.started_tls = False
        self.logged_in = None
        self.sent = []
        self.quit_called = False
        FakeSMTP.instances.append(self)

    def starttls(self, context=None):
        self.started_tls = True

    def login(self, user, password):
        self.logged_in = (user, password)

    def send_message(self, mail):
        self.sent.append(mail)

    def quit(self):
        self.quit_called = True


class TestEmail:
    @pytest.fixture(autouse=True)
    def _smtp(self, monkeypatch):
        FakeSMTP.instances.clear()
        monkeypatch.setattr(ch.smtplib, "SMTP", FakeSMTP)
        monkeypatch.setattr(ch.smtplib, "SMTP_SSL", FakeSMTP)

    def _built(self, **over):
        spec = {"type": "email", "host": "smtp.example", "from": "nvr@example",
                "to": "guard@example, boss@example", "username": "u",
                "password": "p"}
        spec.update(over)
        return ch.build_channel("mail", spec)

    def test_a_comma_separated_list_is_split(self):
        assert self._built().to == ["guard@example", "boss@example"]

    def test_starttls_is_negotiated_by_default(self):
        self._built().send(msg())
        assert FakeSMTP.instances[0].started_tls is True

    def test_the_photo_is_attached_inline(self):
        self._built().send(msg(image=JPEG))
        mail = FakeSMTP.instances[0].sent[0]
        assert any(part.get_content_type() == "image/jpeg"
                   for part in mail.walk())

    def test_updates_thread_into_one_conversation(self):
        """Threading on the DEDUP key, not the message id: an alert's
        updates land in one conversation instead of nine."""
        built = self._built()
        built.send(msg(dedup_key="k"))
        built.send(msg(dedup_key="k"))
        refs = [m["References"] for m in
                FakeSMTP.instances[0].sent + FakeSMTP.instances[1].sent]
        assert refs[0] == refs[1]

    def test_a_rejected_login_is_permanent(self, monkeypatch):
        def boom(self, user, password):
            raise ch.smtplib.SMTPAuthenticationError(535, b"nope")

        monkeypatch.setattr(FakeSMTP, "login", boom)
        with pytest.raises(ch.AuthFailed):
            self._built().send(msg())

    def test_the_probe_connects_and_hangs_up_without_sending(self):
        self._built().probe()
        assert FakeSMTP.instances[0].sent == []
        assert FakeSMTP.instances[0].quit_called

    def test_no_recipients_is_rejected_at_build(self):
        with pytest.raises(ValueError, match="to is required"):
            ch.build_channel("m", {"type": "email", "host": "h",
                                   "from": "a@b", "to": []})


# ── Webhook ─────────────────────────────────────────────────────────


class TestWebhook:
    def test_the_payload_carries_both_a_string_and_the_structure(self, http):
        """So the far end can use either without parsing ours."""
        built = ch.build_channel("w", {"type": "webhook", "url": "https://x/y"})
        built.send(msg(group=3, members=[("high", "a", "c1"),
                                         ("low", "b", "c1")]))
        body = http.calls[0]["json"]
        assert "Person at gate" in body["message"]
        assert body["severity"] == "high"
        assert body["group_size"] == 3
        assert body["alerts"][0]["title"] == "a"

    def test_get_sends_no_body_for_a_dumb_relay(self, http):
        built = ch.build_channel("w", {"type": "webhook", "url": "https://x/y",
                                       "method": "GET"})
        built.send(msg())
        assert http.calls[0]["method"] == "GET"
        assert "json" not in http.calls[0]

    def test_custom_headers_are_sent(self, http):
        built = ch.build_channel("w", {"type": "webhook", "url": "https://x/y",
                                       "headers": {"X-Key": "abc"}})
        built.send(msg())
        assert http.calls[0]["headers"]["X-Key"] == "abc"

    def test_an_unsupported_method_is_rejected(self):
        with pytest.raises(ValueError, match="method"):
            ch.build_channel("w", {"type": "webhook", "url": "https://x/y",
                                   "method": "DELETE"})


# ── The message ─────────────────────────────────────────────────────


class TestMessage:
    def test_the_text_leads_with_severity_and_what_happened(self):
        text = msg().text()
        assert text.splitlines()[0] == "[HIGH] Person at gate"

    def test_a_grouped_message_says_how_many_and_lists_some(self):
        text = msg(group=4, members=[("high", "Person", "c"),
                                     ("medium", "Motion", "c"),
                                     ("medium", "Line crossed", "c"),
                                     ("low", "Car", "c")]).text()
        assert "+3 more" in text
        assert "Motion" in text

    def test_a_long_message_is_truncated_not_rejected(self):
        text = msg(body="x" * 10000).text(limit=200)
        assert len(text) <= 200

    def test_the_subject_names_the_camera_and_the_burst(self):
        assert msg(group=3).subject() == "Person at gate — Front Door (+2)"

    def test_severity_never_defaults_upward(self):
        # An unknown severity must not be able to be treated as critical.
        assert ch.severity_rank("banana") < ch.severity_rank("medium")
        assert ch.severity_name("banana") == "low"


# ── Regressions found in review ─────────────────────────────────────


class TestReviewRegressions:
    def test_a_matrix_txn_id_is_hashed_rather_than_truncated(self):
        """Truncation collides: two long, different seeds became one id,
        and Matrix would post only the first while reporting success for
        both."""
        long_a = "Perimeter cameras overnight escalation to the guard|cam1:1"
        long_b = "Perimeter cameras overnight escalation to the guard|cam1:2"
        assert ch._txn_id(long_a) != ch._txn_id(long_b)
        assert len(ch._txn_id(long_a)) <= 64

    def test_a_discord_edit_addresses_a_message_inside_its_thread(self, http):
        """Without thread_id Discord answers 404 — and a 404 used to
        condemn the whole channel as permanently dead."""
        built = ch.build_channel("d", {
            "type": "discord",
            "webhook_url": "https://discord.com/api/webhooks/1/tok",
            "thread_id": "99"})
        built.edit(msg(), "m1")
        assert "thread_id=99" in http.calls[0]["url"]
        assert "/messages/m1" in http.calls[0]["url"]

    def test_a_webhook_url_with_a_query_still_builds_a_valid_edit_url(
            self, http):
        built = ch.build_channel("d", {
            "type": "discord",
            "webhook_url": "https://discord.com/api/webhooks/1/tok?x=1"})
        built.edit(msg(), "m1")
        assert http.calls[0]["url"].endswith("/messages/m1")

    def test_an_EDIT_that_404s_does_not_condemn_the_channel(self, http):
        """Somebody deleting the notification in Discord must not take
        the operator's alerting down."""
        http.responses.append(Resp(404))
        built = ch.build_channel("d", {
            "type": "discord",
            "webhook_url": "https://discord.com/api/webhooks/1/tok"})
        with pytest.raises(ch.NotSupported):
            built.edit(msg(), "m1")

    def test_a_SEND_that_404s_still_does(self, http):
        http.responses.append(Resp(404))
        built = ch.build_channel("d", {
            "type": "discord",
            "webhook_url": "https://discord.com/api/webhooks/1/tok"})
        with pytest.raises(ch.AuthFailed):
            built.send(msg())

    def test_redaction_hides_a_secret_that_is_FIRST_in_the_path(self):
        """The secret is not reliably at the end: a relay whose path is
        /<token>/send had its token printed verbatim."""
        assert "a1b2c3d4e5f6g7h8" not in ch._redact_url(
            "https://relay.example.com/a1b2c3d4e5f6g7h8/send")

    def test_a_failed_starttls_does_not_leak_the_socket(self, monkeypatch):
        """An expired certificate otherwise leaks one fd per attempt,
        three per alert with retries, and the eventual EMFILE surfaces
        as failures in UNRELATED channels."""
        closed = []

        class Boom(FakeSMTP):
            def starttls(self, context=None):
                raise ssl_error()

            def quit(self):
                closed.append(self)

        def ssl_error():
            import ssl
            return ssl.SSLCertVerificationError("expired")

        monkeypatch.setattr(ch.smtplib, "SMTP", Boom)
        FakeSMTP.instances.clear()
        built = ch.build_channel("m", {"type": "email", "host": "h",
                                       "from": "a@b", "to": ["c@d"]})
        with pytest.raises(ch.DeliveryError):
            built.send(msg())
        assert closed, "the socket was opened and never closed"

    def test_the_email_photo_is_referenced_from_an_html_part(self, monkeypatch):
        """A cid on a plain-text message is just an attachment nobody
        opens; the HTML alternative is what makes it render in the body,
        which at 3am is the whole point of attaching it."""
        FakeSMTP.instances.clear()
        monkeypatch.setattr(ch.smtplib, "SMTP", FakeSMTP)
        built = ch.build_channel("m", {"type": "email", "host": "h",
                                       "from": "a@b", "to": ["c@d"]})
        built.send(msg(image=JPEG))
        mail = FakeSMTP.instances[0].sent[0]
        types = [part.get_content_type() for part in mail.walk()]
        assert "multipart/related" in types
        assert "text/html" in types
        assert 'cid:snapshot' in mail.get_body(("html",)).get_content()

    def test_a_channel_that_cannot_render_buttons_is_not_sent_any(self, http):
        """A Discord webhook cannot carry components at all."""
        built = ch.build_channel("d", {
            "type": "discord",
            "webhook_url": "https://discord.com/api/webhooks/1/tok"})
        assert built.can_act is False
        stripped = msg(actions=[ch.Action("View", "https://x")]).without_actions()
        assert stripped.actions == []
        assert stripped.title == "Person at gate"
