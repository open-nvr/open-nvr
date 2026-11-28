# Site agents: the security model

A site agent ([SITE_AGENT.md](SITE_AGENT.md)) runs on a computer on a customer's network, holds the passwords of that network's cameras, and takes its instructions from an OpenNVR server on the internet. This page says what it trusts, what someone in each of the usual attacker positions can and cannot do, and what is left. It is for whoever has to sign off on putting one in a network.

## In short

| If someone… | they can | they cannot |
|---|---|---|
| **takes over the OpenNVR server** | see and record that site's cameras; reach what looks like a camera on that site's camera networks, on camera ports | reach the site's router, the agent's own machine, other networks or the internet through the agent; make the agent open files or run anything; make it install a program (updates must carry the release's signature) |
| **sits between the site and the server** (including a firewall that inspects HTTPS) | block the connection | read or change anything: the agent trusts only the server's own key, and a new key only after the server proves it |
| **gets a setup link** before the person at the site uses it | set up that site, once, within its lifetime | do anything after the link was used or revoked; take over a site that has cameras without an admin's Approve |
| **is on the site's network** | see that an agent is there | connect to the agent (it accepts no incoming connections); get camera passwords (they go only to devices an admin ticked, and only to devices that answer like cameras) |
| **steals the site's computer** | use its credential until it is revoked | (revoke the site on the server; change the camera passwords: the box held them) |

## What the agent connects to

The agent decides where it connects, not the server. It only connects to:

- addresses on a **private** network its computer is **directly** on (`10.x`, `172.16–31.x`, `192.168.x`, `100.64.x`, IPv6 ULA);
- on **camera ports** (80, 443, 554, 8000, 8080, 8088, 8554, 37777, 34567 and a few more), or a port the camera itself announced in ONVIF discovery or answered on in a camera scan.

It never connects to:
- its own machine;
- a router (any next hop in the machine's routing table, on Linux and Windows);
- another network;
- the internet.

The check is on the address it actually reaches, so a name that changes its answer (DNS rebinding) is caught too.

Wider access needs an entry in the agent's local config on the site's computer (`allow_subnets`, `allow_ports`), which the server cannot write. Also:
- **Camera streams must be `rtsp://` or `rtsps://`.** The ffmpeg fallback is held to network protocols (`-protocol_whitelist`), so a URL from the server can't make it read a file.
- **Camera requests from the server** (ONVIF, PTZ, settings) follow no redirects. They go only to cameras of that site, and only within the rule above.
- **The camera scan is the same rule.** It runs only when an admin asks, touches only camera ports, and keeps only devices that answer like cameras.

## How the agent knows the server

- **One key.** At setup, the agent saves the key of the certificate it met. From then on it trusts that key only, not the computer's certificate store. A firewall that inspects HTTPS with its own certificate authority (which the computer trusts) therefore can't read the site's credential or the camera passwords: the agent refuses it.
- **A renewed certificate with a new key.** The agent accepts it only after the server proves it holds the site's credential: an HMAC over the agent's nonce and the key the agent sees. Nothing secret is sent before that proof, and a man in the middle shows its own key and can't make it.
- **The same keys for video and control.** A video destination the server names gets nothing unless it holds the server's key.

## Setup links

- **The secret stays in the browser.** It is in the part of the link after `#`, which browsers never send, so it is in no access log, proxy log or Referer.
  - The setup page asks the server about the link by a separate random id only, which never uses the link up: chat and mail previews open links.
  - The page then removes the secret from the address bar.
- **The server proves itself before the secret is sent.** `opennvr-host setup` gets an HMAC over its nonce and the server's key, made with a key derived from the secret. Without that proof it stops and sends nothing.
- **The code is never on a command line.** The agent reads it from the terminal, so it stays out of shell history and process lists.
- **Limits:** single use (atomically), 3 days by default (1 hour to 7 days), revocable, at most 5 open per site. Using one voids the site's other links and its short code.
- **Offered only with a publicly trusted certificate**, so nobody at the site ever clicks through a certificate warning.
- **Replacing a site's agent** while the site has cameras holds the new agent until an admin clicks **Approve**. Until then it is sent no cameras and no passwords.
- **The server stores only derived values:** a hash to enrol with, and the proof key encrypted with its vault key.
- **What is left:** someone who has read the database **and** holds the vault key **and** sits between a not-yet-enrolled agent and the server could pose as the server to that agent.

## Camera passwords

- **On the server:** stored encrypted. They are sent only to that site's agent, over the pinned connection.
- **On the agent:** kept in memory only. They are on no command line (the built-in relay needs no process per camera), and in no log or report.
- **When probing cameras:** the password goes only to the devices an admin ticked, and only to devices the agent itself reported. Each probe is in the audit log with the devices it went to.

## Updates

An agent installs a new build of itself only if all of these hold:
- **The release signed it.** The signature is ed25519 over a manifest, made in CI with a key only the release workflow holds. The public keys are compiled into the agent, and the server only relays the manifest; it can't make a signature.
- **It is newer:** built later, a version not lower, and not below the manifest's floor. So an old signed build with a known flaw can't be pushed back.
- **Its file matches:** its SHA-256 is the one the manifest lists for that machine.

On Linux the agent runs as a throwaway user and can't replace itself. A separate root updater does that:
- it trusts nothing the agent staged, and checks all of the above again with the installed agent's keys;
- it hashes the exact bytes it installs, and follows no links;
- it puts the previous build back if the new one doesn't reach the server within 2 minutes.

Updates can be switched off per site. A build signed with an offline standby key replaces a lost or leaked release key without reinstalling anything ([SITE_AGENT.md](SITE_AGENT.md#the-signing-keys)).

## The agent's machine

- **What the Linux service may do:** it runs as a throwaway user (`DynamicUser`) with no privileges. It can't write to the system or read home folders, and it can open only network sockets, plus netlink to list the computer's networks.
- **Where the files are:** the program is root-owned, and the credential is a root-only file handed to the service read-only.
- **Listening:** the agent accepts no incoming connections. Its relay port is on loopback only, and its one UDP socket only takes the replies to its own ONVIF discovery.
- **One agent per site:** a second one on the same machine refuses to start.

## On the server

- **Bounded public routes:**
  - small request bodies (enrol 16 KB, reports 1 MB, camera answers 8 MB, the uplink test 10 MB once an hour);
  - enrolment limited per address and in total;
  - per-site limits for each kind of call;
  - at most one long poll held per site, and no database connection held while it waits.
- **Revoking or replacing an agent** ends its running streams at once, and its credential stops working on its next call.
- **The access log** records paths without query strings, so playback and HLS tokens are not written to it.

## What is not covered yet

- **Recording through an internet outage:** nothing is buffered at the site yet.
- **Vendor event streams** (Hikvision alertStream, Dahua eventManager) are not relayed.
- **Sites behind an HTTP proxy only:** the video route needs a direct connection out.
- **A stolen box:** on a computer without disk encryption (a Raspberry Pi, say), the credential and camera passwords can be read from its storage. Revoke the site, and change the camera passwords.
- **Alerts:** they are not yet sent by e-mail or Telegram.
