# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Rankin
"""Paths, fixed choices and the helpers that read and write the lab's settings files."""
import json
import os
import re
import socket
import sys

sys.path.insert(0, os.environ.get("LAB_SCRIPTS", "/opt/scripts"))
import media_common as mc  # noqa: E402,F401  (re-exported for the other modules)
import ptp_common as pc  # noqa: E402,F401  (re-exported for the other modules)

CONFIG_DIR   = os.environ.get("CONFIG_DIR", "/config")
PIPELINE_CFG = os.path.join(CONFIG_DIR, "pipeline.json")
TUNING_CFG   = os.path.join(CONFIG_DIR, "autoframe.json")
STATUS_IN    = os.path.join(CONFIG_DIR, "autoframe-status.json")
PREVIEW_IN   = os.path.join(os.environ.get("PREVIEW_DIR", "/preview"), "preview.bin")
DOMAIN       = os.environ.get("MXL_DOMAIN", "/dev/shm/mxl")
FLOW_A       = os.environ.get("FLOW_A", "")
FLOW_B       = os.environ.get("FLOW_B", "")
FLOW_AUDIO   = os.environ.get("FLOW_AUDIO", "7fbec3b1-1b0f-417d-9059-8b94a47197ef")
AUDIO_STATUS = os.path.join(os.environ.get("PREVIEW_DIR", "/preview"), "audio-status.json")
AUDIO_LEVELS = os.path.join(os.environ.get("PREVIEW_DIR", "/preview"), "audio-levels.json")
SDP_FILE     = os.path.join(CONFIG_DIR, "aes67.sdp")
ST2110_SDP   = os.path.join(CONFIG_DIR, "st2110-{}.sdp")
# The page lives beside app.py, one level up from this package
STATIC_DIR   = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
NDI_DIR      = os.path.join(CONFIG_DIR, "ndi")
NDI_CFG      = os.path.join(NDI_DIR, "ndi-config.v1.json")
MODE_FILE    = os.path.join(NDI_DIR, "mode.json")
DISCOVERY_MODES = ("mdns", "local", "external")
SERVER_RE    = re.compile(r"[A-Za-z0-9.\-]+(:\d{1,5})?")

SERVICES = {
    "ndi-discovery":    "mxl-ndi-discovery",
    "ndi-ingest":       "mxl-ndi-ingest",
    "autoframe":        "mxl-autoframe",
    "ndi-out-original": "mxl-ndi-out-original",
    "ndi-out-vertical": "mxl-ndi-out-vertical",
    "aes67-out":        "mxl-aes67-out",
    "st2110-out":       "mxl-st2110-out",
    "fabric":           "mxl-fabric",
    "ptp":              "mxl-ptp",
}

RESOLUTIONS = {"1920x1080", "1280x720"}
OUTPUTS     = {"1080x1920", "720x1280"}
FRAME_RATES = {25, 30, 50, 60}
SCALERS     = {"lanczos", "4-tap", "bilinear"}

TUNING_DEFAULTS = {
    "mode": "weighted", "ease": 0.08, "deadband_px": 24, "max_step_px": 6,
    "det_period_s": 0.20, "recentre_s": 3.0, "score_threshold": 0.6,
    "nms_threshold": 0.3, "top_k": 10, "min_face_px": 0, "bias_px": 0,
    "manual_pos": 0.5, "preview": True,
}


# ------------------------------------------------------------------ files
def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {} if default is None else dict(default)


def write_json_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def vm_ip():
    """This VM's LAN address. The GUI uses host networking, so this is the host's."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("1.1.1.1", 80))
        return sock.getsockname()[0]
    except OSError:
        return ""
    finally:
        sock.close()


def discovery_state():
    saved = load_json(MODE_FILE)
    mode = saved.get("mode")
    if mode not in DISCOVERY_MODES:
        mode = "local" if os.environ.get("NDI_DISCOVERY") == "server" else "mdns"
    return {"mode": mode, "servers": saved.get("servers", ""), "vm_ip": vm_ip()}


def write_discovery(mode, servers):
    """Write the NDI config every lab container reads, plus the lab's own record."""
    os.makedirs(NDI_DIR, exist_ok=True)
    if mode == "mdns":
        cfg = {"ndi": {}}
    else:
        cfg = {"ndi": {"networks": {"discovery": servers}}}
    write_json_atomic(NDI_CFG, cfg)
    write_json_atomic(MODE_FILE, {"mode": mode, "servers": servers if mode == "external" else ""})


def pipeline_settings():
    cfg = load_json(PIPELINE_CFG)
    env = os.environ
    return {
        "NDI_SRC": cfg.get("NDI_SRC", env.get("NDI_SRC", "")),
        "NDI_URL": cfg.get("NDI_URL", env.get("NDI_URL", "")),
        "SRC_W":   int(cfg.get("SRC_W", env.get("SRC_W", 1920))),
        "SRC_H":   int(cfg.get("SRC_H", env.get("SRC_H", 1080))),
        "SRC_FPS": cfg.get("SRC_FPS", env.get("SRC_FPS", 50)),
        # Auto unless turned off, either saved from the GUI or in .env
        "SRC_AUTO": bool(cfg["SRC_AUTO"]) if "SRC_AUTO" in cfg
                    else (env.get("SRC_AUTO") or "true").strip().lower() in ("1", "true", "yes"),
        "OUT_W":   int(cfg.get("OUT_W", env.get("OUT_W", 1080))),
        "OUT_H":   int(cfg.get("OUT_H", env.get("OUT_H", 1920))),
        "SCALER":  cfg.get("SCALER", env.get("SCALER", "lanczos")),
    }


def media_configured(settings):
    """
    Whether there is anything for an ordered restart to start: an NDI source,
    an ST 2110 SDP, or flows from the peer.
    """
    r = mc.routing_settings()
    if r["input"] == "ndi":
        return bool(settings["NDI_SRC"])
    if r["input"] == "st2110":
        return bool(mc.st2110_settings()["in_video_sdp"].strip())
    return r["input"] == "peer" or r["flow_b"] == "peer"
