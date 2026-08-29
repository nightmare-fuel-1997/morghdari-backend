/* Morghdari v0.4 Demo Console — vanilla JS, zero dependencies.
   Talks to the same-origin backend (/api/v1/...) served by Django.
   فارسی: اسکریپت دمو — بدون وابستگی؛ درخواست‌ها هم‌مبدأ هستند. */

"use strict";

const $ = (id) => document.getElementById(id);
const API = "/api/v1";
const SLOTS = { A: null, B: null }; // {access, refresh, profile}

/* ── i18n ───────────────────────────────────────────────── */

const I18N = {
  "header-subtitle": {
    en: "Showcasing v0.4 capabilities — Alert Engine",
    fa: "نمایش قابلیت‌های نسخه ۰٫۴ — موتور هشدار",
  },
  "s1-title": { en: "Sign in", fa: "ورود" },
  "s1-desc": {
    en: "JWT carries your org + roles. Claims are decoded live below.",
    fa: "توکن JWT شامل سازمان و نقش‌های شماست. ادعاها به‌صورت زنده در همانجا رمزگشایی می‌شوند.",
  },
  "s1-session-a": { en: "Session A", fa: "نشست الف" },
  "s1-username": { en: "Username", fa: "نام کاربری" },
  "s1-password": { en: "Password", fa: "رمز عبور" },
  "s1-login-a": { en: "Login A", fa: "ورود الف" },
  "s1-session-b": { en: "Session B", fa: "نشست ب" },
  "s1-victim": {
    en: "(victim slot · for demonstrating offboarding)",
    fa: "(نشست قربانی — برای نمایش فرآیند خروج عضو)",
  },
  "s1-login-b": { en: "Login B", fa: "ورود ب" },

  "s2-title": { en: "No rotation proof", fa: "اثبات عدم چرخش توکن" },
  "s2-desc": {
    en: "Same refresh token used twice — both succeed. Accepted risk: a leaked refresh lives 7 days.",
    fa: "یک توکن نوسازی دوبار پشت سر هم ارسال می‌شود و هر بار موفق است. ریسک پذیرفته‌شده: توکن نشت‌یافته تا ۷ روز معتبر خواهد بود.",
  },
  "s2-btn": { en: "Run double-refresh on A", fa: "اجرای نوسازی دوگانه روی الف" },

  "s3-title": { en: "Tamper test", fa: "آزمون دستکاری" },
  "s3-desc": {
    en: "Flips one character of the access token — signature check must reject it.",
    fa: "یک نویسه از توکن دسترسی تغییر می‌کند — بررسی امضا باید آن را رد کند.",
  },
  "s3-btn": { en: "Send tampered token", fa: "ارسال توکن دستکاری‌شده" },

  "s4-title": { en: "Org members", fa: "اعضای سازمان" },
  "s4-desc": {
    en: "Reads: any member of the org. Writes: owner only (buttons auto-disable).",
    fa: "خواندن: هر عضوی از سازمان. نوشتن: فقط مالک (دکمه‌ها خودکار غیرفعال می‌شوند).",
  },
  "s4-invite": { en: "Invite a member", fa: "دعوت عضو جدید" },
  "s4-owner": { en: "(owner)", fa: "(مالک)" },
  "s4-role": { en: "Role", fa: "نقش" },
  "s4-invite-btn": { en: "Invite", fa: "دعوت" },
  "s4-invite-hint": {
    en: "New users are created with an UNUSABLE password — credentials travel out-of-band.",
    fa: "کاربران جدید با رمزعبور غیرقابل‌استفاده ایجاد می‌شوند — اطلاعات ورود جداگانه ارسال می‌شود.",
  },
  "s4-list-btn": { en: "List members", fa: "فهرست اعضا" },
  "s4-empty": {
    en: "No rows visible — exactly what an unauthorized or org-less session must see.",
    fa: "هیچ ردیفی نمایش داده نمی‌شود — رفتار صحیح برای نشست بدون دسترسی یا بدون سازمان.",
  },

  "s5-title": { en: "Offboarding chain", fa: "زنجیره خروج عضو" },
  "s5-desc": {
    en: "Owner deactivates Session B's member → B refreshes (still valid!) → new token has NO org claim → B's reads return HTTP 200 with an EMPTY payload. No blacklist needed.",
    fa: "مالک عضویت نشست ب را غیرفعال می‌کند → ب نوسازی می‌کند (هنوز معتبر!) → توکن جدید فاقد ادعای سازمان است → درخواست‌های خواندن ب با کد ۲۰۰ و پاسخ خالی برمی‌گردند. نیازی به فهرست سیاه نیست.",
  },
  "s5-step1": {
    en: "Login B as a normal member (step 1, slot B)",
    fa: "ورود ب به‌عنوان عضو عادی (گام ۱، نشست ب)",
  },
  "s5-step2": {
    en: 'Login A as the owner, press "Deactivate B" below',
    fa: 'ورود الف به‌عنوان مالک، دکمه «غیرفعال‌سازی ب» را بزنید',
  },
  "s5-step3": {
    en: 'Press "B: refresh & read" — watch authority vanish',
    fa: 'دکمه «ب: نوسازی و خواندن» را بزنید — از بین رفتن دسترسی را ببینید',
  },
  "s5-deact-btn": {
    en: "Deactivate Session-B member",
    fa: "غیرفعال‌سازی عضو نشست ب",
  },
  "s5-ghost-btn": { en: "B: refresh & read", fa: "ب: نوسازی و خواندن" },

  "s6-title": { en: "Support session", fa: "نشست پشتیبانی" },
  "s6-desc": {
    en: "Platform-staff only (superuser rejected). Blank incident → 400. Opened sessions see ONLY the target org.",
    fa: "فقط کارکنان پلتفرم (سوپریوزر رد می‌شود). شماره حادثه خالی → خطای ۴۰۰. نشست بازشده فقط سازمان مقصد را می‌بیند.",
  },
  "s6-org-label": {
    en: "Target org_id (UUID)",
    fa: "شناسه سازمان مقصد (UUID)",
  },
  "s6-incident": { en: "Incident ID", fa: "شماره حادثه" },
  "s6-reason": { en: "Reason", fa: "دلیل" },
  "s6-blank-btn": {
    en: "Try blank incident (400)",
    fa: "ارسال بدون شماره حادثه (۴۰۰)",
  },
  "s6-open-btn": { en: "Open audited session", fa: "باز کردن نشست تحت نظارت" },
  "s6-hint": {
    en: "Every opening writes a SUPPORT_ORG_ENTER row into account_activity_logs.",
    fa: "هر بار باز شدن نشست، یک ردیف SUPPORT_ORG_ENTER در account_activity_logs ثبت می‌شود.",
  },

  "s8-title": { en: "Telemetry explorer", fa: "کاوشگر تله‌متری" },
  "s8-desc": {
    en: "The loop closes: readings that entered via MQTT come back here through the RLS-scoped read API.",
    fa: "حلقه بسته می‌شود: داده‌هایی که از طریق MQTT وارد شده‌اند از اینجا و از طریق API خواندن با محدودیت RLS بازمی‌گردند.",
  },
  "s8-saloon": { en: "Saloon id", fa: "شناسه سالن" },
  "s8-slot": { en: "Slot id (optional)", fa: "شناسه اسلات (اختیاری)" },
  "s8-since": { en: "Since", fa: "از" },
  "s8-until": { en: "Until", fa: "تا" },
  "s8-buffered": { en: "include buffered", fa: "شامل بازپخش‌ها" },
  "s8-fetch-btn": { en: "Fetch telemetry", fa: "دریافت تله‌متری" },

  "s9-title": { en: "How your data flows", fa: "مسیر جریان داده‌ها" },
  "s9-desc": {
    en: "Every guarantee below is enforced by code you can read, and pinned by tests that run on every push.",
    fa: "هر تضمین زیر توسط کدی اجرا می‌شود که قابل مطالعه است و توسط تست‌هایی تثبیت شده که در هر ارسال اجرا می‌شوند.",
  },
  "s9-gw": { en: "Gateway", fa: "دروازه" },
  "s9-gw-sub": {
    en: "schema-2.0 payload",
    fa: "بار payload از نوع schema-2.0",
  },
  "s9-gw-hint": {
    en: "qos=1 · one device_ts / window",
    fa: "qos=1 · یک device_ts در هر بازه زمانی",
  },
  "s9-broker": { en: "EMQX broker", fa: "بروکر EMQX" },
  "s9-broker-sub": { en: "authenticates the path", fa: "احراز هویت مسیر" },
  "s9-broker-hint": {
    en: "the topic IS the identity",
    fa: "موضوع پیام همان هویت دستگاه است",
  },
  "s9-bridge": { en: "Subscriber bridge", fa: "پل اشتراک" },
  "s9-bridge-sub": { en: "$share/telemetry_workers", fa: "$share/telemetry_workers" },
  "s9-bridge-hint": {
    en: "dedup NX 90000 · DLQ with reasons",
    fa: "حذف تکرار NX 90000 · صندوق مرده با دلیل",
  },
  "s9-lanes": { en: "Two lanes", fa: "دو خط" },
  "s9-lanes-sub": { en: "realtime | historical", fa: "لحظه‌ای | تاریخی" },
  "s9-lanes-hint": {
    en: "trim ~100k · storms never delay live",
    fa: "برش ~100k · طوفان‌ها هرگز داده لحظه‌ای را به تأخیر نمی‌اندازند",
  },
  "s9-drain": { en: "Celery drain", fa: "تخلیه Celery" },
  "s9-drain-sub": { en: "XACK after commit", fa: "XACK پس از ثبت" },
  "s9-drain-hint": {
    en: "per-org RLS batches · crash-safe",
    fa: "دسته‌های RLS بر اساس سازمان · مقاوم در برابر خرابی",
  },
  "s9-db": { en: "TimescaleDB", fa: "TimescaleDB" },
  "s9-db-sub": { en: "hypertable · 180d retention", fa: "جدول ابری · نگهداری ۱۸۰ روزه" },
  "s9-db-hint": {
    en: "UNIQUE + ignore_conflicts = once",
    fa: "UNIQUE + ignore_conflicts = دقیقاً یک‌بار",
  },
  "s9-api": { en: "This API", fa: "این API" },
  "s9-api-sub": { en: "RLS-scoped reads", fa: "خواندن با محدودیت RLS" },
  "s9-api-hint": {
    en: "cross-org ⇒ HTTP 200 empty",
    fa: "بدون سازمان ⇒ HTTP 200 با پاسخ خالی",
  },
  "s9-poison": {
    en: "Poison at any step dead-letters WITH a reason — bad_json · UNSUPPORTED_SCHEMA_VERSION · TOPIC_PAYLOAD_IDENTITY_MISMATCH · UNKNOWN_SLOT · GATEWAY_NOT_PROVISIONED.",
    fa: "پیام ناسالم در هر مرحله با ذکر دلیل به صندوق مرده می‌رود — bad_json · UNSUPPORTED_SCHEMA_VERSION · TOPIC_PAYLOAD_IDENTITY_MISMATCH · UNKNOWN_SLOT · GATEWAY_NOT_PROVISIONED.",
  },

  "s10-title": { en: "Alert engine", fa: "موتور هشدار" },
  "s10-desc": {
    en: "The safety brain: threshold cache → breach state machine → 3-tier escalation. Fires exactly once at 900 s of sustained breach.",
    fa: "مغز ایمنی: کش آستانه → ماشین حالت نقض → تشدید سه‌سطحی. هشدار دقیقاً یک‌بار پس از ۹۰۰ ثانیه نقض پایدار صادر می‌شود.",
  },
  "s10-fetch-btn": { en: "Fetch alerts", fa: "دریافت هشدارها" },
  "s10-state-btn": { en: "State machines", fa: "ماشین‌های حالت" },

  "s11-title": { en: "Alert quality dashboard", fa: "داشبورد کیفیت هشدار" },
  "s11-desc": {
    en: "MET-ALERT-QUALITY counters from the live backend (quality.py): MTTR by tier, acked vs never-acked resolution, alerts per flock.",
    fa: "شمارنده‌های کیفیت هشدار از بک‌اند زنده (quality.py): میانگین زمان بازیابی به تفکیک سطح، مقایسه رفع با تأیید دستی و بدون تأیید، تعداد هشدار به ازای هر دوره پرورش.",
  },
  "s11-refresh-btn": {
    en: "Refresh quality metrics",
    fa: "بروزرسانی معیارهای کیفیت",
  },

  "s12-title": { en: "How alerts fire", fa: "نحوه صدور هشدار" },
  "s12-desc": {
    en: "Every guarantee is enforced by a Redis state machine with exactly-once semantics, pinned by tests.",
    fa: "هر تضمین توسط ماشین حالت Redis با رعایت دقیق اجرای یک‌بار اجرا می‌شود و توسط تست‌ها تثبیت شده است.",
  },
  "s12-reading": { en: "Reading commits", fa: "ثبت خواندن" },
  "s12-reading-sub": { en: "telemetry row saved", fa: "ردیف تله‌متری ذخیره شد" },
  "s12-reading-hint": {
    en: "evaluation runs after commit, before XACK",
    fa: "ارزیابی پس از ثبت و پیش از XACK اجرا می‌شود",
  },
  "s12-threshold": { en: "Threshold hash", fa: "هش آستانه" },
  "s12-threshold-sub": {
    en: "per saloon/day cache",
    fa: "کش بر اساس سالن و روز",
  },
  "s12-threshold-hint": {
    en: "midnight cron · flock-change re-cache",
    fa: "کرون نیمه‌شب · بازسازی کش هنگام تغییر دوره پرورش",
  },
  "s12-gates": { en: "Gates first", fa: "ابتدا دروازه‌ها" },
  "s12-gates-sub": {
    en: "velocity → mute → capability",
    fa: "سرعت → بی‌صدا → قابلیت",
  },
  "s12-gates-hint": {
    en: "velocity bypasses everything",
    fa: "سرعت از همه دروازه‌ها عبور می‌کند",
  },
  "s12-breach": { en: "Breach key", fa: "کلید نقض" },
  "s12-breach-sub": {
    en: "JSON state + TTL refresh",
    fa: "وضعیت JSON + تمدید TTL",
  },
  "s12-breach-hint": {
    en: "Tier 1 fires exactly once at 900 s",
    fa: "سطح ۱ دقیقاً یک‌بار در ۹۰۰ ثانیه فعال می‌شود",
  },
  "s12-escalation": { en: "Escalation sweep", fa: "پویش تشدید" },
  "s12-escalation-sub": { en: "every 2 min", fa: "هر ۲ دقیقه" },
  "s12-escalation-hint": {
    en: "Tier 2 @ 15 min · Tier 3 @ 30 · never demotes",
    fa: "سطح ۲ در ۱۵ دقیقه · سطح ۳ در ۳۰ دقیقه · هرگز کاهش سطح نمی‌یابد",
  },
  "s12-resolution": { en: "Resolution", fa: "رفع" },
  "s12-resolution-sub": { en: "hysteresis +0.5", fa: "هیسترزیس +۰٫۵" },
  "s12-resolution-hint": {
    en: "breach key gone ⇒ RESOLVED",
    fa: "حذف کلید نقض ⇒ RESOLVED",
  },
  "s12-sidecars": {
    en: "Sidecars: LOS watchdog (60 s sweep, 300 s grace, restart suppression 10 min) · replay sweep (REPLAY_ANALYSIS rows, never pushed) · farm correlation (≥ 0.7 slots LOS in 3 min ⇒ one page). Ack writes row + pause key in one atomic block; pause freezes the escalation timer.",
    fa: "سرویس‌های جانبی: نگهبان LOS (پویش ۶۰ ثانیه‌ای، مهلت ۳۰۰ ثانیه‌ای، مهار ری‌استارت ۱۰ دقیقه‌ای) · پویش بازپخش (ردیف‌های REPLAY_ANALYSIS، هرگز ارسال نمی‌شوند) · همبستگی مزرعه (≥ ۰٫۷ اسلات LOS در ۳ دقیقه ⇒ یک هشدار). تأیید ردیف و کلید تعلیق را در یک بلوک اتمی ثبت می‌کند؛ تعلیق تایمر تشدید را متوقف می‌کند.",
  },

  "log-title": { en: "Event log", fa: "گزارش رویدادها" },
  "footer-contract": {
    en: "v0.4 tag · branch demo/v0.4-showcase · contract v1.4.5",
    fa: "نسخه ۰٫۴ · شاخه demo/v0.4-showcase · قرارداد v1.4.5",
  },
  "footer-note": {
    en: "Documented demo — zero JavaScript dependencies",
    fa: "دموی مستند — بدون وابستگی جاوااسکریپتی",
  },

  /* hero + section nav */
  "hero-badge": { en: "Latest Release", fa: "آخرین نسخه" },
  "hero-title": { en: "Morghdari Safety Platform", fa: "پلتفرم ایمنی مُرغداری" },
  "hero-desc": {
    en: "Multi-tenant industrial IoT monitoring with real-time alerting, JWT authentication, and RLS-scoped data access. This console exercises every guarantee live against the backend.",
    fa: "پایش صنعتی IoT چنداجاره‌ای با هشدار لحظه‌ای، احراز هویت JWT و دسترسی داده با محدودیت RLS. این کنسول هر تضمین را به‌صورت زنده روی بک‌اند اجرا می‌کند.",
  },
  "hero-tests": { en: "Tests Passing", fa: "تست موفق" },
  "hero-coverage": { en: "Coverage", fa: "پوشش" },
  "hero-escalation": { en: "Escalation", fa: "تشدید" },
  "hero-isolation": { en: "Org Isolation", fa: "جداسازی سازمان" },
  "nav-auth": { en: "Auth", fa: "احراز هویت" },
  "nav-org": { en: "Organization", fa: "سازمان" },
  "nav-telemetry": { en: "Telemetry", fa: "تله‌متری" },
  "nav-alerts": { en: "Alerts", fa: "هشدارها" },
  "sec-auth-title": { en: "Authentication & Security", fa: "احراز هویت و امنیت" },
  "sec-auth-desc": {
    en: "JWT-based multi-tenant auth with session slots, token integrity, and no-rotation design.",
    fa: "احراز هویت چنداجاره‌ای مبتنی بر JWT با اسلات نشست، یکپارچگی توکن و طراحی بدون چرخش.",
  },
  "sec-org-title": { en: "Organization Management", fa: "مدیریت سازمان" },
  "sec-org-desc": {
    en: "Multi-tenant membership, owner-only writes, and zero-blacklist offboarding.",
    fa: "عضویت چنداجاره‌ای، نوشتن فقط توسط مالک و خروج عضو بدون فهرست سیاه.",
  },
  "sec-tel-title": { en: "Telemetry & Data Pipeline", fa: "تله‌متری و خط لوله داده" },
  "sec-tel-desc": {
    en: "MQTT → EMQX → Celery → TimescaleDB → RLS-scoped API. The complete data flow.",
    fa: "MQTT → EMQX → Celery → TimescaleDB → API با محدودیت RLS. جریان کامل داده.",
  },
  "sec-alert-title": { en: "Alert Engine", fa: "موتور هشدار" },
  "sec-alert-desc": {
    en: "Real-time breach detection with 3-tier escalation, LOS watchdog, and farm correlation.",
    fa: "تشخیص نقض لحظه‌ای با تشدید سه‌سطحی، نگهبان LOS و همبستگی مزرعه.",
  },
};

let currentLang = localStorage.getItem("morghdari-lang") || "en";

function applyLang(lang) {
  currentLang = lang;
  localStorage.setItem("morghdari-lang", lang);
  document.documentElement.lang = lang === "fa" ? "fa" : "en";
  document.documentElement.dir = lang === "fa" ? "rtl" : "ltr";

  document.querySelectorAll("[data-i18n]").forEach((el) => {
    const key = el.getAttribute("data-i18n");
    const entry = I18N[key];
    if (entry && entry[lang] !== undefined) {
      el.textContent = entry[lang];
    }
  });

  // Toggle button label
  const toggleBtn = $("lang-toggle");
  if (toggleBtn) {
    toggleBtn.textContent = lang === "en" ? "فارسی" : "English";
  }
}

function toggleLang() {
  applyLang(currentLang === "en" ? "fa" : "en");
}

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

/* demo endpoints live at site root (/demo/api/...) NOT under /api/v1 */
async function fetchJSON(path, token) {
  const started = performance.now();
  let status = 0;
  let data = null;
  try {
    const response = await fetch(path, {
      headers: { Authorization: `Bearer ${token}` },
    });
    status = response.status;
    data = await response.json().catch(() => null);
    return { status, ok: response.ok, data };
  } catch (error) {
    return { status: 0, ok: false, data: { error: String(error) } };
  } finally {
    logEvent("GET", path, status, Math.round(performance.now() - started));
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
  const claimsBox = $(slot === "A" ? "claims-a" : "identity-b");
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
    (profile.is_platform_staff
      ? ' <span class="badge staff">platform_staff</span>'
      : "") +
    `<br><span class="hint">org: ${esc(profile.org_name ?? "— (org-less)")}</span>`;
  if (claimsBox && slot === "A") {
    claimsBox.hidden = false;
    claimsBox.textContent =
      "decoded claims:\n" + JSON.stringify(claims, null, 2);
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
  const result = await api("POST", "/auth/login/", {
    body: { username, password },
  });
  busy(button, false);
  if (!result.ok) {
    show(
      `identity-${slot.toLowerCase()}`,
      `✗ login failed (${result.status}): ${result.data?.detail || result.data?.error || "invalid credentials"}`
    );
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
      : '<span class="hint">off</span>'
    : '<span class="hint">—</span>';
  return (
    `<tr><td>${member.member_id}</td><td>${esc(member.username)}</td>` +
    `<td>${roleBadges([member.role])}</td><td>${active}</td><td>${action}</td></tr>`
  );
}

async function listMembers(slot = "A") {
  const session = SLOTS[slot];
  if (!session) return;
  const result = await api("GET", "/org/members/", {
    token: session.access,
  });
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
  table.querySelector("tbody").innerHTML =
    rows.map((m) => memberRow(m, myRoles)).join("");
  emptyNote.hidden = rows.length > 0;
  if (rows.length === 0) {
    emptyNote.hidden = false;
    emptyNote.textContent = I18N["s4-empty"][currentLang];
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
    await listMembers();
  }
  const banner = $("members-empty");
  banner.hidden = false;
  banner.textContent =
    `invite → ${result.status}` +
    (result.status === 201
      ? " created (unusable password)"
      : result.status === 200
        ? " INACTIVE membership REACTIVATED (same row reused)"
        : result.status === 409
          ? " conflict: an ACTIVE membership already exists"
          : result.status === 403
            ? " forbidden: owner role required"
            : ` ${JSON.stringify(result.data)}`);
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
  const listing = await api("GET", "/org/members/", {
    token: owner.access,
  });
  const username = victim.profile?.username;
  const row = (Array.isArray(listing.data) ? listing.data : []).find(
    (m) => m.username === username && m.is_active
  );
  if (!row) {
    show(
      "off-out",
      "no ACTIVE membership found for Session-B user — nothing to deactivate"
    );
    return;
  }
  const result = await api(
    "POST",
    `/org/members/${row.member_id}/deactivate/`,
    { token: owner.access }
  );
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
    show(
      "off-out",
      `refresh: ✗ ${refreshed.status} (unexpected — refresh stays valid by contract)`
    );
    return;
  }
  const claims = decodeJwt(refreshed.data.access);
  out.push(
    `refresh → 200 ✓  org_id claim present: ${"org_id" in claims ? "YES ✗" : "NO ✓"}`
  );
  victim.access = refreshed.data.access;

  const read = await api("GET", "/org/members/", {
    token: victim.access,
  });
  out.push(
    `GET /org/members/ → HTTP ${read.status}, payload: ${JSON.stringify(read.data)}`
  );
  out.push("");
  out.push(
    read.status === 200 &&
      Array.isArray(read.data) &&
      read.data.length === 0
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
  show(
    "sup-out",
    `blank incident → ${result.status}\n${result.status === 400 ? "✓ audit anchor enforced" : "unexpected"}`
  );
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

/* ── v0.3 telemetry explorer ─────────────────────────────── */

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
  for (const [key, id] of [
    ["slot_id", "tx-slot"],
    ["since", "tx-since"],
    ["until", "tx-until"],
  ]) {
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
      const delay =
        r.buffered_delay_mins === null ? "—" : r.buffered_delay_mins;
      return (
        `<tr><td>${esc(r.device_ts)}</td><td>${esc(r.slot_id)}</td>` +
        `<td>${lane}</td><td>${esc(String(delay))}</td>` +
        `<td><code>${esc(JSON.stringify(r.metrics))}</code></td></tr>`
      );
    })
    .join("");
  empty.hidden = rows.length > 0;
  if (rows.length === 0) {
    empty.hidden = false;
    empty.textContent =
      "HTTP 200 with an EMPTY list — fail-closed by design (wrong org, " +
      "deactivated member, or simply no readings in this window).";
  }
}

/* ── v0.4 alert engine ───────────────────────────────────── */

async function fetchAlerts() {
  const btn = $("btn-alerts");
  const session = SLOTS.A;
  if (!session) return;
  btn.classList.add("loading");
  try {
  const result = await fetchJSON(
    "/demo/api/alerts/overview/?limit=15",
    session.access
  );
  if (!result.ok) {
    show(
      "alerts-out",
      `HTTP ${result.status} — ${JSON.stringify(result.data)}`
    );
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
    lines.push(
      `  saloon=${m.saloon_id} ${m.starts_at} → ${m.ends_at} · ${m.reason}`
    );
  }
  if (mutes.length === 0) lines.push("  (none)");
  show("alerts-out", lines.join("\n"));
  } finally { btn.classList.remove("loading"); }
}

async function fetchStateMachines() {
  const btn = $("btn-state");
  const session = SLOTS.A;
  if (!session) return;
  btn.classList.add("loading");
  try {
  const result = await fetchJSON(
    "/demo/api/alerts/state-machines/",
    session.access
  );
  if (!result.ok) {
    show(
      "alerts-out",
      `HTTP ${result.status} — ${JSON.stringify(result.data)}`
    );
    return;
  }
  const lines = [];
  for (const [family, info] of Object.entries(result.data)) {
    lines.push(`${family}: ${info.count} key(s)`);
    for (const k of info.keys.slice(0, 5)) {
      lines.push(
        `  ${k.key} = ${k.value}${k.ttl >= 0 ? ` (ttl ${k.ttl}s)` : ""}`
      );
    }
  }  show("alerts-out", lines.join("\n"));
  } finally { btn.classList.remove("loading"); }
}

async function fetchQualityLive() {
  const btn = $("btn-quality-live");
  const session = SLOTS.A;
  if (!session) return;
  btn.classList.add("loading");
  try {
  const result = await fetchJSON(
    "/demo/api/alerts/quality/",
    session.access
  );
  if (!result.ok) {
    show(
      "quality-live-out",
      `HTTP ${result.status} — ${JSON.stringify(result.data)}`
    );
    return;
  }
  const lines = [];
  lines.push("MTTR seconds by tier:");
  for (const [tier, sec] of Object.entries(
    result.data.mttr_seconds_by_tier || {}
  )) {
    lines.push(
      `  tier ${tier}: ${sec === null ? "—" : `${sec}s (~${Math.round(sec / 60)}min)`}`
    );
  }
  lines.push("");
  lines.push("ack → resolve seconds:");
  for (const [tier, sec] of Object.entries(
    result.data.ack_to_resolve_seconds || {}
  )) {
    lines.push(`  tier ${tier}: ${sec === null ? "—" : `${sec}s`}`);
  }
  lines.push("");
  lines.push("alerts per flock (open + closed):");
  for (const row of result.data.alerts_per_flock || []) {
    lines.push(`  ${JSON.stringify(row)}`);
  }
  show("quality-live-out", lines.join("\n"));
  } finally { btn.classList.remove("loading"); }
}

/* ── wiring ───────────────────────────────────────────────── */

document.addEventListener("DOMContentLoaded", async () => {
  // Apply saved language
  applyLang(currentLang);

  $("lang-toggle").addEventListener("click", toggleLang);

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

  // connection chip
  const probe = await api("GET", "/org/members/");
  $("conn-chip").textContent =
    probe.status === 401
      ? "backend: reachable · auth required ✓"
      : `backend: unexpected (${probe.status})`;

  refreshButtons();

  /* ── section nav scroll spy ──────────────────────────── */
  const sections = document.querySelectorAll(".content-section[id]");
  const navPills = document.querySelectorAll(".nav-pill");
  if (sections.length && navPills.length) {
    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            const id = entry.target.id;
            navPills.forEach((pill) => {
              pill.classList.toggle(
                "active",
                pill.getAttribute("href") === "#" + id
              );
            });
          }
        });
      },
      { rootMargin: "-20% 0px -60% 0px" }
    );
    sections.forEach((sec) => observer.observe(sec));
  }
});
