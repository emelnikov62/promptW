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
