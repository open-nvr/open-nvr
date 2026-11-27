# Reaching OpenNVR from outside

To use OpenNVR from outside the site's network, the router in front of the server must forward two things:

| Port | What it carries | Without it |
|---|---|---|
| TCP `443` | The web UI, the API, and the request that starts each video session | Nothing works from outside |
| UDP `8189` (and TCP `8189`) | Real-time video: WebRTC live view, browser webcams, Home Assistant | See below |

If the ICE port is changed in `.env` (`WEBRTC_ICE_PORT`), forward that port instead.

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

The web UI can share 443 through a reverse proxy that routes by host name. The media port can't, because WebRTC media carries no host name.

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
