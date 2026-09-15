#!/bin/sh
# OpenNVR — fake-camera RTSP rig (test-only).
#
# Turns a folder of video files into endlessly looping RTSP streams, so OpenNVR
# can add them exactly as if they were real IP cameras. Two layouts, both live:
#
#     /videos/gate-entry.mp4   ->  rtsp://<rig>:8554/gate-entry  (one file)
#     /videos/parking/*.mp4    ->  rtsp://<rig>:8554/parking     (one folder,
#                                                                 every clip in
#                                                                 it back to back)
#
# The folder is rescanned every FAKECAM_SCAN_INTERVAL seconds: drop in a new
# file or a new folder and its stream appears without restarting the container.
#
# Runs MediaMTX (the same server OpenNVR itself uses) plus one supervised ffmpeg
# publisher per stream. There is NO authentication and RTSP is plaintext: this is
# a lab rig that must stay on the internal Docker network. Do not publish it to a
# LAN.
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
# folder — a subfolder is ONE stream, named after the folder, playing every clip
#          inside it back to back, forever (default)
# file   — every video file is its own stream, named after the file, however
#          deep it sits (the behaviour before grouping existed)
GROUP="${FAKECAM_GROUP:-folder}"
# Seconds between rescans of VIDEO_DIR. A new or changed stream needs two
# consecutive scans to agree on its contents before it starts, so a file still
# being copied in is never published half-written.
SCAN_INTERVAL="${FAKECAM_SCAN_INTERVAL:-5}"

ENCODE="-c:v libx264 -preset veryfast -tune zerolatency -pix_fmt yuv420p -g 50 -bf 0"

CONF=/tmp/fakecams.yml
STATE=/tmp/fakecams/state   # what is publishing right now
SCAN=/tmp/fakecams/scan     # what the latest rescan found

log() { echo "[fakecams] $*"; }

rm -rf /tmp/fakecams
mkdir -p "$STATE" "$SCAN"

# MediaMTX config: RTSP only, plaintext, anyone may publish/read any path, and
# the read-only API reachable from the Docker network (the register script and
# `docker exec` health checks list paths through it). Everything else off.
# `all_others` is what lets a publisher claim a brand-new path at any time, so a
# stream can appear mid-flight with no config reload.
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

# --------------------------------------------------------------- discovery --

# Name with any extension stripped, lowercased, anything exotic folded to '_'.
slugify() {
  printf '%s' "$1" | sed -e 's#.*/##' -e 's/\.[^.]*$//' \
    | tr 'A-Z' 'a-z' | sed -e 's/[^a-z0-9._-]/_/g' -e 's/^[._-]*//' \
    | cut -c1-48
}

is_video() {
  case "$1" in
    *.mp4|*.MP4|*.m4v|*.M4V|*.mkv|*.MKV|*.mov|*.MOV|*.avi|*.AVI|*.ts|*.TS|*.webm|*.WEBM) return 0 ;;
    *) return 1 ;;
  esac
}

# Every video file under $1, one per line, sorted — so a folder's playback order
# (and therefore its stream's content) is identical on every scan.
videos_under() {
  find "$1" -type f 2>/dev/null | sort | while IFS= read -r f; do
    if is_video "$f"; then printf '%s\n' "$f"; fi
  done
}

# Size and mtime of every file in a group. Two scans producing the same
# signature means nothing is still being written and the group can go live.
signature_of() {
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    printf '%s %s\n' "$f" "$(stat -c '%s %Y' "$f" 2>/dev/null || echo '? ?')"
  done < "$1"
}

# Record one candidate stream for this scan: $1 = name to slugify, file list on
# stdin. Names that collide get -2, -3, … in scan order, which stays stable
# because both loops below walk their inputs sorted.
emit_group() {
  want=$(slugify "$1")
  [ -n "$want" ] || want=cam
  slug="$want"; n=2
  while [ -e "$SCAN/$slug.files" ]; do
    slug="${want}-${n}"; n=$((n + 1))
  done
  cat > "$SCAN/$slug.files"
  if [ ! -s "$SCAN/$slug.files" ]; then
    rm -f "$SCAN/$slug.files"
    return 0
  fi
  signature_of "$SCAN/$slug.files" > "$SCAN/$slug.sig"
}

scan_groups() {
  rm -rf "$SCAN"; mkdir -p "$SCAN"

  # Loose files at the top level are one stream each, named after the file — in
  # both grouping modes.
  for f in "$VIDEO_DIR"/*; do
    if [ -f "$f" ] && is_video "$f"; then
      printf '%s\n' "$f" | emit_group "$f"
    fi
  done

  for d in "$VIDEO_DIR"/*/; do
    [ -d "$d" ] || continue
    case "$GROUP" in
      file)
        videos_under "$d" | while IFS= read -r f; do
          printf '%s\n' "$f" | emit_group "$f"
        done
        ;;
      *)
        videos_under "$d" | emit_group "${d%/}"
        ;;
    esac
  done
}

# -------------------------------------------------------------- publishing --

# ffmpeg video args for a whole group. Stream-copy only when every clip already
# agrees on codec and frame size: the concat demuxer cannot splice mismatched
# streams without re-encoding them.
video_args_for() {
  list="$1"
  case "$MODE" in
    copy)      printf '%s' "-c:v copy"; return 0 ;;
    transcode) printf '%s' "$ENCODE"; return 0 ;;
  esac
  first=""; uniform=yes
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    info=$(ffprobe -v error -select_streams v:0 \
             -show_entries stream=codec_name,width,height \
             -of csv=p=0 "$f" 2>/dev/null | head -1)
    if [ -z "$first" ]; then
      first="$info"
    elif [ "$info" != "$first" ]; then
      uniform=no
    fi
  done < "$list"
  case "$first" in
    h264,*)
      if [ "$uniform" = yes ]; then printf '%s' "-c:v copy"; else printf '%s' "$ENCODE"; fi ;;
    *)
      printf '%s' "$ENCODE" ;;
  esac
}

# Transcodes need an explicit rate: without one, a source with irregular
# timestamps makes libx264 duplicate frames up to absurd rates (a 30 fps clip
# published at 200 fps, burning CPU for nothing).
rate_for() {
  rate="$FPS"
  if [ -z "$rate" ]; then
    rate=$(ffprobe -v error -select_streams v:0 -show_entries stream=r_frame_rate \
             -of default=nw=1:nk=1 "$1" 2>/dev/null | head -1 \
           | awk -F/ '{ d = ($2 == "" || $2 == 0) ? 1 : $2; r = $1 / d;
                        if (r >= 1 && r <= 60) printf "%.3f", r }')
  fi
  [ -n "$rate" ] || rate=15
  printf '%s' "$rate"
}

# concat-demuxer playlist. A single quote in a path has to be spelled '\''.
write_concat_list() {
  : > "$2"
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    printf "file '%s'\n" "$(printf '%s' "$f" | sed "s/'/'\\\\''/g")" >> "$2"
  done < "$1"
}

publish_forever() {
  slug="$1"
  files="$STATE/$slug.files"
  concat="$STATE/$slug.concat"
  n=$(grep -c . "$files" 2>/dev/null || true)
  [ -n "$n" ] || n=1

  while [ ! -e "$STATE/$slug.stop" ]; do
    vargs=$(video_args_for "$files")
    if [ "$vargs" != "-c:v copy" ]; then
      vargs="$vargs -r $(rate_for "$(head -1 "$files")")"
    fi

    if [ "$n" -gt 1 ]; then
      write_concat_list "$files" "$concat"
      set -- -f concat -safe 0 -i "$concat"
    else
      set -- -i "$(head -1 "$files")"
    fi

    log "publishing '$slug' ($n clip(s), ffmpeg: $vargs)"
    # -re paces the input at real time, -stream_loop -1 restarts it — the whole
    # playlist, for a folder — forever, and +genpts keeps timestamps monotonic
    # across each clip boundary and the loop seam.
    ffmpeg -hide_banner -loglevel warning -nostdin \
      -re -stream_loop -1 -fflags +genpts "$@" \
      -an $vargs -f rtsp -rtsp_transport tcp \
      "rtsp://127.0.0.1:${RTSP_PORT}/${slug}" &
    ffpid=$!
    echo "$ffpid" > "$STATE/$slug.ffpid"
    wait "$ffpid" 2>/dev/null || true
    rm -f "$STATE/$slug.ffpid"

    [ ! -e "$STATE/$slug.stop" ] || break
    log "publisher '$slug' exited; restarting in 3s"
    sleep 3
  done
}

start_group() {
  slug="$1"
  rm -f "$STATE/$slug.stop"
  cp "$SCAN/$slug.files" "$STATE/$slug.files"
  cp "$SCAN/$slug.sig" "$STATE/$slug.sig"
  publish_forever "$slug" &
  echo $! > "$STATE/$slug.pid"
}

# Flag first so the supervisor will not respawn, then kill ffmpeg and reap the
# supervisor — otherwise a long-lived rig collects a zombie on every change.
stop_group() {
  slug="$1"
  touch "$STATE/$slug.stop"
  if [ -f "$STATE/$slug.ffpid" ]; then
    kill "$(cat "$STATE/$slug.ffpid")" 2>/dev/null || true
  fi
  if [ -f "$STATE/$slug.pid" ]; then
    pid=$(cat "$STATE/$slug.pid")
    kill "$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
  fi
  rm -f "$STATE/$slug.pid" "$STATE/$slug.ffpid" "$STATE/$slug.files" \
        "$STATE/$slug.sig" "$STATE/$slug.concat" "$STATE/$slug.stop"
}

# --------------------------------------------------------------- reconcile --

reconcile() {
  scan_groups

  # Streams that went away, or whose clips changed, come down first — so a
  # rename can hand its name to the replacement in the same pass.
  for pidfile in "$STATE"/*.pid; do
    [ -e "$pidfile" ] || continue
    slug=$(basename "$pidfile" .pid)
    if [ ! -f "$SCAN/$slug.sig" ]; then
      log "'$slug' has no video files any more — stopping it"
      stop_group "$slug"
    elif ! cmp -s "$SCAN/$slug.sig" "$STATE/$slug.sig"; then
      if cmp -s "$SCAN/$slug.sig" "$STATE/$slug.pending"; then
        rm -f "$STATE/$slug.pending"
        log "'$slug' changed on disk — restarting it"
        stop_group "$slug"
      else
        cp "$SCAN/$slug.sig" "$STATE/$slug.pending"
      fi
    fi
  done

  for sigfile in "$SCAN"/*.sig; do
    [ -e "$sigfile" ] || continue
    slug=$(basename "$sigfile" .sig)
    [ ! -f "$STATE/$slug.pid" ] || continue
    # Two scans have to agree before a stream goes live, so a clip still being
    # copied in is never published half-written.
    if cmp -s "$sigfile" "$STATE/$slug.pending"; then
      rm -f "$STATE/$slug.pending"
      start_group "$slug"
    else
      cp "$sigfile" "$STATE/$slug.pending"
      log "new stream '$slug' detected — waiting for its files to settle"
    fi
  done

  # Forget pending markers for names that vanished before ever going live.
  for pending in "$STATE"/*.pending; do
    [ -e "$pending" ] || continue
    slug=$(basename "$pending" .pending)
    [ ! -f "$SCAN/$slug.sig" ] || continue
    rm -f "$pending"
  done
}

# -------------------------------------------------------------------- main --

trap 'kill 0' INT TERM

/mediamtx "$CONF" &
MTX_PID=$!
sleep 2

log "watching $VIDEO_DIR (group=$GROUP, rescan every ${SCAN_INTERVAL}s)"
warned=no
while :; do
  reconcile

  # Pending markers count as "something is coming", so a first pass that has
  # only just spotted the clips does not warn about an empty folder.
  if ls "$STATE"/*.pid >/dev/null 2>&1 || ls "$STATE"/*.pending >/dev/null 2>&1; then
    warned=no
  elif [ "$warned" = no ]; then
    log "WARNING: no video files under $VIDEO_DIR — nothing to serve yet."
    log "         Drop clips (or a folder of clips) into the host folder bound"
    log "         to $VIDEO_DIR; they are picked up without a restart."
    warned=yes
  fi

  if ! kill -0 "$MTX_PID" 2>/dev/null; then
    log "mediamtx exited — stopping the rig"
    exit 1
  fi
  sleep "$SCAN_INTERVAL"
done
