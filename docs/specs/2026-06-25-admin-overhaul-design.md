# Admin Panel Overhaul — Design Spec

**Date:** 2026-06-25
**Status:** Approved (direction) → spec review
**Scope of THIS spec:** Phase 0 (shared foundation) + Phase 1 (finance & analytics).
Phases 2–3 are outlined as future cycles, each gets its own spec/plan.

## 1. Context & goals

The admin panel (`webapp/templates/admin.html`, `webapp/static/js/admin.js` ~1127 LOC,
`webapp/static/css/admin.css`, `api/admin_routes.py` ~1044 LOC) has 12 sections of very
uneven maturity:

- **Polished:** Dashboard, Users, Templates, Support, Face, Notifications
- **Functional/minimal:** Withdrawals, Promos
- **Read-only stubs:** Generations, Payments, Audit

Cross-cutting capabilities are largely absent: search exists only in Users/Referrals;
no CSV export anywhere; no date/status filtering (except Face period tabs, Support status
tabs); no sorting; no bulk actions; no loading states; user feedback is all blocking
`alert()`/`confirm()`/`prompt()`; errors are silently `console.error`'d.

**Goal:** bring the admin to the level of top-tier SaaS dashboards — complete and
comfortable to use — without changing the stack (vanilla JS, no framework, no bundler;
aiohttp + asyncpg backend; modal-based UI).

**Decisions locked in brainstorming (2026-06-25):**
- Users: a **team** on **both desktop and mobile** → responsive + access roles required.
- Top priorities: **finance & analytics** and **UX polish**.
- Build order: **shared foundation first**, then roll out across sections.
- Roles: **owner + agent** (two-tier, simple) — NOT full RBAC.
- Finance: **analytics + drill-down + refunds/corrections** (actionable, via gateway API).

## 2. Approach

**Chosen: a reusable vanilla-JS component layer.** Build a UI-kit and a `DataTable`
component once, plus a generic backend list helper, then refit every section on top of
them. Matches the existing no-framework stack, maximizes consistency, and removes the
duplication currently spread across `admin.js`.

**Rejected:**
- *Lightweight framework (Preact/Alpine/htmx)* — violates the project rule "no framework,
  no bundler" and complicates collaboration.
- *Server-rendered table partials (htmx-style)* — too large a paradigm shift from the
  current SPA pattern; would rewrite the whole backend rendering layer.

**File organization.** `admin.js` is already large; the foundation adds shared code, so
split it to keep files focused (the project values small, single-purpose files):

```
webapp/static/js/
  admin-kit.js     — UI-kit: toast, confirmDialog, formModal, loaders, apiError, fetch wrapper
  admin-table.js   — DataTable component (config-driven)
  admin.js         — section renderers (consume kit + table), nav, auth bootstrap
```

Load order in `admin.html`: `admin-kit.js` → `admin-table.js` → `admin.js`, each with its
own `?v=N` cache-bust (bump on every change). No modules/imports — globals, `var`, per
project code style.

## 3. Phase 0 — Shared foundation

### 3.1 UI-kit (`admin-kit.js`)

Replaces every blocking browser dialog and silent error.

- **`toast(type, msg, opts)`** — non-blocking notification, `type ∈ {success, error,
  info}`, auto-dismiss (default 4s, errors 6s), stacks top-right, manual close. A fixed
  `#toast-host` container injected once.
- **`confirmDialog({title, body, confirmLabel, danger})` → `Promise<bool>`** — replaces
  `confirm()`. Danger variant = coral destructive button. Enter confirms, Esc/overlay
  cancels.
- **`formModal({title, fields, submitLabel})` → `Promise<values|null>`** — replaces
  `prompt()` chains. `fields` = array of `{name, label, type, value, required, hint,
  options}` (`type ∈ text|number|textarea|select|datetime-local|checkbox`). Returns a
  values object, or `null` if cancelled. Inline validation for `required`/`number`.
- **Loaders:** `tableSkeleton(cols, rows)` (shimmer placeholder rows), `btnBusy(btn, on)`
  (spinner + disable), `inlineSpinner()`.
- **`apiError(err)`** — central handler: maps a failed `api()` call to a `toast('error',…)`
  with a readable message (server `{error}` field if present, else generic). All section
  loaders wrap fetches so failures surface to the user instead of the console.
- **`api()`** — keep existing fetch wrapper (auth header injection, 403 → logout); extend
  to throw a typed error consumed by `apiError`, and to support `blob` responses (CSV).

### 3.2 `DataTable` component (`admin-table.js`)

A factory: `DataTable(mountEl, config)` renders a table and owns its own state
(query/filters/sort/page/selection). Re-fetches from the server on any state change.

**Config schema:**
```
{
  endpoint: "/api/admin/payments",   // GET, returns {rows, total}
  columns: [
    { key, label, sortable?, render?(row), align?, hideOnMobile? }
  ],
  searchable: true,                  // shows debounced search box (server q=)
  searchPlaceholder: "...",
  filters: [                         // optional toolbar controls
    { key:"status", label:"Статус", type:"select", options:[...] },
    { key:"provider", label:"Провайдер", type:"select", options:[...] },
    { type:"daterange", fromKey:"from", toKey:"to", label:"Период" }
  ],
  defaultSort: { key:"created_at", order:"desc" },
  rowAction?(row),                   // click → open detail modal
  bulkActions?: [                    // enables row checkboxes + action bar
    { label, danger?, run(selectedIds) }
  ],
  exportCsv?: true,                  // "Экспорт CSV" → endpoint + &format=csv
  pageSize: 50
}
```

**Behaviors out of the box:** debounced server search (300ms); sortable column headers
(click to toggle asc/desc, server `sort`/`order`); filter toolbar (selects + date range,
server query params); pagination (reuse existing prev/next + "X/Y (total)"); row selection
+ floating bulk-action bar with count; CSV export; explicit **loading / empty / error**
states (skeleton on load, "Ничего не найдено" on empty, retry button on error).

**Responsive:** below 768px each row collapses into a stacked card (label: value pairs
from the same column config; `hideOnMobile` columns dropped); filter toolbar becomes a
collapsible sheet; bulk-action bar pins to the bottom.

The existing polished sections (Users, Templates, Support, Face, Notif) keep their bespoke
renderers for now; DataTable is adopted first by the weak/finance sections and rolled out
to the rest in Phase 3. Detail modals continue to use the modal system; only the
list/table layer is unified.

### 3.3 Backend list helper (`admin_routes.py`)

A shared helper standardizes list endpoints (today each builds SQL by hand):

- **`list_query(request, *, table/base_sql, searchable_cols, sortable_cols, filters,
  serialize)`** — parses `q`, `sort`, `order`, declared filter params, `from`/`to` dates,
  `limit`/`offset`, and `format`. Returns `{rows, total}` JSON, or streams CSV when
  `format=csv`.
- **Allowlists (security):** `sort` validated against `sortable_cols`; filter keys against
  declared filters; `q` parametrized `ILIKE` over `searchable_cols` only. No
  string-interpolation of client input into SQL (parametrized everywhere, per project
  asyncpg pattern).
- **CSV:** streaming `web.StreamResponse`, UTF-8 BOM for Excel, `;`-delimited (RU locale),
  **formula-injection guarded** (prefix `'` on cells starting with `= + - @`). Filename
  `promptw-<section>-<date>.csv`.
- **Audit:** every CSV export and every mutating finance action writes an `_audit` row.

Endpoints keep their current paths and `{rows, total}` shape so the change is backward
compatible; new query params are additive.

### 3.4 Roles — owner + agent

Today auth is a **single shared login/password** (`ADMIN_LOGIN`/`ADMIN_PASSWORD`), and all
admins resolve to one identity (`admin_tg_id = next(iter(ADMIN_IDS))`). Support agents are
a separate `support_agents` table with no panel login. To get real two-tier access we add a
minimal admin-account model:

- **New table `admin_accounts`**: `tg_id (PK)`, `login (unique)`, `password_hash`,
  `role ∈ {owner, agent}`, `created_at`, `disabled`. Seed the existing
  `ADMIN_LOGIN`/`ADMIN_PASSWORD` as the first `owner` on startup (idempotent migration in
  `database.py`), so nothing breaks for the current operator.
- **Login** verifies against `admin_accounts` (fallback to env owner if table empty);
  `make_admin_token` carries the `role` claim; `_require_admin` keeps requiring
  `admin_scope` + `ADMIN_IDS`, and a new **`_require_role("owner")`** gates owner-only
  endpoints.
- **Permission matrix (v1):**
  - **owner** — all 12 sections, all actions.
  - **agent** — Support (full), Users (read-only: view + ticket context, no
    adjust/ban/note), Generations (read-only). Everything else hidden in nav AND refused
    server-side (defense in depth — never rely on hiding alone).
- **Nav gating:** `admin.html` nav items tagged with required role; bootstrap hides items
  the role can't access. Server is the source of truth.
- **Account management UI:** a small "Команда/Доступ" view (owner-only) to add/disable
  agent accounts and set role. Reuses `formModal`. (Support-agent ticket assignment stays
  as-is; an admin_account with role=agent is what grants panel login.)

### 3.5 Responsive shell

Sidebar/topbar are already responsive. Add: tables → cards (via DataTable, §3.2); modals
go full-screen below 600px; tap targets ≥44px; toolbar/filters collapse on mobile. Verify
the existing bespoke sections remain usable on a phone (no horizontal scroll traps).

## 4. Phase 1 — Finance & analytics

### 4.1 Dashboard

Upgrade from static KPI cards to a live overview:
- **Time-series charts** (lightweight inline SVG, no chart library — consistent with the
  hand-rolled bars already in the Face section): revenue ₽/day, payments count/day, new
  users/day, generations/day. Date-range selector (7d / 30d / 90d / custom).
- KPI cards gain **sparklines** + period-over-period delta (▲/▼ %).
- New backend `/api/admin/stats/timeseries?metric=&from=&to=&bucket=day`.

### 4.2 Payments

From read-only stub to a full finance surface:
- **DataTable**: search (order_id / user), filters (provider, status, date range),
  sortable (amount, date), CSV export.
- **Drill-down modal** (`rowAction`): full payment record — amounts, tokens, provider,
  order_id, status timeline (created → paid/failed), linked user (click → user detail),
  raw gateway payload (collapsible).
- **Refund action** (owner-only): "Вернуть платёж" → `confirmDialog(danger)` → calls
  ЮКасса/Platega refund API via a new `payments_gw` method; on success marks the payment
  refunded, reverses the token grant where applicable, writes audit. Idempotent (guards
  against double refund); surfaces gateway errors via toast. Only for `paid` payments.
- **Reconciliation hint:** flag payments `paid` at gateway but not credited (or vice
  versa) — a filter preset "Расхождения".

### 4.3 Withdrawals

- **DataTable**: filters (status, method, date), sortable, CSV export.
- **Detail modal** replacing inline `confirm()/prompt()`: full request, user link, history.
- Actions move to `confirmDialog` + `formModal` (reason capture), with `btnBusy` states.
- **Bulk approve** for `pending` rows (owner-only), each writing audit.

### 4.4 Manual balance corrections

Extend the existing `/adjust` flow: reachable from both the user card and the payment
drill-down, via `formModal` (amount ±, reason required). Already audited; ensure reason is
mandatory and the toast confirms the new balance. Owner-only.

## 5. Data model changes

- **`admin_accounts`** table (§3.4) — new, with idempotent create + owner seed in
  `database.py` (follows the existing auto-migration pattern).
- **`payments`**: add `refunded_at TIMESTAMPTZ NULL`, `refund_id TEXT NULL` (additive,
  idempotent `ALTER TABLE ... IF NOT EXISTS` style used elsewhere).
- No destructive migrations. All changes additive and idempotent.

## 6. Security considerations

- **Server-side role enforcement** on every owner-only endpoint (`_require_role`), never
  nav-hiding alone.
- **Refunds:** owner-only, idempotent, confirm-gated, fully audited; validate payment is
  `paid` and not already refunded; never trust client-sent amounts (recompute server-side
  from the stored payment).
- **SQL:** sort/filter allowlists; parametrized `q`; no interpolation.
- **CSV:** formula-injection guard; exports audited (data exfiltration trail).
- **Rate-limiting:** keep existing login rate limit; consider a light limit on refund.

## 7. Testing & verification

No test framework in the project (per CLAUDE.md). Verification is manual + careful review:
- UI-kit: toast/confirm/formModal across success+error+cancel paths.
- DataTable: search, each filter, sort toggles, pagination, bulk select, CSV download,
  empty/error states, mobile card layout.
- Roles: log in as agent → confirm hidden nav AND 403 on owner-only endpoints (curl).
- Refund: sandbox/test payment end-to-end; double-refund guard; audit row present.
- Run locally with StubGenerator where possible; finance actions verified against
  gateway test mode before prod.

## 8. Out of scope (future cycles)

- **Phase 2** — Generations (detail modal, filters, re-run/refund, export), Audit
  (filters, export), Promos (usage analytics, bulk toggle, duplicate).
- **Phase 3** — roll DataTable across remaining bespoke sections, align admin visuals to
  the "Studio" design system (admin currently has its own palette), full a11y pass.
- Full per-section RBAC (only owner+agent now).
- Notification audience segmentation.

## 9. Rollout

- Branches → PR → `bash srv2.sh gitdeploy` (per project deploy flow).
- Static assets are live-served; bump `?v=N` for each changed JS/CSS so Telegram/browser
  cache refreshes; the new `admin_accounts` seed + `payments` columns apply on service
  restart via `database.py` migrations.
- Ship in order: 3.1 UI-kit → 3.3 backend helper → 3.2 DataTable → 3.4 roles → 3.5
  responsive → Phase 1 (Dashboard → Payments → Withdrawals → corrections). Each is a
  small, independently deployable PR.
