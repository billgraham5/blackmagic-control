#!/usr/bin/env bash
#
# Discover exactly what the Camera Control REST API on your camera supports.
#
#   ./scripts/probe-camera.sh micro-studio-g2.local
#   ./scripts/probe-camera.sh 192.168.1.42 --https
#
# Writes the camera's own OpenAPI documentation and a per-endpoint support report
# to ./camera-probe/.

set -uo pipefail

HOST="${1:-}"
SCHEME=http
[[ "${2:-}" == "--https" ]] && SCHEME=https

if [[ -z "$HOST" ]]; then
  echo "usage: $0 <camera-hostname-or-ip> [--https]" >&2
  exit 64
fi

BASE="$SCHEME://$HOST/control/api/v1"
OUT=camera-probe
CURL=(curl --silent --show-error --max-time 5)
[[ "$SCHEME" == https ]] && CURL+=(--insecure)   # camera cert is self-signed

mkdir -p "$OUT"

echo "Probing $BASE"
echo

if ! "${CURL[@]}" --fail --output /dev/null "$BASE/system"; then
  cat >&2 <<'MSG'
Could not reach the API.

Check that:
  - the camera is on the network (Micro Studio 4K G2 needs a USB-C -> Ethernet adapter)
  - "web media manager" is enabled under network access in Blackmagic Camera Setup
  - the hostname resolves (try the raw IP address instead of the .local name)
  - if the camera has a generated certificate, retry with --https
MSG
  exit 1
fi

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
