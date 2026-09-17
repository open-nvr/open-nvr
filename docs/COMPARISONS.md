# Comparisons with Other NVR Projects

If you're evaluating OpenNVR against existing self-hosted or commercial options, this page is for you. We try to be honest about what each alternative does well — bashing competitors makes for poor positioning and worse engineering.

The short version: OpenNVR isn't trying to replace Frigate for the hobbyist Home Assistant user who's happy with what they have. We're solving a different problem — auditable AI surveillance with operator-controlled tactical AI — that the existing options either don't address or address only partially. If reading the comparisons below makes your current tool sound like the right choice, that's fine. We'd rather you pick the right tool than churn into ours.

## Quick orientation

Frigate is the most popular self-hosted NVR for homelab and Home Assistant users, with mature hardware-accelerated detection (Coral, OpenVINO, TensorRT, Hailo) and a recently expanded AI capability set (face recognition, LPR, CLIP semantic search, GenAI descriptions). New capabilities land in-tree under MIT. ZoneMinder is the twenty-year-old legacy choice — broad camera compatibility, deeply documented, GPLv2, designed before the modern AI-surveillance era. Shinobi is the prosumer-SMB option with a polished UI, narrower plugin AI surface, and a dual-licence model (GPLv3 / AGPLv3 community plus Shinobi Pro commercial). Viseron is the closest architectural peer — a small Python-native NVR with a clean detector abstraction, MIT-licensed, Home-Assistant-aligned. Verkada is the commercial cloud incumbent — proprietary, SaaS, vendor-managed AI, no operator-side infrastructure.

OpenNVR differs from all of them on the same five axes: a published Open Adapter Contract v1 so third-party AI ships outside the main repo under any compatible licence; an end-to-end `X-Correlation-Id` audit chain with sha256 model-fingerprint drift detection; two independent default-deny sovereignty gates (`DEPLOYMENT_MODE=offline`, `AI_SOVEREIGNTY=local_only`); a peer-citable architecture paper (DOI 10.5281/zenodo.22804254); and a voice-agent example. The sections below go through each comparison in prose — what each project does well, where the architectures genuinely diverge, and who should pick which.

## Frigate — the most-asked comparison

**What Frigate does well.** Mature, polished, deep Home Assistant
integration, broad hardware-accelerated detection (Coral, OpenVINO,
TensorRT, Hailo), HEVC handling via go2rtc, large engaged community,
clear documentation. The 0.14+ releases meaningfully expanded what
Frigate covers — CLIP / Jina semantic search, generative AI scene
descriptions via external providers (Ollama, Gemini, OpenAI), and
first-class face recognition + LPR landing in 0.16. For a homelab user
running 4–12 cameras with HA at the centre of their stack, Frigate is
the right answer today and we don't want to talk you out of it.

**Where the architectures diverge.** Frigate's capability set has grown
substantially; the architectural difference is *how new capabilities
get added*. Frigate's new task classes land in-tree — they're shipped
when the maintainers ship them, configured as first-party features.
OpenNVR ships an Open Adapter Contract v1 with a published wire spec,
an Apache-2.0 SDK, and a conformance test suite — any third party can
publish a contract-compliant adapter under any compatible licence, with
no in-tree change required. That matters for the audience that needs to
ship adapters under proprietary or classified licences, or whose
detection logic itself is operationally sensitive and can't sit in a
public repo.

The other architectural difference is the audit chain. Frigate has an
event database; OpenNVR threads an end-to-end `X-Correlation-Id` from
alert → middleware → adapter, polls model fingerprints every 60s for
drift, and produces an append-only audit log per inference with
`inference.refused_sovereignty` events for any adapter that tried to
egress under `local_only` policy. That's procurement-grade evidence
generation that Frigate's design doesn't aim at.

**Pick Frigate if.** Home Assistant is your control plane, the
shipping capability set (detection + face + LPR + CLIP search + GenAI
descriptions) covers your needs, and your security posture is "TLS
optional, run behind my home firewall." Frigate's HA integration and
Coral / TensorRT polish are years ahead of v0.1's.

**Pick OpenNVR if.** You need a published adapter contract so adapters
can ship outside the main repo under licences of your choosing, you
need an audit chain for compliance, or your threat model includes
"the vendor's cloud is part of the attack surface" (the paper's
explicit framing).

## ZoneMinder — the legacy choice

**What ZoneMinder does well.** Twenty years of operational maturity, broad
camera compatibility including very old / unusual hardware, well-known
to integrators, deeply documented for the deployment patterns that have
been stable for a decade.

**Where the architectures diverge.** ZoneMinder predates the modern AI
surveillance era and the security-by-default thinking that emerged
after the Mirai botnet and the Verkada breach. AI capabilities are
bolted on via plugins rather than first-class. Modern ZoneMinder
includes a setup-wizard rather than shipped default credentials,
but basic auth and plaintext RTSP are still common deployment
patterns in the wild — particularly on long-running installs that
predate the security-hardening updates. The architecture isn't wrong,
it just isn't shaped for the current threat model.

**Pick ZoneMinder if.** You have an existing ZoneMinder deployment that
works and you don't have AI / audit / sovereignty pressure that justifies
migrating.

**Pick OpenNVR if.** You're starting fresh, the existing ZoneMinder
posture won't survive your next compliance audit, or you need AI
capabilities ZoneMinder's plugin model doesn't reach.

## Shinobi — the prosumer / commercial-adjacent option

**What Shinobi does well.** Polished web UI for non-technical operators, clear commercial-support path, broad protocol support, motion-detection out of the box. Shinobi serves a "prosumer SMB" segment well. Default install runs a setup wizard rather than shipping credentials, and TLS is optional and operator-configured.

**Where the architectures diverge.** Shinobi's plugin system is narrower
than OpenNVR's adapter contract — most AI work is done through a small
fixed set of integrations rather than a published contract any model can
implement against. Sovereignty posture isn't a first-class concern of
the design.

**Pick Shinobi if.** You want a polished commercial-support relationship
with a smaller vendor and your AI needs fit what Shinobi already does.

**Pick OpenNVR if.** Your AI needs are non-standard, your compliance
auditor wants an architectural paper trail, or you want a fully open
Apache-2.0 SDK so you can ship adapters under any licence.

## Viseron — the closest peer

**What Viseron does well.** Modern Python codebase, clean detector abstraction, Home Assistant alignment, MIT licence, small focused codebase that's actually readable. Viseron deserves more attention than it gets. Configure-on-first-boot rather than shipped default credentials; TLS is optional and operator-configured.

**Where the architectures diverge.** Viseron and OpenNVR are
solving overlapping problems with different priorities. Viseron emphasises
HA integration and a clean detector model; OpenNVR emphasises the
contract-driven AI breadth and the audit chain. Viseron doesn't have a
voice agent, doesn't have model-fingerprint drift detection, doesn't
have the sovereignty enforcement layer, and isn't anchored to a
published architectural paper.

**Pick Viseron if.** You want a Python-native, HA-aligned NVR with a
clean detector abstraction and a small codebase you can actually read
through.

**Pick OpenNVR if.** You need any of the security / audit / sovereignty
controls that aren't Viseron's design centre, or you need the broader AI
plugin scope.

## Verkada — the commercial cloud incumbent

**What Verkada does well.** Polished, well-supported, comprehensive hardware portfolio, easy operator UX, no infrastructure for the customer to maintain. TLS is vendor-managed end-to-end and credential management is SaaS-controlled — the customer never sees or holds the keys.

**Where the architectures diverge.** This is the comparison the paper
names directly. The 2021 Verkada aggregation-layer breach — privileged
cloud credentials compromised, ~150,000 cameras across hospitals, schools,
and enterprises simultaneously exposed — illustrates the systemic risk
the offline-first model is explicitly designed to eliminate. Verkada's
architecture trades operator control for vendor convenience; OpenNVR
trades the other way.

**Pick Verkada if.** Your buyer's preference is for a SaaS commercial
relationship and your threat model genuinely doesn't include vendor-side
compromise.

**Pick OpenNVR if.** Your threat model includes vendor-side compromise
(it should, per the paper's §2.1 evolution of attack sophistication), or
your compliance environment requires customer-managed encryption keys, or
you're substituting for FCC Covered List equipment and you'd rather not
swap one centralised aggregation vendor for another.

## Honest about where we're not yet competitive

A v0.1 release earns its honesty section. The polish and UX of Frigate, Shinobi, and Verkada are years ahead of OpenNVR's web frontend today. Hardware-accelerated detection coverage — Coral, OpenVINO, TensorRT, Hailo — is broader in Frigate than in what the `ai-adapter` repo ships out of the box, though anything reachable from Python is one adapter away. HEVC reliability via MediaMTX is acceptable but Frigate's go2rtc handles cheap HEVC cameras better; go2rtc evaluation is on the v0.3 roadmap. Frigate's Home Assistant integration is deeper than the `home-assistant-relay` bridge OpenNVR ships as an example. And Frigate has five years of accumulated users and contributors; OpenNVR is still building that community.

None of that is an argument that those advantages don't matter — they do. The argument is that the audience for whom the audit chain, sovereignty enforcement, contract-driven AI, and published architectural paper *matter* is large enough, and underserved enough by the existing options, to be the right thing to build alongside the polish work.

## How to evaluate honestly

If you're shortlisting NVRs, the questions worth asking each candidate are:

1. **Where do model weights physically live, and who has the keys?**
   (OpenNVR: your hardware, your `CREDENTIAL_ENCRYPTION_KEY`.)
2. **What happens to my video if your cloud goes down or gets compromised?**
   (OpenNVR: nothing — we're not in the path.)
3. **Can I prove to an auditor that no inference data left my network?**
   (OpenNVR: `inference.refused_sovereignty` events in the audit log.)
4. **What does it take to add a new AI capability?**
   (OpenNVR: ~30 lines of Python following the SDK + a Dockerfile.)
5. **Where is your threat model published?**
   (OpenNVR: [DOI 10.5281/zenodo.22804254](https://doi.org/10.5281/zenodo.22804254).)
6. **Honest about gaps?**
   (OpenNVR: see this page + [ROADMAP.md](ROADMAP.md) + paper §8.)

Run those questions against each candidate. If the answers favour
something else, pick that. If they favour us, the rest of the docs are
where you start.
