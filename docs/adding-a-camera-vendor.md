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
- **Secureye** and similar pure rebadgers have **no package at all**. They rebrand
  Dahua or Hikvision hardware unit-by-unit with no native API of their own, so
  there is nothing brand-specific to implement. The fingerprint pass is the
  answer: the manufacturer string matches nothing, and whichever native probe
  (ISAPI or Dahua CGI) succeeds wins. Adding a Secureye package would just
  duplicate one of those two probes.

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
