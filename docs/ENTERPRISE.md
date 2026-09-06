# OpenNVR for the enterprise

OpenNVR is free software; every capability on this page ships in
`main` under AGPL-3.0 and runs on hardware you own. What an enterprise
buys is not access — it is *assurance*: a deployment that is evidenced,
supported, and defensible in front of a procurement officer, an
auditor or a regulator. This page is the whole offer.

## Who this is for

Sites that cannot use the incumbents. Government, defence and critical
infrastructure bound by FCC §889 / NDAA covered-vendor rules. Hospitals,
utilities, ports, campuses and manufacturers whose video may not leave
the premises, whose AI may not run on someone else's cloud, and whose
compliance function needs evidence rather than a vendor's word. Sites
under GDPR or India's DPDP Act that need local processing, operator-held
keys and a record of who saw what.

The platform's answer to each of those is architectural, not
contractual: an offline-first posture that refuses cloud routes by
default, AI sovereignty enforced at adapter registration, an
append-only audit trail with a boot-time posture record, cameras on an
isolated network, apps confined to an internal network with enforced
egress, catalog images built from source and signed. The
[security architecture](SECURITY_ARCHITECTURE.md) and the
[compliance mapping](COMPLIANCE.md) document the controls; the
[reference appliance](REFERENCE_APPLIANCE.md) is the shape a site
follows to get them all; the evidence pack below proves them.

## What an engagement includes

**Deployment on the reference appliance.** We size the site
(cameras, retention, AI), specify the hardware or validate yours
against [REFERENCE_APPLIANCE.md](REFERENCE_APPLIANCE.md), install on
your network, and hand over with the hardening checklist complete and
the first evidence pack all-pass. Air-gapped installs are the normal
case, not the exception — images arrive on media, signatures are
verified offline with `INSTALLER_SIGNATURES` set knowingly, and the
[apps-index pinning](APPS_INSTALL.md#image-signing) is done before
the media is cut.

**The compliance evidence pack.** Generated from your deployment by
`scripts/evidence_pack.py` (below), reviewed and annotated by us
against *your* control framework — ISO 27001, SOC 2, NIST CSF, HIPAA,
IEC 62443, the DPDP Act — with the control-mapping spreadsheet, the
audit-log queries and the operator runbooks an auditor asks for.
Delivered at hand-over and on the cadence your audit cycle needs.

**§889 attestation.** For procurement that must show no covered-vendor
equipment: the platform's built-in security check flags Covered List
cameras from the inventory; OpenNVR Scout is the formal assessment
(OEM-rebrand resolution, CVE cross-reference, signed report) —
[GOVERNMENT_DEPLOYMENT.md](GOVERNMENT_DEPLOYMENT.md).

**Support with response times.** Named severities, defined response
and resolution windows, a private vulnerability-disclosure path, and
an upgrade cadence agreed with your change board: tested images on a
schedule, release notes read for you, nothing surprising on a Friday.

**Custom AI under your control.** Adapters for models you cannot
share (built under NDA, delivered as contract-compliant containers you
own), fine-tuning on your footage that never leaves the site, and
apps built on the App SDK for your workflow — open source in the
catalog if you choose, private to you if you must.

**Architecture review and training.** A named reviewer signs off the
integration with your network, identity and SIEM; a half-day operator
track and a two-day developer track for your teams.

What it never includes: a private fork, a feature only paying
customers get, or a cloud service holding your video. Every capability
above lands in `main`; the [support page](SUPPORT.md) says so in more
words, and why.

## The evidence pack

```
python3 scripts/evidence_pack.py --url https://nvr.example.org --user admin --days 90
```

A read-only tool that asks the deployment's own API the questions an
auditor asks and writes the answers into one zip: posture
(`deployment_mode`, `ai_sovereignty`, plaintext outputs), the camera
security check (covered vendors, public IPs, plaintext streams,
default credentials), recording coverage against the retention period,
AI adapters with their model fingerprints and declared egress,
installed apps with their signer and what they may reach, the firewall
rules, and the audit log for the period — plus `EVIDENCE.md`, a
readable report that marks each check **PASS / ATTENTION / UNKNOWN**
and maps every framework to the files that evidence it, and
`manifest.json` with the SHA-256 of every artefact so the pack is
tamper-evident. Camera stream URLs are redacted of credentials before
they are written; nothing leaves the site but the zip you carry.

An all-pass pack is the hand-over criterion of an engagement and the
monthly artefact a compliance function files. A pack with ATTENTION
rows is a work list. Run it yourself, on any deployment, today.

## How it is priced

Per site, per year, sized by the appliance tier (S / M / L) and the
support level; custom AI and Scout assessments are scoped work. There
is no per-camera licence, no per-user seat and no charge for the
software. Ask for a quote with the site's shape (cameras, retention,
AI, network) and the framework you report under; a scoping call and
an honest answer follow — including "you do not need us" when a
community deployment will do.

Contact: **[contact@opennvr.org](mailto:contact@opennvr.org)** — vendor
onboarding paperwork, security questionnaires, MSAs and insurance
certificates are handled through the same door
([SUPPORT.md](SUPPORT.md#compliance-and-contracting)).

## Why an open platform is the safer buy

Because the alternative is trust without evidence. A closed NVR asks
you to believe its cloud is sovereign and its firmware is clean;
OpenNVR shows you — every line of the middleware is auditable, every
model is fingerprinted, every app's network reach is declared,
enforced and logged, every image is built from public source by the
project's CI and signed. When the auditor asks "how do you know", the
answer is a file in the evidence pack, not a slide from a vendor.
