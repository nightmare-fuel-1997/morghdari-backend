# owner-console — EXCLUSIVE to the platform owner

> EN: A static HTML/CSS/JS console for Morghdari's OWNER. It is a local
> operations view, NOT a product surface.
> فارسی: کنسول اختصاصی مالک پلتفرم؛ یک نمای عملیاتیِ لوکال است، نه محصول.
>
> ⚠️ قواعد جدایی‌ duties (separation-of-duties):
> - This app is EXCLUSIVE to the owner. It is NEVER shipped, bundled,
>   deployed, or served by the backend. It never leaves this machine.
> - It is OUTSIDE every API contract: the real frontend is another team's
>   product. Nothing here defines or implies an endpoint shape.
> - No credentials live in these files; the login form is a PLACEHOLDER.

## Run it

```
serve.bat          # starts a local static server on http://localhost:8788
```

(Or any static server: `python -m http.server 8788` in this folder.)

## What works at v0.1

- Health panel polling `GET /api/v1/health/` on the configured backend URL —
  shows «در دسترس نیست / unavailable» until v0.6 ships that endpoint. That is
  expected, not broken (CONSTRAINTS C-06).
- Everything else (login, dashboards) is an explicitly-marked placeholder so
  the owner can see the intended shell without pretending v0.1 has features.

## Configuration

Backend base URL defaults to `http://localhost:8000`. Override via the gear
field in the header (persisted to localStorage only — nothing leaves the
machine except health polls you initiate against your own backend).
