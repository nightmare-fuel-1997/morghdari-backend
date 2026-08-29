/* Morghdari v0.2 Demo Console — vanilla JS, zero dependencies.
   Talks to the same-origin backend (/api/v1/...) served by Django.
   فارسی: اسکریپت دمو — بدون هیچ وابستگی؛ همهٔ درخواست‌ها هم‌مبدأ هستند. */

"use strict";

const $ = (id) => document.getElementById(id);
const API = "/api/v1";
const SLOTS = { A: null, B: null }; // {access, refresh, profile}

/* ── tiny helpers ─────────────────────────────────────────── */

function b64urlDecode(segment) {
  const pad = segment.length % 4 ? "=".repeat(4 - (segment.length % 4)) : "";
  const b64 = segment.replace(/-/g, "+").replace(/_/g, "/") + pad;
  const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
  return JSON.parse(new TextDecoder().decode(bytes));
}

function decodeJwt(token) {
  try {
    return b64urlDecode(token.split(".")[1]);
  } catch {
    return {};
  }
}

function esc(value) {
  return String(value).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

function statusClass(status) {
  if (status >= 500) return "st-5xx";
  if (status === 401 || status === 403) return "st-401";
  if (status >= 400) return "st-4xx";
  return "st-2xx";
}

function logEvent(method, path, status, ms, note = "") {
  const row = document.createElement("div");
  row.className = "row";
  const time = new Date().toLocaleTimeString();
  row.innerHTML =
    `<time>${esc(time)}</time>` +
    `<span class="chip ${statusClass(status)}">${status}</span>` +
    `<span>${esc(method)} ${esc(path)}</span>` +
    `<span>${ms}ms</span>` +
    (note ? `<span>${esc(note)}</span>` : "");
  $("event-log").prepend(row);
}

async function api(method, path, { body, token } = {}) {
  const started = performance.now();
  const headers = {};
  if (body !== undefined) headers["Content-Type"] = "application/json";
  if (token) headers["Authorization"] = `Bearer ${token}`;
  let status = 0;
  let data = null;
  try {
    const response = await fetch(`${API}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    status = response.status;
    data = await response.json().catch(() => null);
    return { status, ok: response.ok, data };
  } catch (error) {
    return { status: 0, ok: false, data: { error: String(error) } };
  } finally {
    logEvent(method, path, status, Math.round(performance.now() - started));
  }
}

function busy(button, on) {
  button.disabled = on;
}

function show(id, text) {
  const node = $(id);
  node.hidden = false;
  node.textContent = typeof text === "string" ? text : JSON.stringify(text, null, 2);
}

function roleBadges(roles = []) {
  return roles
    .map((role) => `<span class="badge ${esc(role)}">${esc(role)}</span>`)
    .join(" ");
}

function renderIdentity(slot) {
  const session = SLOTS[slot];
  const box = $(`identity-${slot.toLowerCase()}`);
  const claimsBox = $(slot === "A" ? "claims-a" : "identity-b"); // B reuses identity box for claims
  if (!session) {
    box.hidden = true;
    return;
  }
  const profile = session.profile || {};
  const claims = decodeJwt(session.access);
  box.hidden = false;
  box.innerHTML =
    `<strong>Session ${esc(slot)}</strong> → ` +
    `${esc(profile.username ?? "?")} ${roleBadges(profile.roles)}` +
    (profile.is_platform_staff ? ' <span class="badge staff">platform_staff</span>' : "") +
    `<br><span class="hint">org: ${esc(profile.org_name ?? "— (org-less)")}</span>`;
  if (claimsBox && slot === "A") {
    claimsBox.hidden = false;
    claimsBox.textContent = "decoded claims:\n" + JSON.stringify(claims, null, 2);
  }
}

function refreshButtons() {
  const a = SLOTS.A;
  const b = SLOTS.B;
  const isOwner = !!a && (a.profile?.roles ?? []).includes("owner");
  const isStaff = !!a && a.profile?.is_platform_staff === true;

  document
    .querySelectorAll("[data-slot='A']")
    .forEach((btn) => (btn.disabled = !a));
  document
    .querySelectorAll("[data-slot='B']")
    .forEach((btn) => (btn.disabled = !b));
  document
    .querySelectorAll("[data-needs='owner-A']")
    .forEach((btn) => (btn.disabled = !(isOwner && b)));
  document
    .querySelectorAll("[data-needs='staff']")
    .forEach((btn) => (btn.disabled = !isStaff));

  $("conn-chip").textContent = a
    ? `session A: ${a.profile?.username} · ${a.profile?.roles?.join("/") || "org-less"}`
    : "backend: ready · not signed in";
  $("conn-chip").className = "chip " + (a ? "chip-ok" : "chip-muted");
}

/* ── capability actions ───────────────────────────────────── */

async function doLogin(slot, event) {
  event.preventDefault();
  const form = event.target;
  const username = form.querySelector("input[name=username]").value.trim();
  const password = form.querySelector("input[name=password]").value;
  const button = form.querySelector("button[type=submit]");
  busy(button, true);
  const result = await api("POST", "/auth/login/", { body: { username, password } });
  busy(button, false);
  if (!result.ok) {
    show(`identity-${slot.toLowerCase()}`, `✗ login failed (${result.status}): ${result.data?.detail || result.data?.error || "invalid credentials"}`);
    return;
  }
  SLOTS[slot] = {
    access: result.data.access_token,
    refresh: result.data.refresh_token,
    profile: result.data.user,
  };
  renderIdentity(slot);
  refreshButtons();
}

async function doubleRefresh() {
  const session = SLOTS.A;
  if (!session) return;
  const out = [];
  const seen = new Set();
  for (let i = 1; i <= 2; i++) {
    const result = await api("POST", "/auth/token/refresh/", {
      body: { refresh: session.refresh },
    });
    if (!result.ok) {
      show("refresh-out", `attempt ${i}: ✗ ${result.status}`);
      return;
    }
    const jti = decodeJwt(result.data.access).jti;
    seen.add(jti);
    out.push(`attempt ${i}: 200 ✓  access.jti=${jti}`);
  }
  out.push("");
  out.push(
    seen.size === 2
      ? "both attempts succeeded with DIFFERENT access tokens → rotation/blacklist OFF, exactly as contracted"
      : "unexpected: identical jti"
  );
  show("refresh-out", out.join("\n"));
}

async function tamperTest() {
  const session = SLOTS.A;
  if (!session) return;
  const flip = session.access.endsWith("x") ? "y" : "x";
  const tampered = session.access.slice(0, -1) + flip;
  const result = await api("GET", "/org/members/", { token: tampered });
  show(
    "tamper-out",
    `GET /org/members/ with tampered signature → ${result.status}\n` +
      (result.status === 401
        ? "✓ rejected: signature verification did its job"
        : "unexpected response")
  );
}

function memberRow(member, myRoles) {
  const canWrite = myRoles.includes("owner");
  const active = member.is_active
    ? '<span class="pill-active">active</span>'
    : '<span class="pill-off">deactivated</span>';
  const action = canWrite
    ? member.is_active
      ? `<button class="btn btn-danger btn-row-deactivate" data-member-id="${member.member_id}">Deactivate</button>`
      : "<span class=\"hint\">off</span>"
    : "<span class=\"hint\">—</span>";
  return (
    `<tr><td>${member.member_id}</td><td>${esc(member.username)}</td>` +
    `<td>${roleBadges([member.role])}</td><td>${active}</td><td>${action}</td></tr>`
  );
}

async function listMembers(slot = "A") {
  const session = SLOTS[slot];
  if (!session) return;
  const result = await api("GET", "/org/members/", { token: session.access });
  const table = $("members-table");
  const emptyNote = $("members-empty");
  table.hidden = false;
  if (!result.ok) {
    emptyNote.hidden = false;
    emptyNote.textContent = `HTTP ${result.status} — ${result.data?.error || "denied"}`;
    return;
  }
  const rows = Array.isArray(result.data) ? result.data : [];
  const myRoles = decodeJwt(session.access).roles ?? [];
  table.querySelector("tbody").innerHTML = rows.map((m) => memberRow(m, myRoles)).join("");
  emptyNote.hidden = rows.length > 0;
  if (rows.length === 0) {
    emptyNote.hidden = false;
    emptyNote.textContent =
      "No rows visible — exactly what an unauthorized or org-less session must see (fail-closed).";
  }
}

async function inviteMember(event) {
  event.preventDefault();
  const session = SLOTS.A;
  if (!session) return;
  const username = $("inv-user").value.trim();
  const role = $("inv-role").value;
  const result = await api("POST", "/org/members/", {
    body: { username, role },
    token: session.access,
  });
  if (result.status === 201) {
    show("members-empty", false);
    await listMembers();
  } else if (result.status === 200) {
    await listMembers(); // reactivated an inactive row — same member_id comes back
  }
  const banner = $("members-empty");
  banner.hidden = false;
  banner.textContent =
    `invite → ${result.status}` +
    (result.status === 201 ? " created (unusable password)" :
     result.status === 200 ? " INACTIVE membership REACTIVATED (same row reused)" :
     result.status === 409 ? " conflict: an ACTIVE membership already exists" :
     result.status === 403 ? " forbidden: owner role required" :
     ` ${JSON.stringify(result.data)}`);
}

async function deactivateFromTable(event) {
  const button = event.target.closest("button[data-member-id]");
  if (!button) return;
  const session = SLOTS.A;
  const result = await api(
    "POST",
    `/org/members/${button.dataset.memberId}/deactivate/`,
    { token: session.access }
  );
  await listMembers();
}

async function deactivateSessionBMember() {
  const victim = SLOTS.B;
  const owner = SLOTS.A;
  if (!victim || !owner) return;
  // Find B's member id by listing as the OWNER (B may already be stripped).
  const listing = await api("GET", "/org/members/", { token: owner.access });
  const username = victim.profile?.username;
  const row = (Array.isArray(listing.data) ? listing.data : []).find(
    (m) => m.username === username && m.is_active
  );
  if (!row) {
    show("off-out", "no ACTIVE membership found for Session-B user — nothing to deactivate");
    return;
  }
  const result = await api("POST", `/org/members/${row.member_id}/deactivate/`, {
    token: owner.access,
  });
  show(
    "off-out",
    `deactivate ${username} (member_id=${row.member_id}) → ${result.status} ` +
      JSON.stringify(result.data)
  );
}

async function ghostRead() {
  const victim = SLOTS.B;
  if (!victim) return;
  const out = [];
  const refreshed = await api("POST", "/auth/token/refresh/", {
    body: { refresh: victim.refresh },
  });
  if (!refreshed.ok) {
    show("off-out", `refresh: ✗ ${refreshed.status} (unexpected — refresh stays valid by contract)`);
    return;
  }
  const claims = decodeJwt(refreshed.data.access);
  out.push(`refresh → 200 ✓  org_id claim present: ${"org_id" in claims ? "YES ✗" : "NO ✓"}`);
  victim.access = refreshed.data.access; // B now holds the org-less token

  const read = await api("GET", "/org/members/", { token: victim.access });
  out.push(`GET /org/members/ → HTTP ${read.status}, payload: ${JSON.stringify(read.data)}`);
  out.push("");
  out.push(
    read.status === 200 && Array.isArray(read.data) && read.data.length === 0
      ? "C-14 in action: HTTP 200 + EMPTY payload = fail-closed. Correct behavior — never an error."
      : "note: unexpected shape for this demo path"
  );
  renderIdentity("B");
  show("off-out", out.join("\n"));
}

async function supportBlank() {
  const session = SLOTS.A;
  const result = await api("POST", "/platform/support/session/", {
    body: { org_id: $("sup-org").value.trim(), incident_id: "" },
    token: session.access,
  });
  show("sup-out", `blank incident → ${result.status}\n${result.status === 400 ? "✓ audit anchor enforced" : "unexpected"}`);
}

async function supportOpen() {
  const session = SLOTS.A;
  const body = {
    org_id: $("sup-org").value.trim(),
    incident_id: $("sup-inc").value.trim(),
    reason: $("sup-reason").value.trim(),
  };
  const result = await api("POST", "/platform/support/session/", {
    body,
    token: session.access,
  });
  if (!result.ok) {
    show("sup-out", `→ ${result.status} ${JSON.stringify(result.data)}`);
    return;
  }
  const claims = decodeJwt(result.data.access_token);
  show(
    "sup-out",
    "200 ✓ scoped session opened\n" +
      JSON.stringify(
        {
          expires_in_sec: result.data.expires_in_sec,
          support_session: claims.support_session,
          target_org: claims.org_id,
          incident_id: claims.incident_id,
        },
        null,
        2
      ) +
      "\n\nan SUPPORT_ORG_ENTER row was written BEFORE this response was returned"
  );
}

/* ── wiring ───────────────────────────────────────────────── */

/* ── v0.3 telemetry explorer ─────────────────────────────────────────────── */

async function fetchTelemetry() {
  const session = SLOTS.A;
  if (!session) return;
  const saloon = $("tx-saloon").value.trim();
  if (!saloon) {
    show("tx-empty", "saloon_id is required");
    $("tx-empty").hidden = false;
    return;
  }
  const params = new URLSearchParams();
  for (const [key, id] of [["slot_id", "tx-slot"], ["since", "tx-since"], ["until", "tx-until"]]) {
    const value = $(id).value.trim();
    if (value) params.set(key, value);
  }
  if ($("tx-buffered").checked) params.set("include_buffered", "true");

  const result = await api(
    "GET",
    `/saloons/${encodeURIComponent(saloon)}/telemetry/?${params}`,
    { token: session.access }
  );

  const table = $("tx-table");
  const empty = $("tx-empty");
  table.hidden = false;
  if (!result.ok) {
    empty.hidden = false;
    empty.textContent = `HTTP ${result.status} — ${result.data?.detail || "denied"}`;
    return;
  }
  const rows = result.data.results || [];
  table.querySelector("tbody").innerHTML = rows
    .map((r) => {
      const lane = r.is_buffered
        ? '<span class="pill-off">buffered</span>'
        : '<span class="pill-active">live</span>';
      const delay = r.buffered_delay_mins === null ? "—" : r.buffered_delay_mins;
      return `<tr><td>${esc(r.device_ts)}</td><td>${esc(r.slot_id)}</td>` +
             `<td>${lane}</td><td>${esc(String(delay))}</td>` +
             `<td><code>${esc(JSON.stringify(r.metrics))}</code></td></tr>`;
    })
    .join("");
  empty.hidden = rows.length > 0;
  if (rows.length === 0) {
    empty.hidden = false;
    // C-14 family: HTTP 200 + empty is CORRECT — unauthorized or no data yet.
    empty.textContent =
      "HTTP 200 with an EMPTY list — fail-closed by design (wrong org, " +
      "deactivated member, or simply no readings in this window).";
  }
}

/* ── v0.4 alert engine ─────────────────────────────────────────────────── */

/* demo endpoints live at site root (/demo/api/...), NOT under /api/v1 —
   fetchJSON bypasses the API prefix but keeps the same log envelope. */
async function fetchJSON(path, token) {
  const started = performance.now();
  let status = 0;
  let data = null;
  try {
    const response = await fetch(path, { headers: { Authorization: `Bearer ${token}` } });
    status = response.status;
    data = await response.json().catch(() => null);
    return { status, ok: response.ok, data };
  } catch (error) {
    return { status: 0, ok: false, data: { error: String(error) } };
  } finally {
    logEvent("GET", path, status, Math.round(performance.now() - started));
  }
}

async function fetchAlerts() {
  const session = SLOTS.A;
  if (!session) return;
  const result = await fetchJSON("/demo/api/alerts/overview/?limit=15", session.access);
  if (!result.ok) {
    show("alerts-out", `HTTP ${result.status} — ${JSON.stringify(result.data)}`);
    return;
  }
  const alerts = result.data.alerts || [];
  const mutes = result.data.maintenance_mutes || [];
  const lines = [];
  lines.push(`recent alerts (${alerts.length}):`);
  for (const a of alerts) {
    lines.push(
      `  [${a.current_status}] ${a.metric_type} saloon=${a.saloon_id} ` +
      `tier=${a.highest_tier_reached} value=${a.current_value ?? "—"} ` +
      `@ ${a.triggered_at}${a.resolved_at ? ` → resolved ${a.resolved_at}` : ""}`
    );
  }
  if (alerts.length === 0) lines.push("  (none — no breaches recorded yet)");
  lines.push("");
  lines.push(`maintenance mutes (${mutes.length}):`);
  for (const m of mutes) {
    lines.push(`  saloon=${m.saloon_id} ${m.starts_at} → ${m.ends_at} · ${m.reason}`);
  }
  if (mutes.length === 0) lines.push("  (none)");
  show("alerts-out", lines.join("\n"));
}

async function fetchStateMachines() {
  const session = SLOTS.A;
  if (!session) return;
  const result = await fetchJSON("/demo/api/alerts/state-machines/", session.access);
  if (!result.ok) {
    show("alerts-out", `HTTP ${result.status} — ${JSON.stringify(result.data)}`);
    return;
  }
  const lines = [];
  for (const [family, info] of Object.entries(result.data)) {
    lines.push(`${family}: ${info.count} key(s)`);
    for (const k of info.keys.slice(0, 5)) {
      lines.push(`  ${k.key} = ${k.value}${k.ttl >= 0 ? ` (ttl ${k.ttl}s)` : ""}`);
    }
  }
  show("alerts-out", lines.join("\n"));
}

async function fetchQualityLive() {
  const session = SLOTS.A;
  if (!session) return;
  const result = await fetchJSON("/demo/api/alerts/quality/", session.access);
  if (!result.ok) {
    show("quality-live-out", `HTTP ${result.status} — ${JSON.stringify(result.data)}`);
    return;
  }
  const lines = [];
  lines.push("MTTR seconds by tier:");
  for (const [tier, sec] of Object.entries(result.data.mttr_seconds_by_tier || {})) {
    lines.push(`  tier ${tier}: ${sec === null ? "—" : `${sec}s (~${Math.round(sec / 60)}min)`}`);
  }
  lines.push("");
  lines.push("ack → resolve seconds:");
  for (const [tier, sec] of Object.entries(result.data.ack_to_resolve_seconds || {})) {
    lines.push(`  tier ${tier}: ${sec === null ? "—" : `${sec}s`}`);
  }
  lines.push("");
  lines.push("alerts per flock (open + closed):");
  for (const row of result.data.alerts_per_flock || []) {
    lines.push(`  ${JSON.stringify(row)}`);
  }
  show("quality-live-out", lines.join("\n"));
}

document.addEventListener("DOMContentLoaded", async () => {
  $("login-form-a").addEventListener("submit", (e) => doLogin("A", e));
  $("login-form-b").addEventListener("submit", (e) => doLogin("B", e));

  $("btn-refresh2").addEventListener("click", doubleRefresh);
  $("btn-tamper").addEventListener("click", tamperTest);
  $("btn-members").addEventListener("click", () => listMembers("A"));
  $("invite-form").addEventListener("submit", inviteMember);
  $("members-table").addEventListener("click", deactivateFromTable);
  $("btn-deact-b").addEventListener("click", deactivateSessionBMember);
  $("btn-ghost-b").addEventListener("click", ghostRead);
  $("btn-sup-blank").addEventListener("click", supportBlank);
  $("btn-sup-open").addEventListener("click", supportOpen);
  $("btn-tx").addEventListener("click", fetchTelemetry);
  $("btn-alerts").addEventListener("click", fetchAlerts);
  $("btn-state").addEventListener("click", fetchStateMachines);
  $("btn-quality-live").addEventListener("click", fetchQualityLive);

  // connection chip: anonymous ping against a protected route (expect 401)
  const probe = await api("GET", "/org/members/");
  $("conn-chip").textContent = probe.status === 401
    ? "backend: reachable · auth required ✓"
    : `backend: unexpected (${probe.status})`;

  refreshButtons();
});
