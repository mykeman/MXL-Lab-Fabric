#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Rankin
"""
ST 2110-20 output function.

Reads Flow A and/or Flow B and sends each as ST 2110-20 (RFC 4175, 4:2:2
10 bit) RTP multicast, with an SDP file per stream.

Timing works as in aes67-out: the pipeline runs on the system clock in TAI
mode with a base time of zero, so running time is TAI time and mxlsrc stamps
each frame with its grain time. rtpvrawpay then puts the grain time on the
90 kHz media clock into the RTP timestamp (a=mediaclk:direct=0 against the
PTP epoch), and udpsink sends each frame a fixed read delay after it.

Packets of a frame leave as a burst, which is not ST 2110-21 compliant.
Software receivers do not mind; strict hardware receivers may. With "pace"
on, the fq qdisc spreads each stream at a little over its own rate.

Settings come from /config/st2110.json and /config/ptp.json. Live status goes
to /preview/st2110-status.json, the SDPs to /config/st2110-A.sdp and -B.sdp.
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time
import zlib

import gi

gi.require_version("Gst", "1.0")
from gi.repository import GLib, Gst  # noqa: E402

sys.path.insert(0, "/opt/scripts")
import media_common as mc  # noqa: E402
import ptp_common as pc  # noqa: E402

DOMAIN = os.environ.get("MXL_DOMAIN", "/dev/shm/mxl")
# RTP, UDP and IPv4 headers, and Ethernet framing, on top of the pixels
OVERHEAD = 1.06


def log(msg):
    print(f"st2110-out: {msg}", flush=True)


def sdp_path(key):
    return os.path.join(pc.CONFIG_DIR, f"st2110-{key}.sdp")


class Sender:
    def __init__(self):
        self.cfg = mc.st2110_settings()
        self.ptp = pc.ptp_settings()
        self.iface = pc.resolve_iface(self.cfg["out_iface"])
        self.mac = pc.iface_mac(self.iface)
        self.src_ip = ""
        self.keys = [k for k in ("A", "B") if self.cfg["out"][k]["enabled"]]
        self.formats = {}
        self.sdps = {}
        self.sdp_version = int(time.time())
        self.gm_sdp = ""
        self.lock = threading.Lock()
        self.state = "starting"
        self.error = None
        self.kbps = {}
        self.bytes_prev = {}
        self.paced = False
        self.pipe = None
        self.loop = GLib.MainLoop()
        self.exit_code = 0
        self.stop_evt = threading.Event()

    # ------------------------------------------------------------ status
    def write_status(self):
        with self.lock:
            outs = {}
            for k in self.keys:
                o = self.cfg["out"][k]
                f = self.formats.get(k, {})
                outs[k] = {"dest": f"{o['address']}:{o['port']}", "kbps": round(self.kbps.get(k, 0.0), 1),
                           "format": f"{f.get('width')}x{f.get('height')} @ {f.get('fps')}" if f else None}
            data = {"ts": time.time(), "state": self.state, "error": self.error,
                    "iface": self.iface, "src_ip": self.src_ip, "paced": self.paced,
                    "read_delay_ms": self.cfg["read_delay_ms"], "gm": self.gm_sdp, "outputs": outs}
        try:
            pc.write_json_atomic(mc.ST2110_STATUS, data)
        except OSError:
            pass

    def status_loop(self):
        while not self.stop_evt.wait(1.0):
            if self.pipe is not None:
                now = time.time()
                for k in self.keys:
                    sink = self.pipe.get_by_name(f"out_{k}")
                    try:
                        served = sink.get_property("bytes-served")
                    except TypeError:
                        continue
                    b0, t0 = self.bytes_prev.get(k, (served, now))
                    if now > t0:
                        self.kbps[k] = (served - b0) * 8 / 1000 / (now - t0)
                    self.bytes_prev[k] = (served, now)
            self.write_status()

    # --------------------------------------------------------------- SDP
    def refresh_sdps(self, reason=None):
        refclk = mc.refclk_line(self.ptp, self.gm_sdp, self.mac)
        changed = False
        for k in self.keys:
            o, f = self.cfg["out"][k], self.formats[k]
            name = o["session_name"] or f"MXL Lab {mc.FLOW_NAMES[k]}"
            sdp = mc.video_sdp(
                session_id=zlib.crc32(f"{o['address']}:{o['port']}".encode()), version=self.sdp_version,
                src_ip=self.src_ip, name=name, address=o["address"], port=int(o["port"]),
                pt=int(o["payload_type"]), ttl=int(self.cfg["ttl"]),
                width=f["width"], height=f["height"], fps=f["fps"], refclk=refclk)
            if sdp == self.sdps.get(k):
                continue
            changed = True
            self.sdps[k] = sdp
            try:
                with open(sdp_path(k) + ".tmp", "w") as fh:
                    fh.write(sdp)
                os.replace(sdp_path(k) + ".tmp", sdp_path(k))
            except OSError as exc:
                log(f"could not write {sdp_path(k)}: {exc}")
        if changed:
            log("SDP " + ("updated: " + reason if reason else "ready") + " for " + ", ".join(self.keys))
            self.sdp_version += 1

    def ptp_loop(self):
        while not self.stop_evt.wait(2):
            if self.ptp["mode"] == "off":
                continue
            data, err = pc.pmc_query(self.ptp["domain"], ids=("PARENT_DATA_SET", "TIME_STATUS_NP"))
            gm = "" if err else pc.summarise(data)["gm_sdp"]
            if gm != self.gm_sdp:
                self.gm_sdp = gm
                self.refresh_sdps(f"grandmaster {gm or 'not known'}")

    # ------------------------------------------------------------ pacing
    def rate_bits(self, k):
        f = self.formats[k]
        num, _, den = str(f["fps"]).partition("/")
        fps = int(num) / int(den or 1)
        return int(f["width"] * f["height"] * 2.5 * 8 * fps * OVERHEAD)

    def pace(self, on):
        """fq with a per-flow maxrate paces each udpsink socket. Needs NET_ADMIN."""
        if on:
            rate = max(self.rate_bits(k) for k in self.keys) * 1.05
            cmd = ["tc", "qdisc", "replace", "dev", self.iface, "root", "fq",
                   "maxrate", f"{int(rate)}bit", "flow_limit", "20000", "limit", "200000"]
        else:
            cmd = ["tc", "qdisc", "del", "dev", self.iface, "root"]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError) as exc:
            log(f"tc failed: {exc}")
            return False
        if res.returncode != 0 and on:
            log(f"could not set up pacing: {res.stderr.strip()}")
            return False
        if on:
            log(f"pacing each stream at up to {rate / 1e9:.2f} Gb/s with fq on {self.iface}")
        return on

    def left_paced(self):
        """True when the root qdisc is an fq with a maxrate, as pace() sets it."""
        try:
            out = subprocess.run(["tc", "qdisc", "show", "dev", self.iface, "root"],
                                 capture_output=True, text=True, timeout=5).stdout
        except (OSError, subprocess.SubprocessError):
            return False
        return out.startswith("qdisc fq ") and "maxrate" in out

    # ---------------------------------------------------------- pipeline
    def wait_for_flows(self):
        for k in self.keys:
            path = mc.flow_def_path(DOMAIN, mc.FLOW_IDS[k])
            waited = 0
            while not os.path.exists(path):
                if waited % 15 == 0:
                    log(f"waiting for {mc.FLOW_NAMES[k]}")
                with self.lock:
                    self.state = "waiting"
                self.write_status()
                time.sleep(1)
                waited += 1
            with open(path) as fh:
                self.formats[k] = mc.flow_format(json.load(fh))

    def build(self):
        c = self.cfg
        delay_ns = int(float(c["read_delay_ms"]) * 1_000_000)
        branches = []
        for k in self.keys:
            o = c["out"][k]
            branches.append(
                f"mxlsrc video-flow-id={mc.FLOW_IDS[k]} domain={DOMAIN} "
                "! queue max-size-buffers=3 leaky=downstream "
                "! videoconvert ! video/x-raw,format=UYVP "
                # timestamp-offset 0: the RTP timestamp is the grain time on the 90 kHz clock
                f"! rtpvrawpay name=pay_{k} pt={int(o['payload_type'])} mtu={int(c['mtu'])} timestamp-offset=0 "
                f"! udpsink name=out_{k} host={o['address']} port={int(o['port'])} "
                f"multicast-iface={self.iface} ttl-mc={int(c['ttl'])} qos-dscp={int(c['dscp'])} "
                "auto-multicast=true loop=false sync=true async=false buffer-size=33554432 "
                f"processing-deadline=0 ts-offset={delay_ns}"
            )
        pipe = Gst.parse_launch(" ".join(branches))
        # Running time == TAI time: TAI system clock and a base time of zero.
        clock = Gst.SystemClock.obtain()
        clock.set_property("clock-type", Gst.ClockType.TAI)
        pipe.use_clock(clock)
        pipe.set_start_time(Gst.CLOCK_TIME_NONE)
        pipe.set_base_time(0)
        bus = pipe.get_bus()
        bus.add_signal_watch()
        bus.connect("message", self.on_message)
        self.pipe = pipe

    def on_message(self, _bus, msg):
        t = msg.type
        if t == Gst.MessageType.ERROR:
            err, dbg = msg.parse_error()
            log(f"error: {err.message}")
            if dbg:
                log(dbg.splitlines()[0])
            with self.lock:
                self.state, self.error = "error", err.message
            self.write_status()
            self.exit_code = 1
            self.loop.quit()
        elif t == Gst.MessageType.LATENCY:
            self.pipe.recalculate_latency()
        elif t == Gst.MessageType.STATE_CHANGED and msg.src == self.pipe:
            _, new, _ = msg.parse_state_changed()
            if new == Gst.State.PLAYING:
                with self.lock:
                    self.state = "sending"

    # --------------------------------------------------------------- run
    def on_signal(self):
        log("stopping")
        self.loop.quit()
        return GLib.SOURCE_REMOVE

    def run(self):
        signal.signal(signal.SIGTERM, exit_on_signal)
        signal.signal(signal.SIGINT, exit_on_signal)
        if not self.keys:
            with self.lock:
                self.state = "off"
            self.write_status()
            log("ST 2110-20 output is off. Waiting until it is turned on in the GUI.")
            while True:
                time.sleep(3600)
        for _ in range(30):
            self.src_ip = pc.iface_ipv4(self.iface)
            if self.src_ip:
                break
            log(f"no IPv4 address on {self.iface or 'any interface'} yet")
            time.sleep(2)
        if not self.src_ip:
            return 1
        threading.Thread(target=self.status_loop, daemon=True).start()
        self.wait_for_flows()
        if self.ptp["mode"] != "off":
            data, err = pc.pmc_query(self.ptp["domain"], ids=("PARENT_DATA_SET", "TIME_STATUS_NP"))
            self.gm_sdp = "" if err else pc.summarise(data)["gm_sdp"]
        else:
            log("PTP is off. RTP timestamps follow this VM's free-running clock.")
        self.refresh_sdps()
        for k in self.keys:
            o, f = self.cfg["out"][k], self.formats[k]
            log(f"{mc.FLOW_NAMES[k]} {f['width']}x{f['height']} @ {f['fps']} to {o['address']}:{o['port']}, "
                f"about {self.rate_bits(k) / 1e9:.2f} Gb/s")
        if self.cfg["pace"]:
            self.paced = self.pace(True)
        elif self.left_paced():
            log("removing the fq pacing left from an earlier run")
            self.pace(False)
        try:
            os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(50))
        except OSError as exc:
            log(f"could not get real-time priority ({exc})")
        Gst.init(None)
        try:
            self.build()
        except GLib.Error as exc:
            log(f"could not build the pipeline: {exc.message}")
            with self.lock:
                self.state, self.error = "error", exc.message
            self.write_status()
            return 1
        threading.Thread(target=self.ptp_loop, daemon=True).start()
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, self.on_signal)
        GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, self.on_signal)
        self.pipe.set_state(Gst.State.PLAYING)
        self.loop.run()
        self.stop_evt.set()
        self.pipe.set_state(Gst.State.NULL)
        if self.paced:
            self.pace(False)
        with self.lock:
            self.state = "stopped" if self.exit_code == 0 else "error"
        self.write_status()
        return self.exit_code


def exit_on_signal(signum, _frame):
    log("stopping")
    raise SystemExit(0)


if __name__ == "__main__":
    sys.exit(Sender().run())
