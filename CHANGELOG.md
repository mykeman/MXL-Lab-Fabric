# Changelog

All notable changes to this fork. Versions follow [semantic versioning](https://semver.org/).

## 4.0.0

MXL Lab Fabric: ST 2110 in and out, and MXL flows between two labs over RoCEv2.

### Added

- **Fabric function** (fabric). Moves MXL flows to and from a second lab with MXL fabrics on libfabric: the verbs provider for RoCEv2 on ConnectX NICs, or TCP on any NIC. One `mxl-fabrics-demo` target or initiator per flow, supervised by `scripts/run-fabric.py`.
- **Routing**, in a new Routing and fabric tab. Flow A and the audio flow come from NDI, ST 2110, the peer lab or nothing; Flow B from autoframe here, the peer or nothing; any flow made here can be sent to the peer. Either lab can send or receive any flow.
- **Pairing between labs** through the GUIs (`/api/fabric/hello`, `/accept`, `/resend`): the receiver starts a target and hands back its fabric address and memory keys; the sender starts an initiator. A watchdog on each side pairs again after a restart on either side.
- **ST 2110 input**. ndi-ingest can instead receive ST 2110-20 video (4:2:2, 8 or 10 bit) and optional ST 2110-30 audio, set up from pasted SDP files, checked as they are typed.
- **ST 2110-20 output** (st2110-out). Sends Flow A and/or Flow B as 4:2:2 10 bit RFC 4175 multicast with an SDP each, RTP timestamps on the PTP clock, a read delay, and optional fq pacing.
- Image: libfabric 2.5.1 built with verbs, MXL built with `MXL_ENABLE_FABRICS_OFI`, rdma-core, perftest, and the RTP raw video elements checked at build time.
- setup.sh: RDMA device check, socket buffer limits for ST 2110 (`/etc/sysctl.d/90-mxl-lab.conf`), firewall rules for RoCEv2 (UDP 4791) and the fabric ports.

### Changed

- The ordered restart follows the routing: it starts only the functions this lab runs, asks the peer for flows it expects, and pairs the flows it sends.
- The Status tab and header lamps cover the new functions, and Needs attention reports fabric, peer and routing mismatches.

### Fixed

- NDI sources without audio gave a Flow A with no frames: the unlinked audio branch stalled the live pipeline on GStreamer 1.28 (Ubuntu 26.04). Ingest now listens for audio for a few seconds and adds the audio branch only when there is audio. `INGEST_AUDIO` in `.env` overrides it.

### Not yet verified

- Built and run end to end on a pair of ConnectX ports. The settings, SDP handling, routing and pairing logic were checked locally, without RDMA hardware or the image build.

## 3.0.0

Adds audio, PTP and a reworked GUI to the original lab ([mykeman/MXL-Lab](https://github.com/mykeman/MXL-Lab)).

### Added

- **MXL audio flow.** ndi-ingest writes the NDI source's audio to an MXL flow: 48 kHz, 1, 2, 4 or 8 channels, mapped one to one.
- **AES67 output** (aes67-out). Sends the audio flow as AES67 or ST 2110-30 multicast, L24 or L16, 1 ms or 125 us packets, with an SDP file and SAP announcements for Dante Controller. RTP timestamps are taken from `CLOCK_TAI`, so they are PTP time. A read delay and a send delay control when packets leave relative to their timestamps. Includes a 1 kHz test tone source and level meters.
- **PTP function** (ptp). Runs ptp4l, and phc2sys with hardware timestamping, from settings made in the GUI: off, follow only, or able to become grandmaster; Dante (AES67 media profile), ST 2059-2 or custom settings. Keeps the kernel TAI offset in line with the grandmaster so that `CLOCK_TAI` reads PTP time.
- **Tabbed GUI**: Status, NDI, 9:16 framing, and AES67 and clock. The Status tab lists anything that needs attention, worst first, with links to the tab that fixes it.
- Live PTP offset graph, grandmaster details and grandmaster time; an SDP download; audio meters at about 20 readings a second.
- Optional GUI login (`GUI_USER`, `GUI_PASSWORD`) and bind address (`GUI_BIND`).
- `scripts/pin-from-image.sh`, to pin the Dockerfile to the versions in a working image.
- Ruff and shellcheck configuration.

### Changed

- The Dockerfile pins every upstream source to an exact version, and checks the face model's SHA-256.
- The GUI backend is split into a `lab` package, and the page into `index.html`, `app.js` and `app.css`.
- `setup.sh` offers to turn off NTP (needed for PTP), and opens the PTP and SAP ports when ufw is active.
- Apply jobs, and VM boots with PTP on, wait for PTP to settle before starting the media functions.

### Fixed

- Text from the network (NDI source names and addresses) and from the server is escaped before it is put into the page.
- The lab recovers when the grandmaster's time jumps: ptp4l steps the clock when the offset passes 1 s, and the GUI detects any clock step and restarts the media functions in order.
- PTP headers are sent as version 2.0, which every PTPv2 device answers.
- In follow-only mode, ptp4l's priorities are set to 255 so that any grandmaster wins.

## 1.0.0

The original lab: NDI in, MXL flows, 9:16 face-following crop, NDI out, web GUI and install script. See [mykeman/MXL-Lab](https://github.com/mykeman/MXL-Lab).
