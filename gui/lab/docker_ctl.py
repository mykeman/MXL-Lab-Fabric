# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Rankin
"""Container control and status through the Docker socket."""
import docker
import requests
from fastapi import HTTPException

from .settings import SERVICES, discovery_state, mc, pc

# What talking to the Docker socket can raise: the SDK's own errors, and the
# connection errors underneath it when the socket is missing or refuses.
DOCKER_ERRORS = (docker.errors.DockerException, requests.exceptions.RequestException, OSError)

_docker = None


def dock():
    """Docker client over the mounted socket. Raises if the socket is missing."""
    global _docker
    if _docker is None:
        client = docker.from_env()
        client.ping()
        _docker = client
    return _docker


def container(service):
    if service not in SERVICES:
        raise HTTPException(404, f"unknown function {service}")
    try:
        dock()
    except DOCKER_ERRORS as exc:
        raise HTTPException(503, f"cannot reach Docker: {exc}") from exc
    try:
        return dock().containers.get(SERVICES[service])
    except docker.errors.NotFound as exc:
        raise HTTPException(404, f"container {SERVICES[service]} not found, run docker compose up -d first") from exc


def container_status():
    out = {}
    for service, name in SERVICES.items():
        try:
            c = dock().containers.get(name)
            state = c.attrs.get("State", {})
            out[service] = {
                "status": c.status,
                "started": state.get("StartedAt"),
                "restarts": c.attrs.get("RestartCount", 0),
                "exit_code": state.get("ExitCode"),
            }
        except docker.errors.NotFound:
            out[service] = {"status": "missing"}
        except DOCKER_ERRORS as exc:
            out[service] = {"status": "unknown", "error": str(exc)}
    disc = out.get("ndi-discovery", {})
    if discovery_state()["mode"] != "local" and disc.get("status") in ("exited", "created", "missing"):
        out["ndi-discovery"] = {"status": "unused"}
    # Both idle rather than exit when turned off, so show them as not in use
    if pc.ptp_settings()["mode"] == "off" and out.get("ptp", {}).get("status") != "missing":
        out["ptp"] = {"status": "unused"}
    if not pc.audio_settings()["enabled"] and out.get("aes67-out", {}).get("status") != "missing":
        out["aes67-out"] = {"status": "unused"}
    st = mc.st2110_settings()
    if not any(o["enabled"] for o in st["out"].values()) and out.get("st2110-out", {}).get("status") != "missing":
        out["st2110-out"] = {"status": "unused"}
    if not mc.fabric_settings()["enabled"] and out.get("fabric", {}).get("status") != "missing":
        out["fabric"] = {"status": "unused"}
    # Ingest idles when Flow A comes from the peer or from nowhere
    if mc.routing_settings()["input"] in ("peer", "none") and out.get("ndi-ingest", {}).get("status") != "missing":
        out["ndi-ingest"] = {"status": "unused"}
    return out

