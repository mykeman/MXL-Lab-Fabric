# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Rankin
"""Ordered changes to the running lab: one at a time, in the background, with a log for the page."""
import os
import re
import shutil
import threading
import time
import traceback
from datetime import UTC, datetime

from fastapi import HTTPException

from .docker_ctl import container
from .settings import (
    DOMAIN,
    FLOW_A,
    FLOW_AUDIO,
    FLOW_B,
    PIPELINE_CFG,
    mc,
    media_configured,
    pc,
    pipeline_settings,
    write_json_atomic,
)


class ApplyJob:
    """
    Runs one change at a time in the background and keeps a log for the page.

    A change is an optional first step (write settings, restart PTP), then
    either a full ordered restart of the media functions or, when there is
    nothing to start yet, just the AES67 or ST 2110 output.

    The restart follows routing.json: Flow A and the audio flow come from the
    NDI or ST 2110 ingest, or from the peer over the fabric; Flow B from
    autoframe or the peer. Flows sent to the peer are paired once they exist.
    """

    MEDIA_STOP = ("aes67-out", "st2110-out", "ndi-out-vertical", "ndi-out-original", "autoframe", "ndi-ingest")
    PEER_WAIT_S = 40

    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.log = []
        self.result = None
        self.kind = "pipeline"
        self.fabric = None      # FabricLink, set by app.py

    def _say(self, msg):
        self.log.append(f"{time.strftime('%H:%M:%S')}  {msg}")

    def _wait_flow(self, flow_id, timeout=15):
        """Whether the flow is in the domain, waiting up to timeout seconds. 0 checks once."""
        path = os.path.join(DOMAIN, f"{flow_id}.mxl-flow")
        end = time.time() + timeout
        while True:
            if os.path.isdir(path):
                return True
            if time.time() >= end:
                return False
            time.sleep(0.5)

    def start(self, settings, first=None, kind="pipeline", audio=False, st2110=False):
        """
        settings None means no media restart. With audio=True the AES67
        output, and with st2110=True the ST 2110 output, is then restarted
        (or stopped) on its own.
        """
        with self.lock:
            if self.running:
                raise HTTPException(409, "a change is already being applied")
            self.running, self.log, self.result, self.kind = True, [], None, kind
        threading.Thread(target=self._run, args=(settings, first, audio, st2110), daemon=True).start()

    def _audio_only(self):
        c = container("aes67-out")
        if c.status == "running":
            c.stop(timeout=5)
            self._say("Stopped aes67-out")
        if pc.audio_settings()["enabled"]:
            c.start()
            self._say("Started aes67-out")
        else:
            self._say("AES67 output is off")

    def _st2110_only(self):
        c = container("st2110-out")
        if c.status == "running":
            c.stop(timeout=5)
            self._say("Stopped st2110-out")
        if any(o["enabled"] for o in mc.st2110_settings()["out"].values()):
            c.start()
            self._say("Started st2110-out")
        else:
            self._say("ST 2110 output is off")

    def _from_peer(self, keys):
        """Ask the peer for flows and wait for them. Returns the keys that arrived."""
        if not mc.fabric_settings()["enabled"]:
            raise RuntimeError("Routing expects flows from the peer, but the fabric is off. "
                               "Turn it on in the Routing tab.")
        self.fabric.request(keys, self._say)
        names = ", ".join(mc.FLOW_NAMES[k] for k in keys)
        self._say(f"Waiting up to {self.PEER_WAIT_S} s for {names} from the peer")
        end, got = time.time() + self.PEER_WAIT_S, []
        while time.time() < end:
            got = [k for k in keys if os.path.isdir(os.path.join(DOMAIN, f"{mc.FLOW_IDS[k]}.mxl-flow"))]
            if len(got) == len(keys):
                break
            time.sleep(0.5)
        return got

    def _restart_media(self, settings):
        write_json_atomic(PIPELINE_CFG, settings)
        self._say("Saved pipeline settings")
        for svc in self.MEDIA_STOP:
            container(svc).stop(timeout=5)
            self._say(f"Stopped {svc}")
        # Fabric sessions hold flows in the domain; close them before clearing it
        self.fabric.clear(self._say)

        removed = 0
        for entry in os.listdir(DOMAIN):
            path = os.path.join(DOMAIN, entry)
            if os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.unlink(path)
            removed += 1
        self._say(f"Cleared MXL domain ({removed} entries)")

        r = mc.routing_settings()
        if r["input"] in ("ndi", "st2110"):
            container("ndi-ingest").start()
            label = "NDI" if r["input"] == "ndi" else "ST 2110"
            self._say(f"Started ndi-ingest ({label}), waiting for Flow A")
            if not self._wait_flow(FLOW_A):
                hint = "the source name" if r["input"] == "ndi" else "the SDP, the interface and multicast"
                raise RuntimeError(f"Flow A did not appear. Check the ndi-ingest log and {hint}.")
            self._say("Flow A is up")
            if self._wait_flow(FLOW_AUDIO, timeout=5):
                self._say("Audio flow is up")
            else:
                self._say("No audio flow yet. The source may not be sending audio. Carrying on.")
        elif r["input"] == "peer":
            got = self._from_peer(["A", "audio"] if r["flow_b"] != "peer" else ["A", "audio", "B"])
            if "A" not in got:
                raise RuntimeError("Flow A did not arrive from the peer. Check the peer's routing "
                                   "sends Flow A, and the fabric log on both labs.")
            self._say("Flow A is up, from the peer")
            if "audio" in got:
                self._say("Audio flow is up, from the peer")
        else:
            self._say("No input on this lab")
        a_up = self._wait_flow(FLOW_A, timeout=0)

        if a_up and r["ndi_out_original"]:
            container("ndi-out-original").start()
            self._say("Started ndi-out-original")

        b_up = False
        if r["flow_b"] == "local":
            if not a_up:
                raise RuntimeError("Autoframe needs Flow A, and there is none. Check the routing.")
            container("autoframe").start()
            self._say("Started autoframe, waiting for Flow B")
            if not self._wait_flow(FLOW_B):
                raise RuntimeError("Flow B did not appear. Check the autoframe log.")
            self._say("Flow B is up")
            b_up = True
        elif r["flow_b"] == "peer":
            b_up = self._wait_flow(FLOW_B, timeout=0) or "B" in self._from_peer(["B"])
            self._say("Flow B is up, from the peer" if b_up else
                      "Flow B did not arrive from the peer yet. It will be picked up when it does.")

        if b_up and r["ndi_out_vertical"]:
            container("ndi-out-vertical").start()
            self._say("Started ndi-out-vertical")
        if r["send"]:
            self.fabric.sync(self._say)
        if any(o["enabled"] for o in mc.st2110_settings()["out"].values()):
            container("st2110-out").start()
            self._say("Started st2110-out")
        if pc.audio_settings()["enabled"]:
            container("aes67-out").start()
            self._say("Started aes67-out")

    def _run(self, settings, first=None, audio=False, st2110=False):
        try:
            if first is not None:
                first(self._say)
            if settings is not None:
                self._restart_media(settings)
            elif audio:
                self._audio_only()
            elif st2110:
                self._st2110_only()
            self.result = "ok"
        except HTTPException as exc:
            self._say(f"Failed: {exc.detail}")
            self.result = "failed"
        except Exception as exc:  # noqa: BLE001  a background job must report every failure
            self._say(f"Failed: {exc}")
            self.result = "failed"
            traceback.print_exc()
        finally:
            with self.lock:
                self.running = False

    def snapshot(self):
        with self.lock:
            return {"running": self.running, "result": self.result,
                    "log": list(self.log), "kind": self.kind}


def restart_after_step(job, size):
    """A clock step outside an apply: restart the media functions in order."""
    if job.snapshot()["running"]:
        return   # an apply is already restarting everything
    settings = pipeline_settings()

    def first(say):
        say(f"The clock stepped by {size:+.3f} s, so the running MXL flows are out of step. "
            "Restarting the media functions.")
    try:
        job.start(settings if media_configured(settings) else None, first, kind="ptp", audio=True)
    except HTTPException:
        pass


def wait_for_ptp(ptpw, say, timeout=90):
    """Log the port state as it changes, until ptp4l is following or grandmaster."""
    end, last = time.time() + timeout, None
    while time.time() < end:
        snap = ptpw.snapshot()
        state = snap["summary"].get("port_state") or ("no answer" if snap["error"] else "starting")
        if state != last:
            say(f"PTP port state: {state}")
            last = state
        if ptpw.locked():
            s = snap["summary"]
            if s.get("gm_is_self"):
                say("This VM is the grandmaster")
            else:
                off = s.get("offset_ns")
                say(f"Following {s.get('gm_identity') or 'a grandmaster'}"
                    + (f", offset {off / 1000:.1f} us" if off is not None else ""))
            return True
        time.sleep(1)
    say(f"PTP did not settle within {timeout} s. Starting the media functions anyway.")
    return False



STARTUP_WINDOW_S = 180   # a PTP container younger than this means a boot or a restart


def docker_time(text):
    """Docker's StartedAt (RFC 3339 with nanoseconds) as a Unix time, or None."""
    m = re.match(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?Z?$", text or "")
    if not m:
        return None
    base = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UTC)
    return base.timestamp() + float(m.group(2) or 0)


def startup_sequence(job, ptpw):
    """
    After a VM boot every container starts at once, before PTP has stepped the
    clock and set the TAI offset. When the PTP function has only just started,
    wait for it and then restart the media functions in order.
    """
    time.sleep(3)
    if pc.ptp_settings()["mode"] == "off":
        return
    try:
        started = container("ptp").attrs.get("State", {}).get("StartedAt", "")
    except HTTPException:
        return
    started_at = docker_time(started)
    if started_at is None or time.time() - started_at > STARTUP_WINDOW_S:
        return
    settings = pipeline_settings()

    def first(say):
        say("PTP function started recently (VM boot or restart). Waiting for it before starting media.")
        wait_for_ptp(ptpw, say)
    try:
        job.start(settings if media_configured(settings) else None, first, kind="ptp", audio=True)
    except HTTPException:
        pass

