"""Pack → sensor-slot resolution — cache first, DB fallback, DLQ on miss.

gateway_sensor_mapping maps (org, gateway_hardware_id, pack_id) → slot.
The cache (24 h TTL) sits in front because EVERY reading of a pack needs the
same answer; a miss is NOT cached so provisioning can create the mapping later
without a flush. Callers inject `db_fetch` — the subscriber process wires it to
the Django ORM at integration time (M3-05+), keeping this module ORM-free and
unit-testable with a stub.
فارسی: حلِ پک به اسلات — اول کیشِ ۲۴ ساعته، بعد دیتابیس، و در نبودِ ردیف
UNKNOWN_SLOT؛ نتیجهٔ «یافت‌نشده» کش نمی‌شود تا بعداً با پروویژنینگ ساخته شود.
"""

CACHE_TTL_SECONDS = 24 * 3600  # TUTORIAL §3.1: cache-first, 24 h
UNKNOWN_SLOT = "UNKNOWN_SLOT"  # DLQ reason when no mapping row exists


def cache_key(hardware_id: str, pack_id: str) -> str:
    return f"slot:map:{hardware_id}:{pack_id}"


def resolve_pack_slot(
    redis_client,
    org_id: str,
    hardware_id: str,
    pack_id: str,
    db_fetch=None,
):
    """Return slot_id (int), or UNKNOWN_SLOT sentinel when unmapped.

    `db_fetch(org_id, hardware_id, pack_id)` must return the slot id or None;
    passing None for the callable treats every lookup as unmapped (used before
    the ORM wiring exists in tests/tools).
    """
    key = cache_key(hardware_id, pack_id)
    cached = redis_client.get(key)
    if cached is not None:
        return cached  # slot_id is a CharField pk — never int-cast

    if db_fetch is None:
        return UNKNOWN_SLOT

    slot_id = db_fetch(org_id, hardware_id, pack_id)
    if slot_id is None:
        return UNKNOWN_SLOT  # never cache a miss (TUTORIAL §3.1 note)

    redis_client.set(key, slot_id, ex=CACHE_TTL_SECONDS)
    return slot_id
