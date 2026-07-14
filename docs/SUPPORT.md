# Support

OpenNVR has two support tracks. The community track is free and
operates through public channels. The commercial track is paid and exists
for deployments where the community pace and best-effort guarantees
aren't enough — typically regulated, mission-critical, or large-scale
installations.

If you're not sure which you need, default to community. Most users
never need to leave it.

## Community support (free)

### GitHub Discussions

Best for: questions, design discussions, deployment patterns, "is anyone
else seeing this?"

https://github.com/open-nvr/open-nvr/discussions

Discussions get answered by maintainers and other users. Response time
is typically a day or two; if you've been waiting longer, ping the
thread. Lurkers genuinely benefit from public answers, so prefer this
over private DMs.

### GitHub Issues

Best for: confirmed bugs, feature requests with a clear shape, security
fixes that don't need private disclosure.

https://github.com/open-nvr/open-nvr/issues

Please don't open issues for "how do I…?" questions — those belong in
Discussions, where the answer becomes searchable for the next person
with the same question.

For security vulnerabilities, see [SECURITY.md](../SECURITY.md) — those
go through coordinated disclosure, not public Issues.

### Documentation

Most operator questions are already answered in the docs. For getting it running and using it day-to-day, [DOCKER_QUICKSTART.md](../DOCKER_QUICKSTART.md) covers install and common operations and [USER_MANUAL.md](../USER_MANUAL.md) covers the web UI. For deciding whether OpenNVR is the right fit, [USE_CASES.md](USE_CASES.md) walks through per-industry fit and [COMPARISONS.md](COMPARISONS.md) is the honest head-to-head with alternatives. For regulated and procurement-driven deployments, [COMPLIANCE.md](COMPLIANCE.md) maps the architecture to frameworks and [GOVERNMENT_DEPLOYMENT.md](GOVERNMENT_DEPLOYMENT.md) is the procurement brief. For the deeper engineering questions, [SECURITY_ARCHITECTURE.md](SECURITY_ARCHITECTURE.md) holds the threat model and [LOCAL_SETUP.md](LOCAL_SETUP.md) the bare-metal dev shell, and [ROADMAP.md](ROADMAP.md) covers what's next.

If you read the doc that should answer your question and it doesn't, that's a signal the docs have a gap — open a Discussion and we'll close it, both for you and for everyone else who'd have hit the same wall.

### Real-time channels

Coming with the v0.1 launch wave: a community chat (Discord or Matrix —
to be announced when set up). Until then, Discussions is the right
synchronous-feeling channel; maintainers watch it actively.

---

## Commercial support (paid)

Contact: **[contact@opennvr.org](mailto:contact@opennvr.org)**

Commercial support exists because some deployments need things community
volunteers can't offer at scale: response-time guarantees, deployment
help on hardware we don't have, custom adapter authoring under NDA,
compliance evidence packs, and indemnification language that a
procurement officer will accept.

What commercial support buys is **time, risk-transfer, and roadmap
acceleration — not private capability access**. Every shipped feature
in `main` is available to community users on identical terms. Sponsored
development (described below) accelerates *when* a public-roadmap
capability ships, not *who* can use it once it does. Custom adapter
authoring under NDA is the one path that stays private — those
adapters are the customer's IP, not OpenNVR's — but the underlying SDK
and contract that make them possible remain Apache-2.0 and open to
every adapter author.

### What's available

#### Deployment assistance
Hands-on help getting OpenNVR running in your environment. Particularly
relevant for multi-site deployments, regulated environments where you
need the install evidenced, or hardware platforms (ARM SBCs, specialised
accelerators, embedded x86) where the default install path needs
adjustment.

#### Custom adapter authoring
We build adapters for your AI capabilities — under NDA where needed,
delivered as contract-compliant containers you own. Useful when you have
in-house models you can't open-source, or when the model class you need
isn't in the shipped seven and you'd rather not author the adapter
yourself.

#### Compliance evidence packs
ISO 27001 / SOC 2 / HIPAA / FedRAMP / similar — we deliver the audit
artefacts mapped to your specific framework, including the audit-log
queries, control-mapping spreadsheets, and operator-runbook templates
the auditor will want. Builds on the public [COMPLIANCE.md](COMPLIANCE.md)
but tailored to your control framework.

#### SLA-backed incident response
Defined response and resolution windows for production incidents.
Tiered by severity and customer profile. Includes the security
vulnerability disclosure path (faster than the community channel for
deployments where speed matters).

#### Architectural review
For deployments where the OpenNVR architecture interacts with your
existing security, network, or AI infrastructure in non-trivial ways —
a senior reviewer walks through the integration with your team, surfaces
the risks, and signs off on the deployment shape. Particularly useful
for procurement engagements where the buyer's CISO needs a name on the
review.

#### Training
For operations teams who'll be running OpenNVR day-to-day, or for
in-house engineering teams who'll be authoring adapters. Half-day
operator track, two-day developer track, or custom curriculum.

#### Sponsored development
For specific capabilities you'd like prioritised on the public roadmap —
roadmap acceleration with public release. Different from custom adapter
authoring (which is private to you). Useful when your capability has
broader applicability and you'd rather pay for it to be a first-class
ship.

### What's *not* available

A few things are explicitly outside the scope of what commercial support buys. There are no private forks — all sponsored development lands in main, and AGPL makes private forks impractical anyway, but we wouldn't offer one even where the licence permitted. There are no capability gates: paid support buys time and risk-transfer, not features. There's no 24/7 emergency hotline for community-tier users — community support is best-effort, and follow-the-sun coverage is a commercial-tier conversation. And there are no reseller or white-label arrangements at this stage; v0.1 commercial engagement is direct between the customer and the project's commercial entity, which may evolve later.

### How to engage

Email **[contact@opennvr.org](mailto:contact@opennvr.org)** with:

1. **Your deployment shape** — what you're trying to do, what hardware /
   network / scale you're working with, what's blocking you.
2. **What you've tried** — community channels checked, docs read,
   approaches attempted. Helps us figure out whether commercial
   engagement is the right call or whether a Discussion would solve it
   for free.
3. **Your timeline** — what's driving the engagement, when do you need
   what.

A scoping call follows. We'll be honest about whether the engagement
makes sense; we'd rather you spend your budget where it actually moves
your project, even if that means we recommend you don't engage us.

### Compliance and contracting

For procurement that needs vendor onboarding paperwork (DUNS, SAM,
similar), security questionnaires, MSAs, or insurance certificates —
all standard, all handled through the same contact. Allow 2-3 weeks
of lead time for procurement-heavy contracts.

---

## How the project sustains itself

Worth being transparent: the project's existence over time depends on
commercial-support revenue plus community contribution. AGPL-licensed
deployments are free to use under the licence terms; what funds
maintenance, new adapter development, and the roadmap is paid
engagements with the deployments that need that level of support. If
that model is uncomfortable for your organisation, the codebase is
yours under AGPL — fork it, run it, modify it. That's the bargain the
licence makes explicit.

We mention this because some open-source projects pretend they don't
have a sustainability model and then surprise their users when one
materialises. We'd rather you know the shape up front.
