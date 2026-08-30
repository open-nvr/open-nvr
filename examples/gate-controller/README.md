# gate-controller

Barrier actuation for OpenNVR gate automation. Consumes the platform's
contracted `access.decided.v1` events (published by the License Plate
Recognition app when its barrier mode is on) and pulses an HTTP relay
when — and only when — the decision is `allow`. Deny, and any decision
value it does not recognise, actuates nothing: **fail closed**.

Works with any HTTP-reachable relay (Shelly, Tasmota, ESPHome, most
commercial barrier controllers). Per-gate cooldown keeps one car to
one pulse; a failed relay call raises a high-severity `barrier_fault`
alert and stays retriable by the next decision; `dry_run: true` (the
Docker default) lets you commission a site before touching hardware.

The policy/hardware split is the point: the LPR app knows **who** may
enter and never touches hardware; this app knows **which relay opens
which gate** and never makes admission judgements. Either side can be
replaced independently — the contract between them is one documented
event (`docs/EVENT_CONTRACTS.md`).

Run: `python gate_controller.py --config config.yml`
(copy `config.example.yml`), or install from the App Catalog.
