#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Rankin
"""
Fabric function: moves MXL flows to and from the other lab with MXL fabrics
(libfabric; the verbs provider is RoCEv2 on a ConnectX card).

Each flow on the link is one mxl-fabrics-demo process:

  receive  a target. It creates the flow in the local MXL domain from the
           sender's flow definition, and writes its target info (fabric
           address and memory keys) for the GUI to hand back to the sender.
  send     an initiator. It reads the local flow and writes each grain
           straight into the target's memory with RDMA remote write.

The GUI decides what should run and writes one small file per session in
/config/fabric/ (rx-A.json, tx-B.json, ...). This runner starts, replaces and
stops processes to match, and reports to /preview/fabric-status.json.

A sender's process is restarted if it exits. A receiver's is not: a new
target has new memory keys, so the GUI asks the sender to pair again.
"""
import collections
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

sys.path.insert(0, "/opt/scripts")
import media_common as mc  # noqa: E402
import ptp_common as pc  # noqa: E402

DOMAIN = os.environ.get("MXL_DOMAIN", "/dev/shm/mxl")
RUN_DIR = os.path.join(mc.FABRIC_DIR, "run")
DEMO = "mxl-fabrics-demo"
SPEC_RE = re.compile(r"^(rx|tx)-(A|B|audio)\.json$")
RATE_RE = re.compile(r"\|\s*([\d.]+)\s+(grains|samples|batches|\w+)/s")
TX_RESTART_S = 3


def log(msg):
    print(f"fabric: {msg}", flush=True)


class Session:
    def __init__(self, role, key, spec):
        self.role, self.key, self.spec = role, key, spec
        self.nonce = spec.get("nonce", "")
        self.proc = None
        self.lines = collections.deque(maxlen=40)
        self.started = 0.0
        self.starts = 0
        self.exit_code = None
        self.rate = None
        self.target_file = ""
        self.flow_inode = None

    @property
    def name(self):
        return f"{self.role}-{self.key}"

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def _read(self):
        for raw in self.proc.stdout:
            line = raw.rstrip()
            self.lines.append(line)
            m = RATE_RE.search(line)
            if m:
                self.rate = float(m.group(1))
            print(f"[{self.name}] {line}", flush=True)

    def start(self, cfg, node):
        os.makedirs(RUN_DIR, exist_ok=True)
        flow_id = mc.FLOW_IDS[self.key]
        cmd = [DEMO, "-d", DOMAIN, "-p", cfg["provider"], "-n", node]
        if self.role == "rx":
            flow_file = os.path.join(RUN_DIR, f"{self.name}.flow.json")
            with open(flow_file, "w") as f:
                json.dump(self.spec["flow_def"], f)
            # A flow left by an earlier target may have another format, and
            # the new target must own the ring it registers with the NIC.
            shutil.rmtree(os.path.join(DOMAIN, f"{flow_id}.mxl-flow"), ignore_errors=True)
            self.target_file = os.path.join(RUN_DIR, f"{self.name}-{self.nonce}.target.json")
            for old in os.listdir(RUN_DIR):
                if old.startswith(f"{self.name}-") and old.endswith(".target.json"):
                    os.unlink(os.path.join(RUN_DIR, old))
            port = int(cfg["base_port"]) + mc.FABRIC_PORTS[self.key]
            cmd += ["-s", str(port), "-f", flow_file, "-t", self.target_file]
        else:
            info_file = os.path.join(RUN_DIR, f"{self.name}.target.json")
            with open(info_file, "w") as f:
                f.write(self.spec["target_info"])
            cmd += ["-i", "-f", flow_id, "-t", "@" + info_file]
            try:
                self.flow_inode = os.stat(os.path.join(DOMAIN, f"{flow_id}.mxl-flow")).st_ino
            except OSError:
                self.flow_inode = None
        env = dict(os.environ, MXL_LOG_LEVEL=os.environ.get("MXL_LOG_LEVEL", "info"))
        log(f"starting {self.name}: {' '.join(cmd)}")
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, bufsize=1, env=env)
        self.started, self.exit_code, self.rate = time.time(), None, None
        self.starts += 1
        threading.Thread(target=self._read, daemon=True).start()

    def stop(self):
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=4)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        self.exit_code = self.proc.returncode
        log(f"stopped {self.name}")

    def flow_replaced(self):
        """A sender's flow was deleted or recreated under it, so its reader is stale."""
        if self.role != "tx" or self.flow_inode is None:
            return False
        try:
            return os.stat(os.path.join(DOMAIN, f"{mc.FLOW_IDS[self.key]}.mxl-flow")).st_ino != self.flow_inode
        except OSError:
            return True

    def snapshot(self):
        state = "running" if self.running() else ("exited" if self.proc is not None else "starting")
        if state == "running" and self.role == "rx" and not os.path.exists(self.target_file):
            state = "starting"
        return {"role": self.role, "key": self.key, "nonce": self.nonce, "state": state,
                "pid": self.proc.pid if self.proc is not None else None,
                "started": self.started, "starts": self.starts, "exit_code": self.exit_code,
                "rate": self.rate, "target_file": self.target_file if self.role == "rx" else "",
                "log": list(self.lines)[-12:]}


def read_specs():
    want = {}
    try:
        names = os.listdir(mc.FABRIC_DIR)
    except OSError:
        return want
    for name in names:
        m = SPEC_RE.match(name)
        if not m:
            continue
        spec = pc.load_json(os.path.join(mc.FABRIC_DIR, name))
        if spec.get("nonce"):
            want[(m.group(1), m.group(2))] = spec
    return want


def interfaces(provider):
    try:
        out = subprocess.run(["mxl-fabrics-info", "-p", provider], capture_output=True,
                             text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return f"mxl-fabrics-info failed: {exc}"
    return out.strip()


def main():
    os.makedirs(mc.FABRIC_DIR, exist_ok=True)
    sessions = {}
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    said, last_info = None, (None, 0.0, "")
    while not stopping.is_set():
        cfg = mc.fabric_settings()
        iface = pc.resolve_iface(cfg["iface"])
        node = pc.iface_ipv4(iface)
        problem = None
        if not cfg["enabled"]:
            problem = "off"
        elif not node:
            problem = f"no IPv4 address on {iface or 'any interface'}"
        if problem != said:
            log("fabric is off. Waiting until it is turned on in the GUI." if problem == "off"
                else problem or f"fabric on, {cfg['provider']} on {iface} ({node})")
            said = problem
        want = {} if problem else read_specs()

        for sid in list(sessions):
            s = sessions[sid]
            spec = want.get(sid)
            if spec is None or spec.get("nonce") != s.nonce:
                s.stop()
                del sessions[sid]
            elif s.flow_replaced() and s.running():
                log(f"{mc.FLOW_NAMES[s.key]} was replaced, restarting {s.name}")
                s.stop()
        for sid, spec in want.items():
            s = sessions.get(sid)
            if s is None:
                s = sessions[sid] = Session(sid[0], sid[1], spec)
                s.start(cfg, node)
            elif not s.running() and s.role == "tx" and time.time() - s.started > TX_RESTART_S:
                if s.proc is not None and s.proc.poll() is not None and s.exit_code is None:
                    s.exit_code = s.proc.returncode
                    log(f"{s.name} exited with {s.exit_code}, restarting")
                s.start(cfg, node)
            elif not s.running() and s.role == "rx" and s.proc is not None and s.exit_code is None:
                s.exit_code = s.proc.returncode
                log(f"{s.name} exited with {s.exit_code}. Waiting for the GUI to pair again.")

        # The interface list costs a process, so refresh it only now and then
        if not problem and (last_info[0] != cfg["provider"] or time.time() - last_info[1] > 60):
            last_info = (cfg["provider"], time.time(), interfaces(cfg["provider"]))
        try:
            pc.write_json_atomic(mc.FABRIC_STATUS, {
                "ts": time.time(), "state": problem or "on", "provider": cfg["provider"],
                "iface": iface, "node": node, "interfaces": last_info[2] if not problem else "",
                "sessions": {s.name: s.snapshot() for s in sessions.values()}})
        except OSError:
            pass
        stopping.wait(0.5)

    for s in sessions.values():
        s.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
