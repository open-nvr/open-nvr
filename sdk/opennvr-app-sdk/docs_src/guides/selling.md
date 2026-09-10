# Selling an app

**OpenNVR takes no fee.** The platform gives you the hook and stays out
of the transaction: what you sell, how you price it and who you sell to
are yours, and so is the copyright.

## Declare it

```python
AppManifest(
    ...,
    pricing="paid",                      # free | paid | subscription | contact
    price_note="$29 / camera / year — 2 cameras free",
    entitlement="license_key",           # the gate
)
```

The catalog shows the badge and the note, and — because `entitlement` is
`license_key` — refuses to enable the app until a key is verified.

## Verify it

The administrator enters a key; core asks **your** code whether it is
valid. Nothing in the platform inspects the key. Check a signature
offline, or call home; both are fine.

```python
--8<-- "cookbook/10_selling_an_app.py:57:81"
```

Return `Entitlement(valid=False, message=...)` to refuse. The message is
shown to the administrator, so make it actionable — "this key is for
version 1.x" beats "invalid".

## Apply it live

Core stores the key encrypted and re-delivers both the key and your
verdict on the config poll, so `on_entitlement_update` is where a plan
takes effect. Make it idempotent: the first call usually restates what
boot already knew.

## What you can license

Your app is Apache-2.0-friendly: the SDK's licence lets you ship closed
code if you want to. See [Licensing](../licensing.md) for why an app
built on this SDK carries no AGPL obligation from the platform core.

Most successful paid apps sell something the code *needs* rather than
the code itself — a site-tuned model, a data feed, a hosted notifier,
support. That keeps the app listable in the catalog (where every entry
is open and reviewable, which is what makes it installable in a hospital
or a ministry) while the thing of value stays yours.

Full example:
[`10_selling_an_app.py`](https://github.com/open-nvr/open-nvr/blob/main/sdk/opennvr-app-sdk/cookbook/10_selling_an_app.py).
