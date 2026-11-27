# Reaching OpenNVR from outside

To use OpenNVR from outside the site's network, forward TCP `443` to the server. That alone gives viewers on the internet real-time video too. Forwarding the media port as well gives the lowest latency, and Home Assistant needs it:

| Port | What it carries | Without it |
|---|---|---|
| TCP `443` | The web UI, the API, and WebRTC live view over TCP for viewers who reach the server's public address | Nothing works from outside |
| UDP `8189` (and TCP `8189`) | Real-time video over UDP (lowest latency), browser webcams, Home Assistant | See below |

If the ICE port is changed in `.env` (`WEBRTC_ICE_PORT`), forward that port instead.

### WebRTC over 443

nginx reads the first bytes of every connection on 443. A TLS handshake goes to the web UI; anything else is a browser's WebRTC connection check and goes to the media server. The player offers the page's own address and port as a candidate, so a browser that loaded the page can carry live video the same way.

Two limits come from the browsers, not from OpenNVR:

- Chrome and Edge refuse a WebRTC candidate on port 80 or 443 at a **private** address (10.x, 172.16-31.x, 192.168.x, and the 100.64.x range Tailscale uses). On the LAN or over a VPN, live view uses UDP/TCP `8189`, which is normally open there. A VPN that lets only 443 through gets HLS.
- A firewall that inspects traffic and allows only real TLS on 443 (some corporate gateways) drops the WebRTC connection. Those viewers get HLS too.

To turn it off and serve only the web UI on 443, set `NGINX_SINGLE_PORT_WEBRTC=off` in `.env` and run `./start.sh up`.

## What happens without the media port

The web UI still loads, because it only needs 443. Video depends on what is sending it:

- **Live view** falls back to HLS by itself. You see a picture, a second or two behind real time.
- **Browser webcams** (Add Camera → Browser webcam) have nothing to fall back to. The publish page keeps retrying and, after a couple of failures, says the video isn't getting through to the server. On the Cameras list the camera shows *Can't reach server* in the tab that is publishing it.
- **Home Assistant** plays over WebRTC only, so it shows no video.

### "It worked from home this morning"

Without the forward, WebRTC can still connect from some networks. A typical home broadband router lets MediaMTX's packets in towards the browser's public address, so the connection gets made from the server's side. Mobile data and phone hotspots sit behind carrier NAT, which drops those packets.

So a browser webcam that published fine from home Wi-Fi and fails from a hotspot does not point to a server fault. The forward is missing, and home broadband was hiding it. Forward the port and it works from every network.

## Setting it up

1. On the router, forward **UDP and TCP 8189** to the OpenNVR server's LAN address, next to the 443 forward.
2. In **Settings → WebRTC → Reach this server from outside**, check the advertised addresses. OpenNVR finds the public IP by STUN. Enter the public IP or DNS name under *Public address* if it guesses wrong, or if the site has no outbound STUN.
3. Open the UI from outside, for example from a phone on mobile data, and start a live view. A tile with a small **HLS** label in its bottom-left corner fell back because the media port isn't reachable. With the forward in place the label goes away.

### Two servers behind one public IP

Only one server can receive a given public port. If two OpenNVR servers share a public IP, forward a different external port to the second one. For example, forward external `8190` to `8189` on the second server. Then set *Public media port* to `8190` in that server's Settings → WebRTC, so browsers are told to use it.

The web UI can share 443 through a reverse proxy that routes by host name. The media port can't, because WebRTC media carries no host name. The same goes for WebRTC over 443: the proxy can pass non-TLS connections on 443 to one of the servers only. The other one uses its own media port.

## Cameras pushed over 443

Port 443 also accepts camera video pushed from another site, as RTSP over TLS with ALPN `opennvr-rtsps`. This is the route the site agent uses, so a cloud-hosted OpenNVR needs no ingest port of its own. TLS ends at nginx with the site's certificate (the Let's Encrypt one when `PUBLIC_HOSTNAME` is set). MediaMTX accepts a publish only with a JWT for that one camera path. The route needs the default `NGINX_SINGLE_PORT_WEBRTC=on`.

## A real certificate (Let's Encrypt)

A site reached by a DNS name from the internet (a cloud install, or a site with a domain pointing at its router) can have a browser-trusted certificate instead of the self-signed one. Set the name in `.env` and run `./start.sh up` (or `.\start.ps1 up`):

```
PUBLIC_HOSTNAME=nvr.example.com
```

The `acme` service then gets a Let's Encrypt certificate for that name and renews it by itself; nginx picks up each new one without a restart. It needs:

- the name resolving to the site's public address, and
- port **80** forwarded to the server, next to 443 (the HTTP-01 challenge; everything else on port 80 still redirects to HTTPS).

Until a certificate is issued, and whenever a renewal fails, the current certificate stays in place: a failure never breaks the site. Check progress with `docker logs opennvr_acme`. The self-signed pair is kept as `nginx-certs/server.crt.selfsigned` and `server.key.selfsigned`.

`PUBLIC_HOSTNAME` is also used as the WebRTC public address when none is set under Settings → WebRTC. Set `ACME_DISABLE=true` to keep the self-signed certificate, for example when another proxy in front terminates TLS. LAN-only and air-gapped sites leave `PUBLIC_HOSTNAME` unset.

## Checking from the server

MediaMTX logs every WebRTC session:

```sh
docker logs --since 1h opennvr_mediamtx 2>&1 | grep WebRTC
```

- `closed: deadline exceeded while waiting connection`: the session was set up over 443, but no media arrived within the handshake timeout. The media port isn't reachable from where that browser is.
- `peer connection established, local candidate: …, remote candidate: srflx/udp/<ip>/<port>`: connected. If this appears without a forward in place, the connection came in through a permissive NAT on the client side, as described above.

To test the forward itself, send UDP to the public address from outside the site and watch MediaMTX's UDP receive counter move:

```sh
# on the server, before and after
docker exec opennvr_mediamtx cat /proc/net/snmp | awk '/^Udp:/{n++} n==2{print "InDatagrams", $2; exit}'
```

## Cameras at another site, over a VPN

An OpenNVR in the cloud (or at head office) can pull cameras from a remote site through a VPN tunnel, with no port forwarded at the site. OpenNVR only dials camera addresses on private networks (RFC1918, loopback, link-local), as a guard against being used to reach arbitrary hosts.

- **WireGuard or IPsec on a 10.x or 192.168.x subnet:** those addresses are already private. Add the cameras by their tunnel address.
- **Tailscale (100.x addresses):** those are carrier-grade NAT addresses, not private ones, so OpenNVR refuses them until you list the network in `.env`, then run `./start.sh up` (or `.\start.ps1 up`):

  ```
  CAMERA_EXTRA_INTERNAL_CIDRS=100.64.0.0/10
  ```

  Several networks can be listed, comma-separated. Anything wider than a /8 is ignored, because it would switch the check off. Cloud metadata addresses (`169.254.169.254`, `100.100.100.200`) and Tailscale's own `100.100.100.100` are refused whatever is listed.
