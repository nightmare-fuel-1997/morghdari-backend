// Demo API client — JWT from the console login (demo-owner).
// Same endpoints the engineering console uses; nothing new server-side
// except the `dangerous` flag on /demo/api/simulate/.

const API = '/api/v1'
const DEMO = '/demo/api'

const LS_KEY = 'morghdari-dash'

export function loadSession() {
  try {
    const raw = localStorage.getItem(LS_KEY)
    return raw ? JSON.parse(raw) : null
  } catch {
    return null
  }
}

export function saveSession(s) {
  if (s) localStorage.setItem(LS_KEY, JSON.stringify(s))
  else localStorage.removeItem(LS_KEY)
}

async function req(method, path, { body, token } = {}) {
  const headers = {}
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  if (token) headers['Authorization'] = `Bearer ${token}`
  try {
    const r = await fetch(`${API}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    })
    const data = await r.json().catch(() => null)
    return { status: r.status, ok: r.ok, data }
  } catch (e) {
    return { status: 0, ok: false, data: { error: String(e) } }
  }
}

async function demoGet(path, token) {
  try {
    // Public endpoints (e.g. the login identity) are called without a token;
    // a "Bearer undefined" header would 401 before permission checks run.
    const headers = token ? { Authorization: `Bearer ${token}` } : {}
    const r = await fetch(path, { headers })
    const data = await r.json().catch(() => null)
    return { status: r.status, ok: r.ok, data }
  } catch (e) {
    return { status: 0, ok: false, data: { error: String(e) } }
  }
}

export function login(username, password) {
  return req('POST', '/auth/login/', { body: { username, password } })
}

export function fetchFarm(token) {
  return demoGet(`${DEMO}/farm/`, token)
}

// Public barn identity for the login backdrop — no token on purpose; the
// landing shows WHICH barn you are about to sign into (real counts only).
export function fetchIdentity() {
  return demoGet(`${DEMO}/identity/`)
}

export function fetchAlerts(token, { limit = 10 } = {}) {
  return demoGet(`${DEMO}/alerts/overview/?limit=${limit}`, token)
}

export function fetchQuality(token) {
  return demoGet(`${DEMO}/alerts/quality/`, token)
}

export function fetchStateMachines(token) {
  return demoGet(`${DEMO}/alerts/state-machines/`, token)
}

export function fetchPipeline(token) {
  return demoGet(`${DEMO}/api/pipeline/`, token)
}

export function sensorOff(token, slotId = null) {
  const headers = { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` }
  return fetch('/demo/api/sensor-off/', {
    method: 'POST',
    headers,
    body: JSON.stringify(slotId ? { slot_id: slotId } : {}),
  })
    .then((r) => r.json().then((d) => ({ status: r.status, ok: r.ok, data: d })))
    .catch((e) => ({ status: 0, ok: false, data: { error: String(e) } }))
}

export function acknowledgeAlert(token, alertId) {
  const headers = { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` }
  return fetch('/demo/api/ack/', {
    method: 'POST',
    headers,
    body: JSON.stringify({ alert_id: alertId }),
  })
    .then((r) => r.json().then((d) => ({ status: r.status, ok: r.ok, data: d })))
    .catch((e) => ({ status: 0, ok: false, data: { error: String(e) } }))
}

export function simulate(token, { buffered = false, dangerous = false } = {}) {
  // Hit the demo endpoint directly (not under /api/v1).
  const headers = { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` }
  return fetch('/demo/api/simulate/', {
    method: 'POST',
    headers,
    body: JSON.stringify({ buffered, dangerous }),
  })
    .then((r) => r.json().then((d) => ({ status: r.status, ok: r.ok, data: d })))
    .catch((e) => ({ status: 0, ok: false, data: { error: String(e) } }))
}

// ── v0.4 demo dashboard: per-saloon pages ──
export function fetchSaloonSensors(token, saloonId) {
  return demoGet(`${DEMO}/saloons/${saloonId}/sensors/`, token)
}

export function fetchSaloonStats(token, saloonId, { window = '1h', slotId = null } = {}) {
  const q = new URLSearchParams({ window })
  if (slotId) q.set('slot_id', slotId)
  return demoGet(`${DEMO}/saloons/${saloonId}/stats/?${q}`, token)
}

export function fetchSaloonTelemetry(token, saloonId, { since, slotId = null } = {}) {
  const q = new URLSearchParams()
  if (since) q.set('since', since)
  if (slotId) q.set('slot_id', slotId)
  return req('GET', `/saloons/${saloonId}/telemetry/?${q}`, { token })
}

export function fetchEmqxStatus(token) {
  return demoGet(`${DEMO}/emqx-status/`, token)
}
