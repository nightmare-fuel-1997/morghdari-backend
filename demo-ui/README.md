# Morghdari v0.2 Demo Console | کنسول نمایش نسخهٔ ۰٫۲

Standalone, dependency-free web UI showcasing the **v0.2 auth & tenancy**
capabilities to a reviewer/manager. Served by Django itself so the browser and
the API share one origin (no CORS surface).

رابط وبِ مستقل و بدون وابستگی برای نمایش قابلیت‌های نسخهٔ ۰٫۲؛ توسط جنگو سرو
می‌شود تا مرورگر و API هم‌مبدأ باشند.

## Run | اجرا

```powershell
# 1. dev stack up (timescale + redis + emqx)
docker compose -f docker-compose.dev.yml up -d

# 2. migrations + a seed owner for the demo (first time only)
backend\.venv\Scripts\python.exe backend\manage.py migrate
backend\.venv\Scripts\python.exe backend\manage.py shell -c "from django.contrib.auth import get_user_model as U; u=U().objects.create(username='demo-owner'); u.set_password('demo-pass-123'); u.save(); import apps.accounts.models as m, apps.tenants.models as t; o=t.Organization.objects.first() or t.Organization.objects.create(name='Demo Farm'); m.OrgMember.objects.create(user=u, org=o, role='owner')"

# 3. start Django with the demo route enabled
set MORGHDARI_DEMO_UI=1
backend\.venv\Scripts\python.exe backend\manage.py runserver

# 4. open the console
start http://127.0.0.1:8000/demo/
```

The route is GATED: it appears only when `DEBUG` is on or
`MORGHDARI_DEMO_UI=1`. The production image never serves it.

## The guided tour | مسیر نمایش

| # | Card | Capability shown |
|---|------|------------------|
| 1 | Sign in | JWT login; decoded claims (org_id / roles / staff flag) |
| 2 | No-rotation proof | same refresh twice → both 200 (accepted risk §3.2) |
| 3 | Tamper test | flipped signature → 401 |
| 4 | Org members | list (any member) · invite/deactivate (owner-only, buttons auto-disable) |
| 5 | Offboarding chain | deactivate Session-B → B's refresh yields org-less token → reads = HTTP 200 EMPTY (C-14) |
| 6 | Support session | staff-only; blank incident 400; audited 900s scoped token |
| 7 | Event log | every request/response, timestamped |

Suggested script for the manager: log in A as `demo-owner`, log in B as a
worker you invite from card 4, then walk cards 2 → 3 → 5 → 6.
