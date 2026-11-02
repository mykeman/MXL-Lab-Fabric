#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Rankin
"""
ST 2110 ingest: an ST 2110-20 video stream to Flow A and, optionally, an
ST 2110-30 audio stream to the audio flow. The streams are described by the
senders' SDP files, pasted in the GUI and saved in /config/st2110.json.

Started by run-ingest.sh when the input is ST 2110. Like the NDI ingest,
buffers are timestamped when they arrive, and mxlsink maps that to grain
indices, so Flow A runs on this VM's (PTP) clock.

Receiving uncompressed video through kernel sockets is heavy: 1080p50 is
about 2.6 Gb/s in roughly 220,000 packets a second. It needs a large socket
buffer (net.core.rmem_max, set by setup.sh) and a fast CPU. Packet loss shows
up as damaged lines in the picture, not as an error.
"""
import os
import shlex
import subprocess
import sys

sys.path.insert(0, "/opt/scripts")
import media_common as mc  # noqa: E402
import ptp_common as pc  # noqa: E402

DOMAIN = os.environ.get("MXL_DOMAIN", "/dev/shm/mxl")


def log(msg):
    print(f"st2110-in: {msg}", flush=True)


def has_property(element, prop):
    try:
        out = subprocess.run(["gst-inspect-1.0", element], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return f"  {prop} " in out or f"  {prop}:" in out


def udpsrc(stream, iface, buffer_bytes, ssm):
    parts = [f"udpsrc address={stream['address']} port={stream['port']}",
             "auto-multicast=true", f"buffer-size={buffer_bytes}"]
    if iface:
        parts.append(f"multicast-iface={iface}")
    if stream["source"] and ssm:
        parts.append(f"multicast-source=+{stream['source']}")
    return " ".join(parts)


def main():
    cfg = mc.st2110_settings()
    iface = pc.resolve_iface(cfg["in_iface"])
    buffer_bytes = int(cfg["in_buffer_mb"]) * 1024 * 1024
    ssm = has_property("udpsrc", "multicast-source")
    try:
        video = mc.parse_sdp(cfg["in_video_sdp"], "video")
    except mc.SdpError as exc:
        log(f"cannot use the video SDP: {exc}. Fix it in the GUI's ST 2110 tab.")
        return 1
    audio = None
    if cfg["in_audio_sdp"].strip():
        try:
            audio = mc.parse_sdp(cfg["in_audio_sdp"], "audio")
        except mc.SdpError as exc:
            log(f"ignoring the audio SDP: {exc}")

    fps = video["fps"] if "/" in video["fps"] else f"{video['fps']}/1"
    flow_a, flow_audio = mc.FLOW_IDS["A"], mc.FLOW_IDS["audio"]
    log(f"video {video['width']}x{video['height']} @ {video['fps']} {video['depth']} bit "
        f"from {video['address']}:{video['port']}"
        + (f" (source {video['source']})" if video["source"] and ssm else "")
        + f" on {iface or 'the default interface'}")

    # 10 bit 4:2:2 arrives as UYVP (pgroups), 8 bit as UYVY. Flow A is v210.
    desc = (
        udpsrc(video, iface, buffer_bytes, ssm) +
        ' caps="application/x-rtp,media=video,clock-rate=90000,encoding-name=RAW,'
        f"sampling=YCbCr-4:2:2,depth=(string){video['depth']},width=(string){video['width']},"
        f"height=(string){video['height']},colorimetry=(string){video['colorimetry']},"
        f'payload={video["pt"]}"'
        # A short leaky queue moves depayloading off the socket thread
        " ! queue max-size-buffers=0 max-size-bytes=0 max-size-time=50000000 leaky=downstream"
        " ! rtpvrawdepay"
        " ! queue max-size-buffers=4 leaky=downstream"
        " ! videoconvert ! video/x-raw,format=v210"
        f" ! videorate ! video/x-raw,framerate={fps}"
        f" ! mxlsink flow-id={flow_a} domain={DOMAIN}"
    )
    if audio is not None:
        ch = int(pc.audio_settings()["channels"])
        if ch not in (1, 2, 4, 8):
            ch = 2
        mask = hex((1 << ch) - 1)
        depay = "rtpL24depay" if audio["encoding"] == "L24" else "rtpL16depay"
        log(f"audio {audio['channels']} ch {audio['encoding']} from {audio['address']}:{audio['port']}, "
            f"mapped to {ch} ch in the audio flow")
        desc += (
            " " + udpsrc(audio, iface, 8 * 1024 * 1024, ssm) +
            ' caps="application/x-rtp,media=audio,clock-rate=48000,'
            f'encoding-name={audio["encoding"]},channels={audio["channels"]},payload={audio["pt"]}"'
            f" ! {depay} ! queue max-size-time=200000000"
            # Same channel mapping and continuity as the NDI ingest
            ' ! audioconvert mix-matrix="<>"'
            f' ! "audio/x-raw,format=F32LE,layout=interleaved,channels={ch},channel-mask=(bitmask){mask}"'
            " ! audioresample ! audio/x-raw,rate=48000"
            " ! audiorate skip-to-first=true"
            f" ! mxlsink flow-id={flow_audio} domain={DOMAIN}"
        )
    else:
        log("no audio SDP, so there is no audio flow")

    args = ["gst-launch-1.0", "-e", *shlex.split(desc)]
    os.execvp(args[0], args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
