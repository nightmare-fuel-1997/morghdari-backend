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
