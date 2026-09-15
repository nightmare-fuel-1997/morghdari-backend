"""Throttles — M6-10 (contract v1.7.0 O10).

The v1.5.0 surface carries NO default DRF throttle (nginx owns coarse
limits; adding one now would 429 existing paths the contract never
documented as rate-limited). The ONE new strict lane is the O10 password
change: 3/min PER USER (spec §2) — a brute-force gate on the credential
rotation path. Scope is hardcoded here (not a view attribute) because
api_view-wrapped FBVs lose view attributes; SimpleRateThrottle reads the
rate from THROTTLE_RATES[scope] (additive REST_FRAMEWORK key, base.py).

فارسی: تنها مسیرِ سخت‌گیرانهٔ نرخ، تغییرِ رمز عبور است — سه بار در دقیقه به‌ازای
هر کاربر؛ بقیهٔ مسیرها بدونِ نرخ‌گیرِ پیش‌فرض می‌مانند تا قراردادِ ۱.۵.۰ دست‌نخورده بماند.
"""

from rest_framework.throttling import SimpleRateThrottle


class UserRateThrottle3PerMinute(SimpleRateThrottle):
    """O10's auth_strict lane: 3/min keyed by username (then IP)."""

    scope = "auth_strict"

    def get_cache_key(self, request, view):
        user = getattr(request, "user", None)
        ident = user.get_username() if (user and user.is_authenticated) else self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}


class LoginRateThrottle(SimpleRateThrottle):
    """BUG-5 (QA 1.26): login brute-force gate — auth_login 60/min per ATTEMPTED username.

    WHY: POST /auth/login/ carried no DRF throttle, so one account could be
    bombed without limit. Keyed by the attempted username (NOT the shared
    client IP) so the test suite's distinct-user bursts — densest module 43
    total logins, densest single username 13 (ops-engineer in m6_10) — never
    collide with the 60/min lane, while a single-account burst still 429s.

    فارسی: نرخ‌گیرِ ورود — ۶۰ در دقیقه به‌ازای نامِ کاربریِ تلاش‌شده (نه IP) تا
    انفجارِ آزمون‌ها خطا ندهد ولی حمله به یک حساب بسته شود.
    """

    scope = "auth_login"

    def get_cache_key(self, request, view):
        username = ""
        try:
            data = getattr(request, "data", None)
            getter = getattr(data, "get", None)
            if callable(getter):
                username = str(getter("username") or "").strip().lower()
        except Exception:
            username = ""
        ident = f"login:{username}" if username else self.get_ident(request)
        return self.cache_format % {"scope": self.scope, "ident": ident}
