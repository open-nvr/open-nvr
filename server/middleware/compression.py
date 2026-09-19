"""Selective gzip — compress text/JSON responses but never the media plane.

Starlette's GZipMiddleware compresses every response whose client sent
Accept-Encoding: gzip, including streamed video byte-ranges. Video payloads
are already-compressed containers, so gzipping them burns CPU on the hottest
path for zero size win. This subclass passes media endpoints through
untouched and lets everything else compress normally.
"""

from starlette.middleware.gzip import GZipMiddleware

# Path prefixes whose responses are (or can be) video/media payloads.
_MEDIA_PREFIXES = (
    "/api/v1/recordings/playback/hls",  # HLS manifests + byte-range media
    "/api/v1/recordings/export",  # clip export proxy (streams video)
    "/api/v1/media/s/",  # signed media (HA-112): images and clips
)

# Evidence JPEGs. Same argument as the media plane — already-compressed bytes,
# and deflate at level 9 on the event loop is pure burn — but they cannot be
# matched by prefix: the event id sits mid-path, and the JSON siblings
# (/events/plate-stats, /vehicle-report, ...) share the prefix and SHOULD
# compress. So match the suffix under an events guard. Gzipping these also
# drops Content-Length and forces chunked, which with proxy_buffering off is
# worse for the browser too.
_EVENTS_PREFIX = "/api/v1/events/"
_IMAGE_SUFFIXES = (
    "/evidence",
    "/plate-evidence",
    "/scene-evidence",
    "/plate-frame",
)


def _is_media_path(path: str) -> bool:
    if path.startswith(_MEDIA_PREFIXES):
        return True
    if path.startswith("/api/v1/alerts-inbox/") and "/images/" in path:
        return True  # alert images; the inbox's JSON still compresses
    return path.startswith(_EVENTS_PREFIX) and path.endswith(_IMAGE_SUFFIXES)


class SelectiveGZipMiddleware(GZipMiddleware):
    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and _is_media_path(scope.get("path", "")):
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)
