#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Rankin
"""
MXL lab control GUI: the HTTP API and the page.

The work is done in the lab package:
  lab.settings    paths, fixed choices, settings files
  lab.ndi         NDI discovery and format probing
  lab.docker_ctl  container control and status
  lab.monitors    MXL flow, PTP and AES67 status watchers
  lab.jobs        ordered changes to the running lab
  lab.fabric      pairing flows with the other lab over MXL fabrics
"""
import asyncio
import base64
import json
import os
import re
import secrets
import struct
import subprocess
import threading
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from lab.docker_ctl import DOCKER_ERRORS, container, container_status, dock
from lab.fabric import FabricLink
from lab.jobs import ApplyJob, restart_after_step, startup_sequence, wait_for_ptp
from lab.monitors import FlowWatch, PtpWatch, audio_status
from lab.ndi import NdiDiscovery, probe_source
from lab.settings import (
    AUDIO_LEVELS,
    DISCOVERY_MODES,
    FRAME_RATES,
    OUTPUTS,
    PREVIEW_IN,
    RESOLUTIONS,
    SCALERS,
    SDP_FILE,
    SERVER_RE,
    SERVICES,
    ST2110_SDP,
    STATIC_DIR,
    STATUS_IN,
    TUNING_CFG,
    TUNING_DEFAULTS,
    discovery_state,
    load_json,
    mc,
    media_configured,
    pc,
    pipeline_settings,
    vm_ip,
    write_discovery,
    write_json_atomic,
)

# -------------------------------------------------------------------- api
app = FastAPI(title="MXL lab control")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Optional HTTP basic authentication. Set GUI_USER and GUI_PASSWORD in .env
# to turn it on. It keeps casual visitors out; it is not a substitute for
# keeping the GUI on a trusted network, since it controls Docker on this VM.
GUI_USER = os.environ.get("GUI_USER", "")
GUI_PASSWORD = os.environ.get("GUI_PASSWORD", "")

if GUI_USER and GUI_PASSWORD:
    @app.middleware("http")
    async def basic_auth(request: Request, call_next):
        user = password = ""
        header = request.headers.get("authorization", "")
        if header.startswith("Basic "):
            try:
                user, _, password = base64.b64decode(header[6:]).decode().partition(":")
            except (ValueError, UnicodeDecodeError):
                pass
        if not (secrets.compare_digest(user.encode(), GUI_USER.encode())
                and secrets.compare_digest(password.encode(), GUI_PASSWORD.encode())):
            return Response("Authentication required", status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="MXL lab"'})
        return await call_next(request)
ndi = NdiDiscovery()
flows = FlowWatch()
job = ApplyJob()
fabric = FabricLink(job)
job.fabric = fabric
ptpw = PtpWatch()
# A clock step outside an apply breaks the running MXL flows: restart them in order
ptpw.on_step = lambda size: restart_after_step(job, size)
threading.Thread(target=startup_sequence, args=(job, ptpw), daemon=True).start()


class PipelineIn(BaseModel):
    NDI_SRC: str = Field(min_length=1, max_length=256)
    NDI_URL: str = ""
    resolution: str
    output: str
    SRC_FPS: int
    SCALER: str


class TuningIn(BaseModel):
    mode: str = "weighted"
    ease: float = Field(0.08, ge=0.005, le=0.5)
    deadband_px: float = Field(24, ge=0, le=300)
    max_step_px: float = Field(6, ge=0.5, le=80)
    det_period_s: float = Field(0.2, ge=0.04, le=2.0)
    recentre_s: float = Field(3.0, ge=0.2, le=60)
    score_threshold: float = Field(0.6, ge=0.1, le=0.99)
    nms_threshold: float = Field(0.3, ge=0.05, le=0.9)
    top_k: int = Field(10, ge=1, le=50)
    min_face_px: float = Field(0, ge=0, le=1000)
    bias_px: float = Field(0, ge=-600, le=600)
    manual_pos: float = Field(0.5, ge=0, le=1)
    preview: bool = True


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/api/ndi/sources")
def ndi_sources():
    return ndi.snapshot()


@app.post("/api/ndi/refresh")
def ndi_refresh():
    ndi.restart.set()
    return {"ok": True}


@app.get("/api/pipeline")
def get_pipeline():
    return {"settings": pipeline_settings(), "job": job.snapshot(),
            "choices": {"resolutions": ["auto"] + sorted(RESOLUTIONS, reverse=True),
                        "outputs": sorted(OUTPUTS, reverse=True),
                        "frame_rates": sorted(FRAME_RATES),
                        "scalers": ["lanczos", "4-tap", "bilinear"]}}


@app.post("/api/pipeline/apply")
def apply_pipeline(body: PipelineIn):
    auto = body.resolution == "auto"
    if not auto and body.resolution not in RESOLUTIONS:
        raise HTTPException(400, "unsupported source resolution")
    if body.output not in OUTPUTS:
        raise HTTPException(400, "unsupported output size")
    if not auto and body.SRC_FPS not in FRAME_RATES:
        raise HTTPException(400, "unsupported frame rate")
    if body.SCALER not in SCALERS:
        raise HTTPException(400, "unsupported scaler")
    url = body.NDI_URL.strip()
    if url and not re.fullmatch(r"[A-Za-z0-9.\-]+:\d{1,5}", url):
        raise HTTPException(400, "address must look like 192.168.1.169:5961")
    ow, oh = (int(v) for v in body.output.split("x"))
    name = body.NDI_SRC.strip()
    if auto:
        prev = pipeline_settings()
        sw, sh, fps = prev["SRC_W"], prev["SRC_H"], prev["SRC_FPS"]
    else:
        sw, sh = (int(v) for v in body.resolution.split("x"))
        fps = body.SRC_FPS
    settings = {"NDI_SRC": name, "NDI_URL": url, "SRC_W": sw, "SRC_H": sh,
                "SRC_FPS": fps, "SRC_AUTO": auto, "OUT_W": ow, "OUT_H": oh,
                "SCALER": body.SCALER}

    def detect(say):
        say(f"Reading the format of {name}")
        fmt = probe_source(name, url)
        settings.update({"SRC_W": fmt["width"], "SRC_H": fmt["height"], "SRC_FPS": fmt["fps"]})
        say(f"Source is {fmt['label']}")

    job.start(settings, detect if auto else None)
    return {"ok": True}


class ProbeIn(BaseModel):
    NDI_SRC: str = Field(min_length=1, max_length=256)
    NDI_URL: str = ""


@app.post("/api/ndi/probe")
def ndi_probe(body: ProbeIn):
    url = body.NDI_URL.strip()
    if url and not re.fullmatch(r"[A-Za-z0-9.\-]+:\d{1,5}", url):
        raise HTTPException(400, "address must look like 192.168.1.169:5961")
    try:
        return probe_source(body.NDI_SRC.strip(), url)
    except RuntimeError as exc:
        raise HTTPException(422, str(exc)) from exc
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(504, "the source did not answer in time") from exc


@app.get("/api/pipeline/job")
def get_job():
    return job.snapshot()


class DiscoveryIn(BaseModel):
    mode: str
    servers: str = ""


@app.get("/api/discovery")
def get_discovery():
    return discovery_state()


@app.post("/api/discovery")
def set_discovery(body: DiscoveryIn):
    mode = body.mode
    if mode not in DISCOVERY_MODES:
        raise HTTPException(400, "mode must be mdns, local or external")
    servers = ""
    if mode == "local":
        servers = vm_ip()
        if not servers:
            raise HTTPException(500, "could not work out this VM's IP address")
    elif mode == "external":
        parts = [p.strip() for p in body.servers.split(",") if p.strip()]
        if not parts:
            raise HTTPException(400, "enter the discovery server address")
        bad = [p for p in parts if not SERVER_RE.fullmatch(p)]
        if bad:
            raise HTTPException(400, f"not a valid address: {', '.join(bad)}")
        servers = ",".join(parts)

    def first(say):
        write_discovery(mode, servers)
        label = {"mdns": "mDNS", "local": f"internal discovery server ({servers})",
                 "external": f"external discovery server {servers}"}[mode]
        say(f"NDI discovery set to {label}")
        ndi.reset()
        try:
            disc = container("ndi-discovery")
        except HTTPException as exc:
            if mode == "local":
                raise HTTPException(404, "the discovery server container does not exist. "
                                         "Run setup.sh again to create it.") from exc
            return
        if mode == "local":
            if disc.status != "running":
                disc.start()
                time.sleep(1)
            say("Internal discovery server is running")
        elif disc.status == "running":
            disc.stop(timeout=5)
            say("Stopped the internal discovery server")
        say("Scanning with the new setting, the source list refreshes within about 15 s")

    settings = pipeline_settings()
    job.start(settings if media_configured(settings) else None, first, kind="discovery")
    return {"ok": True}


# ------------------------------------------------------------------ audio
AUDIO_CHOICES = {"channels": [1, 2, 4, 8], "encodings": ["L24", "L16"], "ptimes": [1000, 125],
                 "sources": ["flow", "tone"]}
MCAST_RE = re.compile(r"(22[4-9]|23\d)\.\d{1,3}\.\d{1,3}\.\d{1,3}")


class AudioIn(BaseModel):
    enabled: bool = False
    source: str = "flow"
    channels: int = 2
    encoding: str = "L24"
    ptime_us: int = 1000
    address: str = "239.69.10.1"
    port: int = Field(5004, ge=1024, le=65535)
    ttl: int = Field(16, ge=1, le=255)
    payload_type: int = Field(96, ge=96, le=127)
    dscp: int = Field(34, ge=0, le=63)
    iface: str = ""
    # Below zero cancels the sender's own buffering. The read delay keeps the
    # audio available early enough for that.
    send_delay_ms: float = Field(1.0, ge=-2, le=50)
    read_delay_ms: float = Field(60.0, ge=0, le=500)
    session_name: str = Field("MXL Lab Audio", min_length=1, max_length=64)
    sap: bool = True


def check_iface(name):
    if name and not os.path.isdir(f"/sys/class/net/{name}"):
        raise HTTPException(400, f"interface {name} does not exist on this VM")


def iface_choices():
    return {"ifaces": pc.list_ifaces(), "default_iface": pc.default_iface()}


@app.get("/api/audio")
def get_audio():
    return {"settings": pc.audio_settings(), "choices": AUDIO_CHOICES, **iface_choices()}


@app.post("/api/audio/apply")
def apply_audio(body: AudioIn):
    if body.channels not in AUDIO_CHOICES["channels"]:
        raise HTTPException(400, "channels must be 1, 2, 4 or 8")
    if body.encoding not in AUDIO_CHOICES["encodings"]:
        raise HTTPException(400, "encoding must be L24 or L16")
    if body.ptime_us not in AUDIO_CHOICES["ptimes"]:
        raise HTTPException(400, "packet time must be 1000 or 125 us")
    if body.source not in AUDIO_CHOICES["sources"]:
        raise HTTPException(400, "source must be flow or tone")
    address = body.address.strip()
    if not MCAST_RE.fullmatch(address) or any(int(o) > 255 for o in address.split(".")):
        raise HTTPException(400, "address must be an IPv4 multicast address, such as 239.69.10.1")
    if body.port % 2:
        raise HTTPException(400, "RTP port must be even")
    if any(ch in body.session_name for ch in "\r\n"):
        raise HTTPException(400, "session name must be one line")
    check_iface(body.iface.strip())

    before = pc.audio_settings()
    data = body.model_dump()
    data.update(address=address, iface=body.iface.strip(), session_name=body.session_name.strip())
    settings = pipeline_settings()
    # The channel count is set where the audio enters MXL, so changing it
    # needs the whole chain restarted. Anything else only touches aes67-out.
    full = data["channels"] != before["channels"] and media_configured(settings)

    def first(say):
        write_json_atomic(pc.AUDIO_CFG, data)
        say("Saved audio settings")
        if full:
            say(f"Channel count changed to {data['channels']}, restarting from the ingest")

    job.start(settings if full else None, first, kind="audio", audio=True)
    return {"ok": True}


@app.get("/api/audio/levels/stream")
async def audio_levels_stream(request: Request):
    """
    Server-sent events with each new meter reading, about 20 a second. Only
    the GUI's display reads this; the sender just writes a small file.
    """
    async def events():
        last_seq, last_send = None, 0.0
        while not await request.is_disconnected():
            try:
                with open(AUDIO_LEVELS) as f:
                    data = json.load(f)
            except (OSError, ValueError):
                data = None
            now = time.time()
            if data and data.get("seq") != last_seq and now - data.get("ts", 0) < 2:
                last_seq, last_send = data.get("seq"), now
                yield f"data: {json.dumps(data)}\n\n"
            elif now - last_send > 10:
                last_send = now
                yield ": keepalive\n\n"
            await asyncio.sleep(0.04)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.get("/api/audio/sdp")
def get_sdp():
    if not os.path.exists(SDP_FILE):
        raise HTTPException(404, "no SDP yet. It is written once the AES67 output is sending.")
    with open(SDP_FILE) as f:
        text = f.read()
    return Response(text, media_type="application/sdp",
                    headers={"Content-Disposition": 'attachment; filename="mxl-lab-audio.sdp"',
                             "Cache-Control": "no-store"})


# -------------------------------------------------------------------- PTP
PTP_LIMITS = {"log_sync": (-7, 4), "log_announce": (-3, 4), "log_delay_req": (-7, 6)}


class PtpIn(BaseModel):
    mode: str = "off"
    profile: str = "dante"
    iface: str = ""
    timestamping: str = "software"
    domain: int = Field(0, ge=0, le=127)
    priority1: int = Field(128, ge=0, le=255)
    priority2: int = Field(128, ge=0, le=255)
    log_sync: int = -3
    log_announce: int = 1
    log_delay_req: int = 0
    announce_timeout: int = Field(3, ge=2, le=10)
    delay_mechanism: str = "E2E"
    dscp_event: int = Field(46, ge=0, le=63)
    dscp_general: int = Field(46, ge=0, le=63)


@app.get("/api/ptp")
def get_ptp():
    return {"settings": pc.ptp_settings(), "profiles": pc.PTP_PROFILES,
            "limits": PTP_LIMITS, **iface_choices()}


@app.post("/api/ptp/apply")
def apply_ptp(body: PtpIn):
    if body.mode not in pc.PTP_MODES:
        raise HTTPException(400, "mode must be off, follow or auto")
    if body.profile not in ("dante", "st2059", "custom"):
        raise HTTPException(400, "profile must be dante, st2059 or custom")
    if body.timestamping not in ("software", "hardware"):
        raise HTTPException(400, "timestamping must be software or hardware")
    if body.delay_mechanism not in ("E2E", "P2P"):
        raise HTTPException(400, "delay mechanism must be E2E or P2P")
    for key, (lo, hi) in PTP_LIMITS.items():
        if not lo <= getattr(body, key) <= hi:
            raise HTTPException(400, f"{key} must be between {lo} and {hi}")
    iface = body.iface.strip()
    check_iface(iface)
    if body.timestamping == "hardware" and not pc.timestamping_caps(iface or pc.default_iface())["hardware"]:
        raise HTTPException(400, "this interface has no hardware timestamping. "
                                 "Pass a NIC through to the VM, or use software.")
    data = body.model_dump()
    data["iface"] = iface

    def first(say):
        write_json_atomic(pc.PTP_CFG, data)
        say("Saved PTP settings")
        c = container("ptp")
        if c.status == "running":
            c.restart(timeout=5)
        else:
            c.start()
        if data["mode"] == "off":
            say("PTP is off. The VM clock is left where it is.")
            return
        ptpw.reset()
        time.sleep(2)
        ptpw.reset()
        say("Restarted the PTP function, waiting for it to settle")
        wait_for_ptp(ptpw, say)
        say("Restarting media functions, because the clock may have stepped")

    settings = pipeline_settings()
    job.start(settings if media_configured(settings) else None, first, kind="ptp", audio=True)
    return {"ok": True}


# ---------------------------------------------------------------- routing
class RoutingIn(BaseModel):
    input: str = "ndi"
    flow_b: str = "local"
    ndi_out_original: bool = True
    ndi_out_vertical: bool = True
    send: list[str] = []


def routing_payload():
    r = mc.routing_settings()
    return {"settings": r, "local_flows": mc.local_flows(r), "peer_flows": mc.peer_flows(r),
            "flow_ids": mc.FLOW_IDS, "choices": {"inputs": list(mc.INPUTS), "flow_b": list(mc.FLOW_B_SOURCES)}}


@app.get("/api/routing")
def get_routing():
    return routing_payload()


@app.post("/api/routing/apply")
def apply_routing(body: RoutingIn):
    if body.input not in mc.INPUTS:
        raise HTTPException(400, "input must be ndi, st2110, peer or none")
    if body.flow_b not in mc.FLOW_B_SOURCES:
        raise HTTPException(400, "Flow B must be local, peer or off")
    data = body.model_dump()
    if data["input"] == "none" and data["flow_b"] == "local":
        raise HTTPException(400, "autoframe needs Flow A. Choose an input, or take Flow B from the peer.")
    local = mc.local_flows(data)
    bad = [k for k in data["send"] if k not in local]
    if bad:
        raise HTTPException(400, "only flows made on this lab can be sent: "
                                 + ", ".join(mc.FLOW_NAMES.get(k, k) for k in bad))
    data["send"] = [k for k in mc.FLOW_IDS if k in data["send"]]
    if (data["send"] or mc.peer_flows(data)) and not mc.fabric_settings()["enabled"]:
        note = "The fabric is off, so nothing goes to or comes from the peer until it is turned on."
    else:
        note = None

    def first(say):
        write_json_atomic(mc.ROUTING_CFG, data)
        say("Saved routing")
        if note:
            say(note)

    settings = pipeline_settings()
    job.start(settings, first, kind="routing")
    return {"ok": True}


# ---------------------------------------------------------------- ST 2110
class SdpCheckIn(BaseModel):
    text: str = Field("", max_length=20000)
    media: str = "video"


class St2110InputIn(BaseModel):
    in_video_sdp: str = Field("", max_length=20000)
    in_audio_sdp: str = Field("", max_length=20000)
    in_iface: str = ""
    in_buffer_mb: int = Field(256, ge=8, le=2048)


class St2110OutIn(BaseModel):
    enabled: bool = False
    address: str = "239.69.20.1"
    port: int = Field(5004, ge=1024, le=65535)
    payload_type: int = Field(96, ge=96, le=127)
    session_name: str = Field("", max_length=64)


class St2110OutputIn(BaseModel):
    out: dict[str, St2110OutIn]
    out_iface: str = ""
    ttl: int = Field(16, ge=1, le=255)
    dscp: int = Field(34, ge=0, le=63)
    read_delay_ms: float = Field(40.0, ge=0, le=500)
    mtu: int = Field(1460, ge=576, le=8972)
    pace: bool = False


def sdp_summary(text, media):
    if not text.strip():
        return None
    try:
        return {"ok": True, **mc.parse_sdp(text, media)}
    except mc.SdpError as exc:
        return {"ok": False, "error": str(exc)}


def st2110_status():
    st = load_json(mc.ST2110_STATUS)
    if st.get("ts"):
        st["age"] = round(time.time() - st["ts"], 1)
    return st


@app.get("/api/st2110")
def get_st2110():
    cfg = mc.st2110_settings()
    return {"settings": cfg, "video": sdp_summary(cfg["in_video_sdp"], "video"),
            "audio": sdp_summary(cfg["in_audio_sdp"], "audio"), "status": st2110_status(),
            **iface_choices()}


@app.post("/api/st2110/check")
def check_sdp(body: SdpCheckIn):
    if body.media not in ("video", "audio"):
        raise HTTPException(400, "media must be video or audio")
    return sdp_summary(body.text, body.media) or {"ok": False, "error": "empty"}


@app.post("/api/st2110/input")
def apply_st2110_input(body: St2110InputIn):
    try:
        video = mc.parse_sdp(body.in_video_sdp, "video")
    except mc.SdpError as exc:
        raise HTTPException(400, f"video SDP: {exc}") from exc
    if video["width"] < video["height"] or video["width"] > 4096 or video["height"] > 2160:
        raise HTTPException(400, "only landscape video up to 4096x2160 is supported")
    if body.in_audio_sdp.strip():
        try:
            mc.parse_sdp(body.in_audio_sdp, "audio")
        except mc.SdpError as exc:
            raise HTTPException(400, f"audio SDP: {exc}") from exc
    check_iface(body.in_iface.strip())
    cfg = mc.st2110_settings()
    cfg.update(body.model_dump(), in_iface=body.in_iface.strip())
    settings = pipeline_settings()
    settings.update(SRC_W=video["width"], SRC_H=video["height"], SRC_FPS=video["fps"], SRC_AUTO=False)
    active = mc.routing_settings()["input"] == "st2110"

    def first(say):
        write_json_atomic(mc.ST2110_CFG, cfg)
        say(f"Saved ST 2110 input: {video['width']}x{video['height']} @ {video['fps']} "
            f"from {video['address']}:{video['port']}")
        if not active:
            say("The input is not ST 2110 in the Routing tab, so nothing restarts")

    job.start(settings if active else None, first, kind="st2110in")
    return {"ok": True}


@app.post("/api/st2110/output")
def apply_st2110_output(body: St2110OutputIn):
    cfg = mc.st2110_settings()
    for key, o in body.out.items():
        if key not in ("A", "B"):
            raise HTTPException(400, "outputs are A and B")
        address = o.address.strip()
        if not MCAST_RE.fullmatch(address) or any(int(p) > 255 for p in address.split(".")):
            raise HTTPException(400, f"{mc.FLOW_NAMES[key]}: address must be an IPv4 multicast address")
        if o.port % 2:
            raise HTTPException(400, f"{mc.FLOW_NAMES[key]}: RTP port must be even")
        cfg["out"][key] = dict(o.model_dump(), address=address, session_name=o.session_name.strip())
    dests = [(o["address"], o["port"]) for o in cfg["out"].values() if o["enabled"]]
    if len(dests) != len(set(dests)):
        raise HTTPException(400, "the two outputs need different destinations")
    check_iface(body.out_iface.strip())
    cfg.update(body.model_dump(exclude={"out"}), out_iface=body.out_iface.strip())

    def first(say):
        write_json_atomic(mc.ST2110_CFG, cfg)
        say("Saved ST 2110 output settings")

    job.start(None, first, kind="st2110", st2110=True)
    return {"ok": True}


@app.get("/api/st2110/sdp/{key}")
def get_st2110_sdp(key: str):
    if key not in ("A", "B"):
        raise HTTPException(404, "no such output")
    path = ST2110_SDP.format(key)
    if not os.path.exists(path):
        raise HTTPException(404, "no SDP yet. It is written once the output is sending.")
    with open(path) as f:
        text = f.read()
    return Response(text, media_type="application/sdp",
                    headers={"Content-Disposition": f'attachment; filename="mxl-lab-flow-{key}.sdp"',
                             "Cache-Control": "no-store"})


# ----------------------------------------------------------------- fabric
PEER_URL_RE = re.compile(r"https?://[A-Za-z0-9.\-\[\]:]+(:\d{1,5})?/?")


class FabricIn(BaseModel):
    enabled: bool = False
    provider: str = "verbs"
    iface: str = ""
    base_port: int = Field(47100, ge=1024, le=65000)
    peer_url: str = Field("", max_length=200)
    peer_user: str = Field("", max_length=64)
    # Blank keeps the saved password
    peer_password: str = Field("", max_length=128)


class AcceptIn(BaseModel):
    key: str
    flow_def: dict


class ResendIn(BaseModel):
    keys: list[str]


def fabric_brief():
    snap = fabric.snapshot()
    runner = snap["runner"]
    return {"enabled": mc.fabric_settings()["enabled"], "state": runner.get("state"),
            "age": runner.get("age"), "provider": runner.get("provider"), "node": runner.get("node"),
            "sessions": {k: {x: v.get(x) for x in ("role", "key", "state", "rate", "starts")}
                         for k, v in (runner.get("sessions") or {}).items()},
            "peer": {"ok": snap["peer"]["ok"], "error": snap["peer"]["error"], "info": snap["peer"]["info"]}}


@app.get("/api/fabric")
def get_fabric():
    cfg = mc.fabric_settings()
    cfg["peer_password"] = ""
    cfg["has_password"] = bool(mc.fabric_settings()["peer_password"])
    return {"settings": cfg, **fabric.snapshot(), "hello": fabric.hello(), **iface_choices()}


@app.post("/api/fabric/apply")
def apply_fabric(body: FabricIn):
    if body.provider not in ("verbs", "tcp"):
        raise HTTPException(400, "provider must be verbs or tcp")
    url = body.peer_url.strip()
    if url and not PEER_URL_RE.fullmatch(url):
        raise HTTPException(400, "peer address must look like http://192.168.10.12:8080")
    iface = body.iface.strip()
    check_iface(iface)
    if body.enabled and not pc.iface_ipv4(pc.resolve_iface(iface)):
        raise HTTPException(400, "that interface has no IPv4 address. Give it one on the fabric network first.")
    data = body.model_dump()
    data.update(iface=iface, peer_url=url.rstrip("/"))
    if not data["peer_password"]:
        data["peer_password"] = mc.fabric_settings()["peer_password"]

    def first(say):
        fabric.clear(say)
        write_json_atomic(mc.FABRIC_CFG, data)
        say("Saved fabric settings")
        c = container("fabric")
        if c.status == "running":
            c.restart(timeout=5)
        else:
            c.start()
        say("Restarted the fabric function")
        if not data["enabled"]:
            say("The fabric is off")
            return
        if url:
            peer = fabric.check_peer()
            say(f"Peer {peer['info']['host']} answers" if peer["ok"] else f"Peer: {peer['error']}")
        say("Flows pair within a few seconds")

    job.start(None, first, kind="fabric")
    return {"ok": True}


@app.post("/api/fabric/test")
def test_fabric():
    return fabric.check_peer()


@app.post("/api/fabric/repair")
def repair_fabric():
    """Pair everything again: our sends now, and ask the peer for what we receive."""
    def work():
        fabric.clear()
        r = mc.routing_settings()
        for key in r["send"]:
            fabric.send(key)
        if mc.peer_flows(r):
            fabric.request(mc.peer_flows(r))
    threading.Thread(target=work, daemon=True).start()
    return {"ok": True}


# Called by the other lab's GUI
@app.get("/api/fabric/hello")
def fabric_hello():
    return fabric.hello()


@app.post("/api/fabric/accept")
def fabric_accept(body: AcceptIn):
    if body.key not in mc.FLOW_IDS:
        raise HTTPException(400, "unknown flow")
    return {"target_info": fabric.accept(body.key, body.flow_def)}


@app.post("/api/fabric/resend")
def fabric_resend(body: ResendIn):
    return {"ok": True, "sending": fabric.resend([k for k in body.keys if k in mc.FLOW_IDS])}


@app.get("/api/tuning")
def get_tuning():
    values = dict(TUNING_DEFAULTS)
    values.update(load_json(TUNING_CFG))
    return {"values": values, "defaults": TUNING_DEFAULTS}


@app.put("/api/tuning")
def put_tuning(body: TuningIn):
    if body.mode not in ("weighted", "largest", "manual", "hold"):
        raise HTTPException(400, "mode must be weighted, largest, manual or hold")
    data = body.model_dump()
    data["top_k"] = int(data["top_k"])
    write_json_atomic(TUNING_CFG, data)
    return {"ok": True, "values": data}


@app.post("/api/tuning/reset")
def reset_tuning():
    write_json_atomic(TUNING_CFG, TUNING_DEFAULTS)
    return {"ok": True, "values": TUNING_DEFAULTS}


@app.get("/api/status")
def status():
    af = load_json(STATUS_IN)
    if af.get("ts"):
        af["age"] = round(time.time() - af["ts"], 1)
    try:
        dock()
        containers, docker_error = container_status(), None
    except DOCKER_ERRORS as exc:
        containers = {svc: {"status": "unknown"} for svc in SERVICES}
        docker_error = f"{exc}. Is /var/run/docker.sock mounted into the GUI container?"
    return {"containers": containers, "docker_error": docker_error,
            "autoframe": af, "flows": flows.status(), "job": job.snapshot(),
            "ptp": ptpw.snapshot(), "ptp_mode": pc.ptp_settings()["mode"],
            "audio": audio_status(), "routing": mc.routing_settings(),
            "st2110": st2110_status(), "fabric": fabric_brief()}


_preview_cache = {"mtime": None, "meta": None, "jpg": None}


def read_preview():
    """Latest preview frame as (meta, jpeg bytes), or None. Re-read only when the file changes."""
    try:
        mtime = os.stat(PREVIEW_IN).st_mtime_ns
    except OSError:
        return None
    if mtime != _preview_cache["mtime"]:
        try:
            with open(PREVIEW_IN, "rb") as f:
                data = f.read()
            n = struct.unpack(">I", data[:4])[0]
            _preview_cache.update(mtime=mtime, meta=json.loads(data[4:4 + n]), jpg=data[4 + n:])
        except (OSError, ValueError, struct.error):
            return None
    return _preview_cache["meta"], _preview_cache["jpg"]


@app.get("/api/preview")
async def preview(after: int = -1):
    """
    Next preview frame. Waits up to a second for a frame other than `after`,
    so the page receives each frame as soon as it exists. The crop position
    for that exact frame is in the X-Preview header.
    """
    deadline = time.time() + 1.0
    while True:
        latest = read_preview()
        if latest is not None:
            meta, jpg = latest
            if meta.get("seq") != after and time.time() - meta.get("ts", 0) < 5:
                return Response(jpg, media_type="image/jpeg",
                                headers={"Cache-Control": "no-store",
                                         "X-Preview": json.dumps(meta)})
        if time.time() > deadline:
            return Response(status_code=204, headers={"Cache-Control": "no-store"})
        await asyncio.sleep(0.02)


@app.post("/api/containers/{service}/restart")
def restart(service: str):
    container(service).restart(timeout=5)
    return {"ok": True}


@app.get("/api/containers/{service}/logs")
def logs(service: str, tail: int = 150):
    tail = max(10, min(tail, 2000))
    text = container(service).logs(tail=tail, timestamps=False).decode("utf-8", "replace")
    return JSONResponse({"service": service, "text": text})
