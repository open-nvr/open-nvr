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
)


class SelectiveGZipMiddleware(GZipMiddleware):
    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path", "").startswith(
            _MEDIA_PREFIXES
        ):
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)
