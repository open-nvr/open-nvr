# OpenNVR Design Notes

Rationale behind non-obvious design decisions. Inline code comments stay short
and say *what* the code does; the longer *why* lives here.

**How references work**
- Code comments point here as `See DESIGN_NOTES: <topic>` — the `<topic>`
  matches a heading below.
- Security-control rationale is *not* here — it lives in
  [`SECURITY_ARCHITECTURE.md`](SECURITY_ARCHITECTURE.md), referenced from code as
  `See V-###`.

---

## Transport-security probe

`transport_probe_service.py` detects whether each camera speaks RTSPS so the
operator can choose a per-camera transport policy (the camera-facing counterpart
to the operator-facing V-019 hardening). Control: V-003.

**What it does** — for an RTSP URL: parse host + RTSPS port, open an async TCP
connection, wrap in TLS with a permissive context (`CERT_NONE`,
`check_hostname=False` — cameras ship self-signed/factory certs, so the question
is "is a TLS server listening?", not identity, which is tracked under V-018). TLS
handshake completes → `SUPPORTED`; TCP refused / unreachable / handshake fails →
`NOT_SUPPORTED`; DNS failure / timeout / OS error → `INCONCLUSIVE` (the
operator's existing choice is preserved).

**What it deliberately doesn't do** — no RTSP DESCRIBE (handshake completion is
enough; DESCRIBE adds ~200ms and doesn't change the result), no certificate
validation, no multi-port scanning.

**Port selection** — (1) an explicit `rtsps_port` arg wins; (2) else an explicit
non-554 port in the URL is reused; (3) else 322 (RFC 2326/7826 default). Async
(`asyncio.open_connection`) so fleet re-probes don't serialise; 5s default
timeout per camera.

**Runtime enforcement** (`enforce_transport_policy`) — refuses to hand an
incompatible URL to MediaMTX:

| policy | URL | result |
|---|---|---|
| `rtsps_required` | `rtsps://` | allow |
| `rtsps_required` | `rtsp://` | REFUSE (raises) |
| `rtsps_preferred` | `rtsps://` | allow |
| `rtsps_preferred` | `rtsp://` | allow + warning |
| `plaintext_allowed` / `None` | any | allow |
| unknown value | any | REFUSE (fail closed) |

`None` = camera-create before the probe has run (pass it explicitly). The
`rtsps_preferred` warning is informational — the fix is to update the URL, not
silently rewrite it (rewriting on a guessed RTSPS port breaks non-standard
cameras).

## First-time-setup token

Closes the bootstrap-race admin-takeover window created by V-001's
`password_set=False` admin bootstrap: without a gate, any unauthenticated caller
on the management network could race the operator to `POST /auth/first-time-setup`
and claim the admin account.

At startup, `maybe_arm` checks whether any user is still `password_set=False`; if
so it mints a random token, keeps it in a process-local singleton, and prints it
to stdout + the audit log once. `/auth/first-time-setup` requires that token
(constant-time compare) and consumes it on success so it can't be replayed.

**Why in-memory, not the DB / a file** — the token only needs to live for the
bootstrap window; an operator who misses the printed value just restarts to
re-arm. This avoids a migration and avoids persisting an ephemeral credential.

**Residual** — an attacker with read access to the process stdout or the audit
log file is already a higher-privilege threat, out of scope for this gate;
address it with deployment-side file-permission hardening.

## KAI-C sovereignty & the Docker bridge

`AI_SOVEREIGNTY=local_only` means "all AI inference happens on THIS machine",
not literally "loopback only". In bridge-networking mode adapters are reached by
Docker service DNS (e.g. `http://yolov8-adapter:9002`) resolving to an address
inside `OPENNVR_DOCKER_SUBNET` (default 172.28.0.0/16); packets between bridge
containers stay in the host kernel and never hit the physical NIC, so they count
as on-machine. The check therefore accepts loopback + the operator's own bridge
subnet (configurable via `OPENNVR_DOCKER_SUBNET`).

It deliberately does **not** accept generic RFC1918 — an `adapter-vm.internal`
resolving to `192.168.1.50` on a peer host would violate "all inference on THIS
box". Control: V-022.

## Recording identity markers (surviving a DB wipe)

Camera directories on disk are named only by the numeric id (`cam-<id>`), and
that id is a DB sequence that restarts at 1 on a fresh database. So after a DB
wipe + camera re-add, the first camera would silently inherit another camera's
entire archive — the reconciler's startup backfill would index it all under
the new camera with zero warnings.

Defense (`server/services/camera_identity.py`): every camera has a stable
`cameras.uuid`, stamped into `<recordings>/cam-<X>/.camera-identity.json`.
The reconciler, the segment webhook and camera provisioning all verify the
marker before indexing. Unmarked pre-upgrade dirs are adopted iff no file
predates the camera row's `created_at` (a normal upgrade adopts everything
automatically; post-wipe inheritance can't). Conflicted or ownerless trees
are MOVED — never deleted — to `<recordings>/orphaned/`, where Settings →
Recording lists them for re-attach or explicit deletion. The `orphaned/`
directory itself is the registry (no DB rows), so orphan state survives the
very DB wipes it exists for. The ownerless scan is skipped while the cameras
table is empty, so booting against a misconfigured/fresh `DATABASE_URL` can
never mass-quarantine a healthy tree.

File-safety invariant: orphaned footage is deleted only by the explicit admin
Delete action or by `retention_days` aging (0 = keep forever); the
space-pressure purge never touches it. **`.camera-identity.json` files are
load-bearing — preserve them when copying or restoring recording archives.**
