"""Config flow for OpenNVR (design §7.2).

* **user**: URL, API token, verify SSL. Checked with ``GET /system/info``:
  it must be an OpenNVR speaking contract 1.x, the token must hold the
  required scopes (the server reports them, contract 1.1), and missing
  recommended scopes are named on the next step.
* **cameras**: which cameras to show; all by default (and then cameras added
  later appear too).
* **zeroconf**: sites running the optional mDNS sidecar are offered; the user
  still enters a token. A discovery never rewrites a configured entry's URL:
  an unauthenticated LAN announcement must not redirect where the token goes.
* **reauth** (token revoked/expired), **reconfigure** (URL, SSL, token), and
  **options** (cameras, notification link lifetime).

``unique_id`` is the server's ``site_id``, so a changed URL or token is the
same site, and a different server at the old URL is refused.
"""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pyopennvr import (
    OpenNVRAuthError,
    OpenNVRClient,
    OpenNVRConnectionError,
    OpenNVRContractError,
    OpenNVRError,
    OpenNVRNotFoundError,
    OpenNVRSSLError,
    SystemInfo,
    check_contract,
)
import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_API_TOKEN, CONF_URL, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from . import OpenNVRConfigEntry
from .const import (
    CONF_CAMERAS,
    CONF_MEDIA_TTL,
    DEFAULT_MEDIA_TTL,
    DOMAIN,
    RECOMMENDED_SCOPES,
    REQUIRED_SCOPES,
)

_LOGGER = logging.getLogger(__name__)

TOKEN_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
URL_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.URL))


def normalise_url(raw: str) -> str | None:
    """``nvr.local`` → ``https://nvr.local``; a pasted ``.../api/v1`` or
    trailing slash is dropped. None if it isn't an http(s) URL."""
    url = raw.strip()
    if "://" not in url:
        url = f"https://{url}"
    try:
        parts = urlsplit(url)
        _ = parts.port  # raises on a malformed port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    path = parts.path.rstrip("/")
    if path.endswith("/api/v1"):
        path = path[: -len("/api/v1")]
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


class InvalidSite(Exception):
    """Validation failed; ``error`` is the translation key for the form."""

    def __init__(self, error: str, **placeholders: str) -> None:
        super().__init__(error)
        self.error = error
        self.placeholders = placeholders


async def validate_site(hass: HomeAssistant, url: str, token: str,
                        verify_ssl: bool) -> tuple[SystemInfo, OpenNVRClient]:
    """Reach the site with this token; raise InvalidSite otherwise."""
    token = token.strip()
    if not token.startswith("onvr_"):
        raise InvalidSite("token_format")
    client = OpenNVRClient(url, token, async_get_clientsession(hass, verify_ssl),
                           verify_ssl=verify_ssl)
    try:
        info = await client.get_system_info()
    except OpenNVRSSLError as err:
        raise InvalidSite("ssl_error") from err
    except OpenNVRAuthError as err:
        if err.status == 403:
            # /system/info itself needs settings.view.
            raise InvalidSite("missing_scopes", scopes="settings.view") from err
        raise InvalidSite("invalid_auth") from err
    except OpenNVRNotFoundError as err:
        raise InvalidSite("not_opennvr") from err
    except OpenNVRConnectionError as err:
        raise InvalidSite("cannot_connect") from err
    except (OpenNVRError, KeyError, TypeError, ValueError, AttributeError) as err:
        # Something answered, but not with OpenNVR's /system/info.
        _LOGGER.debug("Not an OpenNVR /system/info reply from %s: %s", url, err)
        raise InvalidSite("not_opennvr") from err
    try:
        check_contract(info)
    except OpenNVRContractError as err:
        raise InvalidSite("unsupported_version", version=info.contract_version) from err
    scopes = info.scopes
    if scopes is not None:
        missing = [s for s in REQUIRED_SCOPES if s not in scopes]
        if missing:
            raise InvalidSite("missing_scopes", scopes=", ".join(missing))
    return info, client


class OpenNVRConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for OpenNVR."""

    VERSION = 1

    def __init__(self) -> None:
        self._url: str | None = None
        self._token: str | None = None
        self._verify_ssl = True
        self._info: SystemInfo | None = None
        self._client: OpenNVRClient | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: OpenNVRConfigEntry) -> OpenNVROptionsFlow:
        return OpenNVROptionsFlow()

    async def _try(self, url: str, token: str, verify_ssl: bool,
                   errors: dict[str, str], placeholders: dict[str, str]) -> bool:
        try:
            self._info, self._client = await validate_site(self.hass, url, token, verify_ssl)
        except InvalidSite as err:
            errors["base"] = err.error
            placeholders.update(err.placeholders)
            return False
        self._url, self._token, self._verify_ssl = url, token.strip(), verify_ssl
        return True

    # ── user ─────────────────────────────────────────────────────────────

    async def async_step_user(self, user_input: dict[str, Any] | None = None
                              ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            url = normalise_url(user_input[CONF_URL])
            if url is None:
                errors[CONF_URL] = "invalid_url"
            elif await self._try(url, user_input[CONF_API_TOKEN],
                                 user_input[CONF_VERIFY_SSL], errors, placeholders):
                await self.async_set_unique_id(self._info.site_id)
                self._abort_if_unique_id_configured()
                return await self.async_step_cameras()
        schema = vol.Schema({
            vol.Required(CONF_URL): URL_SELECTOR,
            vol.Required(CONF_API_TOKEN): TOKEN_SELECTOR,
            vol.Required(CONF_VERIFY_SSL, default=True): bool,
        })
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(
                schema, {k: v for k, v in user_input.items() if k != CONF_API_TOKEN})
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors,
                                    description_placeholders=placeholders)

    # ── cameras ──────────────────────────────────────────────────────────

    async def async_step_cameras(self, user_input: dict[str, Any] | None = None
                                 ) -> ConfigFlowResult:
        assert self._client is not None and self._info is not None
        errors: dict[str, str] = {}
        try:
            cameras = await self._client.get_cameras()
        except OpenNVRError:
            return self.async_abort(reason="cannot_connect")
        if not cameras:
            return self._create({})
        if user_input is not None:
            chosen = sorted(int(c) for c in user_input[CONF_CAMERAS])
            if not chosen:
                errors["base"] = "no_cameras"
            else:
                all_ids = sorted(c.id for c in cameras)
                return self._create({} if chosen == all_ids else {CONF_CAMERAS: chosen})
        scopes = self._info.scopes
        missing = ([s for s in RECOMMENDED_SCOPES if s not in scopes]
                   if scopes is not None else [])
        schema = vol.Schema({
            vol.Required(CONF_CAMERAS, default=[str(c.id) for c in cameras]):
                _camera_selector(cameras),
        })
        return self.async_show_form(
            step_id="cameras", data_schema=schema, errors=errors,
            description_placeholders={"site": self._info.name,
                                      "missing_scopes": ", ".join(missing) or "-"})

    def _create(self, options: dict[str, Any]) -> ConfigFlowResult:
        assert self._info is not None
        return self.async_create_entry(
            title=self._info.name,
            data={CONF_URL: self._url, CONF_API_TOKEN: self._token,
                  CONF_VERIFY_SSL: self._verify_ssl},
            options=options)

    # ── zeroconf ─────────────────────────────────────────────────────────

    async def async_step_zeroconf(self, discovery_info: ZeroconfServiceInfo
                                  ) -> ConfigFlowResult:
        props = discovery_info.properties
        https = str(props.get("https", "1")) == "1"
        try:
            port = int(props.get("port") or discovery_info.port or (443 if https else 80))
        except (TypeError, ValueError):
            return self.async_abort(reason="not_opennvr")
        ip = discovery_info.ip_address
        host = f"[{ip}]" if ip.version == 6 else str(ip)
        default_port = 443 if https else 80
        url = f"{'https' if https else 'http'}://{host}" + (
            f":{port}" if port != default_port else "")
        # The mDNS record carries no site id (it needs a token to learn), so
        # discoveries are told apart by URL until the token is entered.
        self._async_abort_entries_match({CONF_URL: url})
        await self.async_set_unique_id(url)
        self._abort_if_unique_id_configured()
        self._url = url
        self.context["title_placeholders"] = {"name": host}
        return await self.async_step_zeroconf_confirm()

    async def async_step_zeroconf_confirm(self, user_input: dict[str, Any] | None = None
                                          ) -> ConfigFlowResult:
        assert self._url is not None
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {"url": self._url}
        if user_input is not None and await self._try(
                self._url, user_input[CONF_API_TOKEN], user_input[CONF_VERIFY_SSL],
                errors, placeholders):
            await self.async_set_unique_id(self._info.site_id, raise_on_progress=False)
            # Already set up under another URL: leave that entry alone.
            self._abort_if_unique_id_configured()
            return await self.async_step_cameras()
        schema = vol.Schema({
            vol.Required(CONF_API_TOKEN): TOKEN_SELECTOR,
            # A LAN server usually has a self-signed certificate.
            vol.Required(CONF_VERIFY_SSL, default=False): bool,
        })
        return self.async_show_form(step_id="zeroconf_confirm", data_schema=schema,
                                    errors=errors, description_placeholders=placeholders)

    # ── reauth ───────────────────────────────────────────────────────────

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None
                                        ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {"name": entry.title}
        if user_input is not None and await self._try(
                entry.data[CONF_URL], user_input[CONF_API_TOKEN],
                entry.data.get(CONF_VERIFY_SSL, True), errors, placeholders):
            await self.async_set_unique_id(self._info.site_id)
            self._abort_if_unique_id_mismatch(reason="wrong_site")
            return self.async_update_reload_and_abort(
                entry, data_updates={CONF_API_TOKEN: self._token})
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_API_TOKEN): TOKEN_SELECTOR}),
            errors=errors, description_placeholders=placeholders)

    # ── reconfigure ──────────────────────────────────────────────────────

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None
                                     ) -> ConfigFlowResult:
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            url = normalise_url(user_input[CONF_URL])
            token = user_input.get(CONF_API_TOKEN) or entry.data[CONF_API_TOKEN]
            if url is None:
                errors[CONF_URL] = "invalid_url"
            elif await self._try(url, token, user_input[CONF_VERIFY_SSL], errors,
                                 placeholders):
                await self.async_set_unique_id(self._info.site_id)
                self._abort_if_unique_id_mismatch(reason="wrong_site")
                return self.async_update_reload_and_abort(entry, data_updates={
                    CONF_URL: self._url, CONF_API_TOKEN: self._token,
                    CONF_VERIFY_SSL: self._verify_ssl})
        schema = self.add_suggested_values_to_schema(vol.Schema({
            vol.Required(CONF_URL): URL_SELECTOR,
            # Blank keeps the current token.
            vol.Optional(CONF_API_TOKEN): TOKEN_SELECTOR,
            vol.Required(CONF_VERIFY_SSL): bool,
        }), {CONF_URL: entry.data[CONF_URL],
             CONF_VERIFY_SSL: entry.data.get(CONF_VERIFY_SSL, True)})
        return self.async_show_form(step_id="reconfigure", data_schema=schema, errors=errors,
                                    description_placeholders=placeholders)


class OpenNVROptionsFlow(OptionsFlowWithReload):
    """Cameras and notification link lifetime."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None
                              ) -> ConfigFlowResult:
        entry: OpenNVRConfigEntry = self.config_entry
        if entry.state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="not_loaded")
        cameras = list(entry.runtime_data.coordinator.data.all_cameras.values())
        errors: dict[str, str] = {}
        if user_input is not None:
            options: dict[str, Any] = {CONF_MEDIA_TTL: int(user_input[CONF_MEDIA_TTL])}
            if cameras:
                chosen = sorted(int(c) for c in user_input.get(CONF_CAMERAS, []))
                if not chosen:
                    errors["base"] = "no_cameras"
                elif chosen != sorted(c.id for c in cameras):
                    options[CONF_CAMERAS] = chosen
            if not errors:
                return self.async_create_entry(data=options)
        current = entry.options.get(CONF_CAMERAS)
        fields: dict[Any, Any] = {}
        if cameras:
            default = [str(c.id) for c in cameras
                       if current is None or c.id in set(current)]
            fields[vol.Required(CONF_CAMERAS, default=default)] = _camera_selector(cameras)
        fields[vol.Required(CONF_MEDIA_TTL,
                            default=entry.options.get(CONF_MEDIA_TTL, DEFAULT_MEDIA_TTL))] = (
            NumberSelector(NumberSelectorConfig(min=1, max=168, step=1,
                                                unit_of_measurement="h",
                                                mode=NumberSelectorMode.BOX)))
        return self.async_show_form(step_id="init", data_schema=vol.Schema(fields),
                                    errors=errors)


def _camera_selector(cameras: list) -> SelectSelector:
    return SelectSelector(SelectSelectorConfig(
        options=[SelectOptionDict(value=str(c.id), label=c.name)
                 for c in sorted(cameras, key=lambda c: c.id)],
        multiple=True, mode=SelectSelectorMode.LIST))
