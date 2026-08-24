# v0.5 TUTORIAL — build provisioning + zero-touch yourself

> ⚠️ **Pre-spiral draft** — re-validate at spiral start. **How to use:** 1–4 teach,
> 5 is hands-on, 6 warns, 7 checks. KB lessons: *Provisioning Lifecycle*
> (#/provisioning), *Security* (#/security). Canonical: zero-touch design doc, Saas.md
> §9/§18, Security §5.1.

---

## 1. The Mission

Make hardware untrustworthy by default. A gateway that just walked out of its box can
fetch its configuration and talk — but every byte it sends waits in a buffer until a
human with the right role says "I have seen this device, let it in." And when
credentials must change, the swap completes only when the DEVICE confirms.

**Done means:** pending → approve drains in order · reject discards · suspended fails
at EMQX · installer cannot self-approve · rotation ACK-commits end-to-end.

---

## 2. The Toolbox

| Tool | What | Why here | When NOT |
|---|---|---|---|
| `transition()` service function | Atomic status flip + audit + cache delete | One door = auditable state machine | Never `.save()` provisioning_status directly anywhere else |
| Provisioning audit log | Non-repudiation of every flip | Answers "who approved what, when" | Not optional even for "temporary" flips |
| `gateway:status:{slot}` cache key | Fast subscriber gating checks | Every message checks status | Must be invalidated IN the transition transaction |
| Basic auth vs bcrypt credentials | Gateway config-endpoint login | EMQX verifies the SAME hashes natively | Never Django pbkdf2 hashes — EMQX can't parse them |
| Pending buffer (`telemetry:pending_approval`) | Holds pre-approval telemetry | Approval drains it; rejection discards it (R3) | Not for SUSPENDED/REJECTED — those discard |
| Staged rotation (`pending_password_hash`) | Two-phase credential swap | Commit only on gateway ACK | Never email credentials; never swap without ACK |
| Platform review permission (GS-BOARD) | Admin/support default, extensible set | No org-board role exists | Don't hardcode two usernames — use the permission |

---

## 3. The Build

### 3.1 The one-door transition service

```python
@transaction.atomic
def transition(slot_id, new_status, by_user, reason=""):
    slot = GatewaySlot.objects.select_for_update().get(pk=slot_id)
    ProvisioningAuditLog.objects.create(
        gateway_slot_id=slot_id, previous_status=slot.provisioning_status,
        new_status=new_status, changed_by=by_user, reason=reason)
    slot.provisioning_status = new_status
    slot.save()
    cache.delete(f"gateway:status:{slot_id}")
```

Four things in ONE transaction: row lock (`select_for_update` kills concurrent-flip
races), audit row (non-repudiation), the flip itself, cache invalidation (AU-STATUS-
CACHE — stale cache meant an hour of wrong gating). Every state change in the system
calls THIS; grep-enforce it.

### 3.2 Review permission — extensible platform side (GS-BOARD)

```python
def can_review_gateway_requests(user):
    if user.is_superuser:
        return True
    if user.is_platform_staff and user.staff_role in {"admin", "support"}:
        return True
    return PlatformPermission.objects.filter(          # your future delegates go here
        user=user, codename="gateway_review").exists()
```

Defaults today (System Admin + Platform Support); your additions tomorrow are rows,
not deploys. There is no org-board role and never will be — the docs say so.

### 3.3 Sync endpoint — the installer proposes, the server disposes

```python
@api_view(["POST"])
def provisioning_sync(request):
    require_installer_role(request, body["org_id"])          # scope-checked, AU-fix
    hw = GatewayHardwareRegistry.objects.get(
        slot_id=body["gateway_slot_id"], removed_at__isnull=True)   # ACTIVE row only
    for m in body["sensor_slot_mappings"]:
        GatewaySensorMapping.objects.update_or_create(
            gateway_hardware_id=hw.hardware_id, pack_id=m["pack_id"],
            defaults={"slot_id": m["slot_id"], "org_id": org})
    transition(body["gateway_slot_id"], "PENDING_APPROVAL", request.user)
```

Three trust rules in one endpoint: installers assert TOPOLOGY (what goes where) but
never HARDWARE IDs (derived from the registry — they can't claim arbitrary devices);
the result is always PENDING_APPROVAL (installers can't self-approve — D18); org comes
from their token, not the payload. A fourth rule guards concurrency: the handler first
acquires `lock:sync:{gateway_slot_id}` (Redis `SET NX EX 30`) — a second simultaneous
sync for the same gateway gets **409 Conflict + Retry-After**, never a silent last-wins
merge over half-written topology (AU-RACES, Blueprint §6).

### 3.4 Subscriber branch — where trust meets telemetry

```python
status = status_cache(gateway_slot)                # cached, invalidated on transitions
if status == "PENDING_APPROVAL":
    redis.xadd("telemetry:pending_approval", entry);  return
if status in ("REJECTED", "SUSPENDED"):
    archive_or_discard(entry);  return             # R3: discard ONLY these two states
redis.xadd("telemetry:realtime", entry)
```

Buffered-never-lost is the decision that survived three audits. Approval dispatches
`drain_pending_approval` which replays entries IN ORDER into the live lane.

### 3.5 Config endpoint — the zero-touch handshake

```python
def config_endpoint(request, gateway_slot_id):
    cred = MqttDeviceCredential.objects.filter(device_id=gateway_slot_id, is_active=True).first()
    if not cred or not bcrypt.checkpw(password.encode(), cred.password_hash.encode()):
        return HttpResponse(status=401)            # same hash EMQX verifies
    if slot.provisioning_status in ("REJECTED", "SUSPENDED"):
        return HttpResponse(status=403)
    return JsonResponse(build_config(slot))        # org_id + farm_id + zones; NO slot_ids
```

bcrypt because EMQX's `password_based:bcrypt` reads the identical hash — one credential,
two authenticators. Nginx rate-limits this view to 1 r/s/IP (brute-force guard).
The payload carries `org_id`/`farm_id` (topic prefix needs them) and OMITS slot ids
(R2): gateways publish bare `pack_id`s; mappings live server-side where they stay true.

### 3.6 Rotation — commit on ACK

```python
# stage (never activates anything)
cred.pending_password_hash = make_bcrypt(new_password)
cred.pending_since = now(); cred.save()
send_command(gateway_slot_id, "ROTATE_CREDENTIAL", {...})

# on ACK envelope from the gateway:
with transaction.atomic():
    locked = MqttDeviceCredential.objects.select_for_update().get(device_id=gateway_slot_id)
    locked.password_hash = locked.pending_password_hash     # atomic swap
    locked.pending_password_hash = None
    locked.last_rotated_at = now(); locked.save()
```

EMQX re-reads `password_hash` per connection attempt → after the swap only the device
holding the NEW password authenticates. Alert if `pending_since` ages >24 h — a staged
rotation the gateway never confirmed is a lost device or a MITM. Command envelopes are
recognized by the presence of `command_uuid` (firmware §8.4 discrimination rule).

---

## 4. Cheat Sheet

| Concept | Purpose | Key detail | Gotcha |
|---|---|---|---|
| PENDING_INSTALLATION → PENDING_APPROVAL → PROVISIONED / REJECTED / SUSPENDED | Lifecycle | Only PROVISIONED processes telemetry | PENDING_SYNC exists only on the legacy installer-PWA path, never in the zero-touch chain; REJECTED is terminal |
| `transition()` | Single door | lock+audit+flip+cache in one tx | direct saves bypass audit |
| `/provisioning/sync/` | Installer batch submit | installer role scoped per org | never trusts client hardware ids |
| `/gateways/{id}/config/` | Zero-touch fetch | Basic auth = slot + MQTT password | 429 via nginx 1 r/s zone |
| pending buffer + drain | Pre-approval holding | FIFO order preserved | reject DISCARDS, approve REPLAYS |
| ROTATE_CREDENTIAL + ACK commit | Credential rotation | bcrypt both sides | pending >24 h = alert |

---

## 5. Your Subtasks

1. **Write `transition()` first; forbid direct saves** (grep check in CI). Verify:
   every flip lands an audit row. Notice: you built the state machine's spine.
2. **Build gateway-request flow + the extensible review permission.** Verify: support
   user reviews; plain manager cannot. Notice: adding a reviewer later = one row.
3. **Implement sync with server-derived hardware ids.** Verify: mapping rows exist
   after a simulated sync. Notice: what the installer CAN'T send is the security.
4. **Add the subscriber status branch.** Verify: pending telemetry accumulates, none
   reaches Timescale. Notice: buffering ≠ processing.
5. **Build approve/reject (+ drain).** Verify: approve replays FIFO; reject empties.
   Notice: order preservation matters for replay math.
6. **Expose the config endpoint behind bcrypt Basic auth.** Verify: wrong password 401;
   rejected slot 403. Notice: nginx owns rate limiting, not Django.
7. **Wire suspension into EMQX ACL.** Verify: suspended gateway's publishes rejected.
   Notice: revocation lives at the broker too, not only in Django.
8. **Implement staged rotation end-to-end with the simulator.** Verify: old password
   dead post-ACK, new one alive. Notice: nothing changed until the ACK arrived.
9. **Concurrency test:** fire two syncs for the same gateway simultaneously. Verify:
   one 200, one **409 + Retry-After**; topology intact, no partial rows. Notice: the
   lock converts a race into a retry — that's the whole fix.
10. **Run R3/R4/R6 suites green; record demo.** Verify: tag-ready. Notice: these
    suites encode decisions older than your employment here.

---

## 6. Pitfalls

| Mistake | Symptom |
|---|---|
| Status cache not invalidated in-tx | Up to TTL hours of wrong gating after approve/suspend |
| Installer-supplied hardware_id | Device impersonation — someone else's serial becomes yours |
| pbkdf2 hashes handed to EMQX | Every gateway auth fails mysteriously |
| Swapping credentials without ACK | Gateway bricked in the field, physically far away |
| Approving via ORM outside `transition()` | Missing audit row; cache stays stale |

---

## 7. Prove It

1. Why does `morghdari_app` creating a mapping need the ACTIVE registry row instead of
   trusting the request? State the attack it prevents. *(§3.3.)*
2. Walk one telemetry packet through the subscriber when status = PENDING_APPROVAL.
   Where exactly does it stop, and what single event releases it? *(§3.4.)*
3. Which TWO authenticators accept the same bcrypt hash, and why is that identity the
   linchpin of rotation? *(§3.5/§3.6.)*
4. What is dangerous about a `pending_since` older than 24 h, and who gets told?
   *(§3.6.)*
5. Why is the review permission a table row rather than a hardcoded role list? Recite
   the owner's requirement verbatim. *(§3.2/GS-BOARD.)*
