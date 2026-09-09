# Security Policy

OpenNVR is a security product. Vulnerability reports get a defined response timeline and coordinated disclosure. This page covers what's in scope, how to report, what to expect afterwards, and the operator-side hygiene that lies outside the project but matters for any deployment.

## Supported versions

The `0.1.x` line is supported. Earlier development snapshots are not — if you're reporting against a pre-tag commit, please reproduce against the latest `0.1.x` release before filing.

## Reporting a vulnerability

**Please do not open a public GitHub issue for sensitive security vulnerabilities.** Public issues are immediately indexed; we want a chance to ship a fix before the world sees the report.

The preferred channel is GitHub's [private vulnerability reporting](https://github.com/open-nvr/open-nvr/security/advisories/new) on this repository — it gives us a private thread, an audit trail, and a way to credit you in the eventual advisory. If that channel isn't workable for you, email **contact@opennvr.org** with the subject `OpenNVR security report` and as much detail as you can share without exposing your own systems.

A useful report identifies the OpenNVR version (`git describe` output or release tag), the deployment shape (standard stack compose, host-mode Linux, bare-metal dev), the minimum steps that reproduce the issue, and the impact an attacker has once it triggers. A suggested fix or mitigation is welcome if you have one, but not required.

## Response timeline

We acknowledge receipt within 48 hours and complete initial triage within seven days — confirmed, unable-to-reproduce, or asking for more information. At fix time we coordinate disclosure on a 30-day default window, extended for severe issues that need an ecosystem-wide fix. Reporters who want public credit get it in the advisory; reporters who want to stay anonymous do. Advisories are published on the [GitHub Security Advisories](https://github.com/open-nvr/open-nvr/security/advisories) tab with CVE assignment where applicable.

## Acknowledgements

OpenNVR's security architecture was built against a documented threat model and hardened through 22 tracked findings (V-001..V-022) before the first public release. That work is in [`docs/SECURITY_ARCHITECTURE.md`](docs/SECURITY_ARCHITECTURE.md), and it is the floor, not the ceiling — an internal audit finds what its authors thought to look for.

The researchers below found what we did not. Both reports landed one layer inside the perimeter we had built: not a broken lock, but a door that checked *whether you had a key* and never *whose key it was*. That is the class of issue an architecture review is worst at catching and an outside reader is best at, and every one of these has made the product measurably harder to attack.

We are grateful to them, and we would like there to be more.

### Reporters

*Most recent first. Fixed releases are linked once shipped.*

---

**Kamal Sentassi** · S9S Security Research · 2026

Five access-control and SSRF issues in a single coordinated report, each verified against the code before a line was changed:

- **Cross-camera event disclosure** over the live-event WebSocket — the subscription was never authorized, only the connection, so any authenticated account could omit a query parameter and stream every camera's detections, alarms and system alerts. The highest-impact of the five.
- **Blind SSRF in camera onboarding** — the create endpoint dialled caller-supplied hosts with none of the guarding the ONVIF router already had.
- **Cloud instance metadata classified as "internal"** by the SSRF guard, making 169.254.169.254 reachable wherever internal hosts were allowed.
- **An unauthenticated health endpoint** publishing the internal MediaMTX admin URL.
- **An unrestricted integration webhook** whose error text distinguished *refused* from *timed out* — an internal port scanner that reported its own findings.

The report named files and line numbers, rated its own findings conservatively, and included a list of what it had checked and found sound. *Fixed in 0.1.5.*

---

**Furkan Arslan** · 2026

**Missing authorization on the IP-keyed ONVIF routes** (CWE-862 / CWE-639). `/connect` and every `/camera/{ip}/…` route checked that the address lay inside the camera LAN but never the caller's relationship to the camera at that address — so any authenticated user could pan, tilt and zoom, or read the stream URI of, any camera on the network. The sibling `/cameras/{id}/…` routes had always required ownership; these had been written to a different rule and nobody had noticed.

The report arrived with a proof of concept that included a negative control and an owner sanity check in the same script, which turned confirmation into a five-minute job. It also pointed out that private vulnerability reporting was not enabled on the repository despite this page pointing there — since fixed. *Fixed in [PR #398](https://github.com/open-nvr/open-nvr/pull/398), 0.1.5.*

---

### If you are thinking about looking

There is no bug bounty. This is an AGPL project without a security budget, and saying so plainly beats implying a reward that does not exist. What we offer instead is a technical reply from people who read the same code you are reading, a fix that lands with your name on it, a GitHub Security Advisory with a CVE where one applies, and a permanent entry above.

Credit is yours to shape: named, named with a link to your site or profile, under a handle, or anonymous — we ask before publishing anything and take the answer as given.

These are the areas where a finding is most likely to be real:

- **Authorization rather than authentication.** Authentication has had the most attention — that is not a claim it is perfect, only where the effort went. Both reports above landed one layer in: what an *authenticated* user may reach. Per-camera scoping across the newer read surfaces, WebSocket and streaming subscriptions, and anywhere an id or an IP arrives from a client and is trusted.
- **Outbound request paths.** Anything that takes a caller-supplied host and connects to it — camera onboarding, ONVIF, adapters, webhooks, the app registry.
- **The app and adapter boundary.** Third-party apps run on an internal network behind an egress proxy that asks core, per connection, whether a host is permitted. Ways around that are interesting.

## Security architecture

OpenNVR is designed so the operator does not configure security — they configure exceptions. Every protection is on by default, and explicitly turning one off lands an audit-log entry.

There are no shipped default credentials. First boot prints a one-time setup token, and the operator chooses an admin password from there. A strong-secret validator refuses to boot if `SECRET_KEY`, `INTERNAL_API_KEY`, `CREDENTIAL_ENCRYPTION_KEY`, or `MEDIAMTX_SECRET` are placeholders or shorter than the minimum length, so the project literally cannot run with the example values left in place. The streaming layer binds MediaMTX to 127.0.0.1 by default and speaks RTSPS, HLS-over-TLS, and WebRTC-over-TLS; plaintext RTSP requires an explicit opt-in that itself lands in the audit log.

Two independent default-deny gates govern what crosses the network boundary. `DEPLOYMENT_MODE=offline` is the default — cloud routes return HTTP 403 unless the operator explicitly switches it to `hybrid` or `cloud`, and that switch is audit-logged at boot. `AI_SOVEREIGNTY=local_only` is the default — adapters that declare `network_egress` are refused registration outright. Both gates fail closed, so a configuration error never silently widens the perimeter.

End to end, every inference carries an `X-Correlation-Id` threading alert → middleware → adapter, model weights are fingerprinted with sha256 and polled for drift, and the resulting events land in an append-only log. The full threat model and control mapping are in [`docs/SECURITY_ARCHITECTURE.md`](docs/SECURITY_ARCHITECTURE.md); the architectural foundation is published in [Singh et al., 2025](https://doi.org/10.5281/zenodo.17261761).

## Operator checklist

A handful of things sit outside OpenNVR's code but matter for any internet-facing deployment, and they're listed here so nobody is caught out by them.

Generate strong secrets with `./scripts/generate-secrets.sh --write` (Linux/macOS) or `.\scripts\generate-secrets.ps1 -Write` (Windows) before the first `docker compose up` — the validator will refuse to boot if you skip this. Lock `.env` to your own user with `chmod 600 .env` so other accounts on the host can't read it. Front the service with a reverse proxy carrying a real TLS certificate before answering requests from anything outside your LAN; OpenNVR itself speaks plain HTTP on port 8000 and relies on the proxy for transport security. Firewall the MediaMTX listeners — RTSPS on 8322, HLS on 8888, WebRTC on 8889 — so only the clients that need them can reach them. Back up the `opennvr_db_data` volume periodically: it holds your camera list, user accounts, and the audit log itself.

## Out of scope

A few areas are explicitly outside the scope of security reports against this repository.

The bare-metal developer shell (`./start.sh build`, `docs/LOCAL_SETUP.md`) is intended for contributors working on trusted machines; it isn't hardened for production and security reports against it will be triaged as documentation rather than vulnerabilities. Third-party adapter container images are not vouched for — OpenNVR validates that they comply with the AI Adapter Contract and can register, but it does not audit the contents of images that didn't come from the official `open-nvr` GitHub organisation; untrusted adapters are run at the operator's own risk. Model behaviour itself — hallucinated detections, biased recognition results, and other ML-quality issues — belongs upstream with the model author rather than as a bug in the OpenNVR transport and audit layer.
