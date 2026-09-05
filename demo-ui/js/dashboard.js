/* Morghdari Live Demo Dashboard — vanilla JS, zero dependencies.
   Polls the REAL demo endpoints (JWT) and renders live SVG charts. */

"use strict";

const $ = (id) => document.getElementById(id);
const API = "/api/v1";
const DEMO = "/demo/api";
const POLL_MS = 5000;
const WINDOW_MINUTES = 30;   // rolling chart window
const POINTS_MAX = 36;       // tail of points plotted

let session = null;          // {access, refresh, profile}

/* ── i18n ───────────────────────────────────────────────── */

const I18N = {
  "kpi-saloons": { en: "Saloons", fa: "سالن‌ها" },
  "kpi-slots": { en: "Slots online", fa: "حسگرهای آنلاین" },
  "kpi-readings": { en: "Readings (2 min)", fa: "خوانش‌ها (۲ دقیقه)" },
  "kpi-alerts": { en: "Open alerts", fa: "هشدارهای فعال" },
};

let currentLang = localStorage.getItem("morghdari-lang") || "en";

function applyLang(lang) {
  currentLang = lang;
  localStorage.setItem("morghdari-lang", lang);
  document.documentElement.lang = lang === "fa" ? "fa" : "en";
  document.documentElement.dir = lang === "fa" ? "rtl" : "ltr";
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    const entry = I18N[el.getAttribute("data-i18n")];
    if (entry && entry[lang] !== undefined) el.textContent = entry[lang];
  });
  const btn = $("lang-toggle");
  if (btn) btn.textContent = lang === "en" ? "فارسی" : "English";
}

/* ── fetch helpers ──────────────────────────────────────── */

function esc(value) {
  return String(value).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

async function api(method, path, { body, token } = {}) {
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (token) headers["Authorization"] = `Bearer ${token}`;
  try {
    const response = await fetch(`${API}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await response.json().catch(() => null);
    return { status: response.status, ok: response.ok, data };
  } catch (error) {
    return { status: 0, ok: false, data: { error: String(error) } };
  }
}

async function fetchJSON(path, token) {
  try {
    const response = await fetch(path, {
      headers: { Authorization: `Bearer ${token}` },
    });
    const data = await response.json().catch(() => null);
    return { status: response.status, ok: response.ok, data };
  } catch (error) {
    return { status: 0, ok: false, data: { error: String(error) } };
  }
}

/* ── session ────────────────────────────────────────────── */

function saveSession() {
  if (session) localStorage.setItem("morghdari-dash", JSON.stringify(session));
  else localStorage.removeItem("morghdari-dash");
}

function loadSession() {
  try {
    const raw = localStorage.getItem("morghdari-dash");
    session = raw ? JSON.parse(raw) : null;
  } catch {
    session = null;
  }
  return session;
}

function signOut() {
  session = null;
  saveSession();
  $("sec-login").hidden = false;
  $("sec-dashboard").hidden = true;
  $("org-label").textContent = "—";
  $("conn-chip").textContent = "not signed in";
  $("conn-chip").className = "chip chip-muted";
}

async function doLogin(event) {
  event.preventDefault();
  $("login-error").hidden = true;
  const result = await api("POST", "/auth/login/", {
    body: {
      username: $("dash-user").value.trim(),
      password: $("dash-pass").value,
    },
  });
  if (!result.ok) {
    $("login-error").hidden = false;
    $("login-error").textContent =
      `login failed: ${result.data?.detail || result.data?.error || result.status}`;
    return;
  }
  session = {
    access: result.data.access_token,
    refresh: result.data.refresh_token,
    profile: result.data.user,
  };
  saveSession();
  start();
}

/* ── SVG charts ─────────────────────────────────────────── */

function chartSvg(points, { color, threshold } = {}) {
  const W = 560, H = 110, PAD = 10;
  const values = points
    .map((p) => p.y)
    .filter((v) => v !== null && v !== undefined);
  if (values.length === 0) {
    return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="no data yet"></svg>`;
  }
  let min = Math.min(...values);
  let max = Math.max(...values);
  const span = max - min || 1;
  min -= span * 0.2;
  max += span * 0.2;
  if (threshold !== undefined) {
    if (threshold < min) min = threshold - span * 0.1;
    if (threshold > max) max = threshold + span * 0.1;
  }
  const x = (i) => PAD + (i / Math.max(points.length - 1, 1)) * (W - 2 * PAD);
  const y = (v) => H - PAD - ((v - min) / (max - min)) * (H - 2 * PAD);
  const path = points
    .map((p, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${y(p.y).toFixed(1)}`)
    .join(" ");

  const grid = [];
  for (let g = 0; g <= 3; g++) {
    const gy = PAD + (g / 3) * (H - 2 * PAD);
    const gv = max - (g / 3) * (max - min);
    grid.push(
      `<line class="grid-line" x1="${PAD}" y1="${gy.toFixed(1)}" x2="${W - PAD}" y2="${gy.toFixed(1)}"/>` +
        `<text class="axis-label" x="${W - PAD - 2}" y="${(gy - 3).toFixed(1)}" text-anchor="end">${gv.toFixed(1)}</text>`
    );
  }
  let thresholdLine = "";
  if (threshold !== undefined) {
    thresholdLine = `<line class="threshold" x1="${PAD}" y1="${y(threshold).toFixed(1)}" x2="${W - PAD}" y2="${y(threshold).toFixed(1)}"/>`;
  }
  return (
    `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="line chart">` +
    grid.join("") +
    `<polyline points="${path}" class="line-${color}"/>` +
    thresholdLine +
    `</svg>`
  );
}

function seriesFromRows(rows, metricKey) {
  return rows
    .slice(-POINTS_MAX)
    .map((r) => ({ y: r.metrics?.[metricKey]?.avg ?? null, t: r.device_ts }));
}

/* ── rendering ──────────────────────────────────────────── */

function renderSaloonCard(saloon) {
  const cardId = `saloon-${saloon.id}`;
  let card = document.getElementById(cardId);
  if (!card) {
    card = document.createElement("section");
    card.className = "saloon-card";
    card.id = cardId;
    card.innerHTML =
      `<div class="saloon-head">
         <h3 class="saloon-name"></h3>
         <span class="saloon-meta"></span>
       </div>
       <div class="pack-tiles"></div>
       <div class="chart chart-temp"></div>
       <div class="chart chart-nh3"></div>`;
    $("saloon-cards").appendChild(card);
  }
  card.querySelector(".saloon-name").textContent = saloon.name;
  card.querySelector(".saloon-meta").textContent = `saloon #${saloon.id}`;
  card.querySelector(".pack-tiles").innerHTML = saloon.slots
    .map((slot) => {
      const metrics = slot.last_metrics || {};
      const temp = metrics.temp?.avg;
      const nh3 = metrics.nh3?.avg;
      const cls = nh3 >= 25 ? "danger" : nh3 >= 20 ? "warn" : "";
      const led = slot.online ? "on" : "stale";
      return (
        `<div class="pack-tile">
           <div class="tile-top">
             <span>${esc(slot.pack_id || slot.slot_id)}</span>
             <span class="led ${led}" title="online"></span>
           </div>
           <div class="tile-val ${cls}">${temp != null ? temp.toFixed(1) : "—"}°C</div>
           <div class="tile-sub">NH₃ ${nh3 != null ? nh3.toFixed(1) : "—"} ppm</div>
         </div>`
      );
    })
    .join("");
}

function renderCharts(saloonId, tempRows, nh3Rows) {
  const card = document.getElementById(`saloon-${saloonId}`);
  if (!card) return;
  const tempSeries = seriesFromRows(tempRows, "temp");
  const nh3Series = seriesFromRows(nh3Rows, "nh3");
  card.querySelector(".chart-temp").innerHTML =
    `<div class="chart-title"><span>Temp °C</span></div>` +
    chartSvg(tempSeries, { color: "temp", threshold: 30 });
  card.querySelector(".chart-nh3").innerHTML =
    `<div class="chart-title"><span>NH₃ ppm</span></div>` +
    chartSvg(nh3Series, { color: "nh3", threshold: 25 });
}

function renderAlerts(alerts) {
  const list = $("alert-list");
  if (!alerts.length) {
    list.innerHTML = `<span class="hint">no alerts yet — the feed powers them up</span>`;
    return;
  }
  list.innerHTML = alerts
    .map((a) => {
      const tier = a.highest_tier_reached || 1;
      const badge = a.resolved_at
        ? `<span class="badge resolved">resolved</span>`
        : `<span class="badge tier-${tier}">tier ${tier}</span>`;
      return (
        `<div class="alert-row ${a.resolved_at ? "resolved" : `tier-${tier}`}">
           ${badge}
           <span>${esc(a.metric_type)}</span>
           <span>saloon ${a.saloon_id}</span>
           <span class="hint">${a.current_value != null ? `${a.current_value} ppm` : ""}</span>
           <span class="hint">${esc(String(a.triggered_at || "").slice(0, 19).replace("T", " "))}</span>
         </div>`
      );
    })
    .join("");
}

function renderPipeline(data) {
  const grid = $("pipeline-grid");
  const streams = data.streams || {};
  const groups = data.groups || {};
  const items = [
    ["realtime", streams.realtime ?? "—"],
    ["historical", streams.historical ?? "—"],
    ["dead letter", streams.dead_letter ?? "—"],
    ["realtime lag", groups.realtime?.lag ?? "—"],
    ["hist lag", groups.historical?.lag ?? "—"],
  ];
  grid.innerHTML = items
    .map(
      ([name, val]) =>
        `<div class="pipe-item"><span class="pipe-name">${esc(name)}</span><span class="pipe-val">${esc(val)}</span></div>`
    )
    .join("");
}

/* ── data loading ───────────────────────────────────────── */

const telemetryCache = {};

async function refresh() {
  if (!session) return;
  const farm = await fetchJSON(`${DEMO}/farm/`, session.access);
  if (farm.status === 401) {
    signOut();
    return;
  }
  if (!farm.ok || !farm.data) return;

  const now = new Date();
  const since = new Date(now.getTime() - WINDOW_MINUTES * 60_000)
    .toISOString()
    .slice(0, 19);
  const until = new Date(now.getTime() + 60_000).toISOString().slice(0, 19);
  const twoMinAgo = new Date(now.getTime() - 120_000).toISOString();

  let slotsTotal = 0;
  let slotsOnline = 0;
  let readings2m = 0;

  for (const saloon of farm.data.saloons) {
    slotsTotal += saloon.slots.length;
    slotsOnline += saloon.slots.filter((s) => s.online).length;
    renderSaloonCard(saloon);

    const telemetry = await api(
      "GET",
      `/saloons/${saloon.id}/telemetry/?since=${encodeURIComponent(since)}&until=${encodeURIComponent(until)}&include_buffered=true`,
      { token: session.access }
    );
    if (telemetry.ok && telemetry.data) {
      const rows = telemetry.data.results || [];
      telemetryCache[saloon.id] = rows;
      readings2m += rows.filter((r) => r.server_received_ts >= twoMinAgo).length;
      renderCharts(saloon.id, rows, rows);
    }
  }

  $("kpi-saloons").textContent = farm.data.saloons.length;
  $("kpi-slots").textContent = `${slotsOnline}/${slotsTotal}`;
  $("kpi-readings").textContent = readings2m;

  const alerts = await fetchJSON(`${DEMO}/api/alerts/overview/?limit=8`, session.access);
  let openAlerts = 0;
  if (alerts.ok && alerts.data && Array.isArray(alerts.data.alerts)) {
    openAlerts = alerts.data.alerts.filter((a) => !a.resolved_at).length;
    renderAlerts(alerts.data.alerts);
  }
  $("kpi-alerts").textContent = openAlerts;

  const pipeline = await fetchJSON(`${DEMO}/api/pipeline/`, session.access);
  if (pipeline.ok && pipeline.data) renderPipeline(pipeline.data);

  $("last-update").textContent = `updated ${now.toLocaleTimeString()}`;
}

/* ─ wiring ─────────────────────────────────────────────────── */

let timer = null;

function start() {
  $("sec-login").hidden = true;
  $("sec-dashboard").hidden = false;
  $("conn-chip").textContent = `session: ${session.profile?.username ?? "?"}`;
  $("conn-chip").className = "chip chip-ok";
  $("org-label").textContent = session.profile?.org_name ?? "Demo Farm";
  refresh();
  if (timer) clearInterval(timer);
  timer = setInterval(() => {
    if (!document.hidden) refresh();
  }, POLL_MS);
}

document.addEventListener("DOMContentLoaded", () => {
  applyLang(currentLang);
  $("lang-toggle").addEventListener("click", () =>
    applyLang(currentLang === "en" ? "fa" : "en")
  );
  $("login-form").addEventListener("submit", doLogin);
  if (loadSession()) start();
  else signOut();
});
