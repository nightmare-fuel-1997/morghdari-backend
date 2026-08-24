/*
 * Owner console logic — deliberately tiny. فارسی: منطقِ کنسول مالک؛ عمداً کوچک.
 * v0.1 has exactly ONE live element: the health panel polling
 * GET /api/v1/health/. That endpoint ships in v0.6 (CONSTRAINTS C-06), so
 * until then the panel must honestly show «در دسترس نیست», never fake data.
 */
(function () {
  "use strict";

  var STORAGE_KEY = "morghdari.owner.backendUrl";
  var urlInput = document.getElementById("backend-url");
  var saveBtn = document.getElementById("save-url");
  var refreshBtn = document.getElementById("refresh-health");
  var statusEl = document.getElementById("health-status");
  var detailsEl = document.getElementById("health-details");

  function backendBase() {
    return (localStorage.getItem(STORAGE_KEY) || urlInput.value || "").replace(/\/+$/, "");
  }

  function setStatus(kind, text) {
    statusEl.className = "status " + kind;
    statusEl.textContent = text;
  }

  function renderDetails(payload) {
    if (!payload || typeof payload !== "object") {
      detailsEl.classList.add("hidden");
      return;
    }
    detailsEl.innerHTML = "";
    Object.keys(payload).forEach(function (key) {
      var dt = document.createElement("dt");
      var dd = document.createElement("dd");
      dt.textContent = key;
      dd.textContent = JSON.stringify(payload[key]);
      detailsEl.appendChild(dt);
      detailsEl.appendChild(dd);
    });
    detailsEl.classList.remove("hidden");
  }

  function checkHealth() {
    var base = backendBase();
    if (!base) {
      setStatus("bad", "No backend URL configured — آدرس بک‌اند تنظیم نشده است");
      return;
    }
    setStatus("unknown", "checking… در حال بررسی");
    fetch(base + "/api/v1/health/", { headers: { Accept: "application/json" } })
      .then(function (res) {
        return res.json().then(function (body) {
          return { ok: res.ok, body: body };
        });
      })
      .then(function (r) {
        setStatus(r.ok ? "ok" : "bad", r.ok ? "UP — فعال" : "HTTP error — خطا");
        renderDetails(r.body);
      })
      .catch(function () {
        // Expected until v0.6: endpoint does not exist yet.
        setStatus(
          "bad",
          "unavailable — در دسترس نیست (endpoint ships in v0.6; CONSTRAINTS C-06)"
        );
        detailsEl.classList.add("hidden");
      });
  }

  saveBtn.addEventListener("click", function () {
    localStorage.setItem(STORAGE_KEY, urlInput.value.trim());
    checkHealth();
  });
  refreshBtn.addEventListener("click", checkHealth);

  checkHealth();
})();
