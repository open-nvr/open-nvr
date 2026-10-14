# Licensing & Commercial Use

OpenNVR is **dual-licensed**, the same proven model as Qt, Linphone, and
PJSIP: a strong open-source license for the community, and a commercial
license for businesses that need terms the open-source license doesn't
offer. One codebase, two ways to use it — you pick the one that fits.

## The three-line summary

1. **The platform core is AGPL-3.0-or-later.** Use it, self-host it,
   modify it, ship it on any hardware — free forever — as long as you
   honor the AGPL (share the complete corresponding source of what you
   run or distribute, including when users interact with it over a
   network).
2. **The SDKs are Apache-2.0.** Apps built on `opennvr-app-sdk` and AI
   adapters built on the adapter contract can be licensed however YOU
   want — open, proprietary, or classified. The edges are permissive on
   purpose: building on OpenNVR must never require a lawyer.
3. **The OpenNVR Commercial License** removes the AGPL's obligations
   for businesses that can't or won't meet them — OEMs, embedders,
   white-label vendors. See below.

## What is licensed how

| Component | License | Why |
|---|---|---|
| Server, web app, KAI-C gateway, camera-agent, example apps | **AGPL-3.0-or-later** | The sovereign core stays open — anyone who ships or serves a modified OpenNVR must share their changes |
| `opennvr-app-sdk` (App Store apps) | **Apache-2.0** | App developers ship under any license, fast |
| AI Adapter contract + adapter SDK | **Apache-2.0** | Any model vendor can integrate, including proprietary/classified models |
| Wire formats, JSON schemas, API specs | **Apache-2.0** (as part of the SDKs/docs) | Interfaces are open; nobody gets locked in — or out |

## Who needs the Commercial License?

You need it when you want to do something the AGPL doesn't allow — most
commonly:

| Your situation | AGPL (free) | Commercial |
|---|---|---|
| Self-hosting for your home, business, farm, or bank — on ANY hardware (Jetson, Pi, your own servers) | ✅ | — |
| Modifying OpenNVR for internal use and complying with AGPL §13 for network users | ✅ | — |
| Community App Store apps / AI adapters (Apache SDKs) | ✅ any license | — |
| **Selling hardware with OpenNVR pre-installed** under your brand, with proprietary modifications or without source-disclosure duties | ❌ | ✅ commercial licence, per camera |
| **Embedding OpenNVR in a proprietary product** (combining/linking beyond the Apache SDK boundaries) | ❌ | ✅ commercial licence, per camera |
| Offering OpenNVR **as a hosted service** without publishing your modifications | ❌ (AGPL §13) | ✅ |
| **White-label** (removing OpenNVR branding) | trademark policy applies | ✅ white-label tier |

To be precise about hardware: the AGPL cannot and does not discriminate
by hardware. An appliance vendor who ships bone-stock or fully-source-
disclosed OpenNVR and honors every AGPL term owes nothing. The
Commercial License is for vendors who want what the AGPL doesn't give:
proprietary changes, no disclosure duties, the OpenNVR trademark on the
box, warranty and indemnity, certified update channels, and support.

Commercial licensees are **required to display the "Powered by
OpenNVR" mark** (see [TRADEMARK.md](../TRADEMARK.md)) unless the
white-label tier is purchased.

## What the Commercial License includes

- Rights to embed, modify, and redistribute without AGPL obligations
- Priced per camera, paid once (volume tiers from 250 cameras), with the
  first year of maintenance included and a yearly subscription as the
  alternative; white-label onboarding is a one-time fee per partner. The
  current list prices, a worked example and a buyers' FAQ are at
  [opennvr.org/pricing](https://opennvr.org/pricing/)
- "Powered by OpenNVR" / "OpenNVR Certified" trademark grant
- Warranty, indemnification, and a support SLA
- Access to certified, signed builds and the priority security channel

**Contact:** [contact@opennvr.org](mailto:contact@opennvr.org). A pilot of
up to 32 cameras is free for 90 days under the commercial terms.

## Contributor License Agreement (CLA)

Dual licensing requires that the project can license every contributed
line under both licenses. All contributions to the AGPL components
therefore require a signed CLA (see [docs/CLA.md](CLA.md)) — you keep
your copyright and grant OpenNVR the rights needed to dual-license.
Contributions to the Apache-2.0 SDKs need only the Apache-2.0 inbound
=outbound norm plus a DCO sign-off.

## Precedents we deliberately follow

- **Linphone (Belledonne)** and **PJSIP (Teluu)** — GPL + commercial
  for the embedded/OEM market. We use AGPL instead of GPL because an
  NVR is a *networked service*: AGPL closes the hosted-service loophole
  GPL leaves open.
- **Qt** — permissive-enough edges, copyleft core, CLA-backed dual
  licensing at scale.
- **Grafana / Mattermost / MinIO** — proof that AGPL cores sustain real
  commercial businesses without abandoning open source.
- We deliberately do **not** follow SSPL/BSL-style source-available
  licenses: AGPL already provides the protection, without giving up the
  "genuinely open source" claim at the heart of OpenNVR's positioning.

*This document is a policy summary, not legal advice; the LICENSE file
and executed commercial agreements are the operative texts.*
