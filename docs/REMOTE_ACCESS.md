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
