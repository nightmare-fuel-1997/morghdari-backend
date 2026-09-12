"""Follow-through topology routes — contract v1.7.2 (spec §1, F2–F8/F14/F15).

Mounted at api/v1/ from config/urls.py, and listed in the contract gate's
`_API_ROOTS` so `implemented == contract` covers it.

Deliberately NOT here (they extend an existing view on the SAME path — one
pattern per route, or the first pattern 405s the second method):

    POST   /farms/                            → api.farm_list      (GET+POST)
    PATCH  /saloons/{saloon_id}/              → api.saloon_detail  (GET+PATCH+DELETE)
    DELETE /saloons/{saloon_id}/              → api.saloon_detail

Nothing in this file can collide with the frozen v1.5/v1.6 paths: every route
below is new, which is why mounting it cannot change any existing behavior.

فارسی: مسیرهای نوشتنِ توپولوژی؛ دو مسیرِ مشترک عمداً اینجا نیستند چون الگوی
تکراری روی یک آدرس، متدِ دوم را ۴۰۵ می‌کند.
"""

from django.urls import path

from apps.farms.api_follow import (
    farm_detail,
    flock_close,
    flock_mortality,
    overview,
    position_create,
    position_update,
    saloon_create,
)

app_name = "farms-follow"

urlpatterns = [
    # F10 — the org home rollup (any member)
    path("overview/", overview, name="overview"),
    # F2/F3 — farm edit + guarded delete (owner). ONE pattern: the path is
    # shared, so PATCH and DELETE live on the same view (a second pattern for
    # `farms/<id>/` would shadow this one and 405 the first method).
    path("farms/<int:farm_id>/", farm_detail, name="farm-detail"),
    # F4 — saloon creation under a farm (manager+)
    path("farms/<int:farm_id>/saloons/", saloon_create, name="saloon-create"),
    # F7/F8 — positions (manager+)
    path(
        "saloons/<int:saloon_id>/positions/",
        position_create,
        name="saloon-position-create",
    ),
    path(
        "saloons/<int:saloon_id>/positions/<int:position_id>/",
        position_update,
        name="saloon-position-update",
    ),
    # F14/F15 — the flock cycle's two missing writers (engineer+)
    path(
        "saloons/<int:saloon_id>/flocks/<int:flock_id>/close/",
        flock_close,
        name="flock-close",
    ),
    path(
        "saloons/<int:saloon_id>/flocks/<int:flock_id>/mortality/",
        flock_mortality,
        name="flock-mortality",
    ),
]
