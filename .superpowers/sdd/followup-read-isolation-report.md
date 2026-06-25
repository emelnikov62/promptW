# followup-read-isolation-report

Date: 2026-06-25

## Part A — Gated read endpoints

### list_query change (api/admin_routes.py ~line 148)

Added `require="admin"` keyword arg. At top of function:

**Before:**
```python
admin_id = _require_admin(request)
```

**After:**
```python
if require == "owner":
    admin_id = await _require_role(request, "owner")
else:
    admin_id = _require_admin(request)
```

### Per-handler changes

| Handler | Route | Before | After |
|---|---|---|---|
| admin_face_stats | GET /api/admin/face-stats | `_require_admin(request)` | `await _require_role(request, "owner")` |
| admin_payment_detail | GET /api/admin/payments/{pid} | `_require_admin(request)` | `await _require_role(request, "owner")` |
| admin_payments (via list_query) | GET /api/admin/payments | list_query default | `list_query(..., require="owner")` |
| admin_withdrawals (via list_query) | GET /api/admin/withdrawals | list_query default | `list_query(..., require="owner")` |
| admin_templates_list | GET /api/admin/templates | `_require_admin(request)` | `await _require_role(request, "owner")` |
| admin_template_get | GET /api/admin/templates/{tpl_id} | `_require_admin(request)` | `await _require_role(request, "owner")` |
| admin_promos_list | GET /api/admin/promos | `_require_admin(request)` | `await _require_role(request, "owner")` |
| admin_referrals | GET /api/admin/referrals | `_require_admin(request)` | `await _require_role(request, "owner")` |
| admin_referral_detail | GET /api/admin/referrals/{tg_id} | `_require_admin(request)` | `await _require_role(request, "owner")` |
| admin_audit_log | GET /api/admin/audit | `_require_admin(request)` | `await _require_role(request, "owner")` |
| admin_notif_overview | GET /api/admin/notif/overview | `_require_admin(request)` | `await _require_role(request, "owner")` |
| admin_notif_log | GET /api/admin/notif/log | `_require_admin(request)` | `await _require_role(request, "owner")` |

### Untouched (must-stay on _require_admin)

admin_me, admin_stats, admin_stats_timeseries, admin_users, admin_user_detail,
admin_generations (delegates to list_query with default require="admin"),
admin_support_list, admin_support_agents_list, admin_support_detail — all verified unchanged.

## Part B — Cosmetic cleanups

### B1: lineChart token fix (webapp/static/js/admin.js ~line 230)

- `fill="var(--accent-soft,rgba(255,107,92,.15))"` → `fill="var(--photo-soft,rgba(255,107,92,.15))"`
- `stroke="var(--accent,#FF6B5C)"` → `stroke="var(--photo,#FF6B5C)"`

### B2: dt-retry id → class (webapp/static/js/admin-table.js ~line 25)

- `id="dt-retry"` → class merged into existing class list: `class="btn btn-outline btn-sm dt-retry"`
- `mount.querySelector("#dt-retry")` → `mount.querySelector(".dt-retry")`

### B3: loadWithdrawals arg fix (webapp/static/js/admin.js ~line 116)

- `loadWithdrawals(0)` → `loadWithdrawals()` in nav loaders map

## Version bumps (webapp/templates/admin.html)

- `admin-table.js?v=1` → `admin-table.js?v=2`
- `admin.js?v=24` → `admin.js?v=25`
- admin.css unchanged (no CSS change)

## Static check outputs

```
python ast.parse: parse OK
node --check admin.js: clean
node --check admin-table.js: clean
```
