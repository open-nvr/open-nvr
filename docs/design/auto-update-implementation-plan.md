# Implementation plan: OpenNVR updates (check, one-click, automatic, air-gapped)

| | |
|---|---|
| **Status** | M0–M3 done; M4 policy done (see [Progress](#10-progress)) |
| **Revision** | r2 (2026-10-29): review fixes folded in; the updater runs inside the core image; manifests are signed with a project Ed25519 key so online and air-gapped verification are the same code. r1 (2026-10-28): first plan |
| **Branch** | `feat/auto-update` (one commit per issue) |
| **Audience** | Whoever implements or reviews this work |

## 1. Goal and non-goals

**Goal.** An operator updates an OpenNVR site to a newer **release** safely, online or fully air-gapped:

1. **See** that an update exists (UI banner, Home Assistant's update entity).
2. **Apply** it with one command on the host or one click in the UI.
3. **Optionally** let it update itself in a maintenance window.
4. **Air-gapped:** carry one signed file in (USB, data diode) and apply it with the same command, the same checks and the same rollback.

Every update is verified, backed up first, health-checked afterwards and rolled back automatically if anything fails.

**Non-goals (v1):** applying builds from `main` automatically (edge is notify/CLI only), Postgres major upgrades, host OS updates (`routers/firmware.py`, a different page), Watchtower-style blind refreshes.

## 2. Facts this plan builds on

| # | Fact | Where | Consequence |
|---|---|---|---|
| F1 | Opt-in check of the latest GitHub release (`UPDATE_CHECK=true`), shown as `latest_version` in system info; the HA update entity reads it. | `server/services/update_check.py`, `routers/system.py:61-76` | "Notify" exists in the backend; the UI does not show it. |
| F2 | Installs are `git clone`s pinning `CORE_TAG`/`ADAPTER_TAG` in `.env`. Releases change compose files too. | `README.md:37`, `.env.example:536-600` | An update moves the checkout **and** the pins. |
| F3 | The README's upgrade line re-pulls the pinned tag: it updates nothing. | `README.md:58` | Fixed in UPD-002. |
| F4 | `:latest` moves on every main push, not only releases. | `publish-images.yml:119,:319`, `publish-detect-pipeline.yml:93`, `build-catalog-app.yml:139` | Fixed in UPD-001. |
| F5 | Core runs Alembic on boot (`init_db`). | `core/database.py:303-330` | The updater never runs migrations; rollback restores the DB dump. |
| F6 | The README says to clone without a tag, so a client checkout can be any `main` commit while its images are pinned to 0.1.8. | `README.md:37` | Preflight detects a checkout that is not at a release commit and refuses with guidance (UPD-206). |
| F7 | `__version__` is a hard-coded string (main, 151 commits past v0.1.8, still says 0.1.8) and `/health` returns only that. | `server/main.py:114,1040` | Images carry their build commit (UPD-003); verification compares image IDs, not version strings. |
| F8 | `start.sh up` runs `compose up -d --remove-orphans`, which removes the fakecams/apps-profile containers it does not know. | `start.sh:1381` | The updater brings the stack up the same way but never with `--remove-orphans`. |
| F9 | Mediamtx (the recorder) is its own container; compose recreates only services whose image or config changed. | `docker-compose.yml:99,:545` | A core-only update does not stop recording. |
| F10 | State outside Postgres: volumes `opennvr_jwt_keys`, `opennvr_kai_c_state` (both small), and recording-tree identity markers. | `docker-compose.yml:946-966`, `services/camera_identity.py` | Rollback restores the two volumes too; a release that changes an on-disk format sets `state_migrations` and is never rolled back automatically. |
| F11 | `cryptography` and `httpx` are direct dependencies of core. | `server/pyproject.toml` | Signature checks and downloads need no new package. |
| F12 | nginx takes bodies up to 256 MB. | `nginx/opennvr.conf:62` | A multi-GB offline bundle is applied from the host, not uploaded in the browser. |

## 3. How to read this
- Milestones **M0–M5** of issues **UPD-xxx**, in execution order. M0 ships on its own. M1+M2 give a working, safe `start.sh update` (online and from a bundle). M3 adds the UI, M4 automatic updates, M5 delta bundles, edge and the local mirror.
- Sizes: S ≤ 1 session, M = 2–3, L = 4+.

## 4. Key decisions

| # | Decision | Why |
|---|---|---|
| D1 | **A signed release manifest is the only thing the updater trusts**: version, git commit, every image's registry digest **and** image ID, Alembic head, `min_upgrade_from`, `manual_steps`, `state_migrations`, `urgent`. Signed with the project's **Ed25519 release key** (CI secret); the public keys ship in the core image. | Digests pin content, the signature says who pinned them. Ed25519 verifies with `cryptography`, fully offline: online and air-gapped are one code path. Cosign image signatures (UPD-004) stay as defence in depth, not as the trust root. |
| D2 | **The updater lives in the core image** (`server/updater/`) and runs as `docker compose run --rm --no-deps opennvr-core python -m updater …`. The **installed** release's image runs preflight; the **target** release's image runs verification. The host does only what must happen on the host: git, `docker pull/tag/load/inspect`, `compose up`, dump/restore, file copies — a small verb set in `start.sh` and `start.ps1`. | No second image to publish and sign; the logic is Python with unit tests, written once; compose runs where it always runs, so bind paths are right on Windows too. |
| D3 | **Download first, swap second**: pull or load every image and fetch the commit before anything stops. | Most failures (network, disk, a bad bundle) happen with zero downtime. |
| D4 | **Back up before switching**: `pg_dump -Fc` run in the `db` container as `POSTGRES_USER` and copied out (never redirected through a PowerShell pipe), plus a tar of `opennvr_jwt_keys` and `opennvr_kai_c_state`. Kept in `backups/` (git-ignored, mode 600), last 3. | Restore is deterministic; Alembic downgrades on client data are not. |
| D5 | **Channels**: `stable` (release tags, the default) and `edge` (main builds pinned by commit, never applied automatically). | Client sites run releases. |
| D6 | **Levels, off by default**: off → notify → manual → auto (window, patch releases only unless widened, random delay, `urgent` skips the delay). | Offline-first: nothing calls out unless asked. |
| D7 | **`.env`**: backed up, and only the pin keys the manifest lists are edited. A pin that differs from the installed release's manifest value was set by the operator on purpose: left alone, with a warning. | `.env` belongs to the operator. |
| D8 | **Run state lives on the host** (`data/updates/state.env`), and core reads it through a read-only mount; core asks for an update by writing `data/update-requests/request.env`, which the host runner picks up (r2.2: replaces the planned DB copy, which `psql` from the host would have had to keep in step). | A DB restore must not erase the record of the run doing it; status must be writable while core is down. |
| D9 | **Images are pulled by digest and then tagged locally** with the pinned tag; compose keeps using tags. Verification compares the running containers' image IDs with the manifest. From a bundle, `docker load` brings the tags and the IDs are checked the same way. | One mechanism for online and offline; a tag cannot be swapped under us. |
| D10 | **Checkout**: `git checkout -B opennvr-release <commit>` after checking the commit is the manifest's; a tracked file changed by the operator refuses the update and lists the files. | No detached HEAD; never overwrite an operator's edits. |
| D11 | **Air-gapped**: one file per release (`opennvr-<ver>-offline.tar`: signed manifest, `git bundle`, `docker save` of every image), applied with `update --bundle <file>`; delta bundles carry only changed images; a site with many NVRs can run a local mirror that the updater points at instead of GitHub. Verification is never switched off. | The most security-sensitive sites get offline verification, not no verification. |

**Rejected:** core holding the Docker socket; a separate updater image (r1 D2); Sigstore keyless as the trust root (needs Rekor/Fulcio trust material offline, a second code path); Watchtower; skipping verification air-gapped.

## 5. Update flow

```
host (start.sh / start.ps1 update)                 updater (python -m updater, in a core image)
───────────────────────────────────                 ─────────────────────────────────────────────
1 lock data/updates/.lock
2 run INSTALLED core image: check ───────────────▶  fetch manifest (GitHub, mirror, or bundle),
                                                     verify signature, compare versions, policy,
                                                     min_upgrade_from, manual_steps, pins vs .env,
                                                     checkout state ─▶ data/updates/plan.json + plan.env
3 git fetch <commit> (or from the bundle); docker pull @digest + tag (or docker load); check IDs
4 pg_dump + volume tars ─▶ backups/; state.json "switching"
5 git checkout -B opennvr-release <commit>; .env pins (backup first); compose up -d (no --remove-orphans)
6 run TARGET core image: verify ─────────────────▶  core /health = target version + commit,
                                                     alembic head = manifest, (host passes the
                                                     running image IDs), cameras recording again
7a ok: state "succeeded", prune backups
7b fail: checkout old commit, restore .env, compose up -d; if the Alembic head moved (and the
   release has no state_migrations): stop core, pg_restore, restore volumes, start core;
   run INSTALLED image: verify again; state "rolled_back" (or "failed", stack on old images)
```

An interrupted run (power cut) is found by the next `update`: it resumes at step 6, then finishes or rolls back.

**Learned while building M1–M2 (r2.1):**
- A release from before the updater (0.1.8) has neither `start.sh update` nor the updater in its image, so every site makes one move by hand (DOCKER_QUICKSTART.md) onto the first release that has it.
- On Docker's containerd image store an image's ID **is** its registry digest; on the classic store it is the config digest. Verification accepts either (`check.accepted_ids`).
- Platforms are named `linux/arm64` by Docker but `linux/arm64/v8` in some indexes; matched with and without the variant.
- An offline bundle should carry the manifests of earlier releases (`history/vX.Y.Z/`), so a site with no record of its installed release can still be verified offline.
- `--adopt` means the release's pins and images replace the site's (found on a live dry run).

**UPD-207 live test (2026-10-29).** Three releases built from this branch (0.2.0; 0.2.1 adds a migration; 0.2.2 adds another and reports a wrong version), installed and updated from signed offline bundles with marker data in the database:

| | Windows (`start.ps1`) | Linux (`start.sh`) |
|---|---|---|
| 0.2.0 → 0.2.1 | updated, verified, marker kept (96 s) | same |
| 0.2.1 → 0.2.2 (broken) | caught, rolled back: 0.2.1 again, database restored (0.2.2's table gone), marker kept | same |
| Tampered bundle | refused before anything changed | — |
| `--restore` | brought a half-rolled-back site back | — |

Found and fixed on the way: Windows PowerShell 5.1 drops the double quotes inside native arguments, so the restore's SQL reached psql as `-c DROP` (database steps now run from a script copied into the container); `--restore` copied `.env` before checking the database was up; any git failure was reported as "not a git checkout" (now git's own words, e.g. *dubious ownership* under sudo). Not covered live: Docker's classic image store (both engines used containerd; unit-tested). Also found, not in this branch: `scripts/generate-secrets.ps1`, `generate-mediamtx-certs.ps1` and `start-mediamtx.ps1` cannot be parsed by Windows PowerShell 5.1 (UTF-8 without BOM).

## 6. Engineering rules
- One issue, one commit on `feat/auto-update` (`feat(update): … (UPD-xxx)`), no Co-Authored-By trailer; not pushed without the owner's go-ahead.
- **Test as a migration**: live tests upgrade an existing stack with its data.
- Never `--remove-orphans`; never edit `.env` outside the pin keys; never log secrets.
- Schema changes: model + Alembic migration with `downgrade()`; nullable or defaulted columns.
- New permission `system.update`, superuser only, backfilled without changing anyone's abilities.
- Every state-changing endpoint is audited.

## 7. Milestones and issues

### M0: Release hygiene
| ID | Size | Issue |
|---|---|---|
| UPD-001 | S | `:latest` moves only on release tags (all image workflows); comments match. |
| UPD-002 | S | README/quickstart upgrade steps that work (checkout the tag, set pins, back up, up), until the updater exists. |
| UPD-003 | S | **Build identity**: `VERSION` and `GIT_COMMIT` build args → image labels and env; `/health` and system info report `version` and `commit`. |
| UPD-004 | M | Cosign-sign core and detect-pipeline images as app images already are (defence in depth). |

### M1: Signed release manifest
| ID | Size | Issue |
|---|---|---|
| UPD-101 | M | `server/updater/manifest.py`: schema, canonical bytes, Ed25519 sign/verify (key rotation: several trusted public keys), version ordering. Unit tests. |
| UPD-102 | M | `scripts/release/make_manifest.py`: collect pins and images from compose + `.env.example`, resolve digests and image IDs from GHCR, sign with `RELEASE_SIGNING_KEY`. Release workflow attaches `release-manifest.json` (+ `.sig`). |
| UPD-103 | S | Backfill a signed manifest for v0.1.8 (`workflow_dispatch`), so today's sites have a starting point. |

### M2: The updater (CLI, online and bundle)
| ID | Size | Issue |
|---|---|---|
| UPD-201 | L | `python -m updater check|verify|finish`: manifest sources (GitHub, mirror URL, bundle dir), preflight, plan output (`plan.json`, `plan.env` for bash), verification. Unit tests. |
| UPD-202 | M | `start.sh update [--to X] [--bundle F] [--dry-run] [--yes]` host verbs; `start.sh up` without `--remove-orphans` for the updater; lock; resume. |
| UPD-203 | M | `start.ps1 update` with the same verbs. |
| UPD-204 | M | Backups (DB + volumes), rollback and restore paths; `update --restore <backup>` escape hatch. |
| UPD-205 | S | `.gitignore` for `backups/` and `data/updates/`; files mode 600. |
| UPD-206 | S | Unmanaged checkout (not at a release commit) and operator-edited files: refuse with exact guidance. |
| UPD-207 | L | E2E upgrade test: previous release with data → candidate; forced failure → rollback restores old version and data. |

### M3: In the product
| ID | Size | Issue |
|---|---|---|
| UPD-301 | M | `system_update_runs` table (copy of host state), policy in `SecuritySetting` `opennvr_update_policy`, `system.update` permission. |
| UPD-302 | M | API: status, policy, request an update (target must equal the verified available version), cancel while queued. Audited. |
| UPD-303 | M | Host runner: `start.sh auto-update enable|disable|status` installs a systemd timer / launchd job / Scheduled Task running `update --from-queue` each minute; heartbeat. |
| UPD-304 | M | UI: Settings → OpenNVR updates (version, notes, Update now, policy, history), banner, progress that survives the core restart. |
| UPD-305 | S | HA update entity install, behind a separate token scope that is off by default. |

### M4: Automatic
| ID | Size | Issue |
|---|---|---|
| UPD-401 | M | Auto policy: window (site-local), patch-only by default, 0–72 h random delay, `urgent` skips the delay, never with `manual_steps`/`state_migrations`. |
| UPD-402 | S | Signed revocation list: a revoked release is never offered. |
| UPD-403 | S | Success/rollback/failure through the alert routes and HA. |

### M5: Air-gapped and edge
| ID | Size | Issue |
|---|---|---|
| UPD-501 | M | Offline bundle: release workflow builds `opennvr-<ver>-offline.tar` (manifest + sig, `git bundle`, `docker save`); `update --bundle` applies it. |
| UPD-502 | S | Delta bundle against a base version (only changed images). |
| UPD-503 | S | Local mirror: `UPDATE_SOURCE_URL` (manifest location) + registry override, so many sites update from one internal mirror. |
| UPD-504 | S | Edge channel: manifests for main builds, notify/CLI only. |

## 8. Risks
| Risk | Mitigation |
|---|---|
| A bad release reaches every site | Patch-only auto, random delay, revocation, the upgrade test blocks the release, rollback. |
| Rollback loses data written during the update window (alerts, events, recordings index) | Minutes long; stated in the UI before *Update now*. |
| Release signing key leaks | Several trusted keys in the image so a key can be rotated by a release; the key lives only in the CI secret store. |
| Operator edited tracked files | Refuse and list them; never stash or overwrite. |
| Windows hosts (WinNAT port reservations, stale D: share) | The updater reuses `start.ps1`'s port preflight; a failed verify rolls back. |
| A release needs a new required `.env` secret | `manual_steps`: auto skips it, manual shows the command. |

## 9. Open questions
1. Client host OSes (decides how much Windows testing the first release needs).
2. Default maintenance window (suggested Sunday 02:00–04:00 site time).
3. Air-gapped clients: one NVR per site or many (decides UPD-503's priority).
4. Who creates and holds the release signing key (needed before UPD-102 can run in CI).

## 10. Progress
| Issue | Status | Commit | Notes |
|---|---|---|---|
| UPD-001 | Done | 82025f6b | core, detect-pipeline, camera-agent; catalog apps keep :latest on main on purpose |
| UPD-002 | Done | cfd55ce0 | DOCKER_QUICKSTART.md "Upgrading to a new release" |
| UPD-003 | Done | bc55573a | /health reports `commit`; system info untouched (HA contract) |
| UPD-004 | Done | 43a160bb |  |
| UPD-101 | Done | ed2c4466 | no release key in keys.json yet (open question 4) |
| UPD-102 | Done | e03bb2f6 | real dry run resolved 23 images from GHCR + Docker Hub |
| UPD-103 | Done | e03bb2f6 | workflow_dispatch + --source. Not needed for the first move: 0.1.8's image has no updater, so sites reach the first updater release by hand |
| UPD-201 | Done | c277a3bd, 119ae87d | manifests also list alembic_revisions; image IDs accept registry digest (containerd store) or config digest (classic store) |
| UPD-202 | Done | d44a0949 | shellcheck clean; not yet run against a stack (UPD-207) |
| UPD-203 | Done | 449918f3 | dry runs against the live dev stack: refusals and an adopted plan correct, nothing changed |
| UPD-204 | Done | d44a0949 | restore drops the public schema first |
| UPD-205 | Done | d44a0949 |  |
| UPD-206 | Done | c277a3bd | refusals in updater/check.py |
| UPD-207 | Done (manual) | 7c9838b6, 83e0ba49 | Live on Windows (start.ps1, Docker Desktop) and Linux (start.sh, Ubuntu 24.04 docker-ce): good update, broken update rolled back with the database, tampered bundle refused, --restore. CI version: tests/upgrade/run.sh + upgrade-test.yml (7b59e7f5); all four scenarios pass on GitHub's runner (first full run, PR #720); it found the launcher refusing 80/443 to a non-root Linux user (b132c2dd) |
| UPD-301 | Done | 60888cf9 | No DB table after all: file hand-off (data/updates read-only into core, data/update-requests read-write); policy in SecuritySetting; system.update for admins only |
| UPD-302 | Done | 60888cf9 | GET status, PUT policy, POST/DELETE request; audited |
| UPD-303 | Done | a9c65098, 4b547396 | --from-queue tick; systemd/launchd/Scheduled Task (S4U when allowed, else logged-on) |
| UPD-304 | Done | d5aa344a | Live on a fresh stack: setup + MFA login, policy saved to the host, Update now lands as request.env, runner tick handles it once and the page shows the result |
| UPD-305 | Done | adcb1ca2 | contract 1.2.0 (update_install); install and progress in the HA update entity; system.update scope, never in the HA preset |
| UPD-401 | Done | a9c65098 | updater/policy.py + 'python -m updater due' |
| UPD-402 | Done | f69aed7f | release/revocations.json, signed by the release workflow; checks refuse a revoked version |
| UPD-403 | Done | b77d3a2c | services/update_notifier.py: host-level system events (opennvr_update), each run and each found version told once |
| UPD-501 | Done | 51533aa1 | scripts/make-offline-bundle.py; containerd-store image IDs are the platform manifest digest (manifests list platform_manifests) |
| UPD-502 | Done | 4683b2fd | --since: only images whose IDs changed |
| UPD-503 | Done | 485f3f75 | --mirror-dir writes a static tree; UPDATE_SOURCE_URL points sites at it |
| UPD-504 | Done | 5e54e515 | rolling `edge` prerelease, X.Y.(Z+1)-edge.N, CORE_TAG=sha-<7>, fetched by commit |
