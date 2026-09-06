# App listing terms

These are the terms under which an app appears in the OpenNVR App
Catalog (`server/config/apps_index.yml`). They are short on purpose. By
opening a listing PR you agree to them; by merging it we do too.

## 1. Two kinds of listing

**Catalog apps** (`kind: installable`) are open source, live in a public
repository under the `open-nvr` GitHub organisation, are built from their
tagged source by OpenNVR's CI, digest-pinned, and reviewed before they
are listed. They are the only apps the platform installs.

**External apps** (`kind: external`) are links to software distributed
elsewhere, in any licence. The platform never installs them, shows them
with a "not reviewed by OpenNVR" mark, and takes no position on what they
do. The author is solely responsible for them.

## 2. What you keep

You keep the copyright in your app. You choose its licence: AGPL-3.0 or
Apache-2.0 for a catalog app; anything for an external one. Your name
and a contact route appear on the listing and stay on it. Moving a
repository under the `open-nvr` organisation gives OpenNVR the right to
build, host and distribute it under its licence; it does not transfer
ownership, and the git history is preserved. Contributors sign the
project [CLA](CLA.md), which covers the same ground for the index entry
itself.

## 3. What you promise

* The listing metadata — name, author, summary, pricing, `network_egress`,
  `requires_tasks` — is truthful and kept current.
* The app does what its listing says and nothing that the operator did
  not see on the card: no undeclared network destinations, no video,
  plates, faces or credentials leaving the site unless a described
  feature the operator enabled requires it.
* You are reachable at the contact given, and you respond to a security
  report within seven days.
* For a paid app: the licence check works offline or degrades
  gracefully, the price note is accurate, and there is no hidden
  dependency on a service the card does not mention.

## 4. What we promise

* A first response to a listing PR within five working days, and a
  named reason for any decline.
* Your app is built from your source, unchanged; we do not modify it
  without a PR you can see.
* We do not charge a fee, take a share of sales, or process payments.
* Removal only for the reasons below, with notice where notice is
  possible.

## 5. Removal

An app is removed from the index, or moved to external, when it:
ships or is found to contain undeclared egress or data exfiltration;
misstates its listing; carries a security vulnerability its maintainer
does not address within thirty days of a private report; infringes
someone else's rights; or has had no maintainer response for six months
(in that case it is first marked *community-maintained* for ninety
days, so anyone can adopt it, and removed only if nobody does).

Anyone can report a listing: open an issue tagged `catalog` or, for a
security matter, a
[private advisory](https://github.com/open-nvr/open-nvr/security/advisories/new).

## 6. Liability

Apps are provided by their authors. OpenNVR reviews catalog apps for
the properties above but does not warrant them, and is not liable for
what any app — catalog or external — does on a deployment. Operators
install apps at their own judgement; the card shows what they need to
judge.

## 7. Trademarks

"OpenNVR" and the OpenNVR logo identify the platform. You may say your
app is *for OpenNVR* or *available in the OpenNVR App Catalog*; you may
not present it as made or endorsed by OpenNVR, or use the marks in your
app's name or icon.

## 8. Changes

These terms may change; a change is announced in `CHANGELOG.md` and
applies to new listings from that release and to existing listings
after thirty days.
