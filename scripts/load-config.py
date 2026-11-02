#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Michael Rankin
"""
Print shell export lines for the pipeline settings.

Values in /config/pipeline.json and /config/audio.json (written by the
web GUI) win over the environment from .env. The input type comes from
/config/routing.json. Used by run-ingest.sh:

    eval "$(python3 /opt/scripts/load-config.py)"
"""
import json
import os
import shlex

KEYS = ["NDI_SRC", "NDI_URL", "SRC_W", "SRC_H", "SRC_FPS", "OUT_W", "OUT_H", "SCALER"]
# audio.json key -> exported name
AUDIO_KEYS = {"channels": "AUDIO_CHANNELS"}
path = os.environ.get("PIPELINE_CONFIG", "/config/pipeline.json")
audio_path = os.environ.get("AUDIO_CONFIG", "/config/audio.json")
routing_path = os.environ.get("ROUTING_CONFIG", "/config/routing.json")

cfg = {}
try:
    with open(path) as f:
        cfg = json.load(f)
except (OSError, ValueError):
    pass

for key in KEYS:
    value = cfg.get(key, os.environ.get(key))
    if value is not None and str(value) != "":
        print(f"export {key}={shlex.quote(str(value))}")

audio = {}
try:
    with open(audio_path) as f:
        audio = json.load(f)
except (OSError, ValueError):
    pass

for key, name in AUDIO_KEYS.items():
    value = audio.get(key, os.environ.get(name))
    if value is not None and str(value) != "":
        print(f"export {name}={shlex.quote(str(value))}")

routing = {}
try:
    with open(routing_path) as f:
        routing = json.load(f)
except (OSError, ValueError):
    pass
print(f"export INPUT={shlex.quote(str(routing.get('input') or 'ndi'))}")
