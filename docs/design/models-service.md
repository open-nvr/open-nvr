# OpenNVR Models — design

*Status: design, Sep 2026. The adapter-side reference implementation
(`opennvr_adapter_sdk.licensed_model`, the yolov8 adapter's licensed
path) ships with this document; the service itself is the next build.*

## What it is

**OpenNVR Models** is the platform's second revenue line after
enterprise deployments: fine-tuned detection models, trained on a
site's own footage, delivered as signed weight bundles to the
adapters a site already runs, under a per-site subscription. The
shape is Frigate+'s — the NVR is free, the better models cost a
modest yearly fee — with two things Frigate+ does not have: the
training data never leaves the site unless the operator sends it, and
the model that comes back is verified, fingerprinted and audited like
everything else on the platform.

The stock models are and stay free. A site that never subscribes
loses nothing it has today.

## Why sites will pay

The stock COCO detector is generic. A loading dock at night, a farm
gate with cattle and quad bikes, a hospital corridor with beds and
wheelchairs, a plate format the OCR has never seen — every site has
the ten things it actually cares about, and a generic model is
mediocre at all of them. A model tuned on *that site's* frames is
markedly better at those ten things and produces fewer false alerts,
which is the whole value of an NVR to an operator. The platform
already collects exactly the data such a model needs: evidence
crops, best frames, tracks, plate reads, and the operator's own
acknowledgements and dismissals in the alert inbox (a labelled
dataset, growing every day).

## Principles

1. **Open code, licensed weights.** Adapters remain open source under
   the org; the licence covers the weights bundle and the service that
   produces it. Nothing in the catalog is closed code
   ([APP_LISTING_TERMS.md](../APP_LISTING_TERMS.md)).
2. **The site decides what leaves.** Training data is exported by an
   explicit operator action, reviewed as a manifest before upload
   (counts per class, per camera, date range, sample thumbnails), and
   the export itself is an audit event. A site may instead train
   locally with the same tooling and never upload a frame.
3. **A licensed model behaves like every other model.** It loads
   from the adapter's weights volume, reports `model.fingerprint`,
   is subject to `AI_SOVEREIGNTY`, and KAI-C's fingerprint drift
   check covers it. The licence adds a signed manifest and an expiry;
   it does not add a network dependency at inference time.
4. **Offline first, always.** A present, verified bundle wins; the
   service is contacted only to fetch or renew. An expired subscription
   degrades to the stock model after a grace period, with a loud
   audit event — it never stops detection.
5. **No per-camera, no per-user, no metering.** One subscription per
   site, flat.

## The pieces

```
  site                                                        OpenNVR Models
  ─────────────────────────────────────────────────           ───────────────────────
  core ──► "OpenNVR Models" app (catalog, licence_key) ──┐
                │                                       │ HTTPS (declared egress:
                │ export manifest → operator approves   │  models.opennvr.org)
                ▼                                       │
        dataset export (crops + labels)  ───────────────┼──► POST /v1/sites/{site}/datasets
                                                        │
        adapter weights volume  ◄── signed bundle ──────┼──◄ GET  /v1/bundles/{model}@{version}
                │                                       │     GET  /v1/manifests/{model}   (signed)
                ▼                                       │     POST /v1/license/verify
     yolov8 / fast-plate-ocr adapter (verifies, loads,  │
     reports fingerprint + licence)                     ┘
```

### The app: "OpenNVR Models"

A catalog app under the org (`open-nvr/app-opennvr-models`), open
source, `pricing: subscription`, `entitlement: license_key`,
`network_egress: ["models.opennvr.org"]` — the only component of the
platform that talks to the service, and it says so on its card. It
holds the licence (core stores the key encrypted, delivers it on the
config poll, and the app's `verify_license` asks the service — plan,
expiry, the models the site is entitled to). Its UI:

* **Models** — what the site has (stock / licensed, version,
  fingerprint, trained on N frames from M cameras, expiry), and
  "update available".
* **Dataset** — what would be exported: counts per label and camera,
  date range, a sample; an *Export and upload* action (audited) and an
  *Export locally* action (a tarball in the recordings volume, for
  local training or a sneakernet upload).
* **Training** — request a fine-tune (which cameras, which classes,
  from which export), job status, the resulting bundle's evaluation
  against the previous one (precision/recall on a held-out slice of
  the site's own data — the number the operator cares about).
* **Install** — write the fetched, verified bundle into the adapter's
  weights volume and ask the adapter to reload
  (`POST /admin/reload`, an adapter-SDK endpoint guarded by the
  internal key). KAI-C sees the new fingerprint on its next poll and
  records `adapter.model_changed`.

The app needs nothing the SDK does not already give it: `nvr.timeline`
and the evidence API for crops, `nvr.alerts.inbox` for labels,
`nvr.state` for job bookkeeping, `entitlement` for the licence,
declared egress for the one host.

### The bundle

A bundle is a directory:

```
bundle/
  manifest.json        { model_id, version, framework, files: [{path, sha256, bytes}],
                         classes, input_size, trained_on: {site_id, frames, cameras, date_range},
                         license: {site_id, plan, issued_at, expires_at}, issuer }
  manifest.sig         Ed25519 signature of manifest.json by the OpenNVR Models key
  weights.onnx         (or the framework's native file)
  classes.json
```

The adapter verifies `manifest.sig` against the Models public key
compiled into the adapter SDK (rotatable: the SDK carries the current
and previous keys), then every file's SHA-256, then that
`license.site_id` matches the site and `expires_at` is in the future
or inside the grace window. Only then is the bundle loaded.
`model.fingerprint` is the manifest's sha256 — one string that
identifies the exact weights, the site and the licence.

### The adapter side (reference implementation, shipped)

`opennvr_adapter_sdk.licensed_model.ensure_licensed_model(dir, model_id, *,
license_key, models_url, site_id)`:

1. If `dir/manifest.json` exists, verifies signature, hashes and
   licence. Valid → return the bundle, **no network**. Expired past
   grace or tampered → fall through.
2. Fetch `GET {models_url}/v1/manifests/{model_id}` with
   `Authorization: Bearer <license_key>`; verify the signature; if the
   version equals the one on disk and the disk copy verified, refresh
   only the licence block.
3. Fetch each file in `files` to `<path>.part`, check its SHA-256,
   rename into place; write the manifest last. A killed download never
   leaves a bundle that verifies.
4. Return `LicensedModel(path, manifest, plan, expires_at, fingerprint)`.

`ModelInfo` gains an optional `license` block
(`{plan, expires_at, site_id, source}`) so KAI-C, the AI page and the
evidence pack can show which models are licensed and until when.
KAI-C ignores fields it does not know, so this is additive.

The yolov8 adapter is the reference: with `OPENNVR_MODELS_LICENSE_KEY`
and `YOLOV8_LICENSED_MODEL=<model_id>` set it loads the site's bundle
from `/app/model_weights/licensed/<model_id>/` instead of
`yolov8n.onnx`, and reports the licence in its capabilities. Unset,
nothing changes. The fast-plate-ocr adapter follows the same path for
site-specific plate formats.

### The service

A small HTTPS service at `models.opennvr.org` (self-hostable for the
enterprise tier — the same container, the operator's key):

| Route | Purpose |
|---|---|
| `POST /v1/license/verify` | `{key}` → `{valid, site_id, plan, expires_at, models: [...]}` — what the app's `verify_license` calls |
| `GET /v1/manifests/{model_id}` | The current signed manifest for this site's model (`Authorization: Bearer <key>`) |
| `GET /v1/bundles/{model_id}@{version}/{file}` | Bundle files, content-addressed |
| `POST /v1/sites/{site}/datasets` | An exported dataset (multipart, resumable) |
| `POST /v1/sites/{site}/jobs` · `GET …/jobs/{id}` | Request and follow a fine-tune |
| `GET /v1/models` | The catalogue of base models a site may start from |

Training runs on the service's GPUs from the site's dataset plus the
base model; evaluation is on a held-out slice of the site's own data
and is reported back with the bundle. Datasets are retained only as
long as the subscription and deleted on request — the terms say so
plainly, because the buyers this is for read terms.

### Keys and rotation

The Models signing key is Ed25519, offline, with the public half
compiled into the adapter SDK (`LICENSED_MODEL_PUBLIC_KEYS`, current +
previous). Rotation ships a new SDK release and re-signs manifests at
next fetch; bundles already on disk remain valid under the previous
key until the next fetch. Licence keys are opaque site tokens
(`omk_<site>_<random>`), revocable server-side; the app's next verify
reports it, the adapter's next fetch refuses, and the on-disk bundle
runs out its grace window.

## Pricing shape

Per site, per year, flat: **Site** (fine-tunes of the detector on the
site's own data, updates as the data grows), **Site + Plates** (adds
site-specific plate OCR), **Enterprise** (self-hosted service, the
operator's own signing key, SLA). No per-camera and no per-user
pricing; a site is a deployment. Figures are set at launch and shown
on the app's card as its `price_note`, the way every paid catalog app
shows its price ([DEVELOPER_PROGRAM.md](../DEVELOPER_PROGRAM.md#selling-your-app)).

The stock models remain free forever, in the image, without a key.

## What this is not

Not a cloud NVR: no video is streamed to the service, ever; only an
operator-approved dataset export of crops and labels. Not a lock-in:
the adapters are open, the bundle format is documented here, and a
site that stops paying keeps its last bundle through the grace period
and the stock model after. Not a metering product: nothing counts
cameras, frames or inferences.

## Build order

1. Adapter SDK `licensed_model` + yolov8 licensed path + `ModelInfo.license`
   (this PR, in the ai-adapter repository).
2. The "OpenNVR Models" app on the App SDK: licence hook, models view,
   dataset export (local first), install/reload.
3. The service: licence verify, manifests, bundles; a manual training
   loop behind it.
4. Automated fine-tune jobs; the evaluation report; the Enterprise
   self-hosted variant.

Each step is useful on its own: step 1 lets any adapter load a signed
bundle from disk today (a site can train locally, sign with its own
key, and run it), step 2 gives operators the dataset export even
before there is a service to send it to.
