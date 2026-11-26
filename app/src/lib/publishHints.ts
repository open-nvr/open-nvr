// Copyright (c) 2026 OpenNVR
// SPDX-License-Identifier: AGPL-3.0-or-later

/**
 * What a browser webcam's publisher says when its video cannot reach the
 * server (lib/whipPublisher). Kept apart from the publisher so it can be
 * tested without a browser.
 *
 * The page and the WHIP request travel over HTTPS (443); the video goes to
 * MediaMTX's ICE port. A site reached over the internet whose router forwards
 * 443 but not the ICE port still works from some networks: home broadband
 * routers usually let MediaMTX's packets in towards the browser's public
 * address, so the connection is made from the server's side. Carrier NAT on
 * mobile data does not. "It worked from home this morning" is therefore no
 * sign that the forward exists, and the hint names the server's router, not
 * the viewer's network.
 */

export function mediaPortHint(port?: number | null): string {
  const p = port ? `port ${port}` : 'the media port'
  return (
    "Video isn't getting through to the OpenNVR server. The router or firewall in front of the server " +
    `must let ${p} through (UDP, and TCP) to it, as well as 443. ` +
    'Without that it may still work from some networks, such as home broadband, ' +
    'but not from mobile data or a phone hotspot. Still trying.'
  )
}
