#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Rankin
set -eu
eval "$(python3 /opt/scripts/load-config.py)"

case "${INPUT:-ndi}" in
  st2110) exec python3 /opt/scripts/st2110_in.py ;;
  ndi) ;;
  *)
    # Flow A comes from the other lab, or nothing: no ingest on this VM.
    echo "ndi-ingest: input is '${INPUT}', nothing to ingest here. Idle."
    exec sleep infinity ;;
esac

# SRC_FPS is either a whole number (50) or a fraction from auto-detect (60000/1001)
case "$SRC_FPS" in
  */*) FPS_CAPS="$SRC_FPS" ;;
  *)   FPS_CAPS="$SRC_FPS/1" ;;
esac

# Audio flow: 48 kHz float, AUDIO_CHANNELS wide. mxlsink wants a channel mask
# of (1 << channels) - 1, so the caps below always carry one.
FLOW_AUDIO=${FLOW_AUDIO:-7fbec3b1-1b0f-417d-9059-8b94a47197ef}
AUDIO_CHANNELS=${AUDIO_CHANNELS:-2}
case "$AUDIO_CHANNELS" in 1|2|4|8) ;; *) AUDIO_CHANNELS=2 ;; esac
AUDIO_MASK=$(printf '0x%x' $(( (1 << AUDIO_CHANNELS) - 1 )))

# NDI_URL (ip:port) connects straight to the sender and skips discovery.
if [ -n "${NDI_URL:-}" ]; then
  set -- url-address="$NDI_URL"
  echo "ndi-ingest: '$NDI_SRC' at $NDI_URL (direct) -> ${SRC_W}x${SRC_H} @ ${FPS_CAPS}, ${AUDIO_CHANNELS} ch audio"
else
  set --
  echo "ndi-ingest: '$NDI_SRC' (discovery) -> ${SRC_W}x${SRC_H} @ ${FPS_CAPS}, ${AUDIO_CHANNELS} ch audio"
fi

# videoscale is a passthrough when the source already matches SRC_W x SRC_H.
# It is there so a source with a different size cannot change the geometry
# of Flow A underneath the other functions.
#
# Audio:
#   audioconvert with an empty mix-matrix maps channel N to channel N, padding
#   with silence or dropping extra channels. Without it, sources with more than
#   two channels (NDI marks them unpositioned) fail to negotiate, and stereo
#   would be upmixed instead of mapped.
#   audiorate makes the stream continuous, the audio equivalent of videorate:
#   receive-time timestamps from NDI jitter, and MXL wants a regular sample
#   cadence. skip-to-first stops it filling silence back to time zero.
#   If the source sends no audio, the audio pad never appears and there is
#   simply no audio flow.
exec gst-launch-1.0 -e \
  ndisrc ndi-name="$NDI_SRC" "$@" timestamp-mode=receive-time \
  ! ndisrcdemux name=demux \
  demux.video \
    ! queue max-size-buffers=4 leaky=downstream \
    ! videoscale add-borders=true \
    ! video/x-raw,width="$SRC_W",height="$SRC_H",pixel-aspect-ratio=1/1 \
    ! videoconvert ! video/x-raw,format=v210 \
    ! videorate ! video/x-raw,framerate="$FPS_CAPS" \
    ! mxlsink flow-id="$FLOW_A" domain="$MXL_DOMAIN" \
  demux.audio \
    ! queue max-size-time=200000000 \
    ! audioconvert mix-matrix="<>" \
    ! "audio/x-raw,format=F32LE,layout=interleaved,channels=$AUDIO_CHANNELS,channel-mask=(bitmask)$AUDIO_MASK" \
    ! audioresample ! audio/x-raw,rate=48000 \
    ! audiorate skip-to-first=true \
    ! mxlsink flow-id="$FLOW_AUDIO" domain="$MXL_DOMAIN"
