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

## Credit and acknowledgements

Researchers who report a valid issue get named credit in the GitHub Security Advisory, in the `CHANGELOG` entry for the fix, and in the list below — unless they would rather stay anonymous, which is equally fine and needs no explanation. We ask before publishing a name, and we will include a link to your profile or site if you want one.

There is no bug bounty. This is an AGPL project without a security budget, and we would rather say so plainly than imply a reward that does not exist. What we can offer is a fast, technical response from people who read the code you are reading, a fix that credits you, and an advisory with a CVE where one applies.

### Reporters

| Researcher | Reported | Fixed in |
| --- | --- | --- |
| Furkan Arslan | Missing authorization on the IP-keyed ONVIF routes (CWE-862 / CWE-639): `/connect` and every `/camera/{ip}/…` route checked that the address lay inside the camera LAN but never the caller's relationship to that camera, so any authenticated user could PTZ or read the stream URI of any camera on the LAN. | [PR #398](https://github.com/open-nvr/open-nvr/pull/398), 0.1.5 |
| Kamal Sentassi — S9S Security Research | Five access-control and SSRF issues found in one coordinated report: cross-camera disclosure over the live-event WebSocket (the subscription was never authorized, only the connection); a blind SSRF in camera-create that had none of the host guarding the ONVIF router already had; cloud instance metadata classified as "internal" by the SSRF guard; an unauthenticated MediaMTX health endpoint publishing the internal admin URL; and an unrestricted integration webhook whose error text distinguished refused from timed-out. | 0.1.5 |

Both reports were accurate down to the file and line, arrived through coordinated disclosure rather than a public issue, and were confirmed against the code before a line was changed. That is the standard we are grateful for and the one we try to match in reply.

### What is most useful to look at

If you are considering a look at OpenNVR, these are the areas where a finding is most likely to be real and most valuable to us:

- **Authorization, not authentication.** Authentication itself has had the most attention — that is not a claim it is perfect, only where the effort went. The findings keep landing one layer in: what an *authenticated* user may reach — per-camera scoping across the newer read surfaces, WebSocket and streaming subscriptions, and anywhere an id or an IP comes off the wire and is trusted. Both reports above landed here.
- **Outbound request paths.** Anything that takes a caller-supplied host and dials it — camera onboarding, ONVIF, adapters, webhooks, the app registry.
- **The app and adapter boundary.** Third-party apps run on an internal network behind an egress proxy that asks core, per connection, whether a host is allowed. Ways around that are interesting.

The [threat model and control mapping](docs/SECURITY_ARCHITECTURE.md) documents the pre-audit hardening history (V-001..V-022), which is a fair map of what has already been looked at.

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
