// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 Michael Rankin
//
// MXL lab control page. Plain JavaScript, no build step.
"use strict";

// Thresholds for the status display. They decide what is shown as a warning,
// not how the lab runs. Values are lab defaults, not taken from a standard.
const LIMITS = {
  autoframeStaleS: 5,      // autoframe status older than this: not reporting
  audioStaleS: 3,          // AES67 sender status older than this: not reporting
  st2110StaleS: 4,         // ST 2110 sender status older than this: not reporting
  fabricStaleS: 4,         // fabric runner status older than this: not reporting
  frameRateTolerance: 0.05, // Flow A rate this far from the source rate: warn
  sendLagWarnMs: 2.5,      // packets leaving later than this after their timestamp: warn
  sendLagRedMs: 5,         // the same, shown in red on the AES67 panel
  ptpOffsetWarnNs: 1e6,    // 1 ms
  ptpOffsetFaultNs: 1e9,   // 1 s; ptp4l steps the clock beyond this
};

const $ = (id) => document.getElementById(id);
const api = async (path, opts = {}) => {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail ? (typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail)) : res.statusText);
  return body;
};

/* ---------- tuning controls ---------- */
const CONTROLS = {
  "c-motion": [
    ["ease", "Follow speed", "Share of the remaining distance moved each tick. Higher is snappier.", 0.01, 0.4, 0.005, (v) => v.toFixed(3)],
    ["max_step_px", "Speed limit", "Most the frame can move per tick, in source pixels.", 1, 40, 0.5, (v) => v + " px"],
    ["deadband_px", "Deadband", "Ignore target moves smaller than this. Stops small hunting.", 0, 150, 1, (v) => v + " px"],
    ["recentre_s", "Return to centre after", "How long with no face before the frame drifts back.", 0.5, 20, 0.5, (v) => v + " s"],
  ],
  "c-detect": [
    ["score_threshold", "Confidence", "Minimum face score. Raise it to drop false faces.", 0.3, 0.95, 0.01, (v) => v.toFixed(2)],
    ["det_period_s", "Detection interval", "Time between detections. Lower costs more CPU.", 0.05, 1.0, 0.01, (v) => Math.round(1 / v) + " per s"],
    ["min_face_px", "Smallest face", "Ignore faces narrower than this in the source frame.", 0, 400, 5, (v) => v + " px"],
    ["top_k", "Max faces", "Most faces considered per detection.", 1, 20, 1, (v) => String(v)],
    ["nms_threshold", "Overlap merge", "How much two boxes can overlap before one is dropped.", 0.1, 0.7, 0.01, (v) => v.toFixed(2)],
  ],
  "c-comp": [
    ["bias_px", "Horizontal offset", "Shift the frame from the face. Negative moves it left.", -300, 300, 2, (v) => (v > 0 ? "+" : "") + v + " px"],
  ],
};
const MODE_HELP = {
  weighted: "Frames all faces, weighted toward the largest.",
  largest: "Frames only the largest face. Better for two-shots.",
  manual: "Detection keeps running but the frame goes where you put it.",
  hold: "The frame stays where it is.",
};

let tuning = {};
let status = {};
let saveTimer = null;

function buildControls() {
  for (const [box, list] of Object.entries(CONTROLS)) {
    const el = $(box);
    for (const [key, label, help, min, max, step] of list) {
      el.insertAdjacentHTML("beforeend",
        `<div class="ctl"><div class="top"><label for="${key}">${label}</label><output id="o-${key}"></output></div>
         <p>${help}</p><input type="range" id="${key}" min="${min}" max="${max}" step="${step}"></div>`);
      $(key).addEventListener("input", (e) => { tuning[key] = Number(e.target.value); showValues(); queueSave(); });
    }
  }
  $("manual_pos").addEventListener("input", (e) => { tuning.manual_pos = Number(e.target.value); showValues(); drawWindow(); queueSave(); });
  $("preview_on").addEventListener("change", (e) => { tuning.preview = e.target.checked; queueSave(); });
  document.querySelectorAll("#frame-modes button").forEach((b) =>
    b.addEventListener("click", () => { tuning.mode = b.dataset.mode; showValues(); queueSave(); }));
  $("reset").addEventListener("click", async () => {
    tuning = (await api("/api/tuning/reset", { method: "POST" })).values;
    showValues(); flash("Defaults restored");
  });
}

function fmt(key, v) {
  for (const list of Object.values(CONTROLS)) for (const c of list) if (c[0] === key) return c[6](v);
  return String(v);
}

function showValues() {
  for (const list of Object.values(CONTROLS)) for (const [key] of list) {
    if (tuning[key] === undefined) continue;
    const input = $(key);
    if (document.activeElement !== input) input.value = tuning[key];
    $("o-" + key).textContent = fmt(key, Number(tuning[key]));
  }
  $("manual_pos").value = tuning.manual_pos;
  $("o-manual_pos").textContent = Math.round(tuning.manual_pos * 100) + "%";
  $("preview_on").checked = !!tuning.preview;
  document.querySelectorAll("#frame-modes button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.mode === tuning.mode)));
  $("modehelp").textContent = MODE_HELP[tuning.mode] || "";
  $("manual-ctl").hidden = tuning.mode !== "manual";
  $("monitor").classList.toggle("manual", tuning.mode === "manual");
}

function queueSave() {
  clearTimeout(saveTimer);
  $("saved").textContent = "";
  saveTimer = setTimeout(async () => {
    try { await api("/api/tuning", { method: "PUT", body: JSON.stringify(tuning) }); flash("Saved, applied live"); }
    catch (e) { flash("Not saved: " + e.message); }
  }, 250);
}
function flash(msg) { $("saved").textContent = msg; }

/* ---------- manual drag on the monitor ---------- */
function dragTo(ev) {
  const af = status.autoframe || {};
  if (tuning.mode !== "manual" || !af.src_w) return;
  const rect = $("monitor").getBoundingClientRect();
  const x = (ev.clientX - rect.left) / rect.width * af.src_w;
  const pos = (x - af.crop_w / 2) / (af.src_w - af.crop_w);
  tuning.manual_pos = Math.min(1, Math.max(0, pos));
  showValues(); drawWindow(); queueSave();
}
let dragging = false;
$("monitor").addEventListener("pointerdown", (e) => { if (tuning.mode === "manual") { dragging = true; $("monitor").setPointerCapture(e.pointerId); dragTo(e); } });
$("monitor").addEventListener("pointermove", (e) => { if (dragging) dragTo(e); });
$("monitor").addEventListener("pointerup", () => { dragging = false; });

let pvMeta = null;      // crop position that came with the frame on screen
let pvMetaAt = 0;

function drawWindow() {
  const st = status.autoframe || {};
  const fresh = pvMeta && Date.now() - pvMetaAt < 2000;
  const af = fresh ? pvMeta : st;
  const live = af.src_w && (fresh || (st.age !== undefined && st.age < LIMITS.autoframeStaleS));
  $("window").hidden = !live;
  $("target").hidden = !live || tuning.mode === "hold";
  if (!live) return;
  let left = af.crop_left;
  if (tuning.mode === "manual") left = tuning.manual_pos * (af.src_w - af.crop_w);
  $("window").style.left = (left / af.src_w * 100) + "%";
  $("window").style.width = (af.crop_w / af.src_w * 100) + "%";
  const tgt = tuning.mode === "manual" ? left + af.crop_w / 2 : af.target;
  $("target").style.left = (tgt / af.src_w * 100) + "%";
}

/* ---------- status polling ---------- */
const FN_LABEL = { "ndi-discovery": "Discovery server", "ndi-ingest": "Ingest", "autoframe": "Autoframe", "ndi-out-original": "Original out", "ndi-out-vertical": "Vertical out", "aes67-out": "AES67 out", "st2110-out": "2110 out", "fabric": "Fabric", "ptp": "PTP" };
const openLogs = new Set();

// Escape text for HTML. Source names, addresses and error messages come from
// the network or the server, so they go through this before any innerHTML.
const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ESC[c]);

function buildFunctions() {
  $("fns").innerHTML = Object.entries(FN_LABEL).map(([svc, label]) => `
    <div class="fn" id="fn-${svc}">
      <div><span class="lamp" id="fl-${svc}"><i></i>${label}</span></div>
      <div class="meta" id="fm-${svc}"></div>
      <div class="acts">
        <button class="btn small" data-logs="${svc}" aria-expanded="false">Logs</button>
        <button class="btn small" data-restart="${svc}">Restart</button>
      </div>
      <pre id="log-${svc}" hidden></pre>
    </div>`).join("");
  $("fns").addEventListener("click", async (e) => {
    const r = e.target.dataset.restart, l = e.target.dataset.logs;
    if (r) {
      e.target.disabled = true; e.target.textContent = "Restarting";
      try { await api(`/api/containers/${r}/restart`, { method: "POST" }); } catch (err) { alert(err.message); }
      e.target.disabled = false; e.target.textContent = "Restart";
    }
    if (l) {
      const open = !openLogs.has(l);
      open ? openLogs.add(l) : openLogs.delete(l);
      e.target.setAttribute("aria-expanded", String(open));
      $("log-" + l).hidden = !open;
      if (open) refreshLog(l);
    }
  });
}

async function refreshLog(svc) {
  try {
    const { text } = await api(`/api/containers/${svc}/logs?tail=150`);
    const pre = $("log-" + svc);
    const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 20;
    pre.textContent = text || "No output yet.";
    if (atBottom) pre.scrollTop = pre.scrollHeight;
  } catch (e) { $("log-" + svc).textContent = e.message; }
}

function ago(iso) {
  if (!iso || iso.startsWith("0001")) return "";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 90) return Math.round(s) + " s";
  if (s < 5400) return Math.round(s / 60) + " min";
  return (s / 3600).toFixed(1) + " h";
}

function showFlow(prefix, f, fps, audio = false) {
  if (!f || !f.exists) { $(prefix + "-rate").textContent = "Not present"; $(prefix + "-sub").textContent = f ? f.id : ""; return; }
  if (audio) {
    $(prefix + "-rate").textContent = f.rate == null ? "-" : (f.rate / 1000).toFixed(1) + "k /s";
    $(prefix + "-rate").classList.toggle("warn", f.rate != null && Math.abs(f.rate - 48000) > 480);
    $(prefix + "-sub").textContent = (f.head != null ? "Head " + f.head + ". " : "Head index not parsed. ") + f.id;
    return;
  }
  const rate = f.rate == null ? "-" : f.rate + " /s";
  $(prefix + "-rate").textContent = rate;
  $(prefix + "-rate").classList.toggle("warn", f.rate != null && fps && Math.abs(f.rate - fps) > 2);
  $(prefix + "-sub").textContent = (f.head != null ? "Head " + f.head + ". " : "Head index not parsed. ") + f.id;
}

async function poll() {
  try {
    status = await api("/api/status");
    for (const [svc, c] of Object.entries(status.containers || {})) {
      for (const id of ["lamp-" + svc, "fl-" + svc]) $(id) && $(id).setAttribute("data-s", c.status);
      $("fm-" + svc).textContent = fnText(svc, c);
    }
    $("docker-error").hidden = !status.docker_error;
    $("docker-error").textContent = status.docker_error ? "Cannot reach Docker: " + status.docker_error : "";

    const af = status.autoframe || {};
    const live = af.age !== undefined && af.age < LIMITS.autoframeStaleS;
    $("r-faces").textContent = live ? af.faces : "-";
    $("r-left").textContent = live ? af.crop_left + " of " + (af.src_w - af.crop_w) : "-";
    $("r-det").textContent = live ? af.det_fps + " /s" : "-";
    $("r-seen").textContent = live && af.last_seen_age != null ? af.last_seen_age.toFixed(1) + " s ago" : "-";
    $("r-out").textContent = live ? `${af.crop_w}x${af.src_h} to ${af.out_w}x${af.out_h}, ${af.fps} fps` : "-";
    drawWindow();

    const fps = (status.autoframe || {}).fps;
    showFlow("fa", status.flows.A, fps);
    showFlow("fb", status.flows.B, fps);
    showFlow("fx", status.flows.audio, fps, true);
    showVia(status.routing || {});
    showSt2110Status(status.st2110 || {});
    showFabricStatus(status.fabric || {});
    showAudio(status.audio || {});
    showPtp(status.ptp || {}, status.ptp_mode);
    showJob(status.job);
    updateOverview();
    for (const svc of openLogs) refreshLog(svc);
  } catch (e) {
    $("docker-error").hidden = false;
    $("docker-error").textContent = "Lost contact with the control service: " + e.message;
    renderAttention([{ lvl: "fault", area: "general", text: "Lost contact with the control service: " + e.message }]);
  }
}

function showPreviewEmpty(empty) {
  $("preview").hidden = empty;
  $("preview-empty").hidden = !empty;
}

/* Fetches each preview frame as soon as autoframe produces it, and moves the
   crop window using the position sent with that frame, so the two stay in step. */
async function previewLoop() {
  let seq = -1, url = null, lastFrame = 0;
  for (;;) {
    if (!tuning.preview || activeTab !== "framing") {
      if (!tuning.preview) showPreviewEmpty(true);
      pvMeta = null; seq = -1;
      await new Promise((r) => setTimeout(r, 400));
      continue;
    }
    try {
      const res = await fetch("/api/preview?after=" + seq, { cache: "no-store" });
      if (res.status === 200) {
        const meta = JSON.parse(res.headers.get("X-Preview") || "{}");
        const blob = await res.blob();
        const next = URL.createObjectURL(blob);
        await new Promise((resolve) => {
          const img = $("preview");
          img.onload = img.onerror = resolve;
          img.src = next;
        });
        if (url) URL.revokeObjectURL(url);
        url = next;
        seq = meta.seq ?? -1;
        pvMeta = meta; pvMetaAt = Date.now(); lastFrame = Date.now();
        showPreviewEmpty(false);
        drawWindow();
      } else if (Date.now() - lastFrame > 3000) {
        showPreviewEmpty(true);
      }
    } catch (e) {
      showPreviewEmpty(true);
      await new Promise((r) => setTimeout(r, 1000));
    }
  }
}

/* ---------- NDI source and pipeline ---------- */
let pipeline = {};
async function loadPipeline() {
  const p = await api("/api/pipeline");
  pipeline = p.settings;
  const fill = (id, values, current, label = (v) => v) =>
    $(id).innerHTML = values.map((v) => `<option value="${v}" ${String(v) === String(current) ? "selected" : ""}>${label(v)}</option>`).join("");
  fill("resolution", p.choices.resolutions, pipeline.SRC_AUTO ? "auto" : `${pipeline.SRC_W}x${pipeline.SRC_H}`,
    (v) => v === "auto" ? "Auto" : v.endsWith("1080") ? "1080p" : "720p");
  const rates = p.choices.frame_rates.map(String);
  if (!rates.includes(String(pipeline.SRC_FPS))) rates.unshift(String(pipeline.SRC_FPS));
  fill("fps", rates, pipeline.SRC_FPS, fpsLabel);
  if (pipeline.SRC_AUTO) $("detected").textContent = `Last detected: ${pipeline.SRC_W}x${pipeline.SRC_H} at ${fpsLabel(pipeline.SRC_FPS)}`;
  syncAuto();
  fill("output", p.choices.outputs, `${pipeline.OUT_W}x${pipeline.OUT_H}`);
  fill("scaler", p.choices.scalers, pipeline.SCALER, (v) => ({ lanczos: "Lanczos", "4-tap": "4-tap", bilinear: "Bilinear" })[v]);
  $("ndi_src").value = pipeline.NDI_SRC;
  $("ndi_url").value = pipeline.NDI_URL || "";
  $("current-src").textContent = pipeline.NDI_SRC ? "Running: " + pipeline.NDI_SRC + (pipeline.NDI_URL ? " at " + pipeline.NDI_URL : "") : "";
  showJob(p.job);
}

function fpsLabel(v) {
  const t = String(v);
  if (!t.includes("/")) return t + " fps";
  const [n, d] = t.split("/").map(Number);
  return (Math.round(n / d * 100) / 100) + " fps";
}

function syncAuto() {
  const auto = $("resolution").value === "auto";
  $("fps").disabled = auto;
  $("fps").title = auto ? "Set from the source when you apply" : "";
}
$("resolution").addEventListener("change", syncAuto);

$("detect").addEventListener("click", async () => {
  const name = $("ndi_src").value.trim();
  if (!name) { $("detected").textContent = "Pick a source first."; return; }
  $("detect").disabled = true;
  $("detected").textContent = "Connecting to the source";
  try {
    const f = await api("/api/ndi/probe", { method: "POST", body: JSON.stringify({ NDI_SRC: name, NDI_URL: $("ndi_url").value.trim() }) });
    $("detected").textContent = "Source is " + f.label;
  } catch (e) { $("detected").textContent = "Could not read the format: " + e.message; }
  $("detect").disabled = false;
});

async function loadSources() {
  const s = await api("/api/ndi/sources").catch((e) => ({ sources: [], error: e.message }));
  const list = $("sources");
  if (s.error) { list.innerHTML = `<li class="none warn">${esc(s.error)}</li>`; $("src-found").textContent = "Discovery failed"; return; }
  if (!$("rescan").disabled) $("src-found").textContent = s.sources.length ? `${s.sources.length} ${s.sources.length === 1 ? "source" : "sources"} found` : "Searching the network";
  if (!s.sources.length) {
    list.innerHTML = `<li class="none">No sources seen yet. Discovery can take a few seconds. If nothing shows, check avahi and that the VM is on the same VLAN as the sender.</li>`;
    return;
  }
  const chosen = $("ndi_src").value;
  list.innerHTML = s.sources.map((src) => `<li><button type="button" data-name="${esc(src.name)}" data-url="${esc(src["url-address"])}" aria-pressed="${src.name === chosen}">
      <span>${esc(src.name)}</span><small>${esc(src["url-address"] || src.address)}${src.name === pipeline.NDI_SRC ? " In use" : ""}</small></button></li>`).join("");
}
$("sources").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-name]");
  if (!b) return;
  $("ndi_src").value = b.dataset.name;
  if (b.dataset.url) $("ndi_url").value = b.dataset.url;
  document.querySelectorAll("#sources button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
});
$("rescan").addEventListener("click", async () => {
  $("rescan").disabled = true;
  $("src-found").textContent = "Searching the network";
  await api("/api/ndi/refresh", { method: "POST" }).catch(() => {});
  // a scan takes about 6 s after the minimum gap; keep the current list until it finishes
  setTimeout(async () => { await loadSources(); $("rescan").disabled = false; }, 9000);
});

$("apply").addEventListener("click", async () => {
  const name = $("ndi_src").value.trim();
  if (!name) { $("joblog").hidden = false; $("joblog").className = "joblog failed"; $("joblog").textContent = "Pick a source or type its name first."; return; }
  const body = { NDI_SRC: name, NDI_URL: $("ndi_url").value.trim(), resolution: $("resolution").value, output: $("output").value, SRC_FPS: parseInt($("fps").value, 10) || 0, SCALER: $("scaler").value };
  try {
    await api("/api/pipeline/apply", { method: "POST", body: JSON.stringify(body) });
    $("apply").disabled = true;
    if (!tracking) trackJob();
  } catch (e) { $("joblog").hidden = false; $("joblog").className = "joblog failed"; $("joblog").textContent = e.message; }
});

const JOB_BOX = { discovery: "disc-log", audio: "audio-log", ptp: "ptp-log", pipeline: "joblog",
  routing: "routing-log", st2110in: "st2110in-log", st2110: "st2110-log", fabric: "fabric-log" };
const APPLY_BUTTONS = ["apply", "disc-apply", "a-apply", "p-apply", "r-apply", "s-in-apply", "s-out-apply", "f-apply"];
let tracking = false;
function showJob(j) {
  if (!j || (!j.running && !j.log.length)) return;
  const box = $(JOB_BOX[j.kind] || "joblog");
  box.hidden = false;
  box.className = "joblog" + (j.result ? " " + j.result : "");
  box.textContent = j.log.join("\n") + (j.running ? "\nWorking" : "");
  box.scrollTop = box.scrollHeight;
  APPLY_BUTTONS.forEach((id) => { $(id).disabled = j.running; });
  // a change started elsewhere, such as the boot sequence, is followed too
  if (j.running && !tracking) trackJob();
}
async function trackJob() {
  tracking = true;
  const j = await api("/api/pipeline/job");
  showJob(j);
  if (j.running) { setTimeout(trackJob, 700); return; }
  tracking = false;
  await loadPipeline(); await loadDiscovery(); await loadAudio(); await loadPtp(); loadSources();
  await loadRouting(); await loadSt2110(); await loadFabric();
}
function startFailed(boxId, msg) {
  const box = $(boxId);
  box.hidden = false; box.className = "joblog failed"; box.textContent = msg;
}

/* ---------- NDI discovery ---------- */
let disc = { mode: "mdns", servers: "", vm_ip: "" };
let discPick = "mdns";
const DISC_LABEL = { mdns: "mDNS", local: "internal discovery server", external: "external discovery server" };

function showDiscovery() {
  document.querySelectorAll("[data-disc]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.disc === discPick)));
  const ip = disc.vm_ip || "this VM's address";
  $("disc-help").textContent = {
    mdns: "Normal NDI discovery. Any NDI machine on this network sees the lab outputs, with nothing to set on other PCs.",
    local: `Runs an NDI Discovery Server on this VM. On each PC, add ${ip} in NDI Access Manager under Advanced, Discovery Servers.`,
    external: "Uses a discovery server you already run. The lab registers its outputs there and finds sources there.",
  }[discPick];
  $("disc-servers-field").hidden = discPick !== "external";
  let now = "In use: " + DISC_LABEL[disc.mode];
  if (disc.mode === "external" && disc.servers) now += " " + disc.servers;
  if (disc.mode === "local" && disc.vm_ip) now += " " + disc.vm_ip;
  $("disc-current").textContent = now;
}

async function loadDiscovery() {
  try {
    disc = await api("/api/discovery");
    discPick = disc.mode;
    if (disc.servers) $("disc-servers").value = disc.servers;
    showDiscovery();
  } catch (e) { $("disc-current").textContent = e.message; }
}

document.querySelectorAll("[data-disc]").forEach((b) =>
  b.addEventListener("click", () => { discPick = b.dataset.disc; showDiscovery(); if (discPick === "external") $("disc-servers").focus(); }));

$("disc-apply").addEventListener("click", async () => {
  const servers = $("disc-servers").value.trim();
  const box = $("disc-log");
  if (discPick === "external" && !servers) {
    box.hidden = false; box.className = "joblog failed"; box.textContent = "Enter the discovery server address first.";
    return;
  }
  try {
    await api("/api/discovery", { method: "POST", body: JSON.stringify({ mode: discPick, servers }) });
    $("disc-apply").disabled = true; $("apply").disabled = true;
    $("sources").innerHTML = ""; $("src-found").textContent = "Searching the network";
    if (!tracking) trackJob();
  } catch (e) { box.hidden = false; box.className = "joblog failed"; box.textContent = e.message; }
});

/* ---------- audio out ---------- */
let audioCfg = {}, audioSrc = "flow";
const ASRC_HELP = {
  flow: "Sends the audio that came in with the NDI source, from the MXL audio flow.",
  tone: "Sends a 1 kHz tone at -20 dBFS on every channel. Use it to check the path to Dante or a 2110 receiver without an NDI source.",
};

function ifaceOptions(list, current, def, withTs) {
  const opts = [`<option value="">Default route (${esc(def || "none")})</option>`];
  for (const i of list) {
    let label = i.name + (i.ipv4 ? ", " + i.ipv4 : "");
    if (withTs) label += i.ts.hardware ? ", hardware timestamps" : i.ts.software ? ", software timestamps" : ", no timestamping";
    opts.push(`<option value="${esc(i.name)}" ${i.name === current ? "selected" : ""}>${esc(label)}</option>`);
  }
  return opts.join("");
}

async function loadAudio() {
  const a = await api("/api/audio");
  audioCfg = a.settings;
  audioSrc = audioCfg.source;
  const sel = (id, vals, cur, label = (v) => v) =>
    $(id).innerHTML = vals.map((v) => `<option value="${v}" ${String(v) === String(cur) ? "selected" : ""}>${label(v)}</option>`).join("");
  sel("a-channels", a.choices.channels, audioCfg.channels, (v) => v === 1 ? "Mono" : v === 2 ? "Stereo" : v + " channels");
  sel("a-encoding", a.choices.encodings, audioCfg.encoding, (v) => v === "L24" ? "L24, 24 bit" : "L16, 16 bit");
  sel("a-ptime", a.choices.ptimes, audioCfg.ptime_us, (v) => v === 1000 ? "1 ms" : "125 us");
  $("a-iface").innerHTML = ifaceOptions(a.ifaces, audioCfg.iface, a.default_iface, false);
  $("a-address").value = audioCfg.address;
  $("a-port").value = audioCfg.port;
  $("a-pt").value = audioCfg.payload_type;
  $("a-ttl").value = audioCfg.ttl;
  $("a-dscp").value = audioCfg.dscp;
  $("a-delay").value = audioCfg.send_delay_ms;
  $("a-read").value = audioCfg.read_delay_ms;
  $("a-name").value = audioCfg.session_name;
  $("a-sap").checked = !!audioCfg.sap;
  $("a-enabled").checked = !!audioCfg.enabled;
  syncLevelStream();
  showAudioSource();
  audioWarn();
}

function showAudioSource() {
  document.querySelectorAll("[data-asrc]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.asrc === audioSrc)));
  $("asrc-help").textContent = ASRC_HELP[audioSrc];
}
document.querySelectorAll("[data-asrc]").forEach((b) => b.addEventListener("click", () => { audioSrc = b.dataset.asrc; showAudioSource(); }));

function audioWarn() {
  const notes = [];
  if ($("a-ptime").value === "125" && ptpPick.profile === "dante") notes.push("Dante receives AES67 at 1 ms packet time only.");
  if (!$("a-address").value.startsWith("239.")) notes.push("Use an address in 239.0.0.0/8, the administratively scoped range.");
  $("a-warn").hidden = !notes.length;
  $("a-warn").className = "note warn";
  $("a-warn").textContent = notes.join(" ");
}
["a-ptime", "a-channels", "a-address"].forEach((id) => $(id).addEventListener("input", audioWarn));

$("a-apply").addEventListener("click", async () => {
  const body = {
    enabled: $("a-enabled").checked, source: audioSrc,
    channels: Number($("a-channels").value), encoding: $("a-encoding").value, ptime_us: Number($("a-ptime").value),
    address: $("a-address").value.trim(), port: Number($("a-port").value), payload_type: Number($("a-pt").value),
    iface: $("a-iface").value, ttl: Number($("a-ttl").value), dscp: Number($("a-dscp").value),
    send_delay_ms: Number($("a-delay").value), read_delay_ms: Number($("a-read").value), session_name: $("a-name").value.trim(), sap: $("a-sap").checked,
  };
  try { await api("/api/audio/apply", { method: "POST", body: JSON.stringify(body) }); if (!tracking) trackJob(); }
  catch (e) { startFailed("audio-log", e.message); }
});

const METER_TICKS = [0, -10, -20, -30, -40, -50, -60];
let meterCount = 0;
const dbToPct = (db) => Math.max(0, Math.min(100, (db + 60) / 60 * 100));
function buildMeters(n) {
  meterCount = n;
  const scale = `<div class="scale" aria-hidden="true">${METER_TICKS.map((t) => `<span style="top:${100 - dbToPct(t)}%">${t}</span>`).join("")}</div>`;
  const bars = Array.from({ length: n }, (_, i) => `<div class="meter"><div class="bar"><div class="fill" id="mf-${i}"></div><div class="pk" id="mp-${i}"></div></div><div class="lbl" id="ml-${i}">${i + 1}</div></div>`).join("");
  $("meters").innerHTML = scale + bars;
}
function showAudio(a) {
  const live = a.age !== undefined && a.age < LIMITS.audioStaleS;
  const state = !live ? (audioCfg.enabled ? "Not running" : "Off")
    : { sending: "Sending", waiting: "Waiting for the audio flow", starting: "Starting", error: "Error: " + (a.error || ""), off: "Off", stopped: "Stopped" }[a.state] || a.state;
  $("a-state").textContent = state;
  $("a-dest").textContent = live && a.dest ? `${a.dest} on ${a.iface}` : "-";
  $("a-fmt").textContent = live && a.channels ? `${a.channels} ch ${a.encoding}, 48 kHz, ${a.ptime_us === 1000 ? "1 ms" : "125 us"}` : "-";
  $("a-rate").textContent = live && a.kbps ? (a.kbps / 1000).toFixed(2) + " Mb/s" : "-";
  $("a-lat").textContent = live && a.latency_ms != null ? a.latency_ms + " ms" : "-";
  $("a-lat").classList.toggle("warn", live && a.latency_ms != null && a.latency_ms > LIMITS.sendLagRedMs);
  const late = live && a.arrival_ms != null && a.read_delay_ms != null && a.arrival_ms > a.read_delay_ms;
  $("a-arr").textContent = live && a.arrival_ms != null
    ? `${a.arrival_ms} ms` + (late ? `, above the ${a.read_delay_ms} ms read delay` : "") : "-";
  $("a-arr").classList.toggle("warn", late);
  $("a-ref").textContent = !live ? "-" : a.ptp_mode === "off" ? "VM clock, no PTP" : a.gm ? "PTP " + a.gm : "PTP, grandmaster not known";
  const peaks = live && a.state === "sending" ? a.peak || [] : [];
  if (!peaks.length) {
    if (meterCount) { meterCount = 0; $("meters").innerHTML = `<span class="none">${live && a.state === "waiting" ? "Waiting for the audio flow. The NDI source may have no audio." : "No audio is being sent."}</span>`; }
    return;
  }
  if (peaks.length !== meterCount) buildMeters(peaks.length);
  // Without the fast stream (it is only open on the Status and AES67 tabs), use the slower status readings
  if (performance.now() - levelStreamAt > 1000) meterIn = { rms: a.rms || [], peak: peaks, at: performance.now() };
}

/* ---------- meter stream and ballistics ---------- */
// Levels arrive about 20 times a second as server-sent events. The display
// rises at once, falls at a steady rate, and holds each peak for a moment,
// like a hardware meter, so it reads smoothly between readings.
const FALL_DB_S = 26, HOLD_MS = 1500, HOLD_FALL_DB_S = 20, FLOOR_DB = -90;
let meterIn = { rms: [], peak: [], at: 0 };
const meterShow = { rms: [], hold: [], holdAt: [] };
let levelSrc = null, levelStreamAt = 0, lastFrame = performance.now();

function syncLevelStream() {
  const want = !document.hidden && (activeTab === "audio" || activeTab === "status") && !!audioCfg.enabled;
  if (want && !levelSrc) {
    levelSrc = new EventSource("/api/audio/levels/stream");
    levelSrc.onmessage = (e) => {
      const d = JSON.parse(e.data);
      if (d.state !== "sending") return;
      levelStreamAt = performance.now();
      meterIn = { rms: d.rms, peak: d.peak, at: levelStreamAt };
    };
  } else if (!want && levelSrc) {
    levelSrc.close();
    levelSrc = null;
  }
}
document.addEventListener("visibilitychange", syncLevelStream);

function meterFrame(t) {
  const dt = Math.min(0.1, (t - lastFrame) / 1000);
  lastFrame = t;
  const stale = t - meterIn.at > 1500;
  const n = meterIn.rms.length;
  for (let i = 0; i < n; i++) {
    const target = stale ? FLOOR_DB : meterIn.rms[i];
    const pk = stale ? FLOOR_DB : meterIn.peak[i];
    const r = meterShow.rms[i] ?? FLOOR_DB;
    meterShow.rms[i] = target >= r ? target : Math.max(target, r - FALL_DB_S * dt);
    let h = meterShow.hold[i] ?? FLOOR_DB;
    if (pk >= h) { h = pk; meterShow.holdAt[i] = t; }
    else if (t - (meterShow.holdAt[i] || 0) > HOLD_MS) h = Math.max(pk, h - HOLD_FALL_DB_S * dt);
    meterShow.hold[i] = h;
  }
  if (activeTab === "audio" && meterCount === n && n && $("mf-0")) {
    for (let i = 0; i < n; i++) {
      const fill = $("mf-" + i);
      fill.style.setProperty("--h", fill.parentElement.clientHeight + "px");
      fill.style.height = dbToPct(meterShow.rms[i]) + "%";
      $("mp-" + i).style.bottom = "calc(" + dbToPct(meterShow.hold[i]) + "% - 2px)";
      $("ml-" + i).textContent = meterShow.hold[i] <= -89 ? "-inf" : meterShow.hold[i].toFixed(0);
    }
  }
  if (activeTab === "status") {
    const box = $("ov-meters");
    if (box.childElementCount !== n) {
      box.innerHTML = Array.from({ length: n }, (_, i) =>
        `<div class="m"><span>${i + 1}</span><div class="t"><div class="f" id="om-${i}"></div></div></div>`).join("");
    }
    for (let i = 0; i < n; i++) {
      const f = $("om-" + i), h = meterShow.hold[i];
      f.style.width = dbToPct(meterShow.rms[i]) + "%";
      f.className = "f" + (h > -6 ? " over" : h > -18 ? " hot" : "");
    }
  }
  requestAnimationFrame(meterFrame);
}
requestAnimationFrame(meterFrame);

/* ---------- PTP ---------- */
let ptpCfg = {}, ptpProfiles = {}, ptpPick = { mode: "off", profile: "dante" };
const PMODE_HELP = {
  off: "No PTP. The AES67 stream is timestamped from the VM's own clock, so PTP-locked receivers will drift against it.",
  follow: "Locks this VM to the grandmaster on the network and never takes over. Use this with a Dante or 2110 network that has a grandmaster. Priorities are fixed at 255 in this mode so any leader wins.",
  auto: "Takes part in the best master election, which compares Priority 1 first. With nothing better on the network, this VM becomes grandmaster. Dante leaders announce 249, so keep Priority 1 at 250 or above if a Dante device should lead.",
};
const PPROF_HELP = {
  dante: "AES67 media profile, which Dante uses in AES67 mode: domain 0, sync 8 per second, announce every 2 s, priorities 250 so a Dante leader wins.",
  st2059: "SMPTE ST 2059-2: domain 127, sync 8 per second, announce 4 per second.",
  custom: "Your own values.",
};
const PROF_FIELDS = { domain: "p-domain", priority1: "p-prio1", priority2: "p-prio2", delay_mechanism: "p-dm", log_sync: "p-sync", log_announce: "p-ann", log_delay_req: "p-dreq", announce_timeout: "p-ato" };
const logLabel = (n) => (n < 0 ? `${2 ** -n} per s` : n === 0 ? "1 per s" : `every ${2 ** n} s`) + `, log ${n}`;
let ptpIfaces = [];

async function loadPtp() {
  const p = await api("/api/ptp");
  ptpCfg = p.settings; ptpProfiles = p.profiles; ptpIfaces = p.ifaces;
  ptpPick = { mode: ptpCfg.mode, profile: ptpCfg.profile };
  const range = (id, [lo, hi], cur) => {
    const vals = []; for (let n = lo; n <= hi; n++) vals.push(n);
    $(id).innerHTML = vals.map((n) => `<option value="${n}" ${n === cur ? "selected" : ""}>${logLabel(n)}</option>`).join("");
  };
  range("p-sync", p.limits.log_sync, ptpCfg.log_sync);
  range("p-ann", p.limits.log_announce, ptpCfg.log_announce);
  range("p-dreq", p.limits.log_delay_req, ptpCfg.log_delay_req);
  $("p-iface").innerHTML = ifaceOptions(p.ifaces, ptpCfg.iface, p.default_iface, true);
  $("p-ts").value = ptpCfg.timestamping;
  $("p-domain").value = ptpCfg.domain;
  $("p-prio1").value = ptpCfg.priority1;
  $("p-prio2").value = ptpCfg.priority2;
  $("p-dm").value = ptpCfg.delay_mechanism;
  $("p-ato").value = ptpCfg.announce_timeout;
  $("p-dscpe").value = ptpCfg.dscp_event;
  $("p-dscpg").value = ptpCfg.dscp_general;
  $("p-current").textContent = "In use: " + { off: "off", follow: "follow only", auto: "can be grandmaster" }[ptpCfg.mode]
    + (ptpCfg.mode !== "off" ? `, domain ${ptpCfg.domain}` : "");
  showPtpPick();
  syncTsChoice();
}

function showPtpPick() {
  document.querySelectorAll("[data-pmode]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.pmode === ptpPick.mode)));
  document.querySelectorAll("[data-pprof]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.pprof === ptpPick.profile)));
  $("pmode-help").textContent = PMODE_HELP[ptpPick.mode];
  $("pprof-help").textContent = PPROF_HELP[ptpPick.profile];
  $("p-prio1").disabled = ptpPick.mode === "follow";
  $("p-prio2").disabled = ptpPick.mode === "follow";
  audioWarn();
}

function syncTsChoice() {
  const name = $("p-iface").value || null;
  const info = ptpIfaces.find((i) => i.name === name) || ptpIfaces.find((i) => $("p-iface").options[0].text.includes("(" + i.name + ")"));
  const hw = info ? info.ts.hardware : false;
  $("p-ts").querySelector('[value="hardware"]').disabled = !hw;
  if (!hw && $("p-ts").value === "hardware") $("p-ts").value = "software";
}
$("p-iface").addEventListener("change", syncTsChoice);

document.querySelectorAll("[data-pmode]").forEach((b) => b.addEventListener("click", () => { ptpPick.mode = b.dataset.pmode; showPtpPick(); }));
document.querySelectorAll("[data-pprof]").forEach((b) => b.addEventListener("click", () => {
  ptpPick.profile = b.dataset.pprof;
  const preset = ptpProfiles[ptpPick.profile];
  if (preset) for (const [key, id] of Object.entries(PROF_FIELDS)) $(id).value = preset[key];
  showPtpPick();
}));
// Editing a profile value by hand makes the profile custom
document.querySelectorAll("[data-prof]").forEach((el) => el.addEventListener("change", () => {
  const preset = ptpProfiles[ptpPick.profile];
  if (!preset) return;
  const same = Object.entries(PROF_FIELDS).every(([key, id]) => String(preset[key]) === String($(id).value));
  if (!same) { ptpPick.profile = "custom"; showPtpPick(); }
}));

$("p-apply").addEventListener("click", async () => {
  const num = (id) => Number($(id).value);
  const body = {
    mode: ptpPick.mode, profile: ptpPick.profile, iface: $("p-iface").value, timestamping: $("p-ts").value,
    domain: num("p-domain"), priority1: num("p-prio1"), priority2: num("p-prio2"),
    log_sync: num("p-sync"), log_announce: num("p-ann"), log_delay_req: num("p-dreq"),
    announce_timeout: num("p-ato"), delay_mechanism: $("p-dm").value,
    dscp_event: num("p-dscpe"), dscp_general: num("p-dscpg"),
  };
  try { await api("/api/ptp/apply", { method: "POST", body: JSON.stringify(body) }); if (!tracking) trackJob(); }
  catch (e) { startFailed("ptp-log", e.message); }
});

// ptp4l reports offsets in a field that tops out at 2^47 ns, about 39 hours
const OFFSET_LIMIT = 140737488355327;
function gmDate(sec) {
  return new Date(sec * 1000).toISOString().replace("T", " ").slice(0, 19) + " UTC";
}
function fmtNs(ns) {
  if (ns == null) return "-";
  // exactly the limit means pinned with no raw value to fall back on
  if (Math.abs(ns) === OFFSET_LIMIT) return (ns < 0 ? "-" : "") + "over 39 h";
  const sec = Math.abs(ns) / 1e9, sign = ns < 0 ? "-" : "";
  if (sec >= 365 * 86400) return sign + (sec / (365.25 * 86400)).toFixed(1) + " years";
  if (sec >= 86400) return sign + (sec / 86400).toFixed(1) + " days";
  if (sec >= 3600) return sign + (sec / 3600).toFixed(1) + " h";
  if (sec >= 60) return sign + (sec / 60).toFixed(1) + " min";
  if (sec >= 1) return sign + sec.toFixed(2) + " s";
  const a = Math.abs(ns);
  if (a < 1000) return ns.toFixed(0) + " ns";
  if (a < 1e6) return (ns / 1000).toFixed(1) + " us";
  return (ns / 1e6).toFixed(2) + " ms";
}
const STATE_TEXT = { SLAVE: "Following", UNCALIBRATED: "Locking", LISTENING: "Listening for a grandmaster", MASTER: "Grandmaster", GRAND_MASTER: "Grandmaster", PASSIVE: "Passive", FAULTY: "Fault", INITIALIZING: "Starting", DISABLED: "Disabled" };

function showPtp(p, mode) {
  const s = p.summary || {};
  let state;
  if (mode === "off") state = "Off";
  else if (p.error) state = "No answer from ptp4l";
  else state = STATE_TEXT[s.port_state] || s.port_state || "Starting";
  if (s.port_state === "MASTER" && !s.gm_is_self) state = "Leader on this link";
  $("p-state").textContent = state;
  $("p-state").classList.toggle("warn", s.port_state === "FAULTY" || (!!p.error && mode !== "off"));
  const following = s.port_state === "SLAVE" || s.port_state === "UNCALIBRATED";
  // ptp4l reports 0 for offset and path delay until its first complete measurement
  const measured = following && (s.last_sync_ingress || 0) > 0 && !(s.offset_ns === 0 && s.path_delay_ns === 0);
  $("p-offset").textContent = !following ? "-" : measured ? fmtNs(s.offset_ns) : "No measurement yet";
  $("p-gm").textContent = mode === "off" ? "" : p.error ? p.error
    : following && !measured ? "Hearing Announce from " + (s.gm_identity || "the grandmaster") + ", but no Sync and Delay measurement has completed. See the PTP log."
    : s.port_state === "LISTENING" ? `No Announce heard on domain ${ptpCfg.domain}. Nothing on the network is reaching this VM as a PTP grandmaster.`
    : s.gm_is_self && ["MASTER", "GRAND_MASTER"].includes(s.port_state) ? "This VM is the grandmaster, " + s.gm_identity
    : s.gm_is_self ? ""
    : s.gm_identity ? "Grandmaster " + s.gm_identity : "";
  $("p-delay").textContent = measured ? fmtNs(s.path_delay_ns) : "-";
  $("p-prio").textContent = s.gm_priority1 != null ? `${s.gm_priority1} / ${s.gm_priority2}` : "-";
  $("p-class").textContent = s.gm_class ?? "-";
  $("p-steps").textContent = s.steps_removed ?? "-";
  $("p-utc").textContent = s.utc_offset != null ? s.utc_offset + " s" + (s.ptp_timescale ? "" : ", arbitrary timescale") : "-";
  $("p-tai").textContent = p.kernel_tai != null ? p.kernel_tai + " s" : "-";
  $("p-gmtime").textContent = s.gm_time ? gmDate(s.gm_time) : "-";
  $("p-gmtime").classList.toggle("warn", !!(s.gm_time && p.boot_time && s.gm_time < p.boot_time));
  $("p-step").textContent = p.last_step
    ? `${p.last_step.seconds > 0 ? "+" : ""}${p.last_step.seconds} s at ${new Date(p.last_step.at * 1000).toLocaleTimeString()}`
    : "none";
  $("lamp-ptp").setAttribute("data-s", mode === "off" ? "unused" : p.error || s.port_state === "FAULTY" ? "exited"
    : ["SLAVE", "MASTER", "GRAND_MASTER"].includes(s.port_state) ? "running" : "restarting");
  lastPtpHist = p.history || [];
  if (activeTab === "audio") drawGraph(lastPtpHist);
}

function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
function drawGraph(hist) {
  const cv = $("p-graph");
  if (!cv.clientWidth) return;
  $("p-graph-empty").hidden = hist.length > 1;
  const w = cv.clientWidth, h = cv.clientHeight, dpr = window.devicePixelRatio || 1;
  if (cv.width !== w * dpr || cv.height !== h * dpr) { cv.width = w * dpr; cv.height = h * dpr; }
  const g = cv.getContext("2d");
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, w, h);
  if (hist.length < 2) return;
  const tEnd = hist[hist.length - 1][0], span = 120;
  const peak = Math.max(1000, ...hist.map(([, o]) => Math.abs(o)));
  // round the scale up to 1, 2 or 5 times a power of ten
  const mag = 10 ** Math.floor(Math.log10(peak));
  const lim = [1, 2, 5, 10].map((m) => m * mag).find((v) => v >= peak);
  const pad = 18, x = (t) => pad + (1 - (tEnd - t) / span) * (w - pad * 2), y = (o) => h / 2 - o / lim * (h / 2 - 10);
  g.font = "12px " + cssVar("--font");
  g.fillStyle = cssVar("--muted");
  g.strokeStyle = cssVar("--line");
  g.lineWidth = 1;
  for (const v of [lim, 0, -lim]) {
    g.beginPath(); g.moveTo(pad, y(v)); g.lineTo(w - pad, y(v)); g.stroke();
    g.fillText((v > 0 ? "+" : "") + fmtNs(v), pad + 4, y(v) + (v > 0 ? 13 : v < 0 ? -4 : -4));
  }
  g.fillText("last 2 min", w - pad - 60, h - 4);
  g.strokeStyle = cssVar("--cue");
  g.lineWidth = 1.5;
  g.beginPath();
  hist.forEach(([t, o], i) => { if (tEnd - t > span) return; i ? g.lineTo(x(t), y(o)) : g.moveTo(x(t), y(o)); });
  g.stroke();
}

/* ---------- routing ---------- */
const FLOW_LABEL = { A: "Flow A", B: "Flow B", audio: "Audio flow" };
let routing = { input: "ndi", flow_b: "local", send: [] };
let routePick = { input: "ndi", flow_b: "local" };
const RIN_HELP = {
  ndi: "The NDI ingest writes Flow A and the audio flow. Pick the source on the NDI tab.",
  st2110: "The ST 2110 ingest writes Flow A and the audio flow, from the SDPs on the ST 2110 tab.",
  peer: "Flow A and the audio flow arrive from the other lab over the fabric. Its routing must send them.",
  none: "No Flow A or audio on this lab. Useful when this lab only takes Flow B from the peer.",
};
const RB_HELP = {
  local: "Autoframe runs here, reading Flow A and writing Flow B.",
  peer: "Flow B arrives from the other lab over the fabric, which runs autoframe. Its routing must send Flow B.",
  off: "No Flow B on this lab.",
};

function localFlows(input, flowB) {
  return [...(["ndi", "st2110"].includes(input) ? ["A", "audio"] : []), ...(flowB === "local" ? ["B"] : [])];
}

function showRoutePick() {
  document.querySelectorAll("[data-rin]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.rin === routePick.input)));
  document.querySelectorAll("[data-rb]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.rb === routePick.flow_b)));
  $("rin-help").textContent = RIN_HELP[routePick.input];
  $("rb-help").textContent = RB_HELP[routePick.flow_b];
  const local = localFlows(routePick.input, routePick.flow_b);
  const checked = new Set([...document.querySelectorAll("[data-send]:checked")].map((c) => c.dataset.send));
  $("r-send").innerHTML = Object.entries(FLOW_LABEL).map(([k, label]) => {
    const ok = local.includes(k);
    return `<label class="check"><input type="checkbox" data-send="${k}" ${ok ? "" : "disabled"} ${ok && checked.has(k) ? "checked" : ""}> ${label}${ok ? "" : ", not made here"}</label>`;
  }).join("");
  const notes = [];
  if (routePick.input === "none" && routePick.flow_b === "local") notes.push("Autoframe needs Flow A. Choose an input, or take Flow B from the peer.");
  if ((routePick.input === "peer" || routePick.flow_b === "peer") && !fabricCfg.enabled) notes.push("This takes flows from the peer, but the fabric is off.");
  $("r-warn").hidden = !notes.length;
  $("r-warn").className = "note warn";
  $("r-warn").textContent = notes.join(" ");
}

async function loadRouting() {
  const r = await api("/api/routing");
  routing = r.settings;
  routePick = { input: routing.input, flow_b: routing.flow_b };
  $("r-ndi-a").checked = !!routing.ndi_out_original;
  $("r-ndi-b").checked = !!routing.ndi_out_vertical;
  $("r-send").innerHTML = Object.keys(FLOW_LABEL).map((k) => `<input type="checkbox" data-send="${k}" ${routing.send.includes(k) ? "checked" : ""} hidden>`).join("");
  showRoutePick();
  const from = { ndi: "NDI", st2110: "ST 2110", peer: "the peer", none: "nothing" }[routing.input];
  $("r-current").textContent = `In use: input from ${from}` + (routing.send.length ? `, sending ${routing.send.map((k) => FLOW_LABEL[k]).join(", ")}` : "");
}

document.querySelectorAll("[data-rin]").forEach((b) => b.addEventListener("click", () => { routePick.input = b.dataset.rin; showRoutePick(); }));
document.querySelectorAll("[data-rb]").forEach((b) => b.addEventListener("click", () => { routePick.flow_b = b.dataset.rb; showRoutePick(); }));

$("r-apply").addEventListener("click", async () => {
  const body = {
    input: routePick.input, flow_b: routePick.flow_b,
    ndi_out_original: $("r-ndi-a").checked, ndi_out_vertical: $("r-ndi-b").checked,
    send: [...document.querySelectorAll("[data-send]:checked")].map((c) => c.dataset.send),
  };
  try { await api("/api/routing/apply", { method: "POST", body: JSON.stringify(body) }); if (!tracking) trackJob(); }
  catch (e) { startFailed("routing-log", e.message); }
});

function showVia(r) {
  const peer = new Set([...(r.input === "peer" ? ["A", "audio"] : []), ...(r.flow_b === "peer" ? ["B"] : [])]);
  const send = new Set(r.send || []);
  for (const [k, id] of [["A", "fa-via"], ["B", "fb-via"], ["audio", "fx-via"]]) {
    $(id).textContent = peer.has(k) ? "from the peer" : send.has(k) ? "also to the peer" : "";
  }
}

/* ---------- ST 2110 ---------- */
let st2110Cfg = { out: {} };

function sdpSummary(id, sum) {
  const el = $(id);
  el.classList.toggle("bad", !!sum && !sum.ok);
  if (!sum) { el.textContent = ""; return; }
  if (!sum.ok) { el.textContent = sum.error; return; }
  const where = `${sum.address}:${sum.port}` + (sum.source ? ` from ${sum.source}` : "");
  el.textContent = sum.width
    ? `${sum.width}x${sum.height} @ ${sum.fps}, ${sum.depth} bit, ${where}`
    : `${sum.channels} ch ${sum.encoding}, ${where}`;
}

let sdpTimer = null;
function checkSdps() {
  clearTimeout(sdpTimer);
  sdpTimer = setTimeout(async () => {
    for (const [ta, out, media] of [["s-vsdp", "s-vsum", "video"], ["s-asdp", "s-asum", "audio"]]) {
      const text = $(ta).value;
      if (!text.trim()) { sdpSummary(out, null); continue; }
      try { sdpSummary(out, await api("/api/st2110/check", { method: "POST", body: JSON.stringify({ text, media }) })); }
      catch (e) { sdpSummary(out, { ok: false, error: e.message }); }
    }
  }, 300);
}
["s-vsdp", "s-asdp"].forEach((id) => $(id).addEventListener("input", checkSdps));

function buildOutputs() {
  $("s-outs").innerHTML = ["A", "B"].map((k) => {
    const o = st2110Cfg.out[k] || {};
    return `<div class="out2110">
      <label class="check"><input type="checkbox" id="so-${k}-on" ${o.enabled ? "checked" : ""}> Send ${FLOW_LABEL[k]}${k === "A" ? ", 16:9" : ", 9:16"}</label>
      <div class="grid-wide">
        <label class="field"><span>Multicast address</span><input id="so-${k}-addr" value="${esc(o.address)}" autocomplete="off" spellcheck="false"></label>
        <label class="field"><span>Port</span><input id="so-${k}-port" type="number" min="1024" max="65534" step="2" value="${esc(o.port)}"></label>
        <label class="field"><span>Payload type</span><input id="so-${k}-pt" type="number" min="96" max="127" value="${esc(o.payload_type)}"></label>
        <label class="field"><span>Session name</span><input id="so-${k}-name" maxlength="64" value="${esc(o.session_name)}" placeholder="MXL Lab ${FLOW_LABEL[k]}"></label>
      </div>
      <div class="row"><span class="saved num" id="so-${k}-live">-</span><a class="btn small" href="/api/st2110/sdp/${k}" download="mxl-lab-flow-${k}.sdp">Download SDP</a></div>
    </div>`;
  }).join("");
}

async function loadSt2110() {
  const r = await api("/api/st2110");
  st2110Cfg = r.settings;
  $("s-vsdp").value = st2110Cfg.in_video_sdp;
  $("s-asdp").value = st2110Cfg.in_audio_sdp;
  sdpSummary("s-vsum", r.video);
  sdpSummary("s-asum", r.audio);
  $("s-in-iface").innerHTML = ifaceOptions(r.ifaces, st2110Cfg.in_iface, r.default_iface, false);
  $("s-out-iface").innerHTML = ifaceOptions(r.ifaces, st2110Cfg.out_iface, r.default_iface, false);
  $("s-buf").value = st2110Cfg.in_buffer_mb;
  $("s-ttl").value = st2110Cfg.ttl;
  $("s-dscp").value = st2110Cfg.dscp;
  $("s-read").value = st2110Cfg.read_delay_ms;
  $("s-mtu").value = st2110Cfg.mtu;
  $("s-pace").checked = !!st2110Cfg.pace;
  buildOutputs();
  $("s-in-current").textContent = routing.input === "st2110" ? "In use as the input" : "Not the input. Choose ST 2110 on the Routing tab to use it.";
}

$("s-in-apply").addEventListener("click", async () => {
  const body = { in_video_sdp: $("s-vsdp").value, in_audio_sdp: $("s-asdp").value,
    in_iface: $("s-in-iface").value, in_buffer_mb: Number($("s-buf").value) };
  try { await api("/api/st2110/input", { method: "POST", body: JSON.stringify(body) }); if (!tracking) trackJob(); }
  catch (e) { startFailed("st2110in-log", e.message); }
});

$("s-out-apply").addEventListener("click", async () => {
  const out = {};
  for (const k of ["A", "B"]) {
    out[k] = { enabled: $(`so-${k}-on`).checked, address: $(`so-${k}-addr`).value.trim(), port: Number($(`so-${k}-port`).value),
      payload_type: Number($(`so-${k}-pt`).value), session_name: $(`so-${k}-name`).value.trim() };
  }
  const body = { out, out_iface: $("s-out-iface").value, ttl: Number($("s-ttl").value), dscp: Number($("s-dscp").value),
    read_delay_ms: Number($("s-read").value), mtu: Number($("s-mtu").value), pace: $("s-pace").checked };
  try { await api("/api/st2110/output", { method: "POST", body: JSON.stringify(body) }); if (!tracking) trackJob(); }
  catch (e) { startFailed("st2110-log", e.message); }
});

function showSt2110Status(st) {
  const on = Object.values(st2110Cfg.out || {}).some((o) => o.enabled);
  const live = st.age !== undefined && st.age < LIMITS.st2110StaleS;
  $("s-state").textContent = !on ? "Off" : !live ? "Not reporting" : st.state === "error" ? "Error: " + (st.error || "") : st.state || "-";
  $("s-src").textContent = live && st.src_ip ? `${st.src_ip} on ${st.iface}` : "-";
  $("s-paced").textContent = live ? (st.paced ? "fq" : "bursts") : "-";
  $("s-ref").textContent = live ? (st.gm ? "PTP " + st.gm : "local clock") : "-";
  for (const k of ["A", "B"]) {
    const el = $(`so-${k}-live`);
    if (!el) continue;
    const o = live && st.outputs ? st.outputs[k] : null;
    el.textContent = o ? `${o.format || "waiting"} to ${o.dest}, ${(o.kbps / 1e6).toFixed(2)} Gb/s` : "-";
  }
}

/* ---------- fabric ---------- */
let fabricCfg = {}, fabricProv = "verbs";
const FPROV_HELP = {
  verbs: "RDMA over Converged Ethernet with the NIC's verbs provider. Needs an RDMA NIC, such as a ConnectX passed through to this VM, on both labs.",
  tcp: "libfabric over plain TCP. Works on any NIC, including a virtual one. Use it to check the setup before RoCEv2, or to compare.",
};
const SESSION_STATE = { running: "Running", starting: "Starting", exited: "Stopped" };

function showFabricPick() {
  document.querySelectorAll("[data-fprov]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.fprov === fabricProv)));
  $("fprov-help").textContent = FPROV_HELP[fabricProv];
}
document.querySelectorAll("[data-fprov]").forEach((b) => b.addEventListener("click", () => { fabricProv = b.dataset.fprov; showFabricPick(); }));

async function loadFabric() {
  const r = await api("/api/fabric");
  fabricCfg = r.settings;
  fabricProv = fabricCfg.provider;
  $("f-enabled").checked = !!fabricCfg.enabled;
  $("f-iface").innerHTML = ifaceOptions(r.ifaces, fabricCfg.iface, r.default_iface, false);
  $("f-port").value = fabricCfg.base_port;
  $("f-peer-url").value = fabricCfg.peer_url;
  $("f-peer-user").value = fabricCfg.peer_user;
  $("f-peer-pass").placeholder = fabricCfg.has_password ? "unchanged" : "none";
  $("f-ifaces").textContent = (r.runner || {}).interfaces || "Shown once the fabric is on.";
  showFabricPick();
  showRoutePick();
}

$("f-apply").addEventListener("click", async () => {
  const body = { enabled: $("f-enabled").checked, provider: fabricProv, iface: $("f-iface").value,
    base_port: Number($("f-port").value), peer_url: $("f-peer-url").value.trim(),
    peer_user: $("f-peer-user").value.trim(), peer_password: $("f-peer-pass").value };
  try { await api("/api/fabric/apply", { method: "POST", body: JSON.stringify(body) }); $("f-peer-pass").value = ""; if (!tracking) trackJob(); }
  catch (e) { startFailed("fabric-log", e.message); }
});
$("f-test").addEventListener("click", async () => {
  const box = $("fabric-log");
  box.hidden = false;
  try {
    const p = await api("/api/fabric/test", { method: "POST" });
    box.className = "joblog " + (p.ok ? "ok" : "failed");
    box.textContent = p.ok
      ? `Peer ${p.info.host} answers. Fabric ${p.info.enabled ? "on, " + p.info.provider + " at " + (p.info.node || "no address") : "off"}. `
        + `It sends ${p.info.sends.map((k) => FLOW_LABEL[k]).join(", ") || "nothing"} and expects ${p.info.expects.map((k) => FLOW_LABEL[k]).join(", ") || "nothing"}.`
      : p.error;
  } catch (e) { box.className = "joblog failed"; box.textContent = e.message; }
});
$("f-repair").addEventListener("click", async () => {
  try { await api("/api/fabric/repair", { method: "POST" }); } catch (e) { startFailed("fabric-log", e.message); }
});

function peerMismatch(f) {
  const info = (f.peer || {}).info;
  if (!info) return [];
  const out = [];
  const expects = new Set(info.expects), sends = new Set(info.sends);
  for (const k of routing.send || []) if (!expects.has(k)) out.push(`This lab sends ${FLOW_LABEL[k]}, but the peer does not expect it.`);
  const mine = [...(routing.input === "peer" ? ["A", "audio"] : []), ...(routing.flow_b === "peer" ? ["B"] : [])];
  for (const k of mine) if (!sends.has(k)) out.push(`This lab expects ${FLOW_LABEL[k]} from the peer, but the peer does not send it.`);
  return out;
}

function showFabricStatus(f) {
  const peer = f.peer || {};
  $("f-state").textContent = !f.enabled ? "Off" : f.state === "on" ? `On, ${f.provider}` : f.state || "Not reporting";
  $("f-node").textContent = f.node || "-";
  $("f-peer").textContent = peer.ok ? `${peer.info.host}, ${peer.info.node || "no fabric address"}` : peer.ok === false ? "Not answering" : "-";
  const rows = Object.values(f.sessions || {});
  $("f-sessions").innerHTML = `<tr><th>Flow</th><th>Direction</th><th>State</th><th>Rate</th><th>Starts</th></tr>` + (rows.length
    ? rows.map((s) => `<tr><td>${FLOW_LABEL[s.key]}</td><td>${s.role === "tx" ? "To the peer" : "From the peer"}</td>`
      + `<td>${SESSION_STATE[s.state] || esc(s.state)}</td><td>${s.rate == null ? "-" : s.rate.toFixed(1) + " /s"}</td><td>${s.starts}</td></tr>`).join("")
    : `<tr class="empty"><td colspan="5">No flows on the fabric</td></tr>`);
  if (activeTab === "routing") {
    api("/api/fabric").then((r) => {
      const box = $("f-log");
      box.textContent = (r.log || []).join("\n") || "Nothing yet.";
      box.scrollTop = box.scrollHeight;
      if ((r.runner || {}).interfaces) $("f-ifaces").textContent = r.runner.interfaces;
    }).catch(() => {});
  }
}

/* ---------- tabs ---------- */
const TABS = ["status", "ndi", "framing", "audio", "st2110", "routing"];
let activeTab = "status";
let lastPtpHist = [];

function showTab(name, focus = false) {
  if (!TABS.includes(name)) name = "status";
  activeTab = name;
  for (const t of TABS) {
    const on = t === name;
    $("t-" + t).setAttribute("aria-selected", String(on));
    $("t-" + t).tabIndex = on ? 0 : -1;
    $("tab-" + t).hidden = !on;
  }
  if (focus) $("t-" + name).focus();
  if (location.hash.slice(1) !== name) history.replaceState(null, "", "#" + name);
  syncLevelStream();
  if (name === "audio") requestAnimationFrame(() => drawGraph(lastPtpHist));
  if (name === "ndi") loadSources();
  if (name === "framing") drawWindow();
}
document.querySelectorAll("[data-tab]").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
document.querySelector(".tabs").addEventListener("keydown", (e) => {
  const i = TABS.indexOf(activeTab);
  const next = { ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: TABS.length - 1 }[e.key];
  if (next === undefined) return;
  e.preventDefault();
  showTab(TABS[(next + TABS.length) % TABS.length], true);
});
window.addEventListener("hashchange", () => showTab(location.hash.slice(1)));

/* ---------- status overview ---------- */
const RANK = { off: 0, ok: 1, busy: 2, warn: 3, fault: 4 };
const worse = (a, b) => (RANK[b] > RANK[a] ? b : a);
const AREA_TAB = { ndi: "ndi", framing: "framing", audio: "audio", clock: "audio", st2110: "st2110", fabric: "routing", general: "status" };
const MEDIA = ["ndi-ingest", "autoframe", "ndi-out-original", "ndi-out-vertical"];
const MODE_LABEL = { weighted: "Follow group", largest: "Follow nearest", manual: "Manual", hold: "Hold" };

function fnText(svc, c) {
  const up = c.status === "running" ? "Running " + ago(c.started)
    : c.status === "exited" ? "Stopped, exit code " + c.exit_code
    : c.status === "unused" ? (svc === "ndi-discovery" ? "Not used with the current discovery setting" : "Turned off")
    : c.status;
  return up + (c.restarts ? `, ${c.restarts} restarts` : "");
}

function findProblems() {
  const out = [];
  const add = (lvl, area, text) => out.push({ lvl, area, text });
  const cs = status.containers || {};
  const running = (svc) => (cs[svc] || {}).status === "running";
  const flows = status.flows || {};
  const af = status.autoframe || {};
  const a = status.audio || {};
  const p = status.ptp || {}, ps = p.summary || {};
  const mode = status.ptp_mode;

  if (status.docker_error) add("fault", "general", "Cannot reach Docker: " + status.docker_error);

  const j = status.job || {};
  if (j.running) {
    const area = { pipeline: "ndi", discovery: "ndi", audio: "audio", ptp: "clock", st2110in: "st2110", st2110: "st2110", routing: "fabric", fabric: "fabric" }[j.kind] || "general";
    add("busy", area, "Applying changes: " + ((j.log || []).slice(-1)[0] || "starting").replace(/^\S+\s+/, ""));
  }

  // Media chain, as the routing sets it up
  const r = status.routing || routing;
  const expected = new Set([...(["ndi", "st2110"].includes(r.input) ? ["ndi-ingest"] : []),
    ...(r.flow_b === "local" ? ["autoframe"] : []), ...(r.ndi_out_original ? ["ndi-out-original"] : []), ...(r.ndi_out_vertical ? ["ndi-out-vertical"] : [])]);
  if (r.input === "ndi" && !pipeline.NDI_SRC) add("warn", "ndi", "No NDI source selected. Pick one on the NDI tab.");
  else if (r.input === "st2110" && !(st2110Cfg.in_video_sdp || "").trim()) add("warn", "st2110", "The input is ST 2110 but there is no video SDP. Paste it on the ST 2110 tab.");
  else if (!j.running) {
    for (const svc of MEDIA) {
      const c = cs[svc];
      if (!expected.has(svc)) continue;
      if (c && c.status !== "running" && c.status !== "unknown")
        add(["restarting", "created"].includes(c.status) ? "warn" : "fault", svc === "ndi-ingest" ? "ndi" : "framing",
          `${FN_LABEL[svc]}: ${fnText(svc, c)}`);
    }
    if (expected.has("ndi-ingest") && running("ndi-ingest") && flows.A && !flows.A.exists)
      add("fault", r.input === "st2110" ? "st2110" : "ndi", "Flow A is not present, so ingest is not receiving video.");
    if (r.input === "peer" && flows.A && !flows.A.exists) add("fault", "fabric", "Flow A has not arrived from the peer.");
    if (r.flow_b === "peer" && flows.B && !flows.B.exists) add("warn", "fabric", "Flow B has not arrived from the peer.");
    if (running("autoframe") && flows.B && !flows.B.exists) add("fault", "framing", "Flow B is not present, so the vertical output has no picture.");
    if (running("autoframe") && !(af.age < LIMITS.autoframeStaleS)) add("warn", "framing", "Autoframe is running but has not reported recently.");
    const fps = af.fps;
    if (fps && flows.A && flows.A.rate != null && Math.abs(flows.A.rate - fps) > Math.max(1, fps * LIMITS.frameRateTolerance))
      add("warn", "ndi", `Flow A runs at ${flows.A.rate} /s, not the expected ${fps}.`);
  }
  if (disc.mode === "local" && !running("ndi-discovery") && !j.running)
    add("fault", "ndi", "The internal discovery server is selected but not running.");

  // AES67
  if (audioCfg.enabled && !j.running) {
    const live = a.age !== undefined && a.age < LIMITS.audioStaleS;
    if (!running("aes67-out")) add("fault", "audio", "AES67 output is turned on but its function is not running.");
    else if (!live) add("fault", "audio", "AES67 output is not reporting.");
    else if (a.state === "error") add("fault", "audio", "AES67 output error: " + (a.error || "unknown"));
    else if (a.state === "waiting") add("warn", "audio", "AES67 output is waiting for the audio flow. The NDI source may have no audio.");
    if (live && a.arrival_ms != null && a.read_delay_ms != null && a.arrival_ms > a.read_delay_ms)
      add("warn", "audio", `Audio reaches the sender ${a.arrival_ms} ms after its timestamp, above the ${a.read_delay_ms} ms read delay. Packets will be late.`);
    if (live && a.latency_ms != null && a.latency_ms > LIMITS.sendLagWarnMs)
      add("warn", "audio", `Packets leave ${a.latency_ms} ms after their timestamp. Receivers need more latency than that.`);
    if (mode === "off") add("warn", "clock", "AES67 output is on but PTP is off, so PTP-locked receivers will drift against it.");
  }

  // ST 2110 out
  const st = status.st2110 || {};
  if (Object.values(st2110Cfg.out || {}).some((o) => o.enabled) && !j.running) {
    if (!running("st2110-out")) add("fault", "st2110", "ST 2110 video output is turned on but its function is not running.");
    else if (!(st.age < LIMITS.st2110StaleS)) add("fault", "st2110", "ST 2110 video output is not reporting.");
    else if (st.state === "error") add("fault", "st2110", "ST 2110 output error: " + (st.error || "unknown"));
    else if (st.state === "waiting") add("warn", "st2110", "ST 2110 output is waiting for its flow.");
    if (mode === "off") add("warn", "clock", "ST 2110 output is on but PTP is off, so receivers cannot lock to it.");
  }

  // Fabric
  const f = status.fabric || {};
  const wantsFabric = (r.send || []).length || r.input === "peer" || r.flow_b === "peer";
  if (wantsFabric && !f.enabled) add("warn", "fabric", "The routing sends or receives flows over the fabric, but the fabric is off.");
  else if (f.enabled && !j.running) {
    if (!running("fabric")) add("fault", "fabric", "The fabric is on but its function is not running.");
    else if (!(f.age < LIMITS.fabricStaleS)) add("fault", "fabric", "The fabric function is not reporting.");
    else if (f.state !== "on") add("fault", "fabric", "Fabric: " + f.state);
    if (f.peer && f.peer.ok === false) add("fault", "fabric", "The peer lab is not answering: " + f.peer.error);
    for (const s of Object.values(f.sessions || {}))
      if (s.state === "exited") add("warn", "fabric", `${FLOW_LABEL[s.key]} ${s.role === "tx" ? "to" : "from"} the peer stopped. It pairs again by itself.`);
    for (const m of peerMismatch(f)) add("warn", "fabric", m);
    if (wantsFabric && mode === "off") add("warn", "clock", "Flows cross the fabric with the sender's timestamps, so both labs need PTP.");
  }

  // PTP
  if (mode && mode !== "off" && !(j.running && j.kind === "ptp")) {
    if (p.error) add("fault", "clock", "PTP: " + p.error);
    else if (ps.gm_time && p.boot_time && ps.gm_time < p.boot_time)
      add("fault", "clock", `The grandmaster's clock reads ${gmDate(ps.gm_time)}, earlier than this VM booted `
        + `(${gmDate(p.boot_time)}). Linux will not set its clock to a time before boot, so the VM cannot follow it. `
        + `Give the grandmaster a real time, or let this VM lead: see the README under "Grandmaster time before boot".`);
    else if (ps.port_state === "FAULTY") add("fault", "clock", "PTP port is in a fault state. Check the PTP log.");
    else if (!["SLAVE", "MASTER", "GRAND_MASTER"].includes(ps.port_state))
      add("warn", "clock", "PTP is " + (STATE_TEXT[ps.port_state] || ps.port_state || "starting").toLowerCase() + ".");
    else if (ps.port_state === "SLAVE" && ps.offset_ns != null && Math.abs(ps.offset_ns) > LIMITS.ptpOffsetFaultNs)
      add("fault", "clock", `The VM clock is ${fmtNs(ps.offset_ns)} from the grandmaster. ptp4l should step it within a few seconds; if it stays, click Apply timing.`);
    else if (ps.port_state === "SLAVE" && ps.offset_ns != null && Math.abs(ps.offset_ns) > LIMITS.ptpOffsetWarnNs)
      add("warn", "clock", `PTP offset is ${fmtNs(ps.offset_ns)}, more than 1 ms.`);
  }
  return out;
}

function renderAttention(items) {
  const order = [...items].sort((x, y) => RANK[y.lvl] - RANK[x.lvl]);
  $("attn").innerHTML = order.length
    ? order.map((i) => {
        const tab = AREA_TAB[i.area];
        const link = tab !== "status" ? `<a href="#${tab}">Open</a>` : "";
        return `<li data-l="${i.lvl}"><span>${esc(i.text)}</span>${link}</li>`;
      }).join("")
    : `<li data-l="ok">Everything that is turned on is running normally.</li>`;
}

function areaHealth(items, areas, baseOk) {
  let h = baseOk ? "ok" : "off";
  for (const i of items) if (areas.includes(i.area)) h = worse(h, i.lvl);
  return h;
}

function updateOverview() {
  const items = findProblems();
  renderAttention(items);
  const cs = status.containers || {};
  const running = (svc) => (cs[svc] || {}).status === "running";
  const mode = status.ptp_mode;
  const ps = (status.ptp || {}).summary || {};

  const h = {
    ndi: areaHealth(items, ["ndi"], running("ndi-ingest")),
    framing: areaHealth(items, ["framing"], running("autoframe")),
    audio: areaHealth(items, ["audio"], audioCfg.enabled && running("aes67-out")),
    clock: areaHealth(items, ["clock"], mode && mode !== "off" && ["SLAVE", "MASTER", "GRAND_MASTER"].includes(ps.port_state)),
    st2110: areaHealth(items, ["st2110"], running("st2110-out") || (status.routing || {}).input === "st2110"),
    fabric: areaHealth(items, ["fabric"], (status.fabric || {}).enabled && running("fabric")),
  };
  const all = items.reduce((acc, i) => worse(acc, i.lvl), Object.values(h).some((v) => v === "ok") ? "ok" : "off");
  const set = (id, v) => $(id).setAttribute("data-h", v);
  set("dot-status", all);
  set("dot-ndi", h.ndi); set("dot-framing", h.framing);
  set("dot-audio", worse(h.audio, h.clock));
  set("dot-st2110", h.st2110); set("dot-routing", h.fabric);
  set("dot-ov-ndi", h.ndi); set("dot-ov-framing", h.framing);
  set("dot-ov-audio", h.audio); set("dot-ov-clock", h.clock);

  // NDI card
  const rin = (status.routing || routing).input;
  $("ov-src").textContent = rin === "st2110" ? "ST 2110 " + ($("s-vsum").textContent || "")
    : rin === "peer" ? "From the peer lab" : rin === "none" ? "No input on this lab"
    : pipeline.NDI_SRC ? pipeline.NDI_SRC + (pipeline.NDI_URL ? " at " + pipeline.NDI_URL : "") : "None selected";
  $("ov-fmt").textContent = pipeline.SRC_W ? `${pipeline.SRC_W}x${pipeline.SRC_H} at ${fpsLabel(pipeline.SRC_FPS)}` + (pipeline.SRC_AUTO ? ", auto" : "") : "-";
  $("ov-disc").textContent = { mdns: "mDNS", local: "Internal server", external: "External server " + (disc.servers || "") }[disc.mode] || "-";
  $("ov-ingest").textContent = cs["ndi-ingest"] ? fnText("ndi-ingest", cs["ndi-ingest"]) : "-";

  // Framing card
  const af = status.autoframe || {};
  const live = af.age !== undefined && af.age < LIMITS.autoframeStaleS;
  $("ov-mode").textContent = MODE_LABEL[tuning.mode] || "-";
  $("ov-faces").textContent = live ? af.faces : "-";
  $("ov-seen").textContent = $("r-seen").textContent;
  $("ov-out").textContent = $("r-out").textContent;

  // Audio card, from the values the AES67 panel already worked out
  $("ov-astate").textContent = $("a-state").textContent;
  $("ov-adest").textContent = $("a-dest").textContent;
  $("ov-alat").textContent = $("a-lat").textContent;
  const a = status.audio || {};
  if (!(a.age < LIMITS.audioStaleS && a.state === "sending")) { meterIn = { rms: [], peak: [], at: 0 }; }

  // Clock card
  $("ov-pstate").textContent = $("p-state").textContent;
  $("ov-poff").textContent = $("p-offset").textContent;
  $("ov-pgm").textContent = mode === "off" ? "-" : ps.gm_is_self && ["MASTER", "GRAND_MASTER"].includes(ps.port_state) ? "This VM" : ps.gm_identity || "-";
  $("ov-ptai").textContent = $("p-tai").textContent;
}

/* ---------- start ---------- */
(async () => {
  buildControls();
  buildFunctions();
  showTab(location.hash.slice(1));
  tuning = (await api("/api/tuning")).values;
  showValues();
  await loadPipeline();
  await loadDiscovery();
  await loadPtp().catch((e) => { $("p-current").textContent = e.message; });
  await loadAudio().catch((e) => startFailed("audio-log", e.message));
  await loadRouting().catch((e) => startFailed("routing-log", e.message));
  await loadSt2110().catch((e) => startFailed("st2110in-log", e.message));
  await loadFabric().catch((e) => startFailed("fabric-log", e.message));
  poll();
  setInterval(poll, 1000);
  previewLoop();
  setInterval(() => { if (activeTab === "ndi") loadSources(); }, 5000);
})();
