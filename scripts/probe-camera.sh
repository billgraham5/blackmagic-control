#!/usr/bin/env bash
#
# Discover exactly what the Camera Control REST API on your camera supports.
#
#   ./scripts/probe-camera.sh
#   ./scripts/probe-camera.sh https://Micro-Studio-Camera-4K-G2.local
#   ./scripts/probe-camera.sh 192.168.1.42 --http
#
# Accepts a bare hostname or the full URL shown in Blackmagic Camera Setup.
# Defaults to HTTPS and falls back to HTTP if the camera does not answer.
#
# Writes the camera's own OpenAPI documentation and a per-endpoint support report
# to ./camera-probe/.

set -uo pipefail

TARGET="${1:-Micro-Studio-Camera-4K-G2.local}"
FORCED_SCHEME=""
for arg in "$@"; do
  [[ "$arg" == "--https" ]] && FORCED_SCHEME=https
  [[ "$arg" == "--http" ]] && FORCED_SCHEME=http
done

# Split a full URL into scheme and host so the Camera Setup URL can be pasted in.
if [[ "$TARGET" == *://* ]]; then
  URL_SCHEME="${TARGET%%://*}"
  HOST="${TARGET#*://}"
  HOST="${HOST%%/*}"
else
  URL_SCHEME=""
  HOST="${TARGET%%/*}"
fi

SCHEME="${FORCED_SCHEME:-${URL_SCHEME:-https}}"
OTHER=$([[ "$SCHEME" == https ]] && echo http || echo https)

OUT=camera-probe
# The camera's certificate is self-signed and issued to its mDNS name, so
# verification would fail even when everything is working.
CURL=(curl --silent --show-error --max-time 5 --insecure)

probe_base() {
  "${CURL[@]}" --fail --output /dev/null "$1://$HOST/control/api/v1/system"
}

if ! probe_base "$SCHEME"; then
  if [[ -z "$FORCED_SCHEME" ]] && probe_base "$OTHER"; then
    echo "note: $SCHEME did not answer, using $OTHER instead" >&2
    SCHEME="$OTHER"
  else
    cat >&2 <<'MSG'
Could not reach the API.

Check that:
  - the camera is on the network (Micro Studio 4K G2 needs a USB-C -> Ethernet adapter)
  - "web media manager" is enabled under network access in Blackmagic Camera Setup
  - the hostname resolves (try the raw IP address instead of the .local name)
MSG
    exit 1
  fi
fi

BASE="$SCHEME://$HOST/control/api/v1"
echo "Probing $BASE"
echo

mkdir -p "$OUT"

echo "== Camera identity =="
"${CURL[@]}" "$BASE/system" | tee "$OUT/system.json"
echo
echo

echo "== Saving the camera's own API documentation =="
"${CURL[@]}" --output "$OUT/documentation.html" "$SCHEME://$HOST/control/documentation.html"
echo "  -> $OUT/documentation.html"

# The docs page loads its OpenAPI YAML files as separate assets; grab whatever it names.
grep -oE '[A-Za-z0-9_/.-]+\.yaml' "$OUT/documentation.html" 2>/dev/null | sort -u |
while read -r spec; do
  name="$(basename "$spec")"
  if "${CURL[@]}" --fail --output "$OUT/$name" "$SCHEME://$HOST/control/${spec#/}" 2>/dev/null; then
    echo "  -> $OUT/$name"
  fi
done
echo

echo "== Endpoint support =="
ENDPOINTS=(
  /video/iso /video/gain /video/shutter /video/shutter/measurement
  /video/whiteBalance /video/whiteBalanceTint /video/ndFilter /video/autoExposure
  /video/supportedISOs /video/supportedGains /video/supportedShutters
  /video/detailSharpening /video/flickerFreeShutters
  /lens/iris /lens/iris/description /lens/zoom /lens/focus /lens/focus/description
  /lens/opticalImageStabilization
  /colorCorrection/lift /colorCorrection/gamma /colorCorrection/gain
  /colorCorrection/offset /colorCorrection/contrast /colorCorrection/color
  /colorCorrection/lumaContribution
  /transports/0 /transports/0/record /transports/0/play /transports/0/stop
  /transports/0/playback /transports/0/timecode
  /system /system/videoFormat /system/codecFormat /system/format
  /system/supportedVideoFormats /system/supportedCodecFormats /system/product
  /presets /presets/active
  /audio/channels /audio/channel/0/input /audio/channel/0/level
  /audio/channel/0/available /audio/supportedInputs
  /media/active /media/workingset
  /camera/colorBars /camera/tallyStatus /camera/power /camera/programFeedDisplay
  /monitoring/focusAssist /monitoring/frameGuideRatio /monitoring/safeAreaPercent
  /clips /event/list
  /livestreams/0
)

: > "$OUT/support.tsv"
for ep in "${ENDPOINTS[@]}"; do
  code="$("${CURL[@]}" --output "$OUT/.body" --write-out '%{http_code}' "$BASE$ep")"
  body="$(tr -d '\n' < "$OUT/.body" | cut -c1-100)"
  printf '%s\t%s\t%s\n' "$code" "$ep" "$body" >> "$OUT/support.tsv"
  case "$code" in
    200) printf '  \033[32m%-4s\033[0m %-42s %s\n' "$code" "$ep" "$body" ;;
    404) printf '  \033[90m%-4s %-42s unsupported\033[0m\n' "$code" "$ep" ;;
    *)   printf '  \033[33m%-4s\033[0m %-42s %s\n' "$code" "$ep" "$body" ;;
  esac
done
rm -f "$OUT/.body"

echo
supported=$(awk -F'\t' '$1==200' "$OUT/support.tsv" | wc -l | tr -d ' ')
echo "$supported of ${#ENDPOINTS[@]} probed endpoints supported. Full report: $OUT/support.tsv"

echo
echo "== Subscribable websocket properties =="
"${CURL[@]}" "$BASE/event/list" | tee "$OUT/event-list.json"
echo
echo
echo "Websocket endpoint: ${SCHEME/http/ws}://$HOST/control/api/v1/event/websocket"
