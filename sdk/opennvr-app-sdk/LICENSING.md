# Licensing — what this package obliges you to

**`opennvr-app-sdk` is Apache-2.0. An app you build with it is yours,
under any licence you choose, including a closed one.** That is the
whole answer. The rest of this page is why it is true, for the reader
who has to explain it to a lawyer.

## The boundary

OpenNVR's platform core is **AGPL-3.0-or-later**. This SDK is
**Apache-2.0**. They are separate programs and they always talk over a
process boundary:

| Your app reaches the platform by | Never by |
|---|---|
| NATS subjects (`opennvr.inference.>`, `opennvr.alerts.*`, `opennvr.events.*`) | linking against core |
| HTTP to core's REST API, via `OpenNVR()` / `AsyncOpenNVR()` | importing core modules |
| HTTP served *by* your app on its contract port (`/health`, `/manifest`, `/state`, `/actions/*`) | sharing an address space with core |

Nothing in this package contains, links, or requires AGPL code. Your app
imports `opennvr_app_sdk`, which is Apache-2.0 and self-contained; the
AGPL core is a separate process your app exchanges messages with, in the
same way a Postgres client is not a derivative work of Postgres. Running
your closed app alongside an OpenNVR deployment triggers no AGPL
obligation on your code — the obligation stays with whoever modifies and
serves the core.

This is deliberate and permanent. Building on OpenNVR must never require
a lawyer, and the catalog is worth more to everyone the more apps are in
it. The project's revenue does not come from app developers and is not
intended to: see
[DEVELOPER_PROGRAM.md](https://github.com/open-nvr/open-nvr/blob/main/docs/DEVELOPER_PROGRAM.md)
— no fee, your licence, your price, your copyright.

## Where the AGPL does apply

You need to think about the AGPL only when you touch the core itself:

- modifying `open-nvr` and **serving it to users over a network** — AGPL
  §13 asks you to offer those users the corresponding source;
- **redistributing** a modified core, or shipping it inside an appliance
  or a proprietary product;
- **white-labelling** or removing OpenNVR branding.

Each of those has a commercial alternative — the OpenNVR Commercial
License. See
[LICENSING.md](https://github.com/open-nvr/open-nvr/blob/main/docs/LICENSING.md)
and [TRADEMARK.md](https://github.com/open-nvr/open-nvr/blob/main/TRADEMARK.md).

## Selling your app

Nothing here stops you. The manifest carries `pricing`, `price_note` and
`entitlement="license_key"`; when an app declares the last one, the
catalog collects a key from the administrator and asks *your* code to
verify it (`ContractMixin.verify_license`). The verdict is yours, the
key is stored encrypted by core, and OpenNVR takes no cut.

## Contributing back

Contributions to this Apache-2.0 SDK need only the ordinary inbound=
outbound norm plus a DCO sign-off (`git commit -s`). The
[CLA](https://github.com/open-nvr/open-nvr/blob/main/docs/CLA.md) applies
to the AGPL core, not to this package.

*A policy summary, not legal advice; the LICENSE file and any executed
agreement are the operative texts.*
