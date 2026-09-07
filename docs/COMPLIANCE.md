# Compliance & Regulatory Mapping

This page maps the architectural threat model in
**[*Eliminating Systemic IP Camera Vulnerabilities via Offline-First Open
Security Architecture* (Singh et al., 2025 — DOI 10.5281/zenodo.17261761)](https://doi.org/10.5281/zenodo.17261761)**
to the controls OpenNVR actually implements. Hand it to your compliance auditor
or procurement officer when they need evidence that the architecture is not
ad-hoc.

The paper identifies six categories of systemic weakness in IP camera
deployments and proposes a three-tier offline-first architecture
(isolated camera network → secure middleware gateway → destination/analytics
layer) with structural safeguards against each. OpenNVR is that architecture's
open-source reference implementation.

## Threat model → countermeasure → implementation

| Paper § / Weakness | Architectural countermeasure | OpenNVR implementation |
|---|---|---|
| **§3.1 Public exposure and credential abuse.** Default / backdoor credentials in firmware (CISA advisories for AVTECH, Edimax). Internet-reachable cameras with weak auth (Mirai, Persirai). | Isolated camera network. No shipped default password. Strong-secret enforcement at boot. | One-time setup token printed at first boot — no admin password ships in the image. Strong-secret validator refuses to boot if `SECRET_KEY`, `INTERNAL_API_KEY`, `CREDENTIAL_ENCRYPTION_KEY`, or `MEDIAMTX_SECRET` are placeholders or shorter than the minimum length. MediaMTX bound to loopback by default. See `SECURITY_ARCHITECTURE.md` V-001 / V-002 / V-015. |
| **§3.2 Insecure protocols and legacy services.** Plaintext RTSP / RTP in commercial cameras by default (despite RFC 7826 + RFC 3711 mandating TLS / SRTP). Telnet / UPnP left active. | TLS/SRTP enforcement at the middleware. Plaintext refused for operator-facing transports. | RTSPS at `:8322`, HLS-TLS at `:8888`, WebRTC-TLS at `:8889` are the only externally-mapped media transports. Per-camera `transport_security` policy (`rtsps_required` / `rtsps_preferred` / `plaintext_allowed`) with TLS-handshake probe on camera-create, runtime enforcement at every stream-provisioning entry point. RTMP and SRT explicitly disabled. See `SECURITY_ARCHITECTURE.md` V-003 / V-019. |
| **§3.3 Fragmented interoperability.** Inconsistent ONVIF compliance, proprietary extensions that weaken secure defaults, heterogeneous fleets where basic protections can't be uniformly applied. | ONVIF Core Specification compliance for device onboarding. Open standards (IETF) for transport. | `server/services/onvif_service.py` discovers Profile S / Profile T per camera; `server/services/onvif_digest_service.py` provides the tested digest-auth fallback path for cameras that need it. Credential vault never exposes plaintext passwords to the UI. Centralized RBAC + per-camera settings in `CameraConfig`. |
| **§3.4 Vendor-controlled cloud and AI pipelines.** Cloud-managed storage / analytics that require decryption outside customer control. AI integrations that violate data / AI sovereignty. | Customer-managed encryption keys. AI workloads run locally or in customer-chosen configurations. | Two independent gates, both default-deny. (1) `DEPLOYMENT_MODE=offline` (default) makes every cloud-touching route — cloud recording, cloud inference, federated streams — return HTTP 403; flipping to `hybrid` or `cloud` is itself audit-logged at boot. (2) `AI_SOVEREIGNTY=local_only` (default) refuses to register AI adapters that declare `network_egress`; flipping to `federated` or `cloud_allowed` permits them and is similarly audit-logged. Camera credentials encrypted at rest with Fernet (`CREDENTIAL_ENCRYPTION_KEY` is operator-managed). |
| **§3.5 Supply chain and firmware transparency.** White-labeled OEM devices inheriting one vendor's flaw across many products (Hikvision CVE-2021-36260 propagation). Opaque firmware update mechanisms. | Open-source middleware replacing proprietary NVR/DVR stack. Community-driven patch cycles. Audit trail for any model swap. | Apache-2.0 SDK + AGPL middleware — every line auditable. SHA-256 model-fingerprint polled every 60 seconds; drift surfaces as `adapter.fingerprint_mismatch` audit event. Append-only audit log records adapter registration, refusal, capability changes, and every inference. End-to-end `X-Correlation-Id` joins alert → middleware → adapter. |
| **§3.6 Lifecycle and patch management.** Vendor firmware cycles average 6–18 months; cameras stop receiving updates after ~3 years. Unmaintained devices remain in production. | Linux LTS-backed update cadence. Patch deployment decoupled from hardware lifecycle. | OpenNVR core ships on Python 3.11+ on Linux LTS. Pre-built images published to `ghcr.io/open-nvr/*` on every tagged release; `docker compose pull` is the update path. Semver releases with CHANGELOG. The middleware patch cadence is days, not vendor-firmware quarters. |

## Compliance framework alignment

The paper's §5 maps the architecture to a set of regulatory and standards
frameworks. OpenNVR inherits that alignment.

| Framework | Reference | What OpenNVR ships against it |
|---|---|---|
| **CISA Secure-by-Design** | [CISA 2024](https://www.cisa.gov/securebydesign) | Secure defaults (no shipped password, strong-secret validator, TLS-by-default), minimized attack surface (loopback-only MediaMTX, default-deny cloud routes), customer-managed cryptography. |
| **NIST Cybersecurity Framework (CSF) 2.0** | [NIST 2024](https://nvlpubs.nist.gov/nistpubs/CSWP/NIST.CSWP.29.pdf) | Identify (camera + adapter registry with capability advertisement), Protect (RBAC, TLS, credential vault), Detect (model fingerprint drift, correlation-ID-tagged events), Respond (audit log, alerts NATS subjects), Recover (recordings retention + replay). |
| **ISO/IEC 27001:2022** | [ISO 2022](https://www.iso.org/standard/82875.html) | Certificate-based authentication for MediaMTX, role-based access control, centralized audit log with append-only semantics, documented control mapping (this page). |
| **ETSI EN 303 645** (Consumer IoT Baseline) | [ETSI 2020](https://www.etsi.org/standards/etsi-en-303-645) | No universal default passwords, secure-by-default communications, software update mechanism, customer control over deletion of personal data. |
| **NIST AI Risk Management Framework (AI RMF 1.0)** | [NIST 2023](https://nvlpubs.nist.gov/nistpubs/ai/nist.ai.100-1.pdf) | AI sovereignty enforcement (`local_only` policy refuses adapters with `network_egress`), model provenance via fingerprint + audit chain, capability-declared permissions reviewed at adapter registration. |
| **EU GDPR** | [Reg. (EU) 2016/679](https://eur-lex.europa.eu/eli/reg/2016/679/oj) | Customer-owned encryption keys, customer-controlled retention, no transfers to vendor-managed cloud by default, audit trail per inference for accountability obligations. |
| **India DPDP Act 2023** | [MeitY](https://www.meity.gov.in/data-protection-framework) | Same controls as the GDPR row — local-only data processing posture, customer-managed keys, retention controlled by the operator. |

## What's explicitly out of scope

The paper's §8 documents residual risks that the architecture does not address, and we surface them here because honesty about scope is part of defensibility.

Vendor firmware vulnerabilities in the cameras themselves remain a residual risk — architectural isolation removes most exploitation paths, but a malicious insider on the isolated VLAN could still exploit unpatched device-level flaws (§8.1). Insider and physical threats sit with the operator — OpenNVR assumes a controlled physical perimeter, so locked racks, port security, and tamper detection are operator controls outside the architecture's scope (§8.2). Hardware supply-chain implants — undocumented SoC backdoors that would bypass network isolation entirely — remain a theoretical residual risk; no large-scale evidence exists in commercial cameras but the threat model acknowledges it (§8.3).

## Procurement use

If you're substituting OpenNVR for an FCC Covered List vendor (Hikvision,
Dahua, certain Hytera / Hangzhou Hikvision Digital Technology equipment) or
defending the architectural choice in a regulated procurement, see
[`GOVERNMENT_DEPLOYMENT.md`](GOVERNMENT_DEPLOYMENT.md). That page is a
printable one-pager for IT decision-makers; this page is the
implementation evidence behind it.

## The evidence pack

Everything on this page, as it stands on one deployment, in one zip:

```
python3 scripts/evidence_pack.py --url https://nvr.example.org --user admin --days 90
```

Posture, the camera security check, recording coverage, adapters and
fingerprints, installed apps with their signer and egress, firewall
rules and the audit log for the period, plus `EVIDENCE.md` (PASS /
ATTENTION / UNKNOWN per check, framework → files) and `manifest.json`
with the SHA-256 of every artefact. Read-only; credentials redacted.
[ENTERPRISE.md](ENTERPRISE.md#the-evidence-pack).

## Audit-chain quick reference

For an auditor asking "show me the evidence trail for an alert":

| Question | Where to find the answer |
|---|---|
| Which inference produced this alert? | Audit log → match `X-Correlation-Id` from the alert to the corresponding `inference.completed` event. |
| Which model weights were loaded at that time? | Capability registry snapshot at the inference timestamp → `model.fingerprint` (sha256). |
| Were the model weights tampered with between deployments? | `adapter.fingerprint_mismatch` audit events between deployments — drift is detected within 60s of polling. |
| Did any data leave the host during this period? | `inference.refused_sovereignty` events for any adapter that tried; empty = nothing crossed the boundary. |
| Who provisioned this camera, and over what transport? | `camera.created` and `camera.transport_security` audit events with operator user_id and the negotiated TLS policy. |

The audit log is JSONL on disk at `$KAI_C_AUDIT_LOG` (defaults to
`/var/log/opennvr/kai-c-audit.jsonl`) and also publishes to NATS on the
`opennvr.audit.*` subject scheme. Forward to SIEM via the
[`examples/alerts-subscriber`](../examples/alerts-subscriber) template.
