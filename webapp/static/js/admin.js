var currentSection = "dashboard";
var PAGE_SIZE = 50;
var authToken = "";

// ── Auth ──
function resolveToken() {
    var p = new URLSearchParams(location.search);
    if (p.get("tgauth")) return p.get("tgauth");
    var stored = localStorage.getItem("pw_admin_token");
    if (stored) return stored;
    return "";
}

function hasAuth() {
    var tg = window.Telegram && Telegram.WebApp;
    if (tg && tg.initData) return true;
    return !!authToken;
}

function getAuthHeaders() {
    var h = {"Content-Type": "application/json"};
    var tg = window.Telegram && Telegram.WebApp;
    if (tg && tg.initData) h["X-Init-Data"] = tg.initData;
    if (authToken) h["X-Auth-Token"] = authToken;
    return h;
}

function api(path, opts) {
    opts = opts || {};
    opts.headers = Object.assign(getAuthHeaders(), opts.headers || {});
    return fetch(path, opts).then(function(r) {
        if (r.status === 403) { logout(); throw new Error("forbidden"); }
        return r.json();
    });
}

function showLogin() {
    document.getElementById("admin-app").style.display = "none";
    document.getElementById("modal-overlay").classList.add("hidden");
    var el = document.getElementById("login-screen");
    if (!el) {
        el = document.createElement("div");
        el.id = "login-screen";
        el.innerHTML = '<div class="login-box"><div class="sidebar-brand">Prompt<span class="brand-accent">W</span> <span class="brand-tag">admin</span></div>' +
            '<div id="login-error" style="color:var(--error);font-size:13px;margin-bottom:12px;display:none"></div>' +
            '<input class="form-input" id="login-user" placeholder="Логин" autocomplete="username" style="margin-bottom:10px;width:100%">' +
            '<input class="form-input" id="login-pass" type="password" placeholder="Пароль" autocomplete="current-password" style="margin-bottom:14px;width:100%">' +
            '<button class="btn btn-primary" id="login-btn" style="width:100%">Войти</button></div>';
        document.body.appendChild(el);
    }
    el.style.display = "flex";
    document.getElementById("login-btn").onclick = doLogin;
    document.getElementById("login-pass").onkeydown = function(e) { if (e.key === "Enter") doLogin(); };
}

function doLogin() {
    var login = document.getElementById("login-user").value.trim();
    var pass = document.getElementById("login-pass").value;
    var errEl = document.getElementById("login-error");
    if (!login || !pass) { errEl.textContent = "Заполните оба поля"; errEl.style.display = "block"; return; }
    errEl.style.display = "none";
    document.getElementById("login-btn").disabled = true;
    fetch("/api/admin/login", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify({login: login, password: pass})
    }).then(function(r) { return r.json(); }).then(function(d) {
        document.getElementById("login-btn").disabled = false;
        if (d.ok && d.token) {
            authToken = d.token;
            localStorage.setItem("pw_admin_token", d.token);
            document.getElementById("login-screen").style.display = "none";
            document.getElementById("admin-app").style.display = "flex";
            showSection("dashboard");
        } else {
            errEl.textContent = d.error || "Ошибка авторизации";
            errEl.style.display = "block";
        }
    }).catch(function() {
        document.getElementById("login-btn").disabled = false;
        errEl.textContent = "Ошибка сети";
        errEl.style.display = "block";
    });
}

function logout() {
    authToken = "";
    localStorage.removeItem("pw_admin_token");
    showLogin();
}

// ── Navigation ──
var navItems = document.querySelectorAll(".nav-item");
var sectionTitles = {dashboard:"Dashboard",users:"Пользователи",generations:"Генерации",payments:"Платежи",withdrawals:"Выводы",templates:"Шаблоны",audit:"Аудит-лог"};

navItems.forEach(function(btn) {
    btn.addEventListener("click", function() {
        showSection(btn.dataset.section);
        var sb = document.getElementById("sidebar");
        if (sb) sb.classList.remove("open");
    });
});

document.getElementById("menu-toggle").addEventListener("click", function() {
    document.getElementById("sidebar").classList.toggle("open");
});

document.getElementById("modal-overlay").addEventListener("click", function(e) {
    if (e.target === this) closeModal();
});

function showSection(name) {
    currentSection = name;
    navItems.forEach(function(b) { b.classList.toggle("active", b.dataset.section === name); });
    document.getElementById("topbar-title").textContent = sectionTitles[name] || name;
    var loaders = {
        dashboard: loadDashboard,
        users: function() { loadUsers(0); },
        generations: function() { loadGenerations(0); },
        payments: function() { loadPayments(0); },
        withdrawals: function() { loadWithdrawals(0); },
        templates: function() { loadTemplates(0); },
        audit: function() { loadAudit(0); }
    };
    if (loaders[name]) loaders[name]();
}

// ── Helpers ──
function esc(s) { var d = document.createElement("div"); d.textContent = s || ""; return d.innerHTML; }
function fmtDate(s) { if (!s) return "—"; var d = new Date(s); return d.toLocaleDateString("ru") + " " + d.toLocaleTimeString("ru", {hour:"2-digit",minute:"2-digit"}); }
function badge(status) { return '<span class="badge badge-' + esc(status) + '">' + esc(status) + '</span>'; }

function pagination(total, offset, loadFn) {
    var pages = Math.ceil(total / PAGE_SIZE);
    var cur = Math.floor(offset / PAGE_SIZE);
    return '<div class="pag">' +
        '<button class="pag-btn" onclick="' + loadFn + '(' + Math.max(0, (cur-1)*PAGE_SIZE) + ')"' + (cur === 0 ? " disabled" : "") + '>&larr;</button>' +
        '<span class="pag-info">' + (cur+1) + ' / ' + Math.max(1,pages) + ' (' + total + ')</span>' +
        '<button class="pag-btn" onclick="' + loadFn + '(' + ((cur+1)*PAGE_SIZE) + ')"' + (cur+1 >= pages ? " disabled" : "") + '>&rarr;</button>' +
        '</div>';
}

function closeModal() { document.getElementById("modal-overlay").classList.add("hidden"); }
function openModal(html) {
    document.getElementById("modal-content").innerHTML = '<button class="modal-close" onclick="closeModal()">&times;</button>' + html;
    document.getElementById("modal-overlay").classList.remove("hidden");
}

// ── Dashboard ──
function loadDashboard() {
    var mc = document.getElementById("main-content");
    mc.innerHTML = '<p style="color:var(--tx2)">Загрузка...</p>';
    api("/api/admin/stats").then(function(d) {
        mc.innerHTML = '<div class="kpi-grid">' +
            kpi("Пользователи", d.users.total, "сегодня +" + d.users.today + " / 7д +" + d.users.week) +
            kpi("Генерации", d.generations.total, "done " + d.generations.done + " / err " + d.generations.error, "accent") +
            kpi("Выручка ₽", d.revenue.total.toLocaleString("ru"), "7д: " + d.revenue.week.toLocaleString("ru") + " ₽ · " + d.revenue.payments + " оплат", "gold") +
            kpi("Токены потрачено", d.tokens.spent.toLocaleString("ru"), "на балансах: " + d.tokens.on_balances.toLocaleString("ru")) +
            kpi("Выводы pending", d.withdrawals.pending, d.withdrawals.pending_amount.toLocaleString("ru") + " ₽", d.withdrawals.pending > 0 ? "error" : "success") +
            '</div>';
    });
}
function kpi(label, value, sub, cls) {
    return '<div class="kpi"><div class="kpi-label">' + esc(label) + '</div><div class="kpi-value ' + (cls||"") + '">' + value + '</div><div class="kpi-sub">' + esc(sub||"") + '</div></div>';
}

// ── Users ──
var usersQuery = "";
function loadUsers(offset) {
    var mc = document.getElementById("main-content");
    var q = usersQuery;
    var url = "/api/admin/users?limit=" + PAGE_SIZE + "&offset=" + offset + (q ? "&q=" + encodeURIComponent(q) : "");
    mc.innerHTML = '<div class="toolbar"><input class="search-input" id="user-search" placeholder="Поиск по tg_id или username" value="' + esc(q) + '" onkeydown="if(event.key===\'Enter\'){usersQuery=this.value;loadUsers(0)}"></div><p style="color:var(--tx2)">Загрузка...</p>';
    api(url).then(function(d) {
        var rows = d.items.map(function(u) {
            return '<tr class="clickable" onclick="loadUserDetail(' + u.tg_id + ')"><td>' + u.tg_id + '</td><td>' + esc(u.username) + '</td><td>' + esc(u.first_name) + '</td><td>' + u.balance + '</td><td>' + (u.banned ? badge("banned") : "—") + '</td><td>' + fmtDate(u.created_at) + '</td></tr>';
        }).join("");
        mc.innerHTML = '<div class="toolbar"><input class="search-input" id="user-search" placeholder="Поиск по tg_id или username" value="' + esc(q) + '" onkeydown="if(event.key===\'Enter\'){usersQuery=this.value;loadUsers(0)}"><button class="btn btn-outline btn-sm" onclick="usersQuery=document.getElementById(\'user-search\').value;loadUsers(0)">Найти</button></div>' +
            '<div class="tbl-wrap"><table class="tbl"><thead><tr><th>TG ID</th><th>Username</th><th>Имя</th><th>Баланс</th><th>Бан</th><th>Регистрация</th></tr></thead><tbody>' + (rows || '<tr><td colspan="6" style="text-align:center;color:var(--tx3)">Нет данных</td></tr>') + '</tbody></table></div>' +
            pagination(d.total, offset, "loadUsers");
    });
}

function loadUserDetail(tgId) {
    api("/api/admin/users/" + tgId).then(function(d) {
        var u = d.user;
        var html = '<h3>Пользователь ' + u.tg_id + '</h3>' +
            '<div class="modal-row"><span class="modal-label">Username</span><span>' + esc(u.username) + '</span></div>' +
            '<div class="modal-row"><span class="modal-label">Имя</span><span>' + esc((u.first_name||"") + " " + (u.last_name||"")) + '</span></div>' +
            '<div class="modal-row"><span class="modal-label">Язык</span><span>' + esc(u.lang) + '</span></div>' +
            '<div class="modal-row"><span class="modal-label">Баланс</span><span>' + u.balance + ' W</span></div>' +
            '<div class="modal-row"><span class="modal-label">Реф. баланс</span><span>' + u.ref_balance + ' ₽</span></div>' +
            '<div class="modal-row"><span class="modal-label">Бан</span><span>' + (u.banned ? "Да" : "Нет") + '</span></div>' +
            '<div class="modal-row"><span class="modal-label">Заметка</span><span>' + esc(u.admin_note || "—") + '</span></div>' +
            '<div class="modal-row"><span class="modal-label">Реферер</span><span>' + (u.referrer_id || "—") + '</span></div>' +
            '<div class="modal-row"><span class="modal-label">Регистрация</span><span>' + fmtDate(u.created_at) + '</span></div>';

        html += '<div class="modal-actions">' +
            '<button class="btn btn-primary btn-sm" onclick="promptAdjust(' + u.tg_id + ')">Начислить/списать</button>' +
            '<button class="btn ' + (u.banned ? "btn-outline" : "btn-danger") + ' btn-sm" onclick="toggleBan(' + u.tg_id + ',' + !u.banned + ')">' + (u.banned ? "Разбанить" : "Забанить") + '</button>' +
            '<button class="btn btn-outline btn-sm" onclick="promptNote(' + u.tg_id + ')">Заметка</button>' +
            '</div>';

        if (d.generations.length) {
            html += '<div class="modal-section"><h4>Генерации (последние 20)</h4><div class="tbl-wrap"><table class="tbl"><thead><tr><th>Тип</th><th>Модель</th><th>Статус</th><th>Стоимость</th><th>Дата</th></tr></thead><tbody>';
            d.generations.forEach(function(g) {
                html += '<tr><td>' + esc(g.gen_type) + '</td><td>' + esc(g.model) + '</td><td>' + badge(g.status) + '</td><td>' + g.cost + '</td><td>' + fmtDate(g.created_at) + '</td></tr>';
            });
            html += '</tbody></table></div></div>';
        }

        if (d.transactions.length) {
            html += '<div class="modal-section"><h4>Транзакции (последние 20)</h4><div class="tbl-wrap"><table class="tbl"><thead><tr><th>Сумма</th><th>Тип</th><th>Описание</th><th>Дата</th></tr></thead><tbody>';
            d.transactions.forEach(function(t) {
                html += '<tr><td style="color:' + (t.amount > 0 ? "var(--success)" : "var(--error)") + '">' + (t.amount > 0 ? "+" : "") + t.amount + '</td><td>' + esc(t.tx_type) + '</td><td>' + esc(t.description) + '</td><td>' + fmtDate(t.created_at) + '</td></tr>';
            });
            html += '</tbody></table></div></div>';
        }

        if (d.payments.length) {
            html += '<div class="modal-section"><h4>Платежи (последние 20)</h4><div class="tbl-wrap"><table class="tbl"><thead><tr><th>Сумма ₽</th><th>Токены</th><th>Провайдер</th><th>Статус</th><th>Дата</th></tr></thead><tbody>';
            d.payments.forEach(function(p) {
                html += '<tr><td>' + p.amount_rub + '</td><td>' + p.tokens + '</td><td>' + esc(p.provider) + '</td><td>' + badge(p.status) + '</td><td>' + fmtDate(p.created_at) + '</td></tr>';
            });
            html += '</tbody></table></div></div>';
        }

        openModal(html);
    });
}

function promptAdjust(tgId) {
    var amount = prompt("Сумма (+ начислить, - списать):");
    if (!amount) return;
    var reason = prompt("Причина:");
    if (!reason) return;
    api("/api/admin/users/" + tgId + "/adjust", {
        method: "POST", body: JSON.stringify({amount: parseInt(amount), reason: reason})
    }).then(function(d) {
        if (d.ok) { alert("Баланс: " + d.balance); loadUserDetail(tgId); }
        else alert("Ошибка: " + (d.error || "unknown"));
    });
}

function toggleBan(tgId, ban) {
    var reason = prompt((ban ? "Причина бана:" : "Причина разбана:"));
    if (reason === null) return;
    api("/api/admin/users/" + tgId + "/ban", {
        method: "POST", body: JSON.stringify({banned: ban, reason: reason})
    }).then(function(d) {
        if (d.ok) { alert(ban ? "Забанен" : "Разбанен"); loadUserDetail(tgId); }
    });
}

function promptNote(tgId) {
    var note = prompt("Заметка:");
    if (note === null) return;
    api("/api/admin/users/" + tgId + "/note", {
        method: "POST", body: JSON.stringify({note: note})
    }).then(function(d) {
        if (d.ok) loadUserDetail(tgId);
    });
}

// ── Generations ──
function loadGenerations(offset) {
    var mc = document.getElementById("main-content");
    mc.innerHTML = '<p style="color:var(--tx2)">Загрузка...</p>';
    api("/api/admin/generations?limit=" + PAGE_SIZE + "&offset=" + offset).then(function(d) {
        var rows = d.items.map(function(g) {
            return '<tr><td>' + g.id + '</td><td>' + g.user_tg_id + (g.username ? " (@" + esc(g.username) + ")" : "") + '</td><td>' + esc(g.gen_type) + '</td><td>' + esc(g.model) + '</td><td>' + badge(g.status) + '</td><td>' + g.cost + '</td><td title="' + esc(g.prompt) + '">' + esc((g.prompt||"").substring(0,40)) + '</td><td>' + fmtDate(g.created_at) + '</td></tr>';
        }).join("");
        mc.innerHTML = '<div class="tbl-wrap"><table class="tbl"><thead><tr><th>ID</th><th>Пользователь</th><th>Тип</th><th>Модель</th><th>Статус</th><th>Стоимость</th><th>Промпт</th><th>Дата</th></tr></thead><tbody>' + (rows || '<tr><td colspan="8" style="text-align:center;color:var(--tx3)">Нет данных</td></tr>') + '</tbody></table></div>' +
            pagination(d.total, offset, "loadGenerations");
    });
}

// ── Payments ──
function loadPayments(offset) {
    var mc = document.getElementById("main-content");
    mc.innerHTML = '<p style="color:var(--tx2)">Загрузка...</p>';
    api("/api/admin/payments?limit=" + PAGE_SIZE + "&offset=" + offset).then(function(d) {
        var rows = d.items.map(function(p) {
            return '<tr><td>' + p.user_tg_id + (p.username ? " (@" + esc(p.username) + ")" : "") + '</td><td>' + p.amount_rub + ' ₽</td><td>' + p.tokens + '</td><td>' + esc(p.provider) + '</td><td>' + badge(p.status) + '</td><td>' + fmtDate(p.created_at) + '</td></tr>';
        }).join("");
        mc.innerHTML = '<div class="tbl-wrap"><table class="tbl"><thead><tr><th>Пользователь</th><th>Сумма</th><th>Токены</th><th>Провайдер</th><th>Статус</th><th>Дата</th></tr></thead><tbody>' + (rows || '<tr><td colspan="6" style="text-align:center;color:var(--tx3)">Нет данных</td></tr>') + '</tbody></table></div>' +
            pagination(d.total, offset, "loadPayments");
    });
}

// ── Withdrawals ──
function loadWithdrawals(offset) {
    var mc = document.getElementById("main-content");
    mc.innerHTML = '<p style="color:var(--tx2)">Загрузка...</p>';
    api("/api/admin/withdrawals?limit=" + PAGE_SIZE + "&offset=" + offset).then(function(d) {
        var rows = d.items.map(function(w) {
            var actions = "";
            if (w.status === "pending") actions = '<button class="btn btn-primary btn-sm" onclick="wdAction(' + w.id + ',\'approve\')">Approve</button> <button class="btn btn-danger btn-sm" onclick="wdAction(' + w.id + ',\'reject\')">Reject</button>';
            else if (w.status === "approved") actions = '<button class="btn btn-outline btn-sm" onclick="wdAction(' + w.id + ',\'paid\')">Mark Paid</button>';
            return '<tr><td>' + w.user_tg_id + (w.username ? " (@" + esc(w.username) + ")" : "") + '</td><td>' + w.amount_rub + ' ₽</td><td>' + esc(w.method) + '</td><td>' + esc(w.details) + '</td><td>' + badge(w.status) + '</td><td>' + actions + '</td><td>' + fmtDate(w.created_at) + '</td></tr>';
        }).join("");
        mc.innerHTML = '<div class="tbl-wrap"><table class="tbl"><thead><tr><th>Пользователь</th><th>Сумма</th><th>Метод</th><th>Реквизиты</th><th>Статус</th><th>Действия</th><th>Дата</th></tr></thead><tbody>' + (rows || '<tr><td colspan="7" style="text-align:center;color:var(--tx3)">Нет данных</td></tr>') + '</tbody></table></div>' +
            pagination(d.total, offset, "loadWithdrawals");
    });
}

function wdAction(id, action) {
    if (!confirm("Подтвердить: " + action + "?")) return;
    var reason = prompt("Причина/комментарий:");
    if (reason === null) return;
    api("/api/admin/withdrawals/" + id + "/action", {
        method: "POST", body: JSON.stringify({action: action, reason: reason})
    }).then(function(d) {
        if (d.ok) loadWithdrawals(0);
        else alert("Ошибка: " + (d.error || "unknown"));
    });
}

// ── Templates ──
var _tplCache = {};   // id -> full row, for the edit modal

function loadTemplates(offset) {
    var mc = document.getElementById("main-content");
    mc.innerHTML = '<p style="color:var(--tx2)">Загрузка...</p>';
    api("/api/admin/templates?limit=" + PAGE_SIZE + "&offset=" + offset).then(function(d) {
        _tplCache = {};
        var rows = d.items.map(function(tt) {
            var ttl = (tt.title && (tt.title.ru || tt.title.en)) || "";
            var en = tt.enabled ? '<span class="badge badge-done">on</span>' : '<span class="badge badge-error">off</span>';
            return '<tr><td>' + esc(tt.id) + '</td><td>' + esc(tt.type) + '</td><td>' + esc(tt.category||"—") + '</td><td>' + tt.cost + '</td><td>' + tt.sort_order + '</td><td>' + en + '</td><td>' + esc(ttl) + '</td>' +
                '<td><button class="btn btn-outline btn-sm" onclick="editTemplate(\'' + esc(tt.id) + '\')">Изм.</button> ' +
                '<button class="btn btn-danger btn-sm" onclick="deleteTemplate(\'' + esc(tt.id) + '\')">Удал.</button></td></tr>';
        }).join("");
        mc.innerHTML = '<div style="margin-bottom:12px"><button class="btn btn-primary btn-sm" onclick="newTemplate()">+ Новый шаблон</button></div>' +
            '<div class="tbl-wrap"><table class="tbl"><thead><tr><th>ID</th><th>Тип</th><th>Категория</th><th>Цена</th><th>Порядок</th><th>Вкл</th><th>Название</th><th>Действия</th></tr></thead><tbody>' +
            (rows || '<tr><td colspan="8" style="text-align:center;color:var(--tx3)">Нет данных</td></tr>') + '</tbody></table></div>' +
            pagination(d.total, offset, "loadTemplates");
    });
}

function _tplForm(t, isNew) {
    function ta(label, id, val) {
        return '<div class="modal-section"><h4>' + label + '</h4><textarea id="tf-' + id + '" rows="' + (id==="definition"?12:3) + '" style="width:100%;font-family:monospace;font-size:12px">' + esc(val) + '</textarea></div>';
    }
    return '<h3 style="margin:0 0 12px">' + (isNew ? "Новый шаблон" : "Шаблон: " + esc(t.id)) + '</h3>' +
        '<div class="modal-section"><h4>ID (слаг)</h4><input id="tf-id" value="' + esc(t.id||"") + '"' + (isNew?"":" disabled") + ' style="width:100%"></div>' +
        '<div class="modal-section"><h4>Тип</h4><select id="tf-type" style="width:100%">' +
            ['photo','video','audio'].map(function(x){return '<option'+(t.type===x?' selected':'')+'>'+x+'</option>';}).join("") + '</select></div>' +
        '<div class="modal-section"><h4>Цена (W)</h4><input id="tf-cost" type="number" value="' + (t.cost||0) + '" style="width:100%"></div>' +
        '<div class="modal-section"><h4>Порядок</h4><input id="tf-sort_order" type="number" value="' + (t.sort_order||0) + '" style="width:100%"></div>' +
        '<div class="modal-section"><h4>Категория</h4><input id="tf-category" value="' + esc(t.category||"") + '" style="width:100%"></div>' +
        '<div class="modal-section"><label><input id="tf-enabled" type="checkbox"' + (t.enabled!==false?' checked':'') + '> Включён</label></div>' +
        ta("Название (JSON {ru,en,es})", "title", JSON.stringify(t.title||{}, null, 1)) +
        ta("Превью (JSON {img,full})", "preview", JSON.stringify(t.preview||{}, null, 1)) +
        ta("Definition (JSON)", "definition", JSON.stringify(t.definition||{}, null, 2)) +
        '<div style="display:flex;gap:8px;margin-top:8px"><button class="btn btn-primary" onclick="saveTemplate(' + (isNew?'true':'false') + ')">Сохранить</button>' +
        '<button class="btn btn-outline" onclick="closeModal()">Отмена</button></div>';
}

function newTemplate() {
    openModal(_tplForm({enabled:true, type:"photo"}, true));
}

function editTemplate(id) {
    api("/api/admin/templates/" + encodeURIComponent(id)).then(function(t) {
        if (t.error) { alert("Ошибка: " + t.error); return; }
        _tplCache[id] = t;
        openModal(_tplForm(t, false));
    });
}

function saveTemplate(isNew) {
    var id = document.getElementById("tf-id").value.trim();
    if (!id) { alert("ID обязателен"); return; }
    var payload;
    try {
        payload = {
            id: id,
            type: document.getElementById("tf-type").value,
            cost: parseInt(document.getElementById("tf-cost").value, 10) || 0,
            sort_order: parseInt(document.getElementById("tf-sort_order").value, 10) || 0,
            category: document.getElementById("tf-category").value.trim() || null,
            enabled: document.getElementById("tf-enabled").checked,
            title: JSON.parse(document.getElementById("tf-title").value || "{}"),
            preview: JSON.parse(document.getElementById("tf-preview").value || "{}"),
            definition: JSON.parse(document.getElementById("tf-definition").value || "{}")
        };
    } catch (e) { alert("Ошибка в JSON: " + e.message); return; }
    var path = isNew ? "/api/admin/templates" : "/api/admin/templates/" + encodeURIComponent(id);
    api(path, { method: isNew ? "POST" : "PUT", body: JSON.stringify(payload) }).then(function(d) {
        if (d.ok) { closeModal(); loadTemplates(0); }
        else alert("Ошибка: " + (d.error || "unknown"));
    });
}

function deleteTemplate(id) {
    if (!confirm("Удалить шаблон «" + id + "»? Это действие необратимо.")) return;
    api("/api/admin/templates/" + encodeURIComponent(id), { method: "DELETE" }).then(function(d) {
        if (d.ok) loadTemplates(0);
        else alert("Ошибка: " + (d.error || "unknown"));
    });
}

// ── Audit ──
function loadAudit(offset) {
    var mc = document.getElementById("main-content");
    mc.innerHTML = '<p style="color:var(--tx2)">Загрузка...</p>';
    api("/api/admin/audit?limit=" + PAGE_SIZE + "&offset=" + offset).then(function(d) {
        var rows = d.items.map(function(a) {
            return '<tr><td>' + a.admin_tg_id + '</td><td>' + esc(a.action) + '</td><td>' + esc(a.target_type||"") + '</td><td>' + esc(a.target_id||"") + '</td><td>' + esc(a.reason||"") + '</td><td>' + fmtDate(a.created_at) + '</td></tr>';
        }).join("");
        mc.innerHTML = '<div class="tbl-wrap"><table class="tbl"><thead><tr><th>Админ</th><th>Действие</th><th>Цель</th><th>ID</th><th>Причина</th><th>Дата</th></tr></thead><tbody>' + (rows || '<tr><td colspan="6" style="text-align:center;color:var(--tx3)">Нет данных</td></tr>') + '</tbody></table></div>' +
            pagination(d.total, offset, "loadAudit");
    });
}

// ── Init ──
authToken = resolveToken();
if (hasAuth()) {
    showSection("dashboard");
} else {
    showLogin();
}
