#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Rankin
"""
Routing, ST 2110 and fabric settings, and SDP helpers, shared by the
ingest, the ST 2110 sender, the fabric runner and the GUI.

- routing.json   where each MXL flow comes from on this VM, and what runs
- st2110.json    ST 2110-20/30 input SDPs and the ST 2110-20 video outputs
- fabric.json    the MXL fabrics link to the other lab (RoCEv2 or TCP)

Standard library only, so every container can import it.
"""
import os
import re

import ptp_common as pc

CONFIG_DIR = pc.CONFIG_DIR
ROUTING_CFG = os.path.join(CONFIG_DIR, "routing.json")
ST2110_CFG = os.path.join(CONFIG_DIR, "st2110.json")
FABRIC_CFG = os.path.join(CONFIG_DIR, "fabric.json")
# Desired fabric sessions (written by the GUI) and what the runner reports back
FABRIC_DIR = os.path.join(CONFIG_DIR, "fabric")
FABRIC_STATUS = os.path.join(os.environ.get("PREVIEW_DIR", "/preview"), "fabric-status.json")
ST2110_STATUS = os.path.join(os.environ.get("PREVIEW_DIR", "/preview"), "st2110-status.json")

FLOW_IDS = {
    "A": os.environ.get("FLOW_A", "5fbec3b1-1b0f-417d-9059-8b94a47197ed"),
    "B": os.environ.get("FLOW_B", "6fbec3b1-1b0f-417d-9059-8b94a47197ee"),
    "audio": os.environ.get("FLOW_AUDIO", "7fbec3b1-1b0f-417d-9059-8b94a47197ef"),
}
FLOW_NAMES = {"A": "Flow A", "B": "Flow B", "audio": "Audio flow"}
# Fixed service (port) per flow, so firewalls and captures stay predictable
FABRIC_PORTS = {"A": 0, "B": 1, "audio": 2}

INPUTS = ("ndi", "st2110", "peer", "none")
FLOW_B_SOURCES = ("local", "peer", "off")

ROUTING_DEFAULTS = {
    # What writes Flow A and the audio flow on this VM:
    #   ndi     NDI ingest            st2110  ST 2110-20/30 ingest
    #   peer    the other lab, over the fabric
    #   none    nothing (this VM only uses Flow B, or only sends)
    "input": "ndi",
    # Flow B: local (autoframe runs here), peer (from the other lab) or off
    "flow_b": "local",
    "ndi_out_original": True,
    "ndi_out_vertical": True,
    # Flows this VM sends to the other lab. Only flows made here can be sent.
    "send": [],
}

ST2110_OUT_DEFAULTS = {
    "enabled": False,
    "address": "239.69.20.1",
    "port": 5004,
    "payload_type": 96,
    "session_name": "",
}

ST2110_DEFAULTS = {
    # Input: the senders' SDP files, pasted in the GUI
    "in_video_sdp": "",
    "in_audio_sdp": "",
    "in_iface": "",
    # Kernel receive buffer per stream. 1080p50 is about 220k packets a second.
    "in_buffer_mb": 256,
    # Outputs: Flow A and Flow B as ST 2110-20, 4:2:2 10 bit
    "out": {"A": dict(ST2110_OUT_DEFAULTS),
            "B": dict(ST2110_OUT_DEFAULTS, address="239.69.20.2")},
    "out_iface": "",
    "ttl": 16,
    "dscp": 34,
    # How far behind MXL time the sender reads, as for AES67 out. Covers the
    # time a grain takes to be committed, including a fabric hop.
    "read_delay_ms": 40.0,
    # RTP packet size, header included. ST 2110-10 standard UDP size limit is 1460.
    "mtu": 1460,
    # Pace each stream with the fq qdisc (maxrate). Closer to 2110-21 than bursts.
    "pace": False,
}

FABRIC_DEFAULTS = {
    "enabled": False,
    "provider": "verbs",      # verbs (RoCEv2 on ConnectX) | tcp (any NIC, for testing)
    "iface": "",              # interface facing the other lab; its IPv4 is the fabric node
    "base_port": 47100,
    # The other lab's GUI, for pairing: http://192.168.10.12:8080
    "peer_url": "",
    "peer_user": "",
    "peer_password": "",
}


def _merge(defaults, data):
    cfg = dict(defaults)
    cfg.update({k: v for k, v in data.items() if k in defaults})
    return cfg


def routing_settings():
    cfg = _merge(ROUTING_DEFAULTS, pc.load_json(ROUTING_CFG))
    if cfg["input"] not in INPUTS:
        cfg["input"] = "ndi"
    if cfg["flow_b"] not in FLOW_B_SOURCES:
        cfg["flow_b"] = "local"
    cfg["send"] = [f for f in cfg.get("send") or [] if f in FLOW_IDS and f in local_flows(cfg)]
    return cfg


def local_flows(cfg):
    """Flows written on this VM, which are the only ones it may send on."""
    out = []
    if cfg["input"] in ("ndi", "st2110"):
        out += ["A", "audio"]
    if cfg["flow_b"] == "local":
        out.append("B")
    return out


def peer_flows(cfg):
    """Flows this VM expects to receive from the other lab."""
    out = []
    if cfg["input"] == "peer":
        out += ["A", "audio"]
    if cfg["flow_b"] == "peer":
        out.append("B")
    return out


def st2110_settings():
    data = pc.load_json(ST2110_CFG)
    cfg = _merge(ST2110_DEFAULTS, data)
    out = {}
    for key in ("A", "B"):
        out[key] = _merge(ST2110_DEFAULTS["out"][key], (data.get("out") or {}).get(key) or {})
    cfg["out"] = out
    return cfg


def fabric_settings():
    cfg = _merge(FABRIC_DEFAULTS, pc.load_json(FABRIC_CFG))
    if cfg["provider"] not in ("verbs", "tcp"):
        cfg["provider"] = "verbs"
    return cfg


def flow_def_path(domain, flow_id):
    return os.path.join(domain, f"{flow_id}.mxl-flow", "flow_def.json")


def flow_format(flow_def):
    """Width, height and frame rate ("50" or "60000/1001") from an NMOS flow definition."""
    rate = flow_def.get("grain_rate") or {}
    num, den = int(rate.get("numerator", 0)), int(rate.get("denominator", 1) or 1)
    fps = str(num) if den == 1 else f"{num}/{den}"
    return {"width": flow_def.get("frame_width"), "height": flow_def.get("frame_height"), "fps": fps}


# -------------------------------------------------------------------- SDP
class SdpError(ValueError):
    pass


def parse_sdp(text, media):
    """
    The parts of an ST 2110 SDP the ingest needs, for the first m= line of
    the given media ("video" or "audio"). Raises SdpError with a reason a
    person can act on.
    """
    if not text or not text.strip():
        raise SdpError(f"no {media} SDP")
    session_c = None
    found = None
    for raw in text.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if line.startswith("m="):
            if found is not None:
                break
            parts = line[2:].split()
            if len(parts) >= 4 and parts[0] == media:
                found = {"port": int(parts[1]), "pt": int(parts[3]), "c": None, "attrs": []}
            continue
        if line.startswith("c="):
            m = re.match(r"c=IN IP4 ([0-9.]+)", line)
            if m:
                if found is None:
                    session_c = m.group(1)
                else:
                    found["c"] = m.group(1)
            continue
        if found is not None and line.startswith("a="):
            found["attrs"].append(line[2:])
    if found is None:
        raise SdpError(f"the SDP has no m={media} line")
    address = found["c"] or session_c
    if not address:
        raise SdpError("the SDP has no c=IN IP4 address")
    out = {"address": address, "port": found["port"], "pt": found["pt"], "source": ""}
    fmtp, rtpmap = {}, ""
    for attr in found["attrs"]:
        if attr.startswith(f"rtpmap:{found['pt']} "):
            rtpmap = attr.split(" ", 1)[1].strip()
        elif attr.startswith(f"fmtp:{found['pt']} "):
            for item in attr.split(" ", 1)[1].split(";"):
                if "=" in item:
                    k, v = item.split("=", 1)
                    fmtp[k.strip()] = v.strip()
        elif attr.startswith("source-filter:"):
            parts = attr.split()
            if len(parts) >= 6:
                out["source"] = parts[-1]
    if media == "video":
        if not rtpmap.lower().startswith("raw/90000"):
            raise SdpError(f"video encoding is {rtpmap or 'not given'}, expected raw/90000 (ST 2110-20)")
        sampling = fmtp.get("sampling", "")
        if sampling != "YCbCr-4:2:2":
            raise SdpError(f"sampling {sampling or 'not given'} is not supported, only YCbCr-4:2:2")
        if fmtp.get("depth") not in ("8", "10"):
            raise SdpError("depth must be 8 or 10")
        if "interlace" in fmtp:
            raise SdpError("interlaced video is not supported")
        try:
            width, height = int(fmtp["width"]), int(fmtp["height"])
        except (KeyError, ValueError) as exc:
            raise SdpError("width and height are missing from the fmtp line") from exc
        rate = fmtp.get("exactframerate", "")
        if not re.fullmatch(r"\d+(/\d+)?", rate):
            raise SdpError("exactframerate is missing from the fmtp line")
        out.update(width=width, height=height, fps=rate, depth=int(fmtp["depth"]),
                   colorimetry=fmtp.get("colorimetry", "BT709"))
    else:
        m = re.match(r"(L24|L16)/(\d+)(?:/(\d+))?", rtpmap)
        if not m:
            raise SdpError(f"audio encoding is {rtpmap or 'not given'}, expected L24 or L16 (ST 2110-30)")
        out.update(encoding=m.group(1), rate=int(m.group(2)), channels=int(m.group(3) or 1))
        if out["rate"] != 48000:
            raise SdpError("only 48 kHz audio is supported")
    return out


def video_sdp(*, session_id, version, src_ip, name, address, port, pt, ttl,
              width, height, fps, refclk):
    """ST 2110-20 SDP for 4:2:2 10 bit progressive video, as this lab sends it."""
    lines = [
        "v=0",
        f"o=- {session_id} {version} IN IP4 {src_ip}",
        f"s={name}",
        "t=0 0",
        f"m=video {port} RTP/AVP {pt}",
        f"c=IN IP4 {address}/{ttl}",
        f"a=source-filter: incl IN IP4 {address} {src_ip}",
        f"a=rtpmap:{pt} raw/90000",
        (f"a=fmtp:{pt} sampling=YCbCr-4:2:2; width={width}; height={height}; "
         f"exactframerate={fps}; depth=10; TCS=SDR; colorimetry=BT709; PM=2110GPM; "
         "SSN=ST2110-20:2017; TP=2110TPW; "),
        refclk,
        "a=mediaclk:direct=0",
    ]
    return "\r\n".join(lines) + "\r\n"


def refclk_line(ptp, gm_sdp, mac):
    if ptp["mode"] != "off" and gm_sdp:
        return f"a=ts-refclk:ptp=IEEE1588-2008:{gm_sdp}:{int(ptp['domain'])}"
    if ptp["mode"] != "off":
        return "a=ts-refclk:ptp=IEEE1588-2008:traceable"
    return f"a=ts-refclk:localmac={mac.upper().replace(':', '-')}"
