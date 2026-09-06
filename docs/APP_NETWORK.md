# App network egress

Apps installed on OpenNVR cannot reach the LAN or the internet unless
their listing declared the destination or an administrator allowed it
for that install. This page says how that works, what an operator sees,
and what an app developer has to do (usually nothing).

## The shape

```
                    opennvr_apps (internal: no route out)
   ┌─────────────┐  ┌───────────┐  ┌──────────────┐
   │ loitering   │  │ alert-    │  │ home-        │   … every app
   │ -detection  │  │ notifier  │  │ assistant-   │
   └──────┬──────┘  └─────┬─────┘  └──────┬───────┘
          │ HTTP/NATS     │ HTTPS_PROXY    │
   ┌──────┴───────────────┴────────────────┴───────┐
   │ opennvr-core · nats · nats-apps · egress-proxy │  ← on both networks
   └───────────────────────────────┬────────────────┘
                                   │ CONNECT api.telegram.org:443 ?
                        opennvr_internal ── LAN ── internet
```

* Every app service in `docker-compose.apps.yml` is on **`opennvr_apps`**,
  a compose network with `internal: true` — Docker gives it no gateway
  and no NAT. From inside an app container, core, the two NATS servers
  and the egress proxy resolve and connect; `192.168.1.50` and
  `api.telegram.org` do not.
* Every app container gets **`HTTP_PROXY` / `HTTPS_PROXY`** pointing at
  the `egress-proxy` service, and a `NO_PROXY` naming the stack's own
  services. Python's `httpx`, `requests`, `urllib` and
  `aiohttp(trust_env=True)` — and most other languages' HTTP clients —
  use those without any code.
* The **egress proxy** (`scripts/egress-proxy`, standard library, ~300
  lines) holds no policy. For each `CONNECT host:port` (every HTTPS
  call) or absolute-URI HTTP request it asks core:

      POST /api/v1/apps/egress/check  {"client_ip", "host", "port"}
      → {"allowed": true|false, "app_id": "...", "reason": "..."}

  Allowed → a plain TCP tunnel (TLS passes through; the proxy never
  sees plaintext). Refused → `403` with a body that names the app and
  the destination. No answer from core → refused.
* **Core** (`server/services/app_egress.py`) identifies the app by
  address — the one its registered contract URL resolves to, the same
  address core polls for `/health` — and answers from two lists:
  the hosts the app's **catalog listing declared** (`network_egress`),
  and the hosts an **administrator allowed** for this install. A
  refusal is logged, counted on the app, written to the audit log, and
  raised **once per app and destination per hour** as a medium alert in
  the inbox that names the host.

Nothing here is a secret an app carries. An app cannot claim to be
another app, and a compromised app cannot widen its own list.

## What the operator sees

On every installed app's card, a **Network** line:

* grey chips — hosts the listing declared (reviewed with the app);
* green chips — hosts an administrator allowed here (with × to remove);
* red chips — destinations the proxy **refused**, with a count and the
  last attempt, and an **Allow** button;
* "no connections outside the stack" when there is nothing.

An inbox alert *"Alert Notifier tried to reach 192.168.1.20:1883"*
means exactly that. If it is the Home Assistant box you configured,
click Allow. If you have no idea what it is, leave it blocked — the
listing terms say an app does nothing the card did not show, and an
undeclared destination is grounds for removal
([APP_LISTING_TERMS.md](APP_LISTING_TERMS.md) §5).

Allow-list entries are host rules, not URLs: `ha.local`, `ha.local:8123`,
`*.ntfy.sh`, `192.168.1.50`, `192.168.1.0/24`. Sixty-four per app.
Superusers only (`PUT /api/v1/apps/{id}/egress`, audit-logged).

### Turning it off

`APPS_EGRESS_ENFORCED=false` in `.env` makes `opennvr_apps` a normal
bridge again; apps then reach anything, and the proxy (still handed to
them) still refuses undeclared hosts for HTTP — set
`APPS_EGRESS_PROXY_URL=` (empty) as well to stop handing it out. Change
either with the stack **down** (`docker compose … down` first): Docker
cannot flip a network's `internal` flag on a network that has
containers attached.

When to: a LAN-only deployment where an app must speak a protocol the
proxy cannot carry (see below) and the operator accepts that the app
sees the LAN. Not for "the alert is annoying" — the Allow button is.

## What an app developer has to do

For HTTP(S): **nothing**. Declare every host you talk to in the
listing's `network_egress` ([CONTRIBUTING_APPS.md](CONTRIBUTING_APPS.md));
host-shaped entries (`api.telegram.org`, `*.vendor.example`,
`10.0.0.0/8`, optional `:port`) become rules, free-text entries ("the
webhook URL the operator configures") stay notes on the card, and the
operator allows those hosts when they configure them.

For anything that is plain TCP — an MQTT client, a database driver, a
custom socket — the connection must be tunnelled through the proxy
explicitly (HTTP `CONNECT`), because Docker will not route it anywhere
else. The SDK tells you where the proxy is:

```python
from opennvr_app_sdk import connect_via_proxy

via = connect_via_proxy(broker_host, 1883)     # None → connect directly
if via is not None:
    import socks                                 # PySocks
    client.proxy_set(proxy_type=socks.HTTP, proxy_addr=via[0], proxy_port=via[1])
```

That is what the Home Assistant relay does for MQTT
(`examples/home-assistant-relay/publishers.py`). `connect_via_proxy`
returns `None` when no proxy is set or the host is on `NO_PROXY`, so
the same code runs unchanged outside the stack.

Protocols the proxy cannot carry: UDP, and anything that must be
dialled by the app *without* a CONNECT (RTSP pulled straight from a
camera). Apps should not do that anyway — frames come from the platform
(`nvr.snapshot()`, the Tier-0 stream, `FrameSource`); an operator who
needs it turns enforcement off, knowingly.

## Guarantees and limits, plainly

* An app on the internal network **cannot** open a connection to the
  LAN or the internet except through the proxy. This is Docker's
  `internal` network, not a firewall rule the app could remove.
* Through the proxy an app reaches **only** hosts its listing declared
  or the operator allowed, and every refusal is visible.
* The proxy is HTTP `CONNECT`: it sees `host:port`, never the bytes of
  a TLS session. It does not inspect, log or buffer payloads.
* Identity is by address. Two apps never share an address on the apps
  network (one container per service); core's resolution of contract
  hosts is cached for sixty seconds, so a re-created container is
  matched within a minute — until then its requests are refused as
  "unknown client", not mis-attributed.
* Policy lives in core, not the privileged installer. A compromised
  core could allow anything — and already has every camera; the
  installer's trust boundary (which images run) is unchanged.
* `APPS_EGRESS_ENFORCED=false` is a deployment-wide switch. There is no
  per-app opt-out by design: an app that needs one belongs on the allow
  list with a host, or in the listing with a declaration.

## Files

| Piece | Where |
|---|---|
| Networks, core/nats membership | `docker-compose.yml` (`opennvr_apps`) |
| App env, proxy service | `docker-compose.apps.yml` |
| The proxy | `scripts/egress-proxy/proxy.py`, `Dockerfile`, `tests/` |
| Policy, identity, denial memory, inbox alert | `server/services/app_egress.py` |
| Routes | `POST /apps/egress/check`, `GET`/`PUT /apps/{id}/egress` (`server/routers/apps.py`) |
| Operator allow list | `installed_apps.egress_allow` (migration `b4d8e2a91c7f`) |
| SDK helper for plain-TCP clients | `opennvr_app_sdk.egress` |
| Card UI | `app/src/views/AppCatalog.tsx` (`NetworkPanel`) |
| Switches | `APPS_EGRESS_ENFORCED`, `APPS_EGRESS_PROXY_URL` (`.env.example`) |
