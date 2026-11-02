# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Rankin
"""
Pairing MXL flows with the other lab over MXL fabrics.

Either lab can send any flow it makes and receive any flow it does not
(routing.json says which). For each flow on the link:

  1. The sending lab's GUI posts the flow definition to the receiving lab's
     /api/fabric/accept.
  2. The receiving GUI stops whatever reads that flow, has the fabric runner
     start a target (which creates the flow), and answers with the target
     info: fabric address and memory keys.
  3. The sending GUI hands the target info to its fabric runner, which starts
     an initiator that writes each grain into the target's memory.

A watchdog keeps it that way. A sender pairs again whenever its local flow is
recreated; a receiver asks the sender to pair again (/api/fabric/resend)
whenever it has no working target for a flow it expects. So a restart on
either side heals by itself within a few seconds.
"""
import base64
import collections
import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid

from fastapi import HTTPException

from .docker_ctl import container
from .settings import DOMAIN, PIPELINE_CFG, mc, pc, pipeline_settings, write_json_atomic

# What reads each flow on this VM, so a new target can be put under them
CONSUMERS = {
    "A": ("autoframe", "ndi-out-original", "st2110-out"),
    "B": ("ndi-out-vertical", "st2110-out"),
    "audio": ("aes67-out",),
}
RETRY_S = 10          # between pairing attempts for one flow
TARGET_WAIT_S = 20    # for the runner to bring up a target


def spec_path(role, key):
    return os.path.join(mc.FABRIC_DIR, f"{role}-{key}.json")


def flow_inode(key):
    try:
        return os.stat(os.path.join(DOMAIN, f"{mc.FLOW_IDS[key]}.mxl-flow")).st_ino
    except OSError:
        return None


class FabricLink:
    def __init__(self, job):
        self.job = job
        self.lock = threading.Lock()
        self.log = collections.deque(maxlen=60)
        self.paired = {}        # sender side: key -> inode of the local flow last paired
        self.tried = {}         # (role, key) -> time of the last attempt
        self.peer = {"ok": None, "error": None, "info": None, "checked": 0.0}
        self.last_msg = None
        threading.Thread(target=self.watchdog, daemon=True).start()

    def say(self, msg, sink=None):
        # The watchdog retries every few seconds; keep one line per repeat
        if msg != self.last_msg:
            self.log.append(f"{time.strftime('%H:%M:%S')}  {msg}")
            self.last_msg = msg
        if sink is not None:
            sink(msg)

    # ------------------------------------------------------------ peer
    def call(self, method, path, body=None, timeout=30):
        cfg = mc.fabric_settings()
        base = cfg["peer_url"].rstrip("/")
        if not base:
            raise RuntimeError("no peer address set")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        if cfg["peer_user"] and cfg["peer_password"]:
            token = base64.b64encode(f"{cfg['peer_user']}:{cfg['peer_password']}".encode()).decode()
            req.add_header("Authorization", "Basic " + token)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                return json.loads(res.read() or b"{}")
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read()).get("detail")
            except ValueError:
                detail = None
            raise RuntimeError(f"peer answered {exc.code}: {detail or exc.reason}") from exc
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise RuntimeError(f"cannot reach the peer at {base}: {getattr(exc, 'reason', exc)}") from exc

    def hello(self):
        """What this lab tells its peer about itself."""
        r = mc.routing_settings()
        f = mc.fabric_settings()
        iface = pc.resolve_iface(f["iface"])
        return {"host": socket.gethostname(), "enabled": f["enabled"], "provider": f["provider"],
                "node": pc.iface_ipv4(iface), "sends": r["send"], "expects": mc.peer_flows(r)}

    def check_peer(self):
        try:
            info = self.call("GET", "/api/fabric/hello", timeout=5)
            self.peer = {"ok": True, "error": None, "info": info, "checked": time.time()}
        except RuntimeError as exc:
            self.peer = {"ok": False, "error": str(exc), "info": None, "checked": time.time()}
        return self.peer

    # ------------------------------------------------------- spec files
    def write_spec(self, role, key, data):
        os.makedirs(mc.FABRIC_DIR, exist_ok=True)
        nonce = uuid.uuid4().hex[:12]
        write_json_atomic(spec_path(role, key), dict(data, nonce=nonce))
        return nonce

    def drop(self, role, key):
        try:
            os.unlink(spec_path(role, key))
            return True
        except FileNotFoundError:
            return False

    def clear(self, say=None):
        """Drop every session. The runner stops the processes within a second."""
        n = sum(self.drop(role, key) for role in ("rx", "tx") for key in mc.FLOW_IDS)
        self.paired.clear()
        if n:
            self.say(f"Closed {n} fabric session{'s' if n != 1 else ''}", say)
            time.sleep(1.5)

    def runner_status(self):
        st = pc.load_json(mc.FABRIC_STATUS)
        if st.get("ts"):
            st["age"] = round(time.time() - st["ts"], 1)
        return st

    def session(self, role, key):
        return (self.runner_status().get("sessions") or {}).get(f"{role}-{key}")

    # ----------------------------------------------------- receiver side
    def accept(self, key, flow_def):
        """The peer wants to send us a flow. Returns our target info."""
        f = mc.fabric_settings()
        if not f["enabled"]:
            raise HTTPException(409, "the fabric is turned off on this lab")
        r = mc.routing_settings()
        if key not in mc.peer_flows(r):
            raise HTTPException(409, f"this lab does not expect {mc.FLOW_NAMES[key]} from its peer. "
                                     "Check the routing on both labs.")
        if str(flow_def.get("id")) != mc.FLOW_IDS[key]:
            raise HTTPException(400, f"flow id {flow_def.get('id')} does not match {mc.FLOW_NAMES[key]} "
                                     f"({mc.FLOW_IDS[key]}). Both labs need the same FLOW_ ids in .env.")
        with self.lock:
            running = []
            if not self.job.snapshot()["running"]:
                for svc in CONSUMERS[key]:
                    c = container(svc)
                    if c.status == "running":
                        c.stop(timeout=5)
                        running.append(svc)
            if key in ("A", "B"):
                fmt = mc.flow_format(flow_def)
                settings = pipeline_settings()
                if key == "A":
                    settings.update(SRC_W=fmt["width"], SRC_H=fmt["height"], SRC_FPS=fmt["fps"], SRC_AUTO=False)
                else:
                    settings.update(OUT_W=fmt["width"], OUT_H=fmt["height"])
                write_json_atomic(PIPELINE_CFG, settings)
            nonce = self.write_spec("rx", key, {"flow_def": flow_def})
            self.say(f"Peer is sending {mc.FLOW_NAMES[key]}, starting a target")
            end, info = time.time() + TARGET_WAIT_S, None
            while time.time() < end:
                s = self.session("rx", key)
                if s and s.get("nonce") == nonce:
                    if s["state"] == "exited":
                        tail = (s.get("log") or ["no output"])[-1]
                        raise HTTPException(500, f"the target exited: {tail}")
                    if s["state"] == "running" and os.path.exists(s.get("target_file", "")):
                        with open(s["target_file"]) as fh:
                            info = fh.read()
                        break
                time.sleep(0.3)
            if info is None:
                raise HTTPException(504, "the fabric function did not start a target in time. "
                                         "Check that it is running and the fabric log.")
            self.tried[("rx", key)] = time.time()
            for svc in running:
                container(svc).start()
            if running:
                self.say(f"Restarted {', '.join(running)} on the new {mc.FLOW_NAMES[key]}")
        return info

    def request(self, keys, say=None):
        """Ask the peer to (re)send flows we expect."""
        try:
            self.call("POST", "/api/fabric/resend", {"keys": list(keys)}, timeout=10)
            self.say(f"Asked the peer to send {', '.join(mc.FLOW_NAMES[k] for k in keys)}", say)
            return True
        except RuntimeError as exc:
            self.say(f"Could not ask the peer for {', '.join(mc.FLOW_NAMES[k] for k in keys)}: {exc}", say)
            return False

    # ------------------------------------------------------- sender side
    def send(self, key, say=None):
        """Pair one local flow with the peer."""
        self.tried[("tx", key)] = time.time()
        inode = flow_inode(key)
        try:
            with open(mc.flow_def_path(DOMAIN, mc.FLOW_IDS[key])) as fh:
                flow_def = json.load(fh)
        except (OSError, ValueError):
            self.say(f"{mc.FLOW_NAMES[key]} is not here yet, cannot send it", say)
            return False
        self.drop("tx", key)
        try:
            res = self.call("POST", "/api/fabric/accept", {"key": key, "flow_def": flow_def})
        except RuntimeError as exc:
            self.say(f"Pairing {mc.FLOW_NAMES[key]} failed: {exc}", say)
            return False
        self.write_spec("tx", key, {"target_info": res["target_info"]})
        self.paired[key] = inode
        self.say(f"Paired {mc.FLOW_NAMES[key]} with the peer, sending", say)
        return True

    def sync(self, say=None):
        """Pair every flow routing.json says to send. Used by the apply job."""
        r = mc.routing_settings()
        if not mc.fabric_settings()["enabled"]:
            if r["send"] or mc.peer_flows(r):
                self.say("The fabric is off, so no flows go to or come from the peer", say)
            return
        for key in r["send"]:
            self.send(key, say)

    def resend(self, keys):
        """The peer asks us to pair again. Answer at once, pair in the background."""
        r = mc.routing_settings()
        wanted = [k for k in keys if k in r["send"]]

        def work():
            for key in wanted:
                if flow_inode(key) is not None:
                    self.send(key)
        threading.Thread(target=work, daemon=True).start()
        return wanted

    # --------------------------------------------------------- watchdog
    def watchdog(self):
        while True:
            time.sleep(3)
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001  the watchdog must keep running
                self.say(f"fabric watchdog: {exc}")

    def tick(self):
        if time.time() - self.peer["checked"] > 15 and mc.fabric_settings()["peer_url"]:
            self.check_peer()
        if self.job.snapshot()["running"] or self.lock.locked():
            return
        f = mc.fabric_settings()
        r = mc.routing_settings()
        if not f["enabled"]:
            return
        now = time.time()
        for key in mc.FLOW_IDS:
            if key not in r["send"] and self.drop("tx", key):
                self.paired.pop(key, None)
                self.say(f"Stopped sending {mc.FLOW_NAMES[key]}")
            if key not in mc.peer_flows(r) and self.drop("rx", key):
                self.say(f"Stopped receiving {mc.FLOW_NAMES[key]}")
        for key in r["send"]:
            inode = flow_inode(key)
            if inode is None or now - self.tried.get(("tx", key), 0) < RETRY_S:
                continue
            if self.paired.get(key) != inode or not os.path.exists(spec_path("tx", key)):
                self.send(key)
        missing = []
        for key in mc.peer_flows(r):
            s = self.session("rx", key)
            healthy = s is not None and s["state"] in ("running", "starting")
            if not healthy and now - self.tried.get(("rx", key), 0) >= RETRY_S:
                missing.append(key)
        if missing:
            for key in missing:
                self.tried[("rx", key)] = now
            self.request(missing)

    def snapshot(self):
        return {"runner": self.runner_status(), "peer": self.peer, "log": list(self.log)}
