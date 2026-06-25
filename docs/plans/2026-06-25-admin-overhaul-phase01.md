# Admin Overhaul — Phase 0 + Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring the PromptW admin panel to top-tier quality by building a reusable UI/data foundation and then deepening the finance & analytics sections on top of it.

**Architecture:** Vanilla-JS shared layer — split `admin.js` into `admin-kit.js` (toasts/dialogs/loaders) + `admin-table.js` (config-driven `DataTable`) + `admin.js` (section renderers). Backend gains one generic list helper in `admin_routes.py` (search/sort/filter/date/CSV) and an `admin_accounts` table for owner/agent roles. Each task below is one independently deployable PR.

**Tech Stack:** Python 3.12, aiohttp, asyncpg, PostgreSQL 16; vanilla JS (no framework, no bundler, `var`, global scope); inline SVG for charts (no chart lib).

## Global Constraints

- No framework, no bundler, no modules — globals + `var`, per `CLAUDE.md` code style.
- No test framework in the project — verification is **manual** (run locally + browser + curl). Every task ends with a manual verification step, not an automated test.
- CSS is minified, one rule per line; CSS tokens live in `:root`.
- Cache-busting: bump `?v=N` in `admin.html` for EVERY changed JS/CSS file, or Telegram/browser serves stale cache.
- List endpoints return `{"items": [...], "total": N}` (existing convention — NOT `{rows,…}`).
- asyncpg: all SQL parametrized (`$1,$2…`); JSONB comes back as strings → `json.loads`; `_serialize` maps Decimal→float, UUID→str, datetime→ISO.
- Auth: admin-session token carries `tg_id`+exp (HMAC, `bot/auth.py`); `auth_middleware` sets `request["tg_id"]` + `request["admin_scope"]`; `_require_admin` requires both + membership.
- Deploy: branch → PR → merge → `bash srv2.sh gitdeploy` (pull + restart). DB migrations apply on restart via `db/database.py`. Static assets served live (no restart needed for JS/CSS, only cache-bump).
- Commits/PRs: one PR per task; keep them small and independently revertable.
- Money is sacred: refunds owner-only, idempotent, confirm-gated, fully audited; recompute amounts server-side, never trust the client.

---

## File Structure

```
webapp/static/js/admin-kit.js     CREATE — toast, confirmDialog, formModal, loaders, apiError, api()
webapp/static/js/admin-table.js   CREATE — DataTable(mountEl, config)
webapp/static/js/admin.js         MODIFY — consume kit+table; section renderers; nav role-gating
webapp/static/css/admin.css       MODIFY — toast/dialog/skeleton/table-toolbar/card/chart styles
webapp/templates/admin.html       MODIFY — load kit+table scripts; nav data-role; toast host
api/admin_routes.py               MODIFY — list_query helper; roles; timeseries; payment detail/refund; withdrawals; corrections
payments_gw.py                    MODIFY — yookassa_refund()
db/database.py                    MODIFY — admin_accounts table + owner seed; payments.refunded_at/refund_id
bot/auth.py                       (unchanged — token already carries tg_id)
```

---

## Task 1 (PR #1): UI-kit — toasts, dialogs, loaders, error handling

**Files:**
- Create: `webapp/static/js/admin-kit.js`
- Modify: `webapp/static/css/admin.css` (append styles)
- Modify: `webapp/templates/admin.html` (load script before admin.js; toast host)
- Modify: `webapp/static/js/admin.js` (move `api()` out; route errors through `apiError`)

**Interfaces:**
- Produces (global functions consumed by all later tasks):
  - `toast(type, msg, ms?)` — `type ∈ "success"|"error"|"info"`; non-blocking.
  - `confirmDialog(opts) → Promise<boolean>` — `opts={title, body?, confirmLabel?, cancelLabel?, danger?}`.
  - `formModal(opts) → Promise<object|null>` — `opts={title, fields:[{name,label,type,value?,required?,hint?,options?}], submitLabel?}`; returns values keyed by `name`, or `null` if cancelled.
  - `tableSkeleton(cols, rows?) → string` (HTML), `btnBusy(btnEl, on)`.
  - `api(path, opts?) → Promise<any>` — throws `{status, message}` on non-2xx; supports `opts.raw` (returns Response for blobs).
  - `apiError(err)` — shows a toast from a thrown api error.

- [ ] **Step 1: Create `admin-kit.js` with the full kit**

```javascript
// admin-kit.js — shared UI primitives for the admin panel. No modules; globals + var.

// ── Auth-aware fetch wrapper (moved here from admin.js so every file shares it) ──
function getAuthHeaders() {
    var h = {"Content-Type": "application/json"};
    var tg = window.Telegram && Telegram.WebApp;
    if (tg && tg.initData) h["X-Init-Data"] = tg.initData;
    if (window.authToken) h["X-Auth-Token"] = window.authToken;
    return h;
}

function api(path, opts) {
    opts = opts || {};
    var headers = Object.assign(getAuthHeaders(), opts.headers || {});
    // multipart: let the browser set Content-Type (with boundary)
    if (opts.body instanceof FormData) delete headers["Content-Type"];
    return fetch(path, Object.assign({}, opts, {headers: headers})).then(function(r) {
        if (r.status === 403) { if (window.logout) logout(); throw {status: 403, message: "Доступ запрещён"}; }
        if (opts.raw) { if (!r.ok) throw {status: r.status, message: "Ошибка " + r.status}; return r; }
        return r.json().then(function(d) {
            if (!r.ok) throw {status: r.status, message: (d && d.error) || ("Ошибка " + r.status)};
            return d;
        }, function() { throw {status: r.status, message: "Некорректный ответ сервера"}; });
    }, function(netErr) {
        if (netErr && netErr.status) throw netErr;
        throw {status: 0, message: "Ошибка сети"};
    });
}

function apiError(err) {
    var msg = (err && err.message) || "Что-то пошло не так";
    if (err && err.status === 403) return; // logout already triggered
    toast("error", msg);
}

// ── Toasts ──
function _toastHost() {
    var h = document.getElementById("toast-host");
    if (!h) { h = document.createElement("div"); h.id = "toast-host"; document.body.appendChild(h); }
    return h;
}
function toast(type, msg, ms) {
    var host = _toastHost();
    var t = document.createElement("div");
    t.className = "toast toast-" + (type || "info");
    t.innerHTML = '<span>' + escHtmlKit(msg) + '</span><button class="toast-x">&times;</button>';
    host.appendChild(t);
    var life = ms || (type === "error" ? 6000 : 4000);
    var timer = setTimeout(remove, life);
    function remove() { clearTimeout(timer); t.classList.add("toast-out"); setTimeout(function(){ t.remove(); }, 200); }
    t.querySelector(".toast-x").onclick = remove;
}
function escHtmlKit(s) { var d = document.createElement("div"); d.textContent = (s == null ? "" : s); return d.innerHTML; }

// ── Confirm dialog (Promise) ──
function confirmDialog(o) {
    o = o || {};
    return new Promise(function(resolve) {
        var ov = document.createElement("div");
        ov.className = "kit-overlay";
        ov.innerHTML =
            '<div class="kit-dialog">' +
            '<div class="kit-dialog-title">' + escHtmlKit(o.title || "Подтвердите") + '</div>' +
            (o.body ? '<div class="kit-dialog-body">' + escHtmlKit(o.body) + '</div>' : '') +
            '<div class="kit-dialog-actions">' +
            '<button class="btn btn-outline" data-act="cancel">' + escHtmlKit(o.cancelLabel || "Отмена") + '</button>' +
            '<button class="btn ' + (o.danger ? "btn-danger" : "btn-primary") + '" data-act="ok">' + escHtmlKit(o.confirmLabel || "ОК") + '</button>' +
            '</div></div>';
        document.body.appendChild(ov);
        function done(v) { ov.remove(); document.removeEventListener("keydown", onKey); resolve(v); }
        function onKey(e) { if (e.key === "Escape") done(false); if (e.key === "Enter") done(true); }
        ov.addEventListener("click", function(e) {
            if (e.target === ov) return done(false);
            var act = e.target.getAttribute("data-act");
            if (act === "ok") done(true); else if (act === "cancel") done(false);
        });
        document.addEventListener("keydown", onKey);
    });
}

// ── Form modal (Promise) ──
function formModal(o) {
    o = o || {};
    var fields = o.fields || [];
    return new Promise(function(resolve) {
        var ov = document.createElement("div");
        ov.className = "kit-overlay";
        var body = fields.map(function(f, i) {
            var id = "fm_" + i;
            var label = '<label class="fm-label" for="' + id + '">' + escHtmlKit(f.label || f.name) + (f.required ? ' <span class="fm-req">*</span>' : '') + '</label>';
            var ctl;
            if (f.type === "textarea") {
                ctl = '<textarea class="form-input" id="' + id + '" data-name="' + escHtmlKit(f.name) + '">' + escHtmlKit(f.value || "") + '</textarea>';
            } else if (f.type === "select") {
                ctl = '<select class="form-input" id="' + id + '" data-name="' + escHtmlKit(f.name) + '">' +
                    (f.options || []).map(function(op) {
                        var val = (typeof op === "object") ? op.value : op;
                        var lab = (typeof op === "object") ? op.label : op;
                        return '<option value="' + escHtmlKit(val) + '"' + (String(f.value) === String(val) ? " selected" : "") + '>' + escHtmlKit(lab) + '</option>';
                    }).join("") + '</select>';
            } else if (f.type === "checkbox") {
                ctl = '<input type="checkbox" id="' + id + '" data-name="' + escHtmlKit(f.name) + '"' + (f.value ? " checked" : "") + '>';
            } else {
                ctl = '<input class="form-input" id="' + id + '" type="' + escHtmlKit(f.type || "text") + '" data-name="' + escHtmlKit(f.name) + '" value="' + escHtmlKit(f.value == null ? "" : f.value) + '">';
            }
            var hint = f.hint ? '<div class="fm-hint">' + escHtmlKit(f.hint) + '</div>' : '';
            return '<div class="fm-row">' + label + ctl + hint + '</div>';
        }).join("");
        ov.innerHTML =
            '<div class="kit-dialog kit-form">' +
            '<div class="kit-dialog-title">' + escHtmlKit(o.title || "") + '</div>' +
            '<div class="kit-form-body">' + body + '</div>' +
            '<div class="fm-error" style="display:none"></div>' +
            '<div class="kit-dialog-actions">' +
            '<button class="btn btn-outline" data-act="cancel">Отмена</button>' +
            '<button class="btn btn-primary" data-act="ok">' + escHtmlKit(o.submitLabel || "Сохранить") + '</button>' +
            '</div></div>';
        document.body.appendChild(ov);
        var errEl = ov.querySelector(".fm-error");
        function collect() {
            var out = {}, bad = null;
            ov.querySelectorAll("[data-name]").forEach(function(el) {
                var name = el.getAttribute("data-name");
                var val = el.type === "checkbox" ? el.checked : el.value;
                var fdef = fields.filter(function(x){ return x.name === name; })[0] || {};
                if (fdef.required && (val === "" || val == null)) bad = bad || fdef.label || name;
                if (fdef.type === "number" && val !== "" && isNaN(Number(val))) bad = bad || (fdef.label || name) + " — число";
                out[name] = (fdef.type === "number" && val !== "") ? Number(val) : val;
            });
            return bad ? {error: bad} : {values: out};
        }
        function done(v) { ov.remove(); document.removeEventListener("keydown", onKey); resolve(v); }
        function onKey(e) { if (e.key === "Escape") done(null); }
        ov.addEventListener("click", function(e) {
            if (e.target === ov) return done(null);
            var act = e.target.getAttribute("data-act");
            if (act === "cancel") return done(null);
            if (act === "ok") {
                var r = collect();
                if (r.error) { errEl.textContent = "Проверьте поле: " + r.error; errEl.style.display = "block"; return; }
                done(r.values);
            }
        });
        document.addEventListener("keydown", onKey);
        var first = ov.querySelector("[data-name]"); if (first) first.focus();
    });
}

// ── Loaders ──
function tableSkeleton(cols, rows) {
    rows = rows || 6;
    var tds = "";
    for (var c = 0; c < cols; c++) tds += '<td><span class="skel"></span></td>';
    var trs = "";
    for (var r = 0; r < rows; r++) trs += '<tr>' + tds + '</tr>';
    return '<table class="data-table"><tbody>' + trs + '</tbody></table>';
}
function btnBusy(btn, on) {
    if (!btn) return;
    if (on) { btn.dataset.label = btn.innerHTML; btn.disabled = true; btn.innerHTML = '<span class="btn-spin"></span>'; }
    else { btn.disabled = false; if (btn.dataset.label) btn.innerHTML = btn.dataset.label; }
}
```

- [ ] **Step 2: Append kit styles to `admin.css`** (one rule per line, match existing minified style)

```css
#toast-host{position:fixed;top:16px;right:16px;z-index:9999;display:flex;flex-direction:column;gap:8px;max-width:360px}
.toast{display:flex;align-items:center;gap:10px;justify-content:space-between;padding:12px 14px;border-radius:12px;font-size:14px;color:#fff;box-shadow:0 6px 24px rgba(0,0,0,.3);animation:toastIn .2s ease}
.toast-success{background:#1f7a4d}
.toast-error{background:#9c2b22}
.toast-info{background:#2f3a52}
.toast-out{opacity:0;transform:translateX(8px);transition:all .2s ease}
.toast-x{background:none;border:0;color:#fff;font-size:18px;cursor:pointer;line-height:1;opacity:.8}
@keyframes toastIn{from{opacity:0;transform:translateX(12px)}to{opacity:1;transform:none}}
.kit-overlay{position:fixed;inset:0;background:rgba(0,0,0,.55);display:flex;align-items:center;justify-content:center;z-index:9998;padding:16px}
.kit-dialog{background:var(--card);border:1px solid var(--brd);border-radius:16px;padding:20px;max-width:440px;width:100%}
.kit-form{max-width:520px}
.kit-dialog-title{font-weight:700;font-size:17px;margin-bottom:10px}
.kit-dialog-body{color:var(--tx2);font-size:14px;margin-bottom:16px;white-space:pre-wrap}
.kit-dialog-actions{display:flex;gap:10px;justify-content:flex-end;margin-top:16px}
.kit-form-body{display:flex;flex-direction:column;gap:14px;max-height:60vh;overflow:auto}
.fm-row{display:flex;flex-direction:column;gap:6px}
.fm-label{font-size:13px;color:var(--tx2)}
.fm-req{color:var(--error)}
.fm-hint{font-size:12px;color:var(--tx3)}
.fm-error{color:var(--error);font-size:13px;margin-top:10px}
.btn-danger{background:#9c2b22;color:#fff}
.btn-spin{display:inline-block;width:14px;height:14px;border:2px solid rgba(255,255,255,.4);border-top-color:#fff;border-radius:50%;animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.skel{display:block;height:12px;border-radius:6px;background:linear-gradient(90deg,var(--card2),var(--brd),var(--card2));background-size:200% 100%;animation:shimmer 1.2s infinite}
@keyframes shimmer{from{background-position:200% 0}to{background-position:-200% 0}}
@media(max-width:600px){.kit-overlay{align-items:flex-end}.kit-dialog{max-width:100%;border-radius:16px 16px 0 0}}
```

- [ ] **Step 3: Wire `admin.html`** — load kit first, bump versions, ensure toast host exists

Modify the script tags at the bottom (currently `admin.js?v=16`) and the CSS link:

```html
    <link rel="stylesheet" href="/static/css/admin.css?v=7">
```
```html
<script src="/static/js/admin-kit.js?v=1"></script>
<script src="/static/js/admin.js?v=17"></script>
```

- [ ] **Step 4: De-dup `admin.js`** — remove the now-shared `api()`/`getAuthHeaders()` (Task-1 versions live in the kit) and make `authToken` a window global so the kit sees it.

In `admin.js`: delete the `function api(...)` (lines ~28-35) and `function getAuthHeaders(){…}` (lines ~20-26). Change `var authToken = "";` (line 3) to `window.authToken = "";` and replace later `authToken =` assignments with `window.authToken =`. Keep `resolveToken`, `hasAuth`, `showLogin`, `doLogin`, `logout`.

- [ ] **Step 5: Route one caller through the new error path (smoke wiring)**

In `loadDashboard()` replace the bare `.then(...)` with a `.catch(apiError)` tail:
```javascript
    api("/api/admin/stats").then(function(d) {
        /* …existing render… */
    }).catch(apiError);
```

- [ ] **Step 6: Manual verification**

Run locally: `python main.py` (needs PostgreSQL; without KIE it uses StubGenerator). Open `http://localhost:8081/admin` (or prod-style URL). In the browser console:
```javascript
toast("success","Сохранено"); toast("error","Что-то сломалось");
confirmDialog({title:"Удалить?",danger:true}).then(console.log);
formModal({title:"Тест",fields:[{name:"amount",label:"Сумма",type:"number",required:true}]}).then(console.log);
```
Expected: toasts appear top-right and auto-dismiss; confirm resolves `true`/`false`; formModal validates required/number and resolves values or `null`. Dashboard still loads; kill the network and confirm a red error toast appears instead of a silent console error.

- [ ] **Step 7: Commit & PR**

```bash
git checkout -b feat/admin-ui-kit
git add webapp/static/js/admin-kit.js webapp/static/js/admin.js webapp/static/css/admin.css webapp/templates/admin.html
git commit -m "feat(admin): UI-kit — toasts, confirm/form dialogs, loaders, error handling"
git push -u origin feat/admin-ui-kit
gh pr create -t "feat(admin): UI-kit foundation" -b "Toasts, confirmDialog, formModal, loaders, central apiError; shared api() moved to admin-kit.js." && gh pr merge --merge --delete-branch
```

---

## Task 2 (PR #2): Backend list helper — search / sort / filter / date / CSV

**Files:**
- Modify: `api/admin_routes.py` (add `list_query` helper near other helpers, ~line 90)

**Interfaces:**
- Produces: `async def list_query(request, *, base_sql, count_sql, params=None, searchable=(), search_cols=(), sortable=(), default_sort="created_at", default_order="desc", filters=(), date_col=None, serialize=_row, csv_name="export") → web.Response`
  - Returns `{"items":[…],"total":N}` JSON, or a streamed CSV when `?format=csv`.
  - `filters`: iterable of allowed query keys mapped to SQL columns, e.g. `{"status":"p.status","provider":"p.provider"}`.
  - `searchable`/`search_cols`: when `q` present, `ILIKE` over `search_cols`.
  - `sortable`: allowlist of `{api_key: sql_col}` for `?sort=&order=`.

- [ ] **Step 1: Add the helper to `admin_routes.py`**

```python
import csv, io

def _parse_filters(request, filters):
    """filters: dict api_key->sql_col. Returns (where_sql_parts, params) for present keys."""
    parts, params = [], []
    for key, col in (filters or {}).items():
        val = request.query.get(key)
        if val not in (None, ""):
            params.append(val)
            parts.append(f"{col} = ${{}}")  # index filled by caller
    return parts, params

def _csv_cell(v):
    s = "" if v is None else str(v)
    # Formula-injection guard for spreadsheet apps.
    if s and s[0] in ("=", "+", "-", "@"):
        s = "'" + s
    return s

async def list_query(request, *, base_sql, count_sql, params=None,
                     search_cols=(), sortable=None, default_sort="created_at",
                     default_order="desc", filters=None, date_col=None,
                     serialize=_row, csv_name="export"):
    """Generic paged/filtered/sortable list. base_sql/count_sql end right before
    WHERE; this appends WHERE/ORDER/LIMIT. params are positional placeholders
    already present in base_sql ($1..$k)."""
    pool = await get_pool()
    params = list(params or [])
    where = []

    # search (ILIKE over allowlisted columns)
    q = (request.query.get("q") or "").strip()
    if q and search_cols:
        params.append(f"%{q}%")
        idx = len(params)
        where.append("(" + " OR ".join(f"{c} ILIKE ${idx}" for c in search_cols) + ")")

    # equality filters (allowlisted)
    for key, col in (filters or {}).items():
        val = request.query.get(key)
        if val not in (None, ""):
            params.append(val); where.append(f"{col} = ${len(params)}")

    # date range (allowlisted single column)
    if date_col:
        frm = request.query.get("from"); to = request.query.get("to")
        if frm: params.append(frm); where.append(f"{date_col} >= ${len(params)}")
        if to:  params.append(to);  where.append(f"{date_col} < (${len(params)}::date + 1)")

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    # sort (allowlist)
    sortable = sortable or {}
    sort_key = request.query.get("sort", default_sort)
    sort_col = sortable.get(sort_key, sortable.get(default_sort, default_sort))
    order = "ASC" if (request.query.get("order", default_order).lower() == "asc") else "DESC"

    total = await pool.fetchval(count_sql + where_sql, *params)

    if request.query.get("format") == "csv":
        rows = await pool.fetch(f"{base_sql}{where_sql} ORDER BY {sort_col} {order}", *params)
        admin_id = _require_admin(request)
        await _audit(admin_id, "export_csv", csv_name, None, None, {"count": len(rows)}, None, _client_ip(request))
        buf = io.StringIO(); buf.write("﻿")  # BOM for Excel
        w = csv.writer(buf, delimiter=";")
        if rows:
            cols = list(rows[0].keys()); w.writerow(cols)
            for r in rows:
                w.writerow([_csv_cell(_serialize(r[c])) for c in cols])
        resp = web.Response(body=buf.getvalue().encode("utf-8"),
                            content_type="text/csv",
                            headers={"Content-Disposition": f'attachment; filename="promptw-{csv_name}.csv"'})
        return resp

    limit = _qint(request, "limit", 50, 1, 200)
    offset = _qint(request, "offset", 0, 0)
    params_page = params + [limit, offset]
    rows = await pool.fetch(
        f"{base_sql}{where_sql} ORDER BY {sort_col} {order} LIMIT ${len(params)+1} OFFSET ${len(params)+2}",
        *params_page)
    return web.json_response({"items": [serialize(r) for r in rows], "total": total})
```

- [ ] **Step 2: Refit `admin_payments` to use it** (proves the helper; keeps the path/shape)

Replace the body of `admin_payments` (lines 384-406) with:
```python
async def admin_payments(request):
    _require_admin(request)
    return await list_query(
        request,
        base_sql="""SELECT p.*, u.username FROM payments p
                    LEFT JOIN users u ON p.user_tg_id = u.tg_id""",
        count_sql="SELECT COUNT(*) FROM payments p LEFT JOIN users u ON p.user_tg_id = u.tg_id",
        search_cols=("u.username", "p.order_id::text", "p.external_id"),
        sortable={"created_at": "p.created_at", "amount_rub": "p.amount_rub", "tokens": "p.tokens"},
        filters={"status": "p.status", "provider": "p.provider"},
        date_col="p.created_at",
        csv_name="payments",
    )
```

- [ ] **Step 3: Manual verification (curl)**

Run locally. Get a token via the login form (copy from localStorage `pw_admin_token`), then:
```bash
TOKEN=...; H="X-Auth-Token: $TOKEN"
curl -s "http://localhost:8081/api/admin/payments?limit=5" -H "$H" | head -c 300
curl -s "http://localhost:8081/api/admin/payments?status=paid&sort=amount_rub&order=asc" -H "$H" | head -c 300
curl -s "http://localhost:8081/api/admin/payments?q=test" -H "$H" | head -c 200
curl -s "http://localhost:8081/api/admin/payments?format=csv" -H "$H" -o /tmp/p.csv && head -3 /tmp/p.csv
```
Expected: JSON `{"items":[…],"total":…}`; sort/filter/search alter results without 500s; CSV downloads with a BOM + `;`-separated header row. Try `sort=DROP TABLE` → falls back to default sort (allowlist), no SQL error.

- [ ] **Step 4: Commit & PR**

```bash
git checkout -b feat/admin-list-helper
git add api/admin_routes.py
git commit -m "feat(admin): generic list_query helper (search/sort/filter/date/CSV); refit payments"
git push -u origin feat/admin-list-helper
gh pr create -t "feat(admin): backend list helper" -b "Allowlisted search/sort/filter/date-range + streamed CSV with formula-injection guard. Payments endpoint refit as the first consumer." && gh pr merge --merge --delete-branch
```

---

## Task 3 (PR #3): `DataTable` component

**Files:**
- Create: `webapp/static/js/admin-table.js`
- Modify: `webapp/static/css/admin.css` (toolbar/bulkbar/card styles)
- Modify: `webapp/templates/admin.html` (load script; bump versions)

**Interfaces:**
- Consumes: `api`, `apiError`, `toast`, `tableSkeleton`, `esc` (from kit/admin.js).
- Produces: `DataTable(mountEl, config) → {reload()}`. Config per spec §3.2:
  `{endpoint, columns:[{key,label,sortable?,render?(row),align?,hideOnMobile?}], searchable?, searchPlaceholder?, filters?:[{key,label,type:"select"|"daterange",options?,fromKey?,toKey?}], defaultSort?:{key,order}, rowAction?(row), bulkActions?:[{label,danger?,run(ids)}], idKey?, exportCsv?, pageSize?}`.

- [ ] **Step 1: Create `admin-table.js`**

```javascript
// admin-table.js — config-driven table with search/filter/sort/paginate/select/export.
function DataTable(mount, cfg) {
    var state = {
        q: "", sort: (cfg.defaultSort && cfg.defaultSort.key) || "created_at",
        order: (cfg.defaultSort && cfg.defaultSort.order) || "desc",
        offset: 0, filters: {}, selected: {}, total: 0, rows: []
    };
    var idKey = cfg.idKey || "id";
    var pageSize = cfg.pageSize || 50;

    function qs() {
        var p = new URLSearchParams();
        if (cfg.searchable && state.q) p.set("q", state.q);
        p.set("sort", state.sort); p.set("order", state.order);
        p.set("limit", pageSize); p.set("offset", state.offset);
        Object.keys(state.filters).forEach(function(k){ if (state.filters[k]!=="") p.set(k, state.filters[k]); });
        return p.toString();
    }

    function load() {
        mount.querySelector(".dt-body").innerHTML = tableSkeleton(cfg.columns.length + (cfg.bulkActions?1:0));
        api(cfg.endpoint + "?" + qs()).then(function(d) {
            state.rows = d.items || []; state.total = d.total || 0;
            renderBody();
        }).catch(function(e){ apiError(e); mount.querySelector(".dt-body").innerHTML = '<div class="dt-error">Не удалось загрузить. <button class="btn btn-outline btn-sm" id="dt-retry">Повторить</button></div>'; var rb=mount.querySelector("#dt-retry"); if(rb) rb.onclick=load; });
    }

    function renderToolbar() {
        var html = '<div class="dt-toolbar">';
        if (cfg.searchable) html += '<input class="form-input dt-search" placeholder="' + esc(cfg.searchPlaceholder || "Поиск…") + '">';
        (cfg.filters || []).forEach(function(f, i) {
            if (f.type === "select") {
                html += '<select class="form-input dt-filter" data-key="' + esc(f.key) + '"><option value="">' + esc(f.label) + ': все</option>' +
                    (f.options||[]).map(function(o){ var v=(typeof o==="object")?o.value:o, l=(typeof o==="object")?o.label:o; return '<option value="'+esc(v)+'">'+esc(l)+'</option>'; }).join("") + '</select>';
            } else if (f.type === "daterange") {
                html += '<input type="date" class="form-input dt-date" data-key="' + esc(f.fromKey||"from") + '" title="С">' +
                        '<input type="date" class="form-input dt-date" data-key="' + esc(f.toKey||"to") + '" title="По">';
            }
        });
        if (cfg.exportCsv) html += '<button class="btn btn-outline btn-sm dt-export" style="margin-left:auto">Экспорт CSV</button>';
        html += '</div>';
        return html;
    }

    function renderBody() {
        var hasSel = !!cfg.bulkActions;
        var head = '<tr>' + (hasSel ? '<th class="dt-selcol"><input type="checkbox" class="dt-all"></th>' : '') +
            cfg.columns.map(function(c){
                var arrow = state.sort===c.key ? (state.order==="asc"?" ▲":" ▼") : "";
                return '<th' + (c.hideOnMobile?' class="dt-hide-m"':'') + (c.sortable?' data-sort="'+esc(c.key)+'" style="cursor:pointer"':'') + '>' + esc(c.label) + arrow + '</th>';
            }).join("") + '</tr>';
        var body = state.rows.length ? state.rows.map(function(row){
            var id = row[idKey];
            var sel = hasSel ? '<td class="dt-selcol"><input type="checkbox" class="dt-row" data-id="'+esc(id)+'"'+(state.selected[id]?" checked":"")+'></td>' : '';
            var tds = cfg.columns.map(function(c){
                var v = c.render ? c.render(row) : esc(row[c.key]==null?"—":row[c.key]);
                return '<td' + (c.hideOnMobile?' class="dt-hide-m"':'') + (c.align?' style="text-align:'+c.align+'"':'') + ' data-label="'+esc(c.label)+'">' + v + '</td>';
            }).join("");
            return '<tr class="dt-row-tr'+(cfg.rowAction?" dt-click":"")+'" data-id="'+esc(id)+'">' + sel + tds + '</tr>';
        }).join("") : '<tr><td colspan="'+(cfg.columns.length+(hasSel?1:0))+'" class="dt-empty">Ничего не найдено</td></tr>';
        mount.querySelector(".dt-body").innerHTML = '<table class="data-table"><thead>'+head+'</thead><tbody>'+body+'</tbody></table>' + pag();
        wireBody();
        renderBulkBar();
    }

    function pag() {
        var pages = Math.ceil(state.total / pageSize), cur = Math.floor(state.offset / pageSize);
        return '<div class="pag"><button class="pag-btn dt-prev"'+(cur===0?" disabled":"")+'>&larr;</button>' +
            '<span class="pag-info">'+(cur+1)+' / '+Math.max(1,pages)+' ('+state.total+')</span>' +
            '<button class="pag-btn dt-next"'+(cur+1>=pages?" disabled":"")+'>&rarr;</button></div>';
    }

    function renderBulkBar() {
        var bar = mount.querySelector(".dt-bulkbar"); if (!bar) return;
        var ids = Object.keys(state.selected).filter(function(k){return state.selected[k];});
        if (!ids.length) { bar.style.display="none"; return; }
        bar.style.display = "flex";
        bar.innerHTML = '<span>Выбрано: '+ids.length+'</span>' + cfg.bulkActions.map(function(a,i){
            return '<button class="btn '+(a.danger?"btn-danger":"btn-primary")+' btn-sm" data-bulk="'+i+'">'+esc(a.label)+'</button>';
        }).join("");
        bar.querySelectorAll("[data-bulk]").forEach(function(b){
            b.onclick = function(){ var a = cfg.bulkActions[+b.dataset.bulk]; Promise.resolve(a.run(ids)).then(function(){ state.selected={}; load(); }); };
        });
    }

    function wireBody() {
        mount.querySelectorAll("[data-sort]").forEach(function(th){
            th.onclick = function(){ var k=th.dataset.sort; if(state.sort===k) state.order=(state.order==="asc"?"desc":"asc"); else {state.sort=k;state.order="asc";} state.offset=0; load(); };
        });
        var prev=mount.querySelector(".dt-prev"), next=mount.querySelector(".dt-next");
        if(prev) prev.onclick=function(){ state.offset=Math.max(0,state.offset-pageSize); load(); };
        if(next) next.onclick=function(){ state.offset+=pageSize; load(); };
        if (cfg.rowAction) mount.querySelectorAll(".dt-click").forEach(function(tr){
            tr.onclick=function(e){ if(e.target.closest("input,button,a")) return; var row=state.rows.filter(function(r){return String(r[idKey])===tr.dataset.id;})[0]; if(row) cfg.rowAction(row); };
        });
        if (cfg.bulkActions) {
            var all=mount.querySelector(".dt-all");
            if(all) all.onchange=function(){ state.rows.forEach(function(r){ state.selected[r[idKey]]=all.checked; }); renderBody(); };
            mount.querySelectorAll(".dt-row").forEach(function(cb){ cb.onchange=function(){ state.selected[cb.dataset.id]=cb.checked; renderBulkBar(); }; });
        }
    }

    // initial shell
    mount.innerHTML = renderToolbar() + (cfg.bulkActions?'<div class="dt-bulkbar" style="display:none"></div>':'') + '<div class="dt-body"></div>';
    var s = mount.querySelector(".dt-search");
    if (s) { var t; s.oninput=function(){ clearTimeout(t); t=setTimeout(function(){ state.q=s.value.trim(); state.offset=0; load(); },300); }; }
    mount.querySelectorAll(".dt-filter").forEach(function(sel){ sel.onchange=function(){ state.filters[sel.dataset.key]=sel.value; state.offset=0; load(); }; });
    mount.querySelectorAll(".dt-date").forEach(function(d){ d.onchange=function(){ state.filters[d.dataset.key]=d.value; state.offset=0; load(); }; });
    var ex = mount.querySelector(".dt-export");
    if (ex) ex.onclick=function(){ var p=new URLSearchParams(qs()); p.set("format","csv"); p.delete("limit"); p.delete("offset"); api(cfg.endpoint+"?"+p.toString(),{raw:true}).then(function(r){return r.blob();}).then(function(b){ var u=URL.createObjectURL(b); var a=document.createElement("a"); a.href=u; a.download="export.csv"; a.click(); URL.revokeObjectURL(u); }).catch(apiError); };

    load();
    return { reload: load };
}
```

- [ ] **Step 2: Append table/toolbar/card CSS to `admin.css`**

```css
.dt-toolbar{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:12px}
.dt-search{min-width:200px}
.dt-bulkbar{align-items:center;gap:10px;padding:10px 12px;background:var(--card2);border:1px solid var(--brd);border-radius:12px;margin-bottom:10px}
.dt-empty,.dt-error{text-align:center;color:var(--tx2);padding:24px}
.dt-selcol{width:36px;text-align:center}
.data-table th[data-sort]{user-select:none}
@media(max-width:768px){.dt-hide-m{display:none}.data-table thead{display:none}.data-table tr{display:block;border:1px solid var(--brd);border-radius:12px;margin-bottom:10px;padding:8px}.data-table td{display:flex;justify-content:space-between;gap:12px;border:0;padding:6px 4px}.data-table td:before{content:attr(data-label);color:var(--tx3);font-size:12px}.dt-bulkbar{position:fixed;left:12px;right:12px;bottom:12px;z-index:50}}
```

- [ ] **Step 3: Wire `admin.html`** — load after kit, before admin.js:
```html
<script src="/static/js/admin-kit.js?v=1"></script>
<script src="/static/js/admin-table.js?v=1"></script>
<script src="/static/js/admin.js?v=18"></script>
```
And bump CSS `admin.css?v=8`.

- [ ] **Step 4: Adopt DataTable in the Generations section as a smoke test** (read-only, lowest risk)

Replace `loadGenerations` in `admin.js` with:
```javascript
function loadGenerations() {
    var mc = document.getElementById("main-content");
    mc.innerHTML = '<div id="gen-table"></div>';
    DataTable(document.getElementById("gen-table"), {
        endpoint: "/api/admin/generations",
        searchable: true, searchPlaceholder: "Поиск по промпту…",
        exportCsv: true,
        defaultSort: {key:"created_at", order:"desc"},
        filters: [
            {key:"gen_type", label:"Тип", type:"select", options:[{value:"photo",label:"Фото"},{value:"video",label:"Видео"},{value:"audio",label:"Аудио"}]},
            {key:"status", label:"Статус", type:"select", options:["done","error","pending"]},
            {type:"daterange"}
        ],
        columns: [
            {key:"id", label:"ID", sortable:true, hideOnMobile:true},
            {key:"user_tg_id", label:"User"},
            {key:"gen_type", label:"Тип"},
            {key:"model", label:"Модель", hideOnMobile:true},
            {key:"status", label:"Статус", render:function(r){return badge(r.status);}},
            {key:"cost", label:"W", sortable:true, align:"right"},
            {key:"prompt", label:"Промпт", render:function(r){return '<span title="'+esc(r.prompt)+'">'+esc((r.prompt||"").slice(0,40))+'</span>';}},
            {key:"created_at", label:"Дата", sortable:true, render:function(r){return fmtDate(r.created_at);}}
        ]
    });
}
```
Update `admin_generations` in `admin_routes.py` to use `list_query` (search on `g.prompt`, filters `gen_type`/`status`, sortable `id`/`cost`/`created_at`, date `g.created_at`, csv_name `generations`):
```python
async def admin_generations(request):
    _require_admin(request)
    return await list_query(
        request,
        base_sql="""SELECT g.id, g.user_tg_id, g.gen_type, g.model, g.status, g.cost, g.prompt, g.created_at
                    FROM generations g""",
        count_sql="SELECT COUNT(*) FROM generations g",
        search_cols=("g.prompt",),
        sortable={"id":"g.id","cost":"g.cost","created_at":"g.created_at"},
        filters={"gen_type":"g.gen_type","status":"g.status"},
        date_col="g.created_at",
        csv_name="generations",
    )
```

- [ ] **Step 5: Manual verification**

Open admin → Генерации. Verify: search filters rows (debounced); type/status selects work; date range narrows; column-header sort toggles ▲/▼; pagination prev/next; CSV export downloads. Resize browser to <768px → rows become stacked cards with labels. Trigger an error (stop the server mid-use) → error state with Повторить button.

- [ ] **Step 6: Commit & PR**

```bash
git checkout -b feat/admin-datatable
git add webapp/static/js/admin-table.js webapp/static/js/admin.js webapp/static/css/admin.css webapp/templates/admin.html api/admin_routes.py
git commit -m "feat(admin): DataTable component; adopt in Generations"
git push -u origin feat/admin-datatable
gh pr create -t "feat(admin): DataTable component" -b "Config-driven table: search/filter/sort/paginate/select/CSV/responsive cards. Generations refit as first consumer." && gh pr merge --merge --delete-branch
```

---

## Task 4 (PR #4): Roles — owner + agent

**Files:**
- Modify: `db/database.py` (create `admin_accounts`, seed owner)
- Modify: `api/admin_routes.py` (account model, login, `_require_role`, gate endpoints, accounts CRUD)
- Modify: `webapp/templates/admin.html` (nav `data-role`; "Доступ" nav item)
- Modify: `webapp/static/js/admin.js` (fetch role on login; hide nav; accounts UI)

**Interfaces:**
- Produces (backend): `def _require_role(request, role) → tg_id` (raises 403 if mismatch); `async def _account_role(tg_id) → "owner"|"agent"|None`; endpoints `GET/POST/PUT /api/admin/accounts`.
- Produces (frontend): `window.adminRole` set after `/api/admin/me`; nav items hidden by role.

- [ ] **Step 1: Migration — `admin_accounts` table + owner seed**

In `db/database.py` inside `_create_tables()` (after the audit-log block, ~line 196), add:
```python
            CREATE TABLE IF NOT EXISTS admin_accounts (
                tg_id BIGINT PRIMARY KEY,
                login VARCHAR(64) UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role VARCHAR(16) NOT NULL DEFAULT 'agent',
                disabled BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
```
Then add a one-time owner seed near `_seed_templates()` call in `init_db` — create `_seed_owner_account()` and call it after `_safe_migrations()`:
```python
async def _seed_owner_account():
    """Seed the env ADMIN_LOGIN/PASSWORD as the first 'owner' account (idempotent).
    Lets the existing operator keep logging in; agents are added via the panel."""
    import os, hashlib, hmac
    login = os.getenv("ADMIN_LOGIN", ""); pw = os.getenv("ADMIN_PASSWORD", "")
    ids = [int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x]
    if not (login and pw and ids):
        return
    tg_id = ids[0]
    ph = hashlib.sha256(pw.encode()).hexdigest()
    async with _pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO admin_accounts (tg_id, login, password_hash, role)
            VALUES ($1,$2,$3,'owner')
            ON CONFLICT (tg_id) DO UPDATE SET login=$2, password_hash=$3, role='owner'
        """, tg_id, login, ph)
```
Note: password hashing here mirrors the env login (sha256). New agent accounts use the same hash function (Step 3).

- [ ] **Step 2: Account role lookup + `_require_role` in `admin_routes.py`**

```python
import hashlib

async def _account_role(tg_id):
    pool = await get_pool()
    row = await pool.fetchrow("SELECT role, disabled FROM admin_accounts WHERE tg_id=$1", tg_id)
    if row and not row["disabled"]:
        return row["role"]
    # env owner fallback (table empty / not yet seeded)
    if tg_id in ADMIN_IDS:
        return "owner"
    return None

def _pw_hash(pw): return hashlib.sha256((pw or "").encode()).hexdigest()

async def _require_role(request, role):
    tg_id = _require_admin(request)
    r = await _account_role(tg_id)
    if r != role and r != "owner":   # owner passes every gate
        raise web.HTTPForbidden(text="role_forbidden")
    return tg_id
```

- [ ] **Step 3: Login resolves a specific account (per-account login)**

Replace `admin_login` credential check (lines 122-133) so it checks `admin_accounts` first, then falls back to the env owner:
```python
    data = await _json_body(request)
    login = (data.get("login") or "").strip()
    password = (data.get("password") or "").strip()
    pool = await get_pool()
    acct = await pool.fetchrow("SELECT tg_id, password_hash, disabled FROM admin_accounts WHERE login=$1", login)
    ok = False; admin_tg_id = None
    if acct and not acct["disabled"] and hmac.compare_digest(acct["password_hash"], _pw_hash(password)):
        ok = True; admin_tg_id = acct["tg_id"]
    elif ADMIN_LOGIN and ADMIN_PASSWORD and hmac.compare_digest(login, ADMIN_LOGIN) and hmac.compare_digest(password, ADMIN_PASSWORD):
        ok = True; admin_tg_id = next(iter(ADMIN_IDS)) if ADMIN_IDS else 0
    if not ok:
        await _audit(0, "login_failed", "admin", None, None, {"login": login}, None, ip)
        return web.json_response({"error": "invalid credentials"}, status=403)
    token = make_admin_token(admin_tg_id, BOT_TOKEN, ttl_sec=12 * 3600)
    await _audit(admin_tg_id, "login_browser", "admin", admin_tg_id, None, None, None, ip)
    return web.json_response({"ok": True, "token": token})
```
**Important:** `_require_admin` currently requires `tg_id in ADMIN_IDS`. Agents aren't in `ADMIN_IDS`. Relax it to also accept enabled `admin_accounts`. Change `_require_admin` to set the gate via a sync-cached set is overkill; instead make `_require_admin` accept either ADMIN_IDS membership OR presence in admin_accounts. Implement by querying once per request is acceptable here:
```python
def _require_admin(request):
    tg_id = request.get("tg_id")
    if not request.get("admin_scope") or not tg_id:
        raise web.HTTPForbidden(text="forbidden")
    return tg_id   # identity proven by admin-scoped token; role/authorization enforced per-endpoint
```
Rationale: the admin-session token is minted ONLY by a successful `/api/admin/login` (which already validated credentials against `admin_accounts`/env), so a valid `admin_scope` token is sufficient proof of an admin identity. Authorization (owner vs agent) is enforced by `_require_role`. This removes the now-incorrect `ADMIN_IDS` coupling for agents while keeping the token-scope guard.

- [ ] **Step 4: `/api/admin/me` + accounts CRUD (owner-only)**

```python
@admin_routes.get("/api/admin/me")
async def admin_me(request):
    tg_id = _require_admin(request)
    return web.json_response({"tg_id": tg_id, "role": await _account_role(tg_id)})

@admin_routes.get("/api/admin/accounts")
async def admin_accounts_list(request):
    await _require_role(request, "owner")
    pool = await get_pool()
    rows = await pool.fetch("SELECT tg_id, login, role, disabled, created_at FROM admin_accounts ORDER BY created_at")
    return web.json_response({"items": [_row(r) for r in rows], "total": len(rows)})

@admin_routes.post("/api/admin/accounts")
async def admin_accounts_create(request):
    admin_id = await _require_role(request, "owner")
    d = await _json_body(request)
    try:
        tg_id = int(d.get("tg_id"))
    except (TypeError, ValueError):
        return web.json_response({"error": "tg_id must be a number"}, status=400)
    login = (d.get("login") or "").strip(); pw = (d.get("password") or "").strip()
    role = d.get("role") if d.get("role") in ("owner", "agent") else "agent"
    if not login or not pw:
        return web.json_response({"error": "login and password required"}, status=400)
    pool = await get_pool()
    try:
        await pool.execute("INSERT INTO admin_accounts (tg_id, login, password_hash, role) VALUES ($1,$2,$3,$4)",
                           tg_id, login, _pw_hash(pw), role)
    except Exception:
        return web.json_response({"error": "tg_id or login already exists"}, status=409)
    await _audit(admin_id, "account_create", "admin_account", tg_id, None, {"login": login, "role": role}, None, _client_ip(request))
    return web.json_response({"ok": True})

@admin_routes.put("/api/admin/accounts/{tg_id}")
async def admin_accounts_update(request):
    admin_id = await _require_role(request, "owner")
    tg_id = int(request.match_info["tg_id"])
    d = await _json_body(request)
    sets, params = [], []
    if d.get("role") in ("owner", "agent"): params.append(d["role"]); sets.append(f"role=${len(params)}")
    if "disabled" in d: params.append(bool(d["disabled"])); sets.append(f"disabled=${len(params)}")
    if d.get("password"): params.append(_pw_hash(d["password"])); sets.append(f"password_hash=${len(params)}")
    if not sets: return web.json_response({"error": "nothing to update"}, status=400)
    params.append(tg_id)
    pool = await get_pool()
    await pool.execute(f"UPDATE admin_accounts SET {', '.join(sets)} WHERE tg_id=${len(params)}", *params)
    await _audit(admin_id, "account_update", "admin_account", tg_id, None, {k:v for k,v in d.items() if k!='password'}, None, _client_ip(request))
    return web.json_response({"ok": True})
```

- [ ] **Step 5: Gate owner-only endpoints**

For each mutating finance/admin endpoint that agents must NOT use, replace `admin_id = _require_admin(request)` with `admin_id = await _require_role(request, "owner")` and make the handler `async` if not already. Apply to: `admin_adjust_balance`, `admin_ban_user`, `admin_set_note`, `admin_withdrawal_action`, all template/promo create/update/delete, notif toggle/weekly. Leave read endpoints and all `support` endpoints on `_require_admin` (agents allowed).

- [ ] **Step 6: Frontend — fetch role, gate nav, accounts UI**

In `admin.html` add `data-role` to owner-only nav buttons and a new nav item:
```html
            <button class="nav-item" data-section="payments" data-role="owner">Платежи</button>
            <button class="nav-item" data-section="withdrawals" data-role="owner">Выводы</button>
            <button class="nav-item" data-section="templates" data-role="owner">Шаблоны</button>
            <button class="nav-item" data-section="promos" data-role="owner">Промокоды</button>
            <button class="nav-item" data-section="referrals" data-role="owner">Рефералы</button>
            <button class="nav-item" data-section="face" data-role="owner">Сходство лиц</button>
            <button class="nav-item" data-section="notif" data-role="owner">Уведомления</button>
            <button class="nav-item" data-section="audit" data-role="owner">Аудит-лог</button>
            <button class="nav-item" data-section="accounts" data-role="owner">Доступ</button>
```
(Dashboard, Пользователи, Генерации, Поддержка stay visible to agents.)

In `admin.js`, after a successful login/bootstrap, fetch role and hide nav:
```javascript
function applyRole() {
    return api("/api/admin/me").then(function(d){
        window.adminRole = d.role || "agent";
        document.querySelectorAll('.nav-item[data-role="owner"]').forEach(function(b){
            b.style.display = (window.adminRole === "owner") ? "" : "none";
        });
        // if current section is now hidden, fall back to dashboard
        var cur = document.querySelector('.nav-item[data-section="'+currentSection+'"]');
        if (cur && cur.style.display === "none") showSection("dashboard");
    }).catch(apiError);
}
```
Call `applyRole()` in `doLogin` success (before `showSection("dashboard")`) and on initial bootstrap when a stored token exists. Add an `accounts` entry to the `loaders` map and a `loadAccounts()` renderer using `DataTable` + `formModal` for add/edit:
```javascript
function loadAccounts() {
    var mc = document.getElementById("main-content");
    mc.innerHTML = '<button class="btn btn-primary" style="margin-bottom:12px" onclick="addAccount()">+ Аккаунт</button><div id="acc-table"></div>';
    window._accTable = DataTable(document.getElementById("acc-table"), {
        endpoint: "/api/admin/accounts", idKey: "tg_id",
        columns: [
            {key:"login", label:"Логин"},
            {key:"tg_id", label:"TG ID"},
            {key:"role", label:"Роль"},
            {key:"disabled", label:"Статус", render:function(r){return r.disabled?'<span class="badge badge-error">off</span>':'<span class="badge badge-paid">on</span>';}},
            {key:"_a", label:"", render:function(r){return '<button class="btn btn-outline btn-sm" onclick="editAccount('+r.tg_id+",'"+esc(r.role)+"',"+(r.disabled?1:0)+')">Изм.</button>';}}
        ]
    });
}
function addAccount() {
    formModal({title:"Новый аккаунт", submitLabel:"Создать", fields:[
        {name:"tg_id", label:"TG ID", type:"number", required:true},
        {name:"login", label:"Логин", required:true},
        {name:"password", label:"Пароль", required:true},
        {name:"role", label:"Роль", type:"select", value:"agent", options:[{value:"agent",label:"Агент"},{value:"owner",label:"Владелец"}]}
    ]}).then(function(v){ if(!v) return; api("/api/admin/accounts",{method:"POST",body:JSON.stringify(v)}).then(function(){ toast("success","Аккаунт создан"); window._accTable.reload(); }).catch(apiError); });
}
function editAccount(tgId, role, disabled) {
    formModal({title:"Аккаунт "+tgId, submitLabel:"Сохранить", fields:[
        {name:"role", label:"Роль", type:"select", value:role, options:[{value:"agent",label:"Агент"},{value:"owner",label:"Владелец"}]},
        {name:"disabled", label:"Отключён", type:"checkbox", value:!!disabled},
        {name:"password", label:"Новый пароль (опц.)", type:"text"}
    ]}).then(function(v){ if(!v) return; if(!v.password) delete v.password; api("/api/admin/accounts/"+tgId,{method:"PUT",body:JSON.stringify(v)}).then(function(){ toast("success","Сохранено"); window._accTable.reload(); }).catch(apiError); });
}
```
Add `accounts: loadAccounts` to the `loaders` map and `accounts:"Доступ"` to `sectionTitles`. Bump `admin.js?v=19`.

- [ ] **Step 7: Manual verification**

Restart locally (applies migration + owner seed). (a) Log in with env owner creds → all 12 nav items + Доступ visible. (b) Доступ → add an agent (a real TG id, login/pass). (c) Log out, log in as the agent → only Dashboard/Пользователи/Генерации/Поддержка visible. (d) curl an owner-only endpoint with the agent token:
```bash
curl -s -X POST "http://localhost:8081/api/admin/users/123/ban" -H "X-Auth-Token: $AGENT_TOKEN" -H "Content-Type: application/json" -d '{}' -o /dev/null -w "%{http_code}\n"
```
Expected: `403`. (e) Owner can still ban. (f) Existing owner login keeps working (back-compat).

- [ ] **Step 8: Commit & PR**

```bash
git checkout -b feat/admin-roles
git add db/database.py api/admin_routes.py webapp/templates/admin.html webapp/static/js/admin.js
git commit -m "feat(admin): owner/agent roles — admin_accounts, per-account login, _require_role gating, accounts UI"
git push -u origin feat/admin-roles
gh pr create -t "feat(admin): owner/agent roles" -b "admin_accounts table + owner seed; per-account login; _require_role gates owner-only endpoints (defense in depth); nav role-gating; accounts management UI." && gh pr merge --merge --delete-branch
```

---

## Task 5 (PR #5): Responsive polish pass

**Files:**
- Modify: `webapp/static/css/admin.css`
- Modify: `webapp/static/js/admin.js` (modal full-screen helper if needed)

- [ ] **Step 1: Make existing modals full-screen on phones**

Append to `admin.css`:
```css
@media(max-width:600px){#modal-content{max-width:100%;width:100%;height:100%;max-height:100%;border-radius:0;overflow:auto}.sidebar-nav .nav-item{padding:14px 16px}.btn,.pag-btn{min-height:44px}.form-input{min-height:44px}}
```

- [ ] **Step 2: Verify tap targets & no horizontal scroll**

Open each section at 375px width (DevTools device toolbar). Check: nav drawer opens/closes; tables (Generations) show as cards; modals (user detail, template edit) are full-screen and scroll; no element forces horizontal scroll; buttons are ≥44px tall.

- [ ] **Step 3: Commit & PR**

```bash
git checkout -b feat/admin-responsive
git add webapp/static/css/admin.css webapp/static/js/admin.js webapp/templates/admin.html
git commit -m "feat(admin): responsive polish — full-screen modals, 44px tap targets"
git push -u origin feat/admin-responsive
gh pr create -t "feat(admin): responsive polish" -b "Full-screen modals on phones, 44px tap targets, mobile spacing." && gh pr merge --merge --delete-branch
```
Bump `admin.css?v=9` in `admin.html`.

---

## Task 6 (PR #6): Dashboard charts + timeseries

**Files:**
- Modify: `api/admin_routes.py` (`/api/admin/stats/timeseries`)
- Modify: `webapp/static/js/admin.js` (`loadDashboard` charts; inline SVG)
- Modify: `webapp/static/css/admin.css` (chart styles)

**Interfaces:**
- Produces: `GET /api/admin/stats/timeseries?metric=revenue|payments|users|generations&from=&to=` → `{"points":[{"d":"2026-06-01","v":123}, …]}`.
- Produces (frontend): `sparkline(points) → svg string`, `lineChart(points, opts) → svg string`.

- [ ] **Step 1: Timeseries endpoint**

```python
@admin_routes.get("/api/admin/stats/timeseries")
async def admin_stats_timeseries(request):
    _require_admin(request)
    pool = await get_pool()
    metric = request.query.get("metric", "revenue")
    frm = request.query.get("from"); to = request.query.get("to")
    # default last 30 days
    where_date = "created_at >= COALESCE($1::date, NOW()::date - INTERVAL '30 days') AND created_at < (COALESCE($2::date, NOW()::date) + 1)"
    qmap = {
        "revenue":     f"SELECT created_at::date d, COALESCE(SUM(amount_rub),0) v FROM payments WHERE status='paid' AND {where_date} GROUP BY d ORDER BY d",
        "payments":    f"SELECT created_at::date d, COUNT(*) v FROM payments WHERE status='paid' AND {where_date} GROUP BY d ORDER BY d",
        "users":       f"SELECT created_at::date d, COUNT(*) v FROM users WHERE {where_date} GROUP BY d ORDER BY d",
        "generations": f"SELECT created_at::date d, COUNT(*) v FROM generations WHERE {where_date} GROUP BY d ORDER BY d",
    }
    sql = qmap.get(metric, qmap["revenue"])
    rows = await pool.fetch(sql, frm, to)
    return web.json_response({"points": [{"d": r["d"].isoformat(), "v": float(r["v"])} for r in rows]})
```

- [ ] **Step 2: Inline SVG charts in `admin.js`**

```javascript
function lineChart(points, opts) {
    opts = opts || {}; var w = opts.w || 560, h = opts.h || 140, pad = 24;
    if (!points.length) return '<div class="chart-empty">Нет данных</div>';
    var vals = points.map(function(p){return p.v;});
    var max = Math.max.apply(null, vals) || 1, min = 0;
    var dx = (w - pad*2) / Math.max(1, points.length - 1);
    var pts = points.map(function(p,i){ var x = pad + i*dx; var y = h - pad - (p.v - min)/(max - min) * (h - pad*2); return x.toFixed(1)+","+y.toFixed(1); });
    var poly = pts.join(" ");
    var area = "M"+pad+","+(h-pad)+" L"+pts.join(" L")+" L"+(pad+(points.length-1)*dx).toFixed(1)+","+(h-pad)+" Z";
    return '<svg class="chart" viewBox="0 0 '+w+' '+h+'" preserveAspectRatio="none">' +
        '<path d="'+area+'" fill="var(--accent-soft,rgba(255,107,92,.15))"/>' +
        '<polyline points="'+poly+'" fill="none" stroke="var(--accent,#FF6B5C)" stroke-width="2"/>' +
        '</svg><div class="chart-cap"><span>'+esc(points[0].d)+'</span><span>макс '+Math.round(max).toLocaleString("ru")+'</span><span>'+esc(points[points.length-1].d)+'</span></div>';
}
```

- [ ] **Step 3: Upgrade `loadDashboard`**

```javascript
function loadDashboard() {
    var mc = document.getElementById("main-content");
    mc.innerHTML = '<div class="kpi-grid" id="kpi-grid"></div>' +
        '<div class="chart-controls" style="margin:16px 0 8px"><select class="form-input" id="dash-metric" style="max-width:220px">' +
        '<option value="revenue">Выручка ₽/день</option><option value="payments">Оплаты/день</option>' +
        '<option value="users">Новые юзеры/день</option><option value="generations">Генерации/день</option></select></div>' +
        '<div id="dash-chart"></div>';
    api("/api/admin/stats").then(function(d) {
        document.getElementById("kpi-grid").innerHTML =
            kpi("Пользователи", d.users.total, "сегодня +" + d.users.today + " / 7д +" + d.users.week) +
            kpi("Генерации", d.generations.total, "done " + d.generations.done + " / err " + d.generations.error, "accent") +
            kpi("Выручка ₽", d.revenue.total.toLocaleString("ru"), "7д: " + d.revenue.week.toLocaleString("ru") + " ₽ · " + d.revenue.payments + " оплат", "gold") +
            kpi("Токены потрачено", d.tokens.spent.toLocaleString("ru"), "на балансах: " + d.tokens.on_balances.toLocaleString("ru")) +
            kpi("Выводы pending", d.withdrawals.pending, d.withdrawals.pending_amount.toLocaleString("ru") + " ₽", d.withdrawals.pending > 0 ? "error" : "success");
    }).catch(apiError);
    function drawChart() {
        var m = document.getElementById("dash-metric").value;
        document.getElementById("dash-chart").innerHTML = '<p style="color:var(--tx2)">Загрузка…</p>';
        api("/api/admin/stats/timeseries?metric=" + m).then(function(d){
            document.getElementById("dash-chart").innerHTML = lineChart(d.points || []);
        }).catch(apiError);
    }
    document.getElementById("dash-metric").onchange = drawChart;
    drawChart();
}
```

- [ ] **Step 4: Chart CSS**
```css
.chart{width:100%;height:140px;background:var(--card);border:1px solid var(--brd);border-radius:12px}
.chart-cap{display:flex;justify-content:space-between;color:var(--tx3);font-size:12px;margin-top:6px}
.chart-empty{color:var(--tx2);padding:24px;text-align:center}
```

- [ ] **Step 5: Manual verification**

Open Dashboard. KPI cards render; metric dropdown switches the chart; line + filled area draw; "Нет данных" shows when a metric has no rows. Verify revenue matches Платежи totals for the period.

- [ ] **Step 6: Commit & PR**

```bash
git checkout -b feat/admin-dashboard-charts
git add api/admin_routes.py webapp/static/js/admin.js webapp/static/css/admin.css webapp/templates/admin.html
git commit -m "feat(admin): dashboard timeseries charts (inline SVG)"
git push -u origin feat/admin-dashboard-charts
gh pr create -t "feat(admin): dashboard charts" -b "Timeseries endpoint + inline-SVG line chart with metric switcher (revenue/payments/users/generations)." && gh pr merge --merge --delete-branch
```
Bump `admin.js?v=20`, `admin.css?v=10`.

---

## Task 7 (PR #7): Payments — DataTable, drill-down, refund

**Files:**
- Modify: `db/database.py` (payments.refunded_at/refund_id)
- Modify: `payments_gw.py` (`yookassa_refund`)
- Modify: `api/admin_routes.py` (payment detail; refund endpoint)
- Modify: `webapp/static/js/admin.js` (`loadPayments` via DataTable; detail modal; refund)

**Interfaces:**
- Produces: `GET /api/admin/payments/{id}` → full row + user; `POST /api/admin/payments/{id}/refund` (owner-only).
- Produces: `async def yookassa_refund(payment_id, amount_rub) → (ok: bool, refund_id: str|None)`.

- [ ] **Step 1: Migration — refund columns**

In `_create_tables()` after the payments-index block (~line 171) add:
```python
            ALTER TABLE payments ADD COLUMN IF NOT EXISTS refunded_at TIMESTAMPTZ;
            ALTER TABLE payments ADD COLUMN IF NOT EXISTS refund_id TEXT;
```

- [ ] **Step 2: `yookassa_refund` in `payments_gw.py`**

```python
YOOKASSA_REFUNDS_API = "https://api.yookassa.ru/v3/refunds"

async def yookassa_refund(payment_id: str, amount_rub) -> tuple:
    """Refund a ЮKassa payment in full. Returns (ok, refund_id|None)."""
    if not payment_id or not yookassa_available():
        return False, None
    import uuid as _uuid
    headers = {"Authorization": _yk_auth(), "Idempotence-Key": "refund-" + str(payment_id),
               "Content-Type": "application/json"}
    body = {"payment_id": payment_id, "amount": {"value": f"{int(amount_rub)}.00", "currency": "RUB"}}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(YOOKASSA_REFUNDS_API, json=body, headers=headers, timeout=20) as r:
                data = await r.json()
                if r.status >= 300:
                    logger.error("YooKassa refund failed %s: %s", r.status, str(data)[:200])
                    return False, None
                return data.get("status") in ("succeeded", "pending"), data.get("id")
    except Exception:
        logger.exception("YooKassa refund error")
        return False, None
```

- [ ] **Step 3: Payment detail + refund endpoints in `admin_routes.py`**

```python
from payments_gw import yookassa_refund

@admin_routes.get("/api/admin/payments/{pid}")
async def admin_payment_detail(request):
    _require_admin(request)
    pool = await get_pool()
    pid = int(request.match_info["pid"])
    p = await pool.fetchrow("""SELECT p.*, u.username, u.first_name FROM payments p
                               LEFT JOIN users u ON p.user_tg_id=u.tg_id WHERE p.id=$1""", pid)
    if not p:
        return web.json_response({"error": "not_found"}, status=404)
    return web.json_response({"payment": _row(p)})

@admin_routes.post("/api/admin/payments/{pid}/refund")
async def admin_payment_refund(request):
    admin_id = await _require_role(request, "owner")
    pool = await get_pool()
    pid = int(request.match_info["pid"])
    reason = ((await _json_body(request)).get("reason") or "").strip()
    p = await pool.fetchrow("SELECT * FROM payments WHERE id=$1", pid)
    if not p:
        return web.json_response({"error": "not_found"}, status=404)
    if p["status"] != "paid":
        return web.json_response({"error": "only paid payments can be refunded"}, status=400)
    if p["refunded_at"]:
        return web.json_response({"error": "already refunded"}, status=409)
    if p["provider"] != "yookassa":
        # Platega has no confirmed refund API — record a manual refund mark only.
        await pool.execute("UPDATE payments SET status='refunded', refunded_at=NOW(), refund_id='manual' WHERE id=$1", pid)
        await _audit(admin_id, "payment_refund_manual", "payment", pid, {"status": p["status"]}, {"status": "refunded"}, reason, _client_ip(request))
        return web.json_response({"ok": True, "manual": True})
    # amount recomputed server-side from the stored payment — never trust client
    ok, refund_id = await yookassa_refund(p["external_id"], int(round(float(p["amount_rub"]))))
    if not ok:
        return web.json_response({"error": "gateway refund failed"}, status=502)
    await pool.execute("UPDATE payments SET status='refunded', refunded_at=NOW(), refund_id=$2 WHERE id=$1", pid, refund_id)
    await _audit(admin_id, "payment_refund", "payment", pid,
                 {"status": p["status"]}, {"status": "refunded", "refund_id": refund_id}, reason, _client_ip(request))
    return web.json_response({"ok": True, "refund_id": refund_id})
```

- [ ] **Step 4: `loadPayments` via DataTable + detail modal + refund (frontend)**

```javascript
function loadPayments() {
    var mc = document.getElementById("main-content");
    mc.innerHTML = '<div id="pay-table"></div>';
    window._payTable = DataTable(document.getElementById("pay-table"), {
        endpoint: "/api/admin/payments", exportCsv: true, searchable: true,
        searchPlaceholder: "order_id / @username / external_id",
        defaultSort: {key:"created_at", order:"desc"},
        filters: [
            {key:"provider", label:"Провайдер", type:"select", options:["yookassa","platega"]},
            {key:"status", label:"Статус", type:"select", options:["paid","pending","failed","refunded"]},
            {type:"daterange"}
        ],
        rowAction: openPayment,
        columns: [
            {key:"user_tg_id", label:"User", render:function(r){return esc(r.username?("@"+r.username):r.user_tg_id);}},
            {key:"amount_rub", label:"₽", sortable:true, align:"right"},
            {key:"tokens", label:"W", sortable:true, align:"right", hideOnMobile:true},
            {key:"provider", label:"Провайдер", hideOnMobile:true},
            {key:"status", label:"Статус", render:function(r){return badge(r.status);}},
            {key:"created_at", label:"Дата", sortable:true, render:function(r){return fmtDate(r.created_at);}}
        ]
    });
}
function openPayment(row) {
    api("/api/admin/payments/" + row.id).then(function(d){
        var p = d.payment;
        var canRefund = (window.adminRole === "owner") && p.status === "paid";
        var html = '<h3 class="modal-title">Платёж #' + p.id + ' ' + badge(p.status) + '</h3>' +
            '<div class="kv"><b>Юзер:</b> ' + esc(p.username?("@"+p.username):p.user_tg_id) + '</div>' +
            '<div class="kv"><b>Сумма:</b> ' + p.amount_rub + ' ₽ → ' + p.tokens + ' W</div>' +
            '<div class="kv"><b>Провайдер:</b> ' + esc(p.provider) + '</div>' +
            '<div class="kv"><b>order_id:</b> ' + esc(p.order_id) + '</div>' +
            '<div class="kv"><b>external_id:</b> ' + esc(p.external_id || "—") + '</div>' +
            '<div class="kv"><b>Создан:</b> ' + fmtDate(p.created_at) + '</div>' +
            '<div class="kv"><b>Оплачен:</b> ' + fmtDate(p.paid_at) + '</div>' +
            (p.refunded_at ? '<div class="kv"><b>Возврат:</b> ' + fmtDate(p.refunded_at) + ' (' + esc(p.refund_id||"") + ')</div>' : '') +
            (canRefund ? '<div style="margin-top:16px"><button class="btn btn-danger" id="pay-refund">Вернуть платёж</button></div>' : '');
        openModal(html);
        var rb = document.getElementById("pay-refund");
        if (rb) rb.onclick = function() {
            confirmDialog({title:"Вернуть " + p.amount_rub + " ₽?", body:"Деньги вернутся плательщику через " + p.provider + ". Действие необратимо.", danger:true, confirmLabel:"Вернуть"}).then(function(ok){
                if (!ok) return;
                btnBusy(rb, true);
                api("/api/admin/payments/" + p.id + "/refund", {method:"POST", body: JSON.stringify({reason:"admin refund"})})
                    .then(function(res){ toast("success", res.manual ? "Помечен возвращённым (вручную)" : "Возврат отправлен"); closeModal(); window._payTable.reload(); })
                    .catch(function(e){ btnBusy(rb,false); apiError(e); });
            });
        };
    }).catch(apiError);
}
```
Add `.kv{font-size:14px;margin:4px 0;color:var(--tx2)}.kv b{color:var(--tx)}` and `.modal-title{margin:0 0 12px}` to `admin.css` if not present. Bump `admin.js?v=21`.

- [ ] **Step 5: Manual verification**

Open Платежи: table with provider/status/date filters, search, sort by amount, CSV export. Click a row → detail modal. As owner on a `paid` ЮKassa payment → "Вернуть платёж" → confirm → (test mode) success toast, status flips to `refunded`, double-refund returns 409. As agent → Платежи nav hidden and `/refund` returns 403. Platega `paid` payment → manual refund mark path.

- [ ] **Step 6: Commit & PR**

```bash
git checkout -b feat/admin-payments
git add db/database.py payments_gw.py api/admin_routes.py webapp/static/js/admin.js webapp/static/css/admin.css webapp/templates/admin.html
git commit -m "feat(admin): payments DataTable + drill-down + refund (ЮKassa API, idempotent, owner-only, audited)"
git push -u origin feat/admin-payments
gh pr create -t "feat(admin): payments depth + refunds" -b "Payments on DataTable (filters/search/sort/CSV), drill-down modal, ЮKassa refund (idempotent, owner-only, audited; Platega manual mark)." && gh pr merge --merge --delete-branch
```

---

## Task 8 (PR #8): Withdrawals — DataTable, detail modal, bulk approve

**Files:**
- Modify: `api/admin_routes.py` (`admin_withdrawals` via list_query)
- Modify: `webapp/static/js/admin.js` (`loadWithdrawals` via DataTable; actions via kit)

- [ ] **Step 1: Refit `admin_withdrawals`**
```python
async def admin_withdrawals(request):
    _require_admin(request)
    return await list_query(
        request,
        base_sql="""SELECT w.*, u.username FROM withdrawals w LEFT JOIN users u ON w.user_tg_id=u.tg_id""",
        count_sql="SELECT COUNT(*) FROM withdrawals w LEFT JOIN users u ON w.user_tg_id=u.tg_id",
        search_cols=("u.username", "w.details"),
        sortable={"created_at":"w.created_at","amount_rub":"w.amount_rub"},
        filters={"status":"w.status","method":"w.method"},
        date_col="w.created_at",
        csv_name="withdrawals",
    )
```

- [ ] **Step 2: `loadWithdrawals` via DataTable with bulk approve + kit actions**
```javascript
function loadWithdrawals() {
    var mc = document.getElementById("main-content");
    mc.innerHTML = '<div id="wd-table"></div>';
    window._wdTable = DataTable(document.getElementById("wd-table"), {
        endpoint:"/api/admin/withdrawals", exportCsv:true, searchable:true,
        searchPlaceholder:"@username / реквизиты", defaultSort:{key:"created_at",order:"desc"},
        filters:[
            {key:"status",label:"Статус",type:"select",options:["pending","approved","paid","rejected"]},
            {key:"method",label:"Метод",type:"select",options:["card","usdt"]},
            {type:"daterange"}
        ],
        bulkActions:[{label:"Одобрить выбранные", run:bulkApproveWd}],
        rowAction: openWithdrawal,
        columns:[
            {key:"user_tg_id",label:"User",render:function(r){return esc(r.username?("@"+r.username):r.user_tg_id);}},
            {key:"amount_rub",label:"₽",sortable:true,align:"right"},
            {key:"method",label:"Метод",hideOnMobile:true},
            {key:"details",label:"Реквизиты",hideOnMobile:true},
            {key:"status",label:"Статус",render:function(r){return badge(r.status);}},
            {key:"created_at",label:"Дата",sortable:true,render:function(r){return fmtDate(r.created_at);}}
        ]
    });
}
function bulkApproveWd(ids) {
    return confirmDialog({title:"Одобрить "+ids.length+" заявок?"}).then(function(ok){
        if(!ok) return;
        return Promise.all(ids.map(function(id){ return api("/api/admin/withdrawals/"+id+"/action",{method:"POST",body:JSON.stringify({action:"approve",reason:"bulk"})}).catch(function(){}); }))
            .then(function(){ toast("success","Готово"); });
    });
}
function openWithdrawal(row) {
    var actions = "";
    if (row.status === "pending") actions = '<button class="btn btn-primary" onclick="wdDo('+row.id+",'approve')\">Одобрить</button> <button class=\"btn btn-danger\" onclick=\"wdDo("+row.id+",'reject')\">Отклонить</button>";
    else if (row.status === "approved") actions = '<button class="btn btn-primary" onclick="wdDo('+row.id+",'paid')\">Выплачено</button>";
    openModal('<h3 class="modal-title">Вывод #'+row.id+' '+badge(row.status)+'</h3>' +
        '<div class="kv"><b>Юзер:</b> '+esc(row.username?("@"+row.username):row.user_tg_id)+'</div>' +
        '<div class="kv"><b>Сумма:</b> '+row.amount_rub+' ₽</div>' +
        '<div class="kv"><b>Метод:</b> '+esc(row.method)+'</div>' +
        '<div class="kv"><b>Реквизиты:</b> '+esc(row.details)+'</div>' +
        '<div class="kv"><b>Создан:</b> '+fmtDate(row.created_at)+'</div>' +
        '<div style="margin-top:16px">'+actions+'</div>');
}
function wdDo(id, action) {
    var ask = action === "reject"
        ? formModal({title:"Причина отклонения", fields:[{name:"reason",label:"Причина",type:"textarea",required:true}], submitLabel:"Отклонить"}).then(function(v){return v?v.reason:null;})
        : confirmDialog({title:"Подтвердить?"}).then(function(ok){return ok?"":null;});
    ask.then(function(reason){
        if (reason === null) return;
        api("/api/admin/withdrawals/"+id+"/action",{method:"POST",body:JSON.stringify({action:action,reason:reason})})
            .then(function(d){ toast("success","Статус: "+d.status); closeModal(); window._wdTable.reload(); }).catch(apiError);
    });
}
```
Bump `admin.js?v=22`.

- [ ] **Step 3: Manual verification**

Открой Выводы: фильтры статус/метод/дата, поиск, сортировка, CSV. Клик по строке → модалка с действиями (через kit, не `prompt`). Reject требует причину; approve/paid через confirm. Выбери несколько pending → "Одобрить выбранные" → все становятся approved, каждое в аудите. Agent: раздел скрыт + 403 на action (owner-only из Task 4 Step 5).

- [ ] **Step 4: Commit & PR**

```bash
git checkout -b feat/admin-withdrawals
git add api/admin_routes.py webapp/static/js/admin.js webapp/templates/admin.html
git commit -m "feat(admin): withdrawals DataTable + detail modal + bulk approve (kit dialogs)"
git push -u origin feat/admin-withdrawals
gh pr create -t "feat(admin): withdrawals depth" -b "Withdrawals on DataTable (filters/search/sort/CSV), detail modal, bulk approve, kit dialogs replacing confirm/prompt." && gh pr merge --merge --delete-branch
```

---

## Task 9 (PR #9): Manual balance corrections via kit

**Files:**
- Modify: `webapp/static/js/admin.js` (replace `promptAdjust` prompt-chain with `formModal`; expose from payment modal)

**Interfaces:**
- Consumes: existing `POST /api/admin/users/{tgId}/adjust` (now owner-only via Task 4).

- [ ] **Step 1: Replace `promptAdjust` with a formModal**

```javascript
function promptAdjust(tgId) {
    formModal({title:"Коррекция баланса", submitLabel:"Применить", fields:[
        {name:"amount", label:"Сумма (W, можно отрицательную)", type:"number", required:true, hint:"Например 100 или -50"},
        {name:"reason", label:"Причина", type:"textarea", required:true}
    ]}).then(function(v){
        if (!v) return;
        api("/api/admin/users/"+tgId+"/adjust",{method:"POST",body:JSON.stringify({amount:v.amount,reason:v.reason})})
            .then(function(d){ toast("success","Новый баланс: "+(d.balance!=null?d.balance:"обновлён")); if(window.loadUserDetail) loadUserDetail(tgId); }).catch(apiError);
    });
}
```

- [ ] **Step 2: Add a "Коррекция баланса" button to the payment detail modal**

In `openPayment` (Task 7), inside the modal HTML when `window.adminRole === "owner"`, add next to refund:
```javascript
            (window.adminRole === "owner" ? '<button class="btn btn-outline" id="pay-adjust" style="margin-left:8px">Коррекция баланса юзера</button>' : '') +
```
And after `openModal(html)`:
```javascript
        var ab = document.getElementById("pay-adjust");
        if (ab) ab.onclick = function(){ promptAdjust(p.user_tg_id); };
```

- [ ] **Step 3: Verify the adjust endpoint returns the new balance**

Check `admin_adjust_balance` in `admin_routes.py` returns `{"ok":True,"balance":new_balance}`. If it returns only `{"ok":True}`, add the new balance to the response so the toast is accurate:
```python
    return web.json_response({"ok": True, "balance": new_balance})
```
(Use the value already computed in that handler.)

- [ ] **Step 4: Manual verification**

User detail → "Коррекция баланса" → formModal (no chained prompts); requires amount+reason; toast shows new balance; audit row written (check Аудит-лог). From a payment modal (owner) the same form opens for the payer. Agent: button absent and endpoint 403.

- [ ] **Step 5: Commit & PR**

```bash
git checkout -b feat/admin-balance-corrections
git add webapp/static/js/admin.js api/admin_routes.py webapp/templates/admin.html
git commit -m "feat(admin): balance corrections via formModal; reachable from payment drill-down"
git push -u origin feat/admin-balance-corrections
gh pr create -t "feat(admin): balance corrections" -b "formModal-based balance adjust (amount+reason) replacing prompt-chain; reachable from user and payment detail; owner-only, audited." && gh pr merge --merge --delete-branch
```
Bump `admin.js?v=23`.

---

## Task 10 (PR #10): Memory + docs wrap-up

**Files:**
- Modify: `C:\Users\ganen\.claude\projects\C--Users-ganen-PromptW\memory\` (new memory + index)
- Modify: `docs/specs/2026-06-25-admin-overhaul-design.md` (mark Phase 0+1 shipped)

- [ ] **Step 1:** Add a `promptw-admin-overhaul.md` project memory documenting the shipped foundation (admin-kit.js / admin-table.js / list_query / roles / refund), the owner+agent model, and that Phases 2-3 remain. Update `MEMORY.md` index.

- [ ] **Step 2:** Edit the spec's Status to "Phase 0+1 shipped (PRs #…)"; commit.

- [ ] **Step 3: Commit & PR** for the spec edit; memory files are local (not in repo).

---

## Self-Review

**1. Spec coverage:**
- §3.1 UI-kit → Task 1 ✓ · §3.2 DataTable → Task 3 ✓ · §3.3 backend list helper → Task 2 ✓ · §3.4 roles → Task 4 ✓ · §3.5 responsive → Task 5 ✓
- §4.1 Dashboard charts → Task 6 ✓ · §4.2 Payments drill-down+refund → Task 7 ✓ · §4.3 Withdrawals → Task 8 ✓ · §4.4 balance corrections → Task 9 ✓
- §5 data model (admin_accounts, payments.refunded_at/refund_id) → Tasks 4 & 7 ✓ · §6 security (role gating, idempotent refund, SQL allowlists, CSV guard) → Tasks 2/4/7 ✓ · §9 rollout order → task order matches ✓

**2. Placeholder scan:** No "TBD/TODO/handle errors"; every code step has concrete code. (The CSV row builder in Task 2 Step 1 was fixed inline to `w.writerow([_csv_cell(_serialize(r[c])) for c in cols])`.)

**3. Type consistency:** `list_query` returns `{items,total}` everywhere; `DataTable` reads `d.items`/`d.total`; `_require_role(request, "owner")` is async and awaited at every call site; `window.adminRole`/`window.authToken` globals consistent across files; `yookassa_refund` returns `(ok, refund_id)` consumed as such.
