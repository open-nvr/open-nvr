# Adding a camera vendor

OpenNVR controls cameras through a **modular driver layer** under
`server/services/camera_drivers/`. Every vendor lives in its own subpackage and
is **auto-discovered** — adding support for a new brand is "drop in a directory,"
with no edits to the registry or the API. This guide shows how.

## Layout

```
camera_drivers/
├── base.py          # CameraDriver ABC + result dataclasses + CAPABILITY_AREAS
├── registry.py      # discovery + selection (you do NOT edit this)
├── capabilities.py  # probe + persistence
├── _probe.py        # shared HTTP fingerprint helper
├── onvif/           # universal ONVIF baseline — the fallback
├── hikvision/       # ISAPI driver (reference implementation)
└── <your-vendor>/   # ← you add this
```

## The vendor package contract

Your package's `__init__.py` must export:

| Symbol | Type | Purpose |
|---|---|---|
| `DRIVER` | `type[CameraDriver]` | your concrete driver class |
| `matches(manufacturer: str) -> bool` | callable | cheap string match; input is already lowercased/stripped |
| `PRIORITY` | `int` | selection order, **lower wins**; ties broken by package name |
| `probe(ip, port, username, password) -> bool` | async, optional but recommended | one cheap HTTP request that fingerprints your native API on the wire |

`PRIORITY` guidance: put a specific brand *below* (smaller number than) the OEM
it rebadges, so the specific match wins the string pass. Hikvision is `10`; the
ONVIF fallback is `1000`.

The registry uses these in order: in-process cache → persisted `driver_name`
from a prior successful probe → **manufacturer pass** (`matches()` confirmed by
`probe()`) → **fingerprint pass** (`probe()` alone, for OEM rebadges whose
manufacturer string lies) → ONVIF fallback.

## Writing the driver

Subclass `OnvifDriver` so every ONVIF-covered area (info, imaging, encoder,
time/NTP, read-only network) works for free; override only the areas your native
API does better. Put the raw HTTP primitive in its own module (mirror
`hikvision/isapi.py`): it returns `(status_code, text)`, raises `HTTPException`
`503`/`504` **only** on transport failure, and uses **HTTP Digest auth**.

`get_capabilities()` must set `driver_name` and flip the `supported_areas` flags
for the areas you actually implement — the UI renders its tabs from those flags.

### Hard rules (enforced by tests)

1. **Never** implement `set_network`, `set_ip`, or `factory_reset`. The ABC has
   no such methods by design — a camera driver must not be able to change a
   device's IP or wipe it (that is how you lock yourself out of a camera).
   `test_driver_registry.py` asserts these are absent on every discovered driver.
2. "Unsupported" is **returned data** (`supported=False` on the result
   dataclass), never a raised exception. Only auth/transport failures raise.
3. Credentials arrive via the driver constructor (loaded server-side, decrypted
   from the encrypted DB column). Never read them from a request body or a query
   param.
4. Write paths patch **only** the keys supplied in the update; never rewrite a
   whole config block.

## Example skeleton

```python
# camera_drivers/acme/__init__.py
from .._probe import fingerprint_get
from .driver import AcmeDriver

DRIVER = AcmeDriver
PRIORITY = 30

def matches(manufacturer: str) -> bool:
    return "acme" in manufacturer

async def probe(ip, port, username, password) -> bool:
    status, text = await fingerprint_get(
        ip, port, "/acme/api/deviceInfo", username, password
    )
    return (status == 200 and "AcmeDevice" in text) or status == 401
```

```python
# camera_drivers/acme/driver.py
from ..onvif.driver import OnvifDriver

class AcmeDriver(OnvifDriver):
    driver_name = "acme"
    # override get_osd/set_osd/get_motion/... using your own HTTP primitive
```

## Finding a vendor's real endpoints

Do **not** conclude an area is unsupported just because another vendor's paths
return 404/400. That mistake was made once already with Secureye: its Hikvision-
style `/ISAPI/Image/...` and `/ISAPI/.../overlays` paths return HTTP 400, so
imaging and OSD were initially reported unsupported and their tabs hidden — when
in fact the device serves both perfectly well under its **own** `/CGI/`
namespace.

The camera's own web UI is the authoritative source. What worked:

1. `GET /` and pull the app bundle (`js/app.min.js.gz`).
2. Grep it for path literals: `grep -oaE '"/[A-Za-z][A-Za-z0-9_./-]{2,40}"'`.
3. Many firmwares embed a full endpoint manifest — Secureye's had a 700-entry
   `h[<n>]="CGI/Image/channels/[1-]/color/template/[0-7]"` table listing every
   supported path with its parameter ranges.
4. Confirm each candidate with a real request before implementing it.

Also check `GET /ISAPI/System/deviceInfo` (or the ONVIF `GetDeviceInformation`)
for a `platformVersionList` — Secureye's advertised `cgi`, `onvif` and `rtsp`
platforms, which was the clue that a native CGI API existed at all.

## Testing

Add `server/tests/test_<vendor>_driver.py`, fully mocked (monkeypatch your HTTP
primitive — no network, no hardware). Follow `tests/test_camera_settings.py`.
Cover: each overridden getter against a canned device response, each setter's
request-body construction, `matches()`/`probe()` behavior, and — via
`tests/test_driver_registry.py`'s pattern — that your vendor is selected for its
manufacturer string and rejected for another's.

That's it. The registry discovers your package on next start (watch the
`camera_drivers: discovered vendors [...]` log line), and the settings UI renders
your capability tabs automatically.

## OEM rebadges and vendors without their own API

Many "brands" are rebadged OEM hardware and do **not** need their own package:

- **CP Plus** is Dahua OEM. It ships as a thin `cpplus/` package (subclass of
  `DahuaCgiDriver`, `probe` re-exported from Dahua) purely so a CP-Plus-specific
  quirk can be overridden later. A CP Plus unit that happens to be Hikvision
  internally still routes correctly: it matches `cpplus` by string, **fails** the
  Dahua probe, and the registry's fingerprint pass lands it on the Hikvision
  driver.
- **Secureye** *did* turn out to need its own package — and the reason is
  instructive. The unit tested (SP-C2QN, platform `CGI_V3.0.0`) is not a Dahua or
  Hikvision rebadge: it implements a **partial, namespace-free ISAPI subset** of
  its own. It answered `/ISAPI/System/deviceInfo` with `200` + `<DeviceInfo`, so
  the original Hikvision probe **claimed it** — and then most of the Hikvision
  driver's endpoints returned HTTP 400 against it.

  Two lessons, both now encoded in the code:
  1. **Probes must be specific, not merely positive.** The Hikvision probe now
     additionally requires the Hikvision XML namespace, so an ISAPI-alike cannot
     satisfy it. `tests/test_secureye_driver.py` pins both directions.
  2. **Don't assume a brand is a rebadge until you have fingerprinted one.** A
     Secureye unit built on genuine Hikvision or Dahua internals will still fail
     the Secureye probe, fall through the fingerprint pass, and land on the right
     native driver — the design handles both cases, but only because detection is
     probe-based rather than name-based.

Rule of thumb: **only create a package when the brand has a native HTTP API you
will actually call.** Otherwise let detection route it — a genuine ONVIF-only
device simply falls through to the baseline.

## Uniview (UNV) — a good next contribution

Uniview is a strong candidate for a future package. It has a native HTTP API
("LAPI", `/LAPI/V1.0/...`, JSON over Digest) and is among the most
ONVIF-conformant vendors, so the ONVIF baseline already covers info/imaging/
encoder/time well — the marginal value of a `uniview/` package is OSD, users,
and SD-card status. A starting point:

```python
def matches(manufacturer: str) -> bool:
    return "uniview" in manufacturer or "unv" in manufacturer

async def probe(ip, port, username, password) -> bool:
    status, text = await fingerprint_get(
        ip, port, "/LAPI/V1.0/System/DeviceInfo", username, password
    )
    return status in (200, 401)
```

It is left to the ONVIF fallback today; the package layout makes it a clean
drop-in when someone has UNV hardware to verify against.
