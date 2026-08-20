<!--
Copyright (c) 2026 OpenNVR
SPDX-License-Identifier: AGPL-3.0-or-later

NORTH-STAR / POSITIONING ONE-PAGER — proposed front door (README).
Lead with this; demote everything else one click deeper.
-->

# OpenNVR

**Your cameras. Your AI. Your hardware. Your control.**

OpenNVR is a self-hosted, security-first AI network video recorder. It started
by fixing the security holes that plague off-the-shelf NVRs, and it answers the
one question every other camera system dodges: *who is watching your footage,
and where does the AI run?* With OpenNVR the answer is always **you, on your own
box** — and if you ever want a bigger brain, you bring your own AI on your own
terms.

> Frigate gives you great local detection. The cloud cams give you a slick app.
> Nobody gives you **sovereign by default + bring-your-own-AI**. That's us.

---

## Talk to your cameras in 60 seconds

```bash
git clone https://github.com/open-nvr/open-nvr && cd open-nvr
examples/camera-agent/quickstart.sh
# open http://localhost:9100/demo, click Start, and SAY: "how many people are at the door?"
```

That's the **camera-agent** — a hands-free voice assistant, fully local, on a
normal CPU. Whisper hears you, a small local LLM reasons and calls the vision
tools, Piper speaks the answer back. No GPU required, no cloud account. One
command. (Have a GPU? On macOS/Windows the installer runs the LLM on the host
machine — Docker's VM can't reach the GPU, the host can — so answers arrive in
seconds instead of minutes. Still your box, still local.)

---

## One app, two ways to run, one brain dial

Same agent, your choice of interface and your choice of brain:

- **Voice** (default) — hands-free: speak, hear the answer. The flagship.
- **Chat** (`--chat`) — type, read. Same tools and scene description, no
  microphone/speaker, so it's lighter on a weak box.

And **where the AI brain runs** is the one dial that matters for sovereignty:

- **Local** (default, `local_only`) — the LLM runs on your box via Ollama.
  Nothing leaves the machine. Air-gap-clean, NDAA-minded. This is the promise.
- **Bring your own** (hybrid, opt-in) — point it at any OpenAI-compatible
  endpoint (`config.cloud.yml`); only the chat text goes out, audited, and the
  system says so out loud.

**Frames never leave your machine either way.** Run small on a laptop CPU
(`OLLAMA_MODEL=qwen2.5:0.5b`) or give it more headroom on a bigger box — same one
command. Model picks and hardware notes:
[`examples/camera-agent/MODELS_AND_LATENCY.md`](examples/camera-agent/MODELS_AND_LATENCY.md).

---

## Why it exists (the security story)

OpenNVR began as a fix for documented NVR security failures — the things that get
cameras banned from government buildings and leaked onto the open internet. The
posture is the product:

- **No vendor egress by default.** Under `local_only`, the AI sovereignty layer
  refuses any adapter that tries to phone home. Air-gap-clean, NDAA-minded.
- **Every AI call is audited** through the KAI-C connector — you can see exactly
  what ran, on what, and where.
- **You own the models.** Swap, upgrade, or bring your own via the open Adapter
  Contract. No black box, no forced cloud, no per-camera SaaS bill.

This is the moat. Lead with it.

---

## What it can do (examples gallery — opt in, don't get overwhelmed)

These are *recipes that showcase the platform*, not things you must learn to
start. Turn on what you need:

- **Ask your cameras** — "how many cars in the lot?", "is the gate open?",
  "did anyone walk past in the last 30 minutes?"
- **Standing monitors** — "tell me if more than 3 people gather at the entrance."
- **Smart alarms** — person-after-6PM, fire/smoke, custom windows; ringing
  banner; emergency-call hook (documented).
- **Open-vocabulary search** — "find a red truck" with no retraining.
- **Faces & watchlist** — enroll known people, flag unknowns.
- **Voice agent** — a spoken camera assistant with a talking
  avatar (the default voice mode).
- **Scheduled reports & webhooks** — recurring summaries, push to your tools.

Each is a demo of the same engine; none of them is *the* product. The product is
the sovereign platform underneath.

---

## The honest to-do (what still makes it easier to love)

We know the friction. The roadmap is about *removing doors*, not adding rooms:

1. **One repo, one quickstart.** Today the stack spans three repos
   (open-nvr / ai-adapter / kai-c) — the single biggest reason people bounce.
   Consolidate the getting-started path so `clone → up` just works.
2. **Lighter footprint.** The `--chat` mode already drops Whisper/Piper. Next:
   trim the vision image (BLIP is the heavy one — it carries PyTorch; whisper and
   piper are lean) so even the voice stack is smaller.
3. **Bring-your-own-AI, frictionless.** The OpenAI-compatible client already
   lets any provider be the brain; make it a one-line setting in the UI.
4. **Home Assistant / NVR integrations** so it slots into setups people already
   run.
5. **The agent goes where people already are.** Today it lives on its own demo
   page. Next: let the same camera-agent join **LiveKit rooms** (and similar
   real-time/voice surfaces) as a participant — so it's reachable from a phone, a
   meeting, or a kiosk, not just the local web UI. One sovereign agent, widely
   available.

---

## The path: popularity → community → business

The order matters. First make it *trivially runnable and obviously useful*
(one-command quickstart) so people star, fork, and file issues.
Then the sovereign + bring-your-own-AI angle earns the audience nobody else
serves — privacy-sensitive homes, schools, clinics, small public agencies, and
anyone who can't legally send footage to a vendor cloud. **That** audience is the
business: supported on-prem deployments, a managed control plane for fleets, and
certified hardware bundles — sold on the one thing the incumbents can't offer,
which is that the customer stays in control.

Sovereign by default. Your AI when you want it. That's the whole pitch.
