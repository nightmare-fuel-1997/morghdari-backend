# v0.2 TUTORIAL — build auth & tenancy yourself

> ⚠️ **Pre-spiral draft** — re-validate against `backend_docs/VERSIONS.md` at spiral
> start. **How to use:** sections 1–4 teach, section 5 is yours to do, 6 warns, 7 checks.
> KB lessons: *Security* (#/security), *Actors & Roles* (#/actors). Canonical:
> Security Architecture §3–4.5.

---

## 1. The Mission

Give every human a token that says who they are, which org they serve, and what role
they hold — then make the database itself enforce it. After this version, firing an
employee means their access dies in ≤15 minutes without anyone touching a server.

**Done means:** three roles log in · cross-role = 403 · forged = 401 · refresh is
rotation-free (same token reusable) · support session demands an incident ID and leaves
an audit row.

---

## 2. The Toolbox

| Tool | What | Why here | When NOT |
|---|---|---|---|
| SimpleJWT, **no rotation / no blacklist** | Stateless tokens; refresh token reusable until expiry (owner decision 2026-08-23) | Revocation bound = 15-min access life; accepted risk on the 7-day refresh | Don't lengthen access lifetime for convenience — that's the revocation budget |
| Claims (`org_id`, `roles[]`) | Identity embedded at issue time | Zero membership queries per request | Not for permissions that change mid-token — re-issue instead |
| TenantMiddleware (GUC) | HTTP twin of v0.1's worker helper | One mechanism, two callers | Never set GUC outside a transaction |
| `require_role` decorator | Single RBAC choke point | One place to audit | Never sprinkle `if user.role ==` checks |
| Support session token | 15-min org-scoped staff access | Audited tenant debugging without superuser | Not a backdoor around RBAC — ops whitelist only |
| `account_activity_logs` row per session/op | Non-repudiation ("who looked, why") | GS-SUPPORT accountability promise | Never log incident-less entries |

---

## 3. The Build

### 3.1 Token issuance — claims at birth

```python
class LoginSerializer(TokenObtainPairSerializer):
    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        member = (
            OrgMember.objects.filter(user=user, is_active=True)
            .order_by("joined_at", "id")          # deterministic pick: OLDEST active membership wins
            .first()
        )
        if member is None:
            raise AuthenticationFailed("no active org membership")   # zero active memberships → 401
        token["org_id"] = str(member.org_id)
        extra_roles = set(
            AccountRoleScope.objects.filter(user=user, org=member.org)
            .values_list("role", flat=True))
        token["roles"] = sorted({member.role} | extra_roles)         # sorted set-union — deterministic
        if user.is_platform_staff:
            token["is_platform_staff"] = True
        return token
```

Line-by-line: `get_token` is the single hook JWT claims flow through. The org claim is ONE
active membership, picked deterministically oldest-first (`order_by("joined_at", "id")`) —
multi-org users currently resolve to their oldest active membership; an org selector in
`LoginRequest` is PARKED (Tracker §6). Zero active memberships → 401. `roles[]` is built as
the sorted set-union of the `org_members` base role and any `account_role_scopes` roles for
that org. `is_platform_staff` travels separately from roles because platform identity and
tenant identity are different axes (D15).

### 3.2 Refresh semantics — the honest revocation story

```python
SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(minutes=15),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "ROTATE_REFRESH_TOKENS": False,      # owner decision 2026-08-23 (Tracker §4i)
    "BLACKLIST_AFTER_ROTATION": False,
}
```

Refresh tokens are REUSABLE until their natural 7-day expiry — there is no rotation and
no blacklist. The accepted risk is written down in Security §3.2: a leaked refresh token
is valid for up to 7 days. Your revocation guarantee is therefore ONLY the 15-minute
access lifetime; keep it short, test it, and never "fix" an auth annoyance by extending
it. If a hard-revocation need ever appears, that's a new written decision — not a
quiet settings flip.

### 3.3 Middleware — one GUC, two callers

```python
class TenantMiddleware:
    def __call__(self, request):
        jwt_claims = getattr(request, "jwt_claims", None)   # verify-once stash from CachedJWTAuthentication
        if jwt_claims and jwt_claims.get("support_session"):
            org_id = jwt_claims["org_id"]               # ① support session → GUC regardless of staff flag
        elif getattr(request.user, "is_platform_staff", False):
            return self.get_response(request)           # ② platform staff → skip GUC entirely
        elif jwt_claims and jwt_claims.get("org_id"):
            org_id = jwt_claims["org_id"]               # ③ authenticated + org_id → GUC
        else:
            return self.get_response(request)           # ④ pass-through — views/DRF decide later
        with transaction.atomic():
            with connection.cursor() as cur:
                cur.execute("SELECT set_config('app.current_org_id', %s, true)",
                            [org_id])
            return self.get_response(request)   # INSIDE atomic() — the GUC must outlive the view
```

Identical mechanics to v0.1's `tenant_context()` — same GUC name, same transaction
scoping, parameterized value. Platform ADMIN takes the separate BYPASSRLS connection
(morghdari_admin); support NEVER does — its scoped session token carries the org claim
instead (§4.5), so ordinary RLS applies to staff too. Full failure-mode rules (garbage
tokens, missing claims, branch ordering) live canonically in Security Architecture §4.1 —
this tutorial only echoes them.

### 3.4 RBAC — one decorator

```python
def require_role(*roles):
    def deco(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if not set(roles) & set(request.auth.get("roles", [])):
                return JsonResponse({"error": "forbidden"}, status=403)
            return view(request, *args, **kwargs)
        return wrapped
    return deco
```

Farm/saloon narrowing comes from `account_role_scopes` (NULL column = all at that
level). All narrowing logic lives HERE — scattered role checks are how privilege
escalation bugs are born.

### 3.5 The support-session endpoint — policy in 20 lines

```python
@api_view(["POST"])
def support_session(request):
    if not request.user.is_platform_staff or request.user.is_superuser:
        return JsonResponse({"error": "forbidden"}, status=403)     # staff ≠ superuser
    body = json.loads(request.body or "{}")
    incident = (body.get("incident_id") or "").strip()
    if not incident:
        return JsonResponse({"error": "incident_id required"}, status=400)
    token = AccessToken.for_user(request.user)
    token["org_id"], token["support_session"], token["incident_id"] = (
        body["org_id"], True, incident)
    AccountActivityLog.objects.create(
        actor=request.user, org_id=body["org_id"], action="SUPPORT_ORG_ENTER",
        details={"incident_id": incident, "reason": body.get("reason", "")})
    return JsonResponse({"org_id": body["org_id"],
                         "incident_id": incident, "expires_in_sec": 900})
```

Read it as four policies: staff-not-superuser only · mandatory incident · 15-min scoped
token · audit BEFORE success returns. The middleware then treats the holder as a user
of exactly that org — RLS does the rest. Note that branch-① precedence in the middleware
(Security §4.1) is what makes this scoped token effective even though its holder has
`is_platform_staff=True` — without it, branch ② would skip the GUC for staff entirely.
Ops whitelist endpoints check the SAME session's incident id on every call.

### 3.6 Tests worth stealing

```python
@pytest.mark.parametrize("role,url,expected", [
    ("worker", "/api/v1/provisioning/sync/", 403),
    ("manager", "/api/v1/farms/1/overview/", 200),
    ...])                        # the whole permission matrix in one function
def test_role_based_access(...): ...          # canonical name — Testing Strategy §2.4
def test_jwt_auth_required(): missing/garbled token → 401   # canonical — §2.4
def test_websocket_org_membership_check(): cross-org WS → close 4003
```

---

## 4. Cheat Sheet

| Command / concept | Purpose | Key detail | Gotcha |
|---|---|---|---|
| POST `/auth/login/` | Issue token pair w/ claims | Claims frozen at issue | Role change needs new login; returns {access_token, refresh_token, user{profile}} — asymmetric BY CONTRACT |
| POST `/auth/token/refresh/` | Fresh access from refresh token | Same token reusable (no rotation) | Client keeps the refresh token for its full 7-day life; returns native {access} only — do NOT unify with login's shape |
| `require_role("manager")` | View-level RBAC | Reads token roles | Scope narrowing is separate |
| `support_session` | Scoped staff debugging | Mandatory incident_id | Superuser deliberately rejected |
| `SUPPORT_ORG_ENTER` audit row | Accountability | details.incident_id | Written BEFORE returning success |

---

## 5. Your Subtasks

1. **Install SimpleJWT, add the settings block.** Verify: bare login issues a pair.
   Notice: rotation flags are settings, not code.
2. **Write the claim serializer; decode a token.** Verify: `org_id` + `roles` visible.
   Notice: claims are signed but READABLE — never put secrets in them.
3. **Wire the no-rotation settings; refresh twice with the same token.** Verify: BOTH
   refreshes succeed; no blacklist rows exist. Notice: revocation now rests entirely on
   the 15-min access life — say the accepted risk aloud.
4. **Add TenantMiddleware; write the fail-closed test.** Verify: authenticated request
   without org claim → empty result, not 500. Notice: identical GUC as v0.1.
5. **Build the RBAC decorator + matrix test.** Verify: parametrized suite green.
   Notice: adding a role later = one table row, zero code.
6. **Build the support-session endpoint; try it WITHOUT incident_id.** Verify: 400.
   Notice: the 400 IS the compliance feature.
7. **Prove the audit row lands; decode the scoped token.** Verify: SUPPORT_ORG_ENTER +
   15-min expiry visible. Notice: RLS now applies to staff.
8. **Offboarding test:** deactivate a member; prove access dies ≤15 min. Notice: no
   code ran to revoke — time did.

---

## 6. Pitfalls

| Mistake | Symptom |
|---|---|
| Long-lived access tokens "for convenience" | Offboarding stops working; you won't notice for months |
| Role checks copied into individual views | Privilege matrix drifts; audits impossible |
| GUC set outside transaction | Leak across pooled connections — intermittent cross-tenant reads |
| Support endpoint accepting blank incident ids | Audit trail becomes fiction |
| Testing only happy-path auth | Forged/expired tokens reach production |

---

## 7. Prove It

1. An engineer is fired at 14:00 holding an access token AND the refresh token. Exactly
   when does their API write access end, and what window of risk remains on the refresh
   token? *(§3.2 + Security §3.2 accepted risk.)*
2. Why does the support session carry `org_id` in the token instead of opening a
   BYPASSRLS connection? *(§3.3/§3.5.)*
3. Where does farm-level narrowing live — the token, the decorator, or a table? Explain
   the split. *(§3.4.)*
4. What breaks FIRST if `set_config` loses its transaction-scoped flag? *(§3.3.)*
5. Which single test would have caught a missing `incident_id` check? *(§3.5/§5.)*
