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
    const r = await fetch(path, { headers: { Authorization: `Bearer ${token}` } })
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

export function fetchAlerts(token) {
  return demoGet(`${DEMO}/alerts/overview/?limit=10`, token)
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
