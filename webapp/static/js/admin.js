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
var sectionTitles = {dashboard:"Dashboard",users:"Пользователи",generations:"Генерации",payments:"Платежи",withdrawals:"Выводы",templates:"Шаблоны",promos:"Промокоды",support:"Поддержка",face:"Сходство лиц",audit:"Аудит-лог"};

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
        promos: function() { loadPromos(0); },
        support: function() { loadSupport("open", 0); },
        face: function() { loadFace(facePeriod); },
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

// ── Face similarity ──
var facePeriod = "all";
function setFacePeriod(p) { facePeriod = p; loadFace(p); }
function pct(n, d) { return d > 0 ? Math.round(n * 100 / d) + "%" : "—"; }
function num(v) { return (v == null) ? "—" : v; }

function loadFace(period) {
    facePeriod = period || "all";
    var mc = document.getElementById("main-content");
    mc.innerHTML = '<p style="color:var(--tx2)">Загрузка...</p>';
    api("/api/admin/face-stats?period=" + facePeriod).then(function(d) {
        var s = d.stats || {};
        var periods = [["day","День"],["week","Неделя"],["month","Месяц"],["all","Всё"]];
        var tabs = '<div class="face-tabs">' + periods.map(function(p) {
            return '<button class="face-tab' + (p[0] === facePeriod ? " active" : "") +
                '" onclick="setFacePeriod(\'' + p[0] + '\')">' + p[1] + '</button>';
        }).join("") + '</div>';

        var ref = s.ref_found || 0;
        var firstAcc = (s.accepted_first || 0);
        var html = tabs + '<div class="kpi-grid">' +
            kpi("Eligible-генераций", num(s.total), "с лицом в рефе: " + num(ref) + " (" + pct(ref, s.total) + ")") +
            kpi("Retry-rate", pct(s.retried || 0, s.total), num(s.retried) + " с ≥2 попыток", (s.retried > 0 ? "accent" : "")) +
            kpi("Acceptance", pct(s.accepted || 0, ref), "с 1-й: " + pct(firstAcc, ref), "success") +
            kpi("Лишних прогонов", num(s.extra_attempts), "потери: " + (s.money_lost || 0).toLocaleString("ru") + " ₽" + (s.unit_cost ? "" : " (задай unit cost)"), (s.extra_attempts > 0 ? "error" : "")) +
            kpi("Средний score", s.avg_score != null ? s.avg_score.toFixed(3) : "—", "медиана: " + (s.median_score != null ? s.median_score.toFixed(3) : "—")) +
            '</div>';

        // Attempts distribution + score bands
        var a1 = s.att1 || 0, a2 = s.att2 || 0, a3 = s.att3 || 0, at = a1 + a2 + a3;
        html += '<div class="face-row">';
        html += '<div class="face-card"><h3>Попытки</h3>' +
            faceBar("1 попытка", a1, at, "var(--success)") +
            faceBar("2 попытки", a2, at, "var(--gold)") +
            faceBar("3 попытки", a3, at, "var(--error)") + '</div>';
        html += '<div class="face-card"><h3>Распределение score (где есть реф)</h3>' +
            faceBar("Отлично ≥0.50", s.band_strong || 0, ref, "var(--success)") +
            faceBar("Ок 0.35–0.50", s.band_ok || 0, ref, "var(--gold)") +
            faceBar("Слабо <0.35", s.band_weak || 0, ref, "var(--error)") + '</div>';
        html += '</div>';

        // Per-template table
        var rows = d.by_template || [];
        var tbl = '<div class="face-card" style="margin-top:16px"><h3>По шаблонам</h3>';
        if (!rows.length) {
            tbl += '<p style="color:var(--tx2)">Нет данных за период.</p>';
        } else {
            tbl += '<table class="tbl"><thead><tr><th>Шаблон</th><th>Всего</th><th>Retry</th><th>Лишних</th><th>Потери ₽</th><th>Accept</th><th>Avg score</th></tr></thead><tbody>';
            rows.forEach(function(r) {
                tbl += '<tr><td>' + esc(r.tpl_id) + '</td><td>' + num(r.total) + '</td><td>' +
                    pct(r.retried || 0, r.total) + '</td><td>' + num(r.extra_attempts) + '</td><td>' +
                    (r.money_lost || 0).toLocaleString("ru") + '</td><td>' + pct(r.accepted || 0, r.total) +
                    '</td><td>' + (r.avg_score != null ? r.avg_score.toFixed(3) : "—") + '</td></tr>';
            });
            tbl += '</tbody></table>';
        }
        tbl += '</div>';
        mc.innerHTML = html + tbl;
    }).catch(function() {
        mc.innerHTML = '<p style="color:var(--error)">Не удалось загрузить статистику.</p>';
    });
}
function faceBar(label, val, total, color) {
    var p = total > 0 ? Math.round(val * 100 / total) : 0;
    return '<div class="face-line"><span class="face-line-l">' + esc(label) + '</span>' +
        '<span class="face-line-bar"><span style="width:' + p + '%;background:' + color + '"></span></span>' +
        '<span class="face-line-v">' + val + ' · ' + p + '%</span></div>';
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

function tfField(label, inner) {
    return '<div class="tf-field"><label class="tf-label">' + label + '</label>' + inner + '</div>';
}

function tfThumbInner(url) {
    if (!url) return '<span class="tf-thumb-empty">Нет превью</span>';
    if (/\.(mp4|webm|mov)(\?|#|$)/i.test(url)) return '<video src="' + esc(url) + '" muted playsinline></video>';
    return '<img src="' + esc(url) + '" alt="">';
}

function refreshTfThumb() {
    var box = document.getElementById("tf-thumb");
    if (box) box.innerHTML = tfThumbInner(document.getElementById("tf-preview-img").value.trim());
}

// Known values for datalist suggestions (free-text still allowed).
var TF_MODELS = ["NanoBanana PRO", "Seedance 2.0", "Grok Imagine 1.5", "Kling 3.0", "Kling Motion 3.0", "Suno V5", "Suno V4.5"];
var TF_RATIOS = ["9:16", "3:4", "1:1", "4:3", "16:9"];
var TF_QUALS = ["480p", "720p", "1080p", "1K", "2K", "4K"];
var _tfOrigDef = {};   // original definition, so unknown keys survive a save
var _tfParams = [];    // working params model for the visual editor

// ── Params visual editor ──
// Edits mutate the cloned model in place (extra keys on existing options survive);
// add/remove re-render. Text inputs write to the model on input WITHOUT re-rendering,
// so focus isn't lost mid-typing.
function _setPath(o, path, val) {
    var ps = path.split(".");
    while (ps.length > 1) { var k = ps.shift(); if (!o[k]) o[k] = {}; o = o[k]; }
    o[ps[0]] = val;
}
function escA(s) { return esc(s == null ? "" : String(s)).replace(/"/g, "&quot;"); }

function tfAddParam(kind) {
    if (kind === "gender") _tfParams.push({ id:"gender", control:"pills", "default":"female", label:{ru:"Пол",en:"Gender",es:"Género"}, options:[
        {value:"female", label:{ru:"Женщина",en:"Woman",es:"Mujer"}, grammar:{noun:"женщина",adjE:"ая"}},
        {value:"male", label:{ru:"Мужчина",en:"Man",es:"Hombre"}, grammar:{noun:"мужчина",adjE:"ый"}} ]});
    else if (kind === "age") _tfParams.push({ id:"age", control:"age", "default":27, min:1, max:99, label:{ru:"Возраст",en:"Age",es:"Edad"} });
    else if (kind === "sheet") _tfParams.push({ id:"", control:"sheet", label:{ru:"",en:"",es:""}, options:[] });
    else if (kind === "sheetg") _tfParams.push({ id:"", control:"sheet", dependsOn:"gender", label:{ru:"",en:"",es:""}, options:{female:[],male:[]} });
    renderParams();
}
function tfDelParam(pi) { _tfParams.splice(pi, 1); renderParams(); }
function tfAddOpt(pi, g) {
    var p = _tfParams[pi];
    var opt = p.control === "pills"
        ? {value:"", label:{ru:"",en:"",es:""}, grammar:{noun:"",adjE:""}}
        : {value:"", label:{ru:"",en:"",es:""}, frag:""};
    if (g) p.options[g].push(opt); else p.options.push(opt);
    renderParams();
}
function tfDelOpt(pi, g, oi) {
    var p = _tfParams[pi];
    if (g) p.options[g].splice(oi, 1); else p.options.splice(oi, 1);
    renderParams();
}
function tfPEdit(el) {
    var pi = +el.dataset.pi, p = _tfParams[pi];
    var num = el.dataset.num === "1";
    var val = num ? (parseInt(el.value, 10) || 0) : el.value;
    if (el.dataset.oi !== undefined) {
        var oi = +el.dataset.oi, g = el.dataset.gender || "";
        var arr = g ? p.options[g] : p.options;
        _setPath(arr[oi], el.dataset.ofld, val);
    } else {
        _setPath(p, el.dataset.fld, val);
    }
}

function _optHtml(p, pi, opt, g, oi) {
    var gAttr = g ? ' data-gender="' + g + '"' : "";
    var base = ' data-pi="' + pi + '" data-oi="' + oi + '"' + gAttr;
    var labels =
        '<input class="tf-mini" placeholder="RU" value="' + escA(opt.label && opt.label.ru) + '"' + base + ' data-ofld="label.ru" oninput="tfPEdit(this)">' +
        '<input class="tf-mini" placeholder="EN" value="' + escA(opt.label && opt.label.en) + '"' + base + ' data-ofld="label.en" oninput="tfPEdit(this)">' +
        '<input class="tf-mini" placeholder="ES" value="' + escA(opt.label && opt.label.es) + '"' + base + ' data-ofld="label.es" oninput="tfPEdit(this)">';
    var extra;
    if (p.control === "pills") {
        extra = '<div class="tf-opt-grid" style="grid-template-columns:1fr 1fr">' +
            '<input class="tf-mini" placeholder="сущ. (женщина)" value="' + escA(opt.grammar && opt.grammar.noun) + '"' + base + ' data-ofld="grammar.noun" oninput="tfPEdit(this)">' +
            '<input class="tf-mini" placeholder="оконч. (ая)" value="' + escA(opt.grammar && opt.grammar.adjE) + '"' + base + ' data-ofld="grammar.adjE" oninput="tfPEdit(this)"></div>';
    } else {
        extra = '<div style="margin-top:6px"><input class="tf-mini" placeholder="фрагмент промта (RU)" value="' + escA(opt.frag) + '"' + base + ' data-ofld="frag" oninput="tfPEdit(this)"></div>';
    }
    return '<div class="tf-opt">' +
        '<div class="tf-optrow"><input class="tf-mini" placeholder="значение (value)" value="' + escA(opt.value) + '"' + base + ' data-ofld="value" oninput="tfPEdit(this)">' +
        '<button class="tf-x" type="button" onclick="tfDelOpt(' + pi + ',\'' + g + '\',' + oi + ')">×</button></div>' +
        '<div class="tf-opt-grid">' + labels + '</div>' + extra + '</div>';
}

function _optsBlock(p, pi, arr, g) {
    return arr.map(function(opt, oi){ return _optHtml(p, pi, opt, g, oi); }).join("") +
        '<div class="tf-addrow"><button class="tf-add" type="button" onclick="tfAddOpt(' + pi + ',\'' + g + '\')">+ вариант</button></div>';
}

function _paramCard(p, pi) {
    var tag = p.control === "pills" ? "Пилюли" : (p.control === "age" ? "Возраст" : (p.dependsOn ? "Список · по полу" : "Список"));
    var head = '<div class="tf-param-head">' +
        '<input class="tf-mini" style="max-width:150px" placeholder="id" value="' + escA(p.id) + '" data-pi="' + pi + '" data-fld="id" oninput="tfPEdit(this)">' +
        '<span class="tf-ptag">' + tag + '</span>' +
        '<button class="tf-x" type="button" onclick="tfDelParam(' + pi + ')">×</button></div>';
    var lbl = (p.label || {});
    var labelRow = '<div class="tf-opt-grid">' +
        '<input class="tf-mini" placeholder="Назв. RU" value="' + escA(lbl.ru) + '" data-pi="' + pi + '" data-fld="label.ru" oninput="tfPEdit(this)">' +
        '<input class="tf-mini" placeholder="Назв. EN" value="' + escA(lbl.en) + '" data-pi="' + pi + '" data-fld="label.en" oninput="tfPEdit(this)">' +
        '<input class="tf-mini" placeholder="Назв. ES" value="' + escA(lbl.es) + '" data-pi="' + pi + '" data-fld="label.es" oninput="tfPEdit(this)"></div>';
    var body;
    if (p.control === "age") {
        body = '<div class="tf-opt-grid" style="margin-top:8px">' +
            '<input class="tf-mini" placeholder="мин" type="number" value="' + (p.min == null ? "" : p.min) + '" data-pi="' + pi + '" data-fld="min" data-num="1" oninput="tfPEdit(this)">' +
            '<input class="tf-mini" placeholder="по умолч." type="number" value="' + (p["default"] == null ? "" : p["default"]) + '" data-pi="' + pi + '" data-fld="default" data-num="1" oninput="tfPEdit(this)">' +
            '<input class="tf-mini" placeholder="макс" type="number" value="' + (p.max == null ? "" : p.max) + '" data-pi="' + pi + '" data-fld="max" data-num="1" oninput="tfPEdit(this)"></div>';
    } else if (p.dependsOn) {
        var o = p.options || {female:[],male:[]};
        body = '<div class="tf-sub">Женщины</div>' + _optsBlock(p, pi, o.female || [], "female") +
               '<div class="tf-sub">Мужчины</div>' + _optsBlock(p, pi, o.male || [], "male");
    } else {
        body = '<div style="margin-top:8px"></div>' + _optsBlock(p, pi, p.options || [], "");
    }
    return '<div class="tf-param">' + head + labelRow + body + '</div>';
}

function renderParams() {
    var host = document.getElementById("tf-params-ed");
    if (host) host.innerHTML = _tfParams.map(_paramCard).join("");
}

function _dl(id, opts) { return '<datalist id="' + id + '">' + opts.map(function(o){ return '<option value="' + esc(o) + '">'; }).join("") + '</datalist>'; }

function tfTextField(label, id, val, ph) {
    return tfField(label, '<input class="tf-in" id="' + id + '" value="' + esc(val == null ? "" : String(val)) + '"' + (ph ? ' placeholder="' + ph + '"' : "") + '>');
}
function tfNumField(label, id, val) {
    return tfField(label, '<input class="tf-in" id="' + id + '" type="number" value="' + (val == null || val === "" ? "" : val) + '">');
}

function _tplForm(t, isNew) {
    var ttl = t.title || {}, pv = t.preview || {};
    var d = t.definition || {};
    _tfOrigDef = d;
    _tfParams = d.params ? JSON.parse(JSON.stringify(d.params)) : [];   // editable clone
    var dsc = d.desc || {};
    var typeOpts = ['photo','video','audio'].map(function(x){ return '<option' + (t.type===x?' selected':'') + '>' + x + '</option>'; }).join("");
    return '<h3>' + (isNew ? "Новый шаблон" : "Шаблон: " + esc(t.id)) + '</h3>' +
        '<div class="tpl-form">' +
            '<div class="tf-grid">' +
                tfField("ID (слаг)", '<input class="tf-in" id="tf-id" value="' + esc(t.id||"") + '"' + (isNew?' placeholder="my-new-template"':' disabled') + '>') +
                tfField("Тип", '<select class="tf-in" id="tf-type">' + typeOpts + '</select>') +
                tfField("Цена (W)", '<input class="tf-in" id="tf-cost" type="number" min="0" value="' + (t.cost||0) + '">') +
                tfField("Порядок", '<input class="tf-in" id="tf-sort_order" type="number" value="' + (t.sort_order||0) + '">') +
                tfField("Категория", '<input class="tf-in" id="tf-category" value="' + esc(t.category||"") + '" placeholder="girls / men / …">') +
                tfField("&nbsp;", '<div class="tf-toggles" style="margin-top:0">' +
                    '<label class="tf-check"><input id="tf-enabled" type="checkbox"' + (t.enabled!==false?' checked':'') + '> Включён</label>' +
                    '<label class="tf-check"><input id="tf-featured" type="checkbox"' + (t.featured?' checked':'') + '> В «Тренды»</label>' +
                '</div>') +
            '</div>' +

            '<div class="tf-sec"><div class="tf-sec-h">Название</div><div class="tf-grid3">' +
                tfField("RU", '<input class="tf-in" id="tf-title-ru" value="' + esc(ttl.ru||"") + '">') +
                tfField("EN", '<input class="tf-in" id="tf-title-en" value="' + esc(ttl.en||"") + '">') +
                tfField("ES", '<input class="tf-in" id="tf-title-es" value="' + esc(ttl.es||"") + '">') +
            '</div></div>' +

            '<div class="tf-sec"><div class="tf-sec-h">Превью</div>' +
                '<div class="tf-preview-box">' +
                    '<div class="tf-thumb" id="tf-thumb">' + tfThumbInner(pv.img) + '</div>' +
                    '<div class="tf-preview-side">' +
                        '<div class="tf-upload-row">' +
                            '<input class="tf-file" type="file" id="tf-preview-file" accept="image/*,video/*">' +
                            '<button class="btn btn-outline btn-sm" type="button" onclick="uploadPreview()">Загрузить</button>' +
                            '<span class="tf-status" id="tf-preview-status"></span>' +
                        '</div>' +
                        '<input class="tf-in" id="tf-preview-img" value="' + esc(pv.img||"") + '" placeholder="URL превью (карточка)" oninput="refreshTfThumb()">' +
                        '<input class="tf-in" id="tf-preview-full" value="' + esc(pv.full||"") + '" placeholder="URL полной версии (опц.)">' +
                    '</div>' +
                '</div>' +
                '<p class="tf-hint">Картинка или видео карточки. Загрузите файл — URL подставится сам, либо вставьте вручную.</p>' +
            '</div>' +

            '<div class="tf-sec"><div class="tf-sec-h">Настройки модели</div>' +
                '<div class="tf-grid">' +
                    tfField("Модель", '<input class="tf-in" id="tf-model" list="tf-models" value="' + esc(d.model||"") + '" placeholder="NanoBanana PRO">') +
                    tfTextField("Соотношение (ratio)", "tf-ratio", d.ratio, "9:16") +
                    tfTextField("Доп. соотношения", "tf-ratios", (d.ratios||[]).join(", "), "9:16, 3:4") +
                    tfField("Качество", '<input class="tf-in" id="tf-quality" list="tf-quals" value="' + esc(d.quality||"") + '" placeholder="480p / 720p">') +
                    tfNumField("Длительность (сек)", "tf-duration", d.duration) +
                    tfTextField("Режим (mode)", "tf-mode", d.mode, "fast") +
                    tfNumField("Мин. фото", "tf-minPhotos", d.minPhotos) +
                    tfNumField("Макс. фото", "tf-maxPhotos", d.maxPhotos) +
                    tfTextField("Поле референса (refField)", "tf-refField", d.refField, "ref-images") +
                '</div>' +
                '<div class="tf-toggles">' +
                    '<label class="tf-check"><input id="tf-sound" type="checkbox"' + (d.sound?' checked':'') + '> Звук</label>' +
                    '<label class="tf-check"><input id="tf-needPhoto" type="checkbox"' + (d.needPhoto?' checked':'') + '> Нужно фото</label>' +
                    '<label class="tf-check"><input id="tf-hidePrompt" type="checkbox"' + (d.hidePrompt?' checked':'') + '> Скрыть промпт</label>' +
                '</div>' +
                _dl("tf-models", TF_MODELS) + _dl("tf-quals", TF_QUALS) +
            '</div>' +

            '<div class="tf-sec"><div class="tf-sec-h">Промпт</div>' +
                '<label class="tf-label">Скелет (со слотами {subject}/{outfit}/…)</label>' +
                '<textarea class="tf-in tf-area" id="tf-skeleton" rows="4">' + esc(d.skeleton||"") + '</textarea>' +
                '<label class="tf-label" style="margin-top:12px">Готовый промпт (для «скрыть промпт»)</label>' +
                '<textarea class="tf-in tf-area" id="tf-prompt" rows="4">' + esc(d.prompt||"") + '</textarea>' +
            '</div>' +

            '<div class="tf-sec"><div class="tf-sec-h">Описание (под превью)</div><div class="tf-grid3">' +
                tfField("RU", '<textarea class="tf-in tf-area" id="tf-desc-ru" rows="3" style="min-height:64px">' + esc(dsc.ru||"") + '</textarea>') +
                tfField("EN", '<textarea class="tf-in tf-area" id="tf-desc-en" rows="3" style="min-height:64px">' + esc(dsc.en||"") + '</textarea>') +
                tfField("ES", '<textarea class="tf-in tf-area" id="tf-desc-es" rows="3" style="min-height:64px">' + esc(dsc.es||"") + '</textarea>') +
            '</div></div>' +

            '<div class="tf-sec"><div class="tf-sec-h">Параметры выбора</div>' +
                '<div id="tf-params-ed" class="tf-params"></div>' +
                '<div class="tf-addrow">' +
                    '<button class="tf-add" type="button" onclick="tfAddParam(\'gender\')">+ Пол</button>' +
                    '<button class="tf-add" type="button" onclick="tfAddParam(\'age\')">+ Возраст</button>' +
                    '<button class="tf-add" type="button" onclick="tfAddParam(\'sheet\')">+ Список</button>' +
                    '<button class="tf-add" type="button" onclick="tfAddParam(\'sheetg\')">+ Список по полу</button>' +
                '</div>' +
                '<p class="tf-hint">Поля, которые пользователь выбирает (пол, возраст, одежда, причёска…). Пусто — без параметров, показывается готовый промпт.</p>' +
            '</div>' +

            '<div class="tf-actions">' +
                '<button class="btn btn-primary" onclick="saveTemplate(' + (isNew?'true':'false') + ')">Сохранить</button>' +
                '<button class="btn btn-outline" onclick="closeModal()">Отмена</button>' +
            '</div>' +
        '</div>';
}

function newTemplate() {
    openModal(_tplForm({enabled:true, type:"photo"}, true));
    renderParams();
}

function editTemplate(id) {
    api("/api/admin/templates/" + encodeURIComponent(id)).then(function(t) {
        if (t.error) { alert("Ошибка: " + t.error); return; }
        _tplCache[id] = t;
        openModal(_tplForm(t, false));
        renderParams();
    });
}

function _tfVal(id) { var el = document.getElementById(id); return el ? el.value.trim() : ""; }

function saveTemplate(isNew) {
    var id = _tfVal("tf-id");
    if (!id) { alert("ID обязателен"); return; }
    var title = {}, ru = _tfVal("tf-title-ru"), en = _tfVal("tf-title-en"), es = _tfVal("tf-title-es");
    if (ru) title.ru = ru; if (en) title.en = en; if (es) title.es = es;
    var img = _tfVal("tf-preview-img"), full = _tfVal("tf-preview-full");
    var preview = {};
    if (img) preview.img = img;
    if (full || img) preview.full = full || img;

    // Rebuild definition from the structured fields, starting from the original so
    // any keys we don't surface as inputs survive the edit.
    var def = Object.assign({}, _tfOrigDef || {});
    function setStr(k, id) { var v = _tfVal(id); if (v) def[k] = v; else delete def[k]; }
    function setNum(k, id) { var v = _tfVal(id); if (v !== "" && !isNaN(+v)) def[k] = parseInt(v, 10); else delete def[k]; }
    function setBool(k, id) { if (document.getElementById(id).checked) def[k] = true; else delete def[k]; }
    setStr("model", "tf-model");
    setStr("ratio", "tf-ratio");
    var rs = _tfVal("tf-ratios").split(",").map(function(s){ return s.trim(); }).filter(Boolean);
    if (rs.length) def.ratios = rs; else delete def.ratios;
    setStr("quality", "tf-quality");
    setNum("duration", "tf-duration");
    setStr("mode", "tf-mode");
    setNum("minPhotos", "tf-minPhotos");
    setNum("maxPhotos", "tf-maxPhotos");
    setStr("refField", "tf-refField");
    setBool("sound", "tf-sound");
    setBool("needPhoto", "tf-needPhoto");
    setBool("hidePrompt", "tf-hidePrompt");
    setStr("skeleton", "tf-skeleton");
    setStr("prompt", "tf-prompt");
    var desc = {}, dru = _tfVal("tf-desc-ru"), den = _tfVal("tf-desc-en"), des = _tfVal("tf-desc-es");
    if (dru) desc.ru = dru; if (den) desc.en = den; if (des) desc.es = des;
    if (Object.keys(desc).length) def.desc = desc; else delete def.desc;
    if (_tfParams && _tfParams.length) def.params = _tfParams; else delete def.params;
    var definition = def;

    var payload = {
        id: id,
        type: document.getElementById("tf-type").value,
        cost: parseInt(_tfVal("tf-cost"), 10) || 0,
        sort_order: parseInt(_tfVal("tf-sort_order"), 10) || 0,
        category: _tfVal("tf-category") || null,
        enabled: document.getElementById("tf-enabled").checked,
        featured: document.getElementById("tf-featured").checked,
        title: title,
        preview: preview,
        definition: definition
    };
    var path = isNew ? "/api/admin/templates" : "/api/admin/templates/" + encodeURIComponent(id);
    api(path, { method: isNew ? "POST" : "PUT", body: JSON.stringify(payload) }).then(function(d) {
        if (d.ok) { closeModal(); loadTemplates(0); }
        else alert("Ошибка: " + (d.error || "unknown"));
    });
}

function uploadPreview() {
    var inp = document.getElementById("tf-preview-file");
    var st = document.getElementById("tf-preview-status");
    if (!inp || !inp.files || !inp.files[0]) { st.className = "tf-status err"; st.textContent = "Выберите файл"; return; }
    var fd = new FormData();
    fd.append("file", inp.files[0]);
    st.className = "tf-status"; st.textContent = "Загрузка…";
    // Auth headers only — NOT Content-Type, so the browser sets the multipart boundary.
    var h = {};
    var tg = window.Telegram && Telegram.WebApp;
    if (tg && tg.initData) h["X-Init-Data"] = tg.initData;
    if (authToken) h["X-Auth-Token"] = authToken;
    fetch("/api/admin/templates/upload", { method: "POST", headers: h, body: fd })
        .then(function(r){ return r.json(); })
        .then(function(d){
            if (!d.url) { st.className = "tf-status err"; st.textContent = "Ошибка: " + (d.error || "unknown"); return; }
            document.getElementById("tf-preview-img").value = d.url;
            if (!document.getElementById("tf-preview-full").value) document.getElementById("tf-preview-full").value = d.url;
            refreshTfThumb();
            st.className = "tf-status ok"; st.textContent = "Готово ✓ — не забудьте Сохранить";
        })
        .catch(function(){ st.className = "tf-status err"; st.textContent = "Ошибка загрузки"; });
}

function deleteTemplate(id) {
    if (!confirm("Удалить шаблон «" + id + "»? Это действие необратимо.")) return;
    api("/api/admin/templates/" + encodeURIComponent(id), { method: "DELETE" }).then(function(d) {
        if (d.ok) loadTemplates(0);
        else alert("Ошибка: " + (d.error || "unknown"));
    });
}

// ── Promos ──
var PROMO_TYPES = {topup:"Пополнение на сумму",bonus_pct:"% бонус при пополнении"};
function promoTypeBadge(t){ return '<span class="badge badge-'+(t==="topup"?"done":"pending")+'">'+(PROMO_TYPES[t]||t)+'</span>'; }

function loadPromos(offset){
    var mc=document.getElementById("main-content");
    mc.innerHTML='<p style="color:var(--tx2)">Загрузка...</p>';
    api("/api/admin/promos?limit="+PAGE_SIZE+"&offset="+offset).then(function(d){
        var rows=d.items.map(function(p){
            var en=p.enabled?'<span class="badge badge-done">on</span>':'<span class="badge badge-error">off</span>';
            var uses=p.used_count+"/"+(p.max_uses||"∞");
            var exp=p.expires_at?fmtDate(p.expires_at):"—";
            var valStr=p.type==="topup"?p.value+" W":p.value+"%";
            return '<tr><td>'+esc(p.code)+'</td><td>'+promoTypeBadge(p.type)+'</td><td>'+valStr+'</td><td>'+uses+'</td><td>'+en+'</td><td>'+exp+'</td><td>'+fmtDate(p.created_at)+'</td>'+
                '<td><button class="btn btn-outline btn-sm" onclick="editPromo('+p.id+')">Изм.</button> '+
                '<button class="btn btn-danger btn-sm" onclick="deletePromo('+p.id+',\''+esc(p.code)+'\')">Удал.</button></td></tr>';
        }).join("");
        mc.innerHTML='<div style="margin-bottom:12px"><button class="btn btn-primary btn-sm" onclick="newPromo()">+ Новый промокод</button></div>'+
            '<div class="tbl-wrap"><table class="tbl"><thead><tr><th>Код</th><th>Тип</th><th>Значение</th><th>Использований</th><th>Вкл</th><th>Истекает</th><th>Создан</th><th>Действия</th></tr></thead><tbody>'+
            (rows||'<tr><td colspan="8" style="text-align:center;color:var(--tx3)">Нет промокодов</td></tr>')+'</tbody></table></div>'+
            pagination(d.total,offset,"loadPromos");
    });
}

function _promoForm(p,isNew){
    var typeOpts=['topup','bonus_pct'].map(function(t){return '<option value="'+t+'"'+(p.type===t?' selected':'')+'>'+PROMO_TYPES[t]+'</option>';}).join("");
    return '<h3>'+(isNew?"Новый промокод":"Редактировать: "+esc(p.code))+'</h3>'+
        '<div class="tpl-form">'+
        tfField("Код",  '<input class="tf-in" id="pf-code" value="'+esc(p.code||"")+'" placeholder="SUMMER2026" style="text-transform:uppercase"'+(isNew?'':' disabled')+'>') +
        tfField("Тип",  '<select class="tf-in" id="pf-type">'+typeOpts+'</select>') +
        tfField("Значение", '<input class="tf-in" id="pf-value" type="number" min="1" value="'+(p.value||"")+'" placeholder="'+(_pTypeHint(p.type))+'">') +
        '<p class="tf-hint" id="pf-hint">'+_pTypeHint(p.type)+'</p>'+
        tfField("Макс. использований", '<input class="tf-in" id="pf-max" type="number" min="0" value="'+(p.max_uses||0)+'" placeholder="0 = безлимит">') +
        tfField("Истекает", '<input class="tf-in" id="pf-expires" type="datetime-local" value="'+(p.expires_at?p.expires_at.slice(0,16):"")+'">') +
        '<label class="tf-check" style="margin:12px 0"><input id="pf-enabled" type="checkbox"'+(p.enabled!==false?' checked':'')+'> Включён</label>'+
        '<div class="tf-actions">'+
            '<button class="btn btn-primary" onclick="savePromo('+(isNew?'0':p.id)+','+isNew+')">Сохранить</button>'+
            '<button class="btn btn-outline" onclick="closeModal()">Отмена</button>'+
        '</div></div>';
}
function _pTypeHint(t){return t==="bonus_pct"?"Процент бонуса при пополнении (напр. 30 = +30%)":"Сумма токенов для начисления (напр. 500)";}

function newPromo(){
    openModal(_promoForm({enabled:true,type:"topup"},true));
    document.getElementById("pf-type").addEventListener("change",function(){
        document.getElementById("pf-hint").textContent=_pTypeHint(this.value);
    });
}

function editPromo(id){
    api("/api/admin/promos?limit=200").then(function(d){
        var p=null;
        d.items.forEach(function(it){if(it.id===id)p=it;});
        if(!p){alert("Не найден");return;}
        openModal(_promoForm(p,false));
        document.getElementById("pf-type").addEventListener("change",function(){
            document.getElementById("pf-hint").textContent=_pTypeHint(this.value);
        });
    });
}

function savePromo(id,isNew){
    var code=document.getElementById("pf-code").value.trim().toUpperCase();
    var type=document.getElementById("pf-type").value;
    var value=parseInt(document.getElementById("pf-value").value,10)||0;
    var max_uses=parseInt(document.getElementById("pf-max").value,10)||0;
    var enabled=document.getElementById("pf-enabled").checked;
    var expires=document.getElementById("pf-expires").value;
    if(!code||value<=0){alert("Код и значение > 0 обязательны");return;}
    var body={code:code,type:type,value:value,max_uses:max_uses,enabled:enabled,expires_at:expires||null};
    var path=isNew?"/api/admin/promos":"/api/admin/promos/"+id;
    api(path,{method:isNew?"POST":"PUT",body:JSON.stringify(body)}).then(function(d){
        if(d.ok){closeModal();loadPromos(0);}
        else alert("Ошибка: "+(d.error||"unknown"));
    });
}

function deletePromo(id,code){
    if(!confirm("Удалить промокод «"+code+"»?"))return;
    api("/api/admin/promos/"+id,{method:"DELETE"}).then(function(d){
        if(d.ok)loadPromos(0);
        else alert("Ошибка: "+(d.error||"unknown"));
    });
}

// ── Support ──
var supStatusFilter = "open";
var supShowAgents = false;

function loadSupportAgents() {
    var box = document.getElementById("sup-agents-box");
    if (!box) return;
    box.innerHTML = '<p style="color:var(--tx2)">Загрузка агентов...</p>';
    api("/api/admin/support/agents").then(function(d) {
        var rows = (d.items || []).map(function(a) {
            return '<tr><td>' + a.tg_id + '</td><td>' + esc(a.name || "—") + '</td><td>' + fmtDate(a.added_at) + '</td>' +
                '<td><button class="btn btn-outline btn-sm" onclick="editAgent(' + a.tg_id + ',\'' + escA(a.name || "") + '\')">Изм.</button> ' +
                '<button class="btn btn-danger btn-sm" onclick="deleteAgent(' + a.tg_id + ')">Удал.</button></td></tr>';
        }).join("");
        box.innerHTML = '<div style="margin-bottom:12px"><button class="btn btn-primary btn-sm" onclick="addAgent()">+ Добавить агента</button></div>' +
            '<div class="tbl-wrap"><table class="tbl"><thead><tr><th>TG ID</th><th>Имя</th><th>Добавлен</th><th>Действия</th></tr></thead><tbody>' +
            (rows || '<tr><td colspan="4" style="text-align:center;color:var(--tx3)">Нет агентов</td></tr>') +
            '</tbody></table></div>';
    });
}

function addAgent() {
    var tgId = prompt("Telegram ID агента:");
    if (!tgId) return;
    var name = prompt("Имя (необязательно):");
    api("/api/admin/support/agents", {
        method: "POST",
        body: JSON.stringify({tg_id: parseInt(tgId, 10), name: name || null})
    }).then(function(d) {
        if (d.ok) loadSupportAgents();
        else alert("Ошибка: " + (d.error || "unknown"));
    });
}

function editAgent(tgId, currentName) {
    var name = prompt("Новое имя:", currentName);
    if (name === null) return;
    api("/api/admin/support/agents/" + tgId, {
        method: "PUT",
        body: JSON.stringify({name: name})
    }).then(function(d) {
        if (d.ok) loadSupportAgents();
        else alert("Ошибка: " + (d.error || "unknown"));
    });
}

function deleteAgent(tgId) {
    if (!confirm("Удалить агента " + tgId + "?")) return;
    api("/api/admin/support/agents/" + tgId, {method: "DELETE"}).then(function(d) {
        if (d.ok) loadSupportAgents();
        else alert("Ошибка: " + (d.error || "unknown"));
    });
}

function toggleAgentsPanel() {
    supShowAgents = !supShowAgents;
    var box = document.getElementById("sup-agents-box");
    if (!box) return;
    box.style.display = supShowAgents ? "block" : "none";
    if (supShowAgents) loadSupportAgents();
}

function loadSupport(status, offset) {
    supStatusFilter = status || "open";
    var mc = document.getElementById("main-content");
    mc.innerHTML = '<p style="color:var(--tx2)">Загрузка...</p>';
    api("/api/admin/support?status=" + supStatusFilter + "&limit=" + PAGE_SIZE + "&offset=" + offset).then(function(d) {
        var agentsBtn = '<button class="btn btn-outline btn-sm" onclick="toggleAgentsPanel()" style="margin-left:auto">' + (supShowAgents ? "Скрыть агентов" : "Агенты поддержки") + '</button>';
        var tabs = '<div style="display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap;align-items:center">' +
            ["open","assigned","closed","all"].map(function(s) {
                var label = {open:"Новые",assigned:"В работе",closed:"Закрытые",all:"Все"}[s];
                var cls = s === supStatusFilter ? "btn btn-primary btn-sm" : "btn btn-outline btn-sm";
                return '<button class="' + cls + '" onclick="loadSupport(\'' + s + '\',0)">' + label + '</button>';
            }).join("") + agentsBtn + '</div>' +
            '<div id="sup-agents-box" style="display:' + (supShowAgents ? "block" : "none") + ';margin-bottom:20px;padding:16px;background:var(--card);border-radius:12px;border:1px solid var(--brd)"></div>';
        if (!d.items.length) {
            mc.innerHTML = tabs + '<p style="color:var(--tx3);text-align:center;padding:40px 0">Нет тикетов</p>';
            if (supShowAgents) loadSupportAgents();
            return;
        }
        var rows = d.items.map(function(t) {
            var user = esc(t.first_name || "") + (t.username ? " @" + esc(t.username) : "") + " (" + t.user_tg_id + ")";
            var agent = t.agent_name ? esc(t.agent_name) : '<span style="color:var(--tx3)">—</span>';
            return '<tr style="cursor:pointer" onclick="openTicket(' + t.id + ')">' +
                '<td>#' + t.id + '</td>' +
                '<td>' + user + '</td>' +
                '<td>' + badge(t.status) + '</td>' +
                '<td>' + agent + '</td>' +
                '<td>' + (t.msg_count || 0) + '</td>' +
                '<td>' + fmtDate(t.updated_at) + '</td></tr>';
        }).join("");
        mc.innerHTML = tabs +
            '<div class="tbl-wrap"><table class="tbl"><thead><tr><th>#</th><th>Пользователь</th><th>Статус</th><th>Агент</th><th>Сообщ.</th><th>Обновлён</th></tr></thead><tbody>' +
            rows + '</tbody></table></div>' +
            pagination(d.total, offset, "_supPage");
        if (supShowAgents) loadSupportAgents();
    });
}

function _supPage(off) { loadSupport(supStatusFilter, off); }

function openTicket(id) {
    api("/api/admin/support/" + id).then(function(t) {
        var user = esc(t.first_name || "") + (t.username ? " @" + esc(t.username) : "") + " (ID " + t.user_tg_id + ")";
        var msgs = (t.messages || []).map(function(m) {
            var cls = m.sender === "user" ? "color:var(--gold)" : "color:var(--success)";
            var img = m.image_url ? '<br><img src="' + esc(m.image_url) + '" style="max-width:300px;border-radius:8px;margin-top:4px">' : "";
            return '<div style="margin-bottom:12px"><span style="font-weight:600;' + cls + '">' + esc(m.sender) + '</span> <span style="color:var(--tx3);font-size:12px">' + fmtDate(m.created_at) + '</span><div style="margin-top:4px;white-space:pre-wrap">' + esc(m.content) + img + '</div></div>';
        }).join("");
        var actions = '';
        if (t.status === "open") {
            actions += '<button class="btn btn-primary btn-sm" onclick="assignTicket(' + t.id + ')">Взять себе</button> ';
        }
        if (t.status !== "closed") {
            actions += '<button class="btn btn-outline btn-sm" onclick="closeTicket(' + t.id + ')">Закрыть</button> ';
        }
        if (t.status !== "closed") {
            actions += '<div style="display:flex;gap:8px;margin-top:12px"><input class="form-input" id="sup-reply-text" placeholder="Ответ..." style="flex:1"><button class="btn btn-primary btn-sm" onclick="replyTicket(' + t.id + ')">Отправить</button></div>';
        }
        openModal(
            '<h3 style="margin-bottom:4px">Тикет #' + t.id + ' ' + badge(t.status) + '</h3>' +
            '<p style="color:var(--tx2);margin-bottom:16px">' + user + '</p>' +
            '<div style="max-height:400px;overflow-y:auto;margin-bottom:16px;padding-right:8px">' + (msgs || '<p style="color:var(--tx3)">Нет сообщений</p>') + '</div>' +
            actions
        );
    });
}

function assignTicket(id) {
    api("/api/admin/support/" + id + "/assign", {method:"POST"}).then(function(d) {
        if (d.ok) { closeModal(); loadSupport(supStatusFilter, 0); }
        else { alert(d.error || "Ошибка"); }
    });
}

function closeTicket(id) {
    api("/api/admin/support/" + id + "/close", {method:"POST"}).then(function(d) {
        if (d.ok) { closeModal(); loadSupport(supStatusFilter, 0); }
        else { alert(d.error || "Ошибка"); }
    });
}

function replyTicket(id) {
    var input = document.getElementById("sup-reply-text");
    if (!input) return;
    var text = input.value.trim();
    if (!text) return;
    input.disabled = true;
    api("/api/admin/support/" + id + "/reply", {
        method: "POST",
        body: JSON.stringify({text: text})
    }).then(function(d) {
        input.disabled = false;
        if (d.id) { openTicket(id); }
        else { alert(d.error || "Ошибка"); }
    }).catch(function() { input.disabled = false; });
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
