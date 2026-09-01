#!/bin/sh
# OpenNVR — fake-camera RTSP rig (test-only).
#
# Serves every video file found under $FAKECAM_VIDEO_DIR as its own endlessly
# looping RTSP stream, so OpenNVR can add them exactly as if they were real IP
# cameras: one file in, one camera out.
#
#     /videos/gate-entry.mp4  ->  rtsp://<fakecams-ip>:8554/gate-entry
#
# Runs MediaMTX (the same server OpenNVR itself uses) plus one ffmpeg publisher
# per file, each supervised by a restart loop. There is NO authentication and
# RTSP is plaintext: this is a lab rig that must stay on the internal Docker
# network. Do not publish it to a LAN.
set -eu

VIDEO_DIR="${FAKECAM_VIDEO_DIR:-/videos}"
RTSP_PORT="${FAKECAM_RTSP_PORT:-8554}"
API_PORT="${FAKECAM_API_PORT:-9997}"
# auto      — copy H.264 sources untouched, re-encode anything else
# copy      — always stream-copy (cheapest; fails on non-H.264 input)
# transcode — always re-encode to H.264 with a 2s GOP. Slower, but it rebuilds
#             clean keyframes at every loop seam; stream-copy looping emits
#             corrupt packets there, which can wedge a consumer's motion
#             detector permanently in "calibrating".
MODE="${FAKECAM_MODE:-auto}"
# Output frame rate, transcode only. Empty = take it from the source (falling
# back to 15 when the source reports something implausible, which recordings
# with irregular timestamps often do).
FPS="${FAKECAM_FPS:-}"

ENCODE="-c:v libx264 -preset veryfast -tune zerolatency -pix_fmt yuv420p -g 50 -bf 0"

CONF=/tmp/fakecams.yml

log() { echo "[fakecams] $*"; }

# MediaMTX config: RTSP only, plaintext, anyone may publish/read any path, and
# the read-only API reachable from the Docker network (the register script and
# `docker exec` health checks list paths through it). Everything else off.
cat > "$CONF" <<YAML
logLevel: info
api: yes
apiAddress: :${API_PORT}
metrics: no
pprof: no
playback: no
rtsp: yes
rtspTransports: [tcp, udp]
rtspEncryption: "no"
rtspAddress: :${RTSP_PORT}
rtmp: no
hls: no
webrtc: no
srt: no
authInternalUsers:
  - user: any
    pass:
    ips: []
    permissions:
      - action: publish
      - action: read
      - action: playback
  - user: any
    pass:
    ips: []
    permissions:
      - action: api
paths:
  all_others:
YAML

# Basename, extension stripped, lowercased, anything exotic folded to '_'.
slugify() {
  printf '%s' "$1" | sed -e 's#.*/##' -e 's/\.[^.]*$//' \
    | tr 'A-Z' 'a-z' | sed -e 's/[^a-z0-9._-]/_/g' -e 's/^[._-]*//' \
    | cut -c1-48
}

publish_forever() {
  src="$1"; path="$2"
  while :; do
    codec=$(ffprobe -v error -select_streams v:0 -show_entries stream=codec_name \
              -of default=nw=1:nk=1 "$src" 2>/dev/null | head -1)
    case "$MODE" in
      copy)      vargs="-c:v copy" ;;
      transcode) vargs="$ENCODE" ;;
      *)         if [ "$codec" = "h264" ]; then vargs="-c:v copy"; else vargs="$ENCODE"; fi ;;
    esac
    # Transcodes need an explicit rate: without one, a source with irregular
    # timestamps makes libx264 duplicate frames up to absurd rates (a 30 fps
    # clip published at 200 fps, burning CPU for nothing).
    if [ "$vargs" != "-c:v copy" ]; then
      rate="$FPS"
      if [ -z "$rate" ]; then
        rate=$(ffprobe -v error -select_streams v:0 -show_entries stream=r_frame_rate \
                 -of default=nw=1:nk=1 "$src" 2>/dev/null | head -1 \
               | awk -F/ '{ d = ($2 == "" || $2 == 0) ? 1 : $2; r = $1 / d;
                            if (r >= 1 && r <= 60) printf "%.3f", r }')
      fi
      [ -n "$rate" ] || rate=15
      vargs="$vargs -r $rate"
    fi
    log "publishing $src as '$path' (source codec=${codec:-unknown}, ffmpeg: $vargs)"
    # -re paces the file at real time, -stream_loop -1 restarts it forever,
    # +genpts keeps timestamps monotonic across the loop seam.
    ffmpeg -hide_banner -loglevel warning -nostdin \
      -re -stream_loop -1 -fflags +genpts -i "$src" \
      -an $vargs -f rtsp -rtsp_transport tcp \
      "rtsp://127.0.0.1:${RTSP_PORT}/${path}" || true
    log "publisher '$path' exited; restarting in 3s"
    sleep 3
  done
}

trap 'kill 0' INT TERM

/mediamtx "$CONF" &
MTX_PID=$!
sleep 2

count=0
taken=" "
for f in "$VIDEO_DIR"/* "$VIDEO_DIR"/*/*; do
  [ -f "$f" ] || continue
  case "$f" in
    *.mp4|*.MP4|*.m4v|*.M4V|*.mkv|*.MKV|*.mov|*.MOV|*.avi|*.AVI|*.ts|*.TS|*.webm|*.WEBM) ;;
    *) continue ;;
  esac
  path=$(slugify "$f")
  [ -n "$path" ] || path="cam"
  # Two files that slugify to the same name still get one path each.
  base="$path"; n=2
  while [ "${taken#* $path }" != "$taken" ]; do
    path="${base}-${n}"; n=$((n + 1))
  done
  taken="$taken$path "
  count=$((count + 1))
  publish_forever "$f" "$path" &
done

if [ "$count" -eq 0 ]; then
  log "WARNING: no video files under $VIDEO_DIR — nothing to serve."
  log "         Drop .mp4 files in the host folder bound to $VIDEO_DIR and restart."
else
  log "serving $count fake camera(s) on rtsp://<this-container>:${RTSP_PORT}/<name>"
fi

wait "$MTX_PID"
