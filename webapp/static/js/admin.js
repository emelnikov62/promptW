var currentSection = "dashboard";
var PAGE_SIZE = 50;
window.authToken = "";

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
    return !!window.authToken;
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
            window.authToken = d.token;
            localStorage.setItem("pw_admin_token", d.token);
            document.getElementById("login-screen").style.display = "none";
            document.getElementById("admin-app").style.display = "flex";
            applyRole().then(function(){ showSection("dashboard"); });
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
    window.authToken = "";
    localStorage.removeItem("pw_admin_token");
    showLogin();
}

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

// ── Navigation ──
var navItems = document.querySelectorAll(".nav-item");
var sectionTitles = {dashboard:"Dashboard",users:"Пользователи",generations:"Генерации",payments:"Платежи",withdrawals:"Выводы",templates:"Шаблоны",promos:"Промокоды",referrals:"Рефералы",support:"Поддержка",face:"Сходство лиц",notif:"Уведомления",audit:"Аудит-лог",accounts:"Доступ"};

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
        payments: function() { loadPayments(); },
        withdrawals: function() { loadWithdrawals(0); },
        templates: function() { loadTemplates(0); },
        promos: function() { loadPromos(0); },
        referrals: function() { loadReferrals(0); },
        support: function() { loadSupport("open", 0); },
        face: function() { loadFace(facePeriod); },
        notif: function() { loadNotif(0); },
        audit: function() { loadAudit(0); },
        accounts: loadAccounts
    };
    if (loaders[name]) loaders[name]();
}

// ── Notifications section ──
function statCard(label, val) {
    return '<div class="card" style="padding:14px;min-width:130px;flex:1"><div style="font-size:24px;font-weight:700">' + val + '</div><div style="color:var(--tx3);font-size:12px">' + label + '</div></div>';
}

function loadNotif(offset) {
    var mc = document.getElementById("main-content");
    mc.innerHTML = '<p style="color:var(--tx2)">Загрузка...</p>';
    api("/api/admin/notif/overview").then(function(d) {
        var kinds = (d.sends || []).map(function(s) {
            return '<tr><td>' + esc(s.kind) + '</td><td>' + s.d7 + '</td><td>' + s.d30 + '</td></tr>';
        }).join("") || '<tr><td colspan="3" style="text-align:center;color:var(--tx3)">Пока ничего не отправлено</td></tr>';
        var toggleBtn = d.enabled
            ? '<button class="btn btn-danger btn-sm" onclick="notifToggle(false)">Выключить</button>'
            : '<button class="btn btn-primary btn-sm" onclick="notifToggle(true)">Включить</button>';
        var statusTxt = d.enabled
            ? '<span style="color:var(--success,#3DD68C)">включена</span>'
            : '<span style="color:var(--danger,#FF6B5C)">выключена</span>';
        mc.innerHTML =
            '<div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px">' +
                statCard("Всего юзеров", d.total_users) +
                statCard("Отписались", d.opted_out) +
                statCard("Получат «Новинки»", d.weekly_eligible) +
            '</div>' +
            '<div class="card" style="padding:14px;margin-bottom:14px">' +
                '<b>Авто-рассылка вовлекающих:</b> ' + statusTxt + ' &nbsp; ' + toggleBtn +
                '<p style="color:var(--tx3);font-size:12px;margin:8px 0 0">Стоп-кран для ежечасного sweep (bonusUnspent / reengage / rewardAvail). Транзакционные и ручная рассылка не зависят.</p>' +
            '</div>' +
            '<div class="card" style="padding:14px;margin-bottom:16px">' +
                '<b>Новинки недели</b>' +
                '<p style="color:var(--tx3);font-size:12px;margin:6px 0 10px">Разошлёт «добавили свежие шаблоны» активным подписанным юзерам (1×/7д, окно 10–22 МСК). Сейчас получателей: ' + d.weekly_eligible + '.</p>' +
                '<button class="btn btn-primary btn-sm" onclick="notifWeekly()">Разослать новинки</button>' +
            '</div>' +
            '<h3 style="margin:18px 0 8px;font-size:15px">Отправки по типам</h3>' +
            '<div class="tbl-wrap"><table class="tbl"><thead><tr><th>Тип</th><th>7 дней</th><th>30 дней</th></tr></thead><tbody>' + kinds + '</tbody></table></div>' +
            '<h3 style="margin:18px 0 8px;font-size:15px">Последние отправки</h3>' +
            '<div id="notif-log"><p style="color:var(--tx2)">Загрузка...</p></div>';
        loadNotifLog(offset || 0);
    });
}

function loadNotifLog(offset) {
    var el = document.getElementById("notif-log");
    if (!el) return;
    api("/api/admin/notif/log?limit=" + PAGE_SIZE + "&offset=" + offset).then(function(d) {
        var rows = d.items.map(function(n) {
            return '<tr><td>' + n.user_tg_id + (n.username ? " (@" + esc(n.username) + ")" : "") + '</td><td>' + esc(n.kind) + '</td><td>' + fmtDate(n.sent_at) + '</td></tr>';
        }).join("") || '<tr><td colspan="3" style="text-align:center;color:var(--tx3)">Нет данных</td></tr>';
        el.innerHTML = '<div class="tbl-wrap"><table class="tbl"><thead><tr><th>Пользователь</th><th>Тип</th><th>Когда</th></tr></thead><tbody>' + rows + '</tbody></table></div>' +
            pagination(d.total, offset, "loadNotifLog");
    });
}

function notifToggle(on) {
    if (!confirm(on ? "Включить авто-рассылку?" : "Выключить авто-рассылку вовлекающих?")) return;
    api("/api/admin/notif/toggle", { method: "POST", body: JSON.stringify({ on: on }) }).then(function(d) {
        if (d.ok) loadNotif(0); else alert("Ошибка");
    });
}

function notifWeekly() {
    if (!confirm("Разослать «Новинки недели» сейчас?")) return;
    api("/api/admin/notif/weekly", { method: "POST", body: JSON.stringify({}) }).then(function(d) {
        if (d.ok) { alert("Отправлено: " + d.sent); loadNotif(0); }
        else if (d.reason === "quiet_hours") alert("Тихие часы (22–10 МСК) — попробуй днём.");
        else alert("Ошибка: " + (d.error || "unknown"));
    });
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
    formModal({title:"Коррекция баланса", submitLabel:"Применить", fields:[
        {name:"amount", label:"Сумма (W, можно отрицательную)", type:"number", required:true, hint:"Например 100 или -50"},
        {name:"reason", label:"Причина", type:"textarea", required:true}
    ]}).then(function(v){
        if (!v) return;
        api("/api/admin/users/"+tgId+"/adjust",{method:"POST",body:JSON.stringify({amount:v.amount,reason:v.reason})})
            .then(function(d){ toast("success","Новый баланс: "+(d.balance!=null?d.balance:"обновлён")); if(window.loadUserDetail) loadUserDetail(tgId); }).catch(apiError);
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

// ── Payments ──
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
            (canRefund ? '<div style="margin-top:16px"><button class="btn btn-danger" id="pay-refund">Вернуть платёж</button>' : '<div style="margin-top:16px">') +
            (window.adminRole === "owner" ? '<button class="btn btn-outline" id="pay-adjust" style="margin-left:8px">Коррекция баланса юзера</button>' : '') +
            '</div>';
        openModal(html);
        var ab = document.getElementById("pay-adjust");
        if (ab) ab.onclick = function(){ promptAdjust(p.user_tg_id); };
        var rb = document.getElementById("pay-refund");
        if (rb) rb.onclick = function() {
            var refundBody = p.provider === 'platega'
                ? "Platega не поддерживает авто-возврат — платёж будет помечен возвращённым вручную (деньги верните в ЛК провайдера). Действие необратимо."
                : "Деньги вернутся плательщику через " + p.provider + ". Действие необратимо.";
            confirmDialog({title:"Вернуть " + p.amount_rub + " ₽?", body:refundBody, danger:true, confirmLabel:"Вернуть"}).then(function(ok){
                if (!ok) return;
                btnBusy(rb, true);
                api("/api/admin/payments/" + p.id + "/refund", {method:"POST", body: JSON.stringify({reason:"admin refund"})})
                    .then(function(res){ toast("success", res.manual ? "Помечен возвращённым (вручную)" : "Возврат отправлен"); closeModal(); window._payTable.reload(); })
                    .catch(function(e){ btnBusy(rb,false); apiError(e); });
            });
        };
    }).catch(apiError);
}

// ── Withdrawals ──
function loadWithdrawals() {
    var mc = document.getElementById("main-content");
    mc.innerHTML = '<div id="wd-table"></div>';
    window._wdTable = DataTable(document.getElementById("wd-table"), {
        endpoint: "/api/admin/withdrawals", exportCsv: true, searchable: true,
        searchPlaceholder: "@username / реквизиты", defaultSort: {key: "created_at", order: "desc"},
        filters: [
            {key: "status", label: "Статус", type: "select", options: ["pending", "approved", "paid", "rejected"]},
            {key: "method", label: "Метод", type: "select", options: ["card", "usdt"]},
            {type: "daterange"}
        ],
        bulkActions: [{label: "Одобрить выбранные", run: bulkApproveWd}],
        rowAction: openWithdrawal,
        columns: [
            {key: "user_tg_id", label: "User", render: function(r) { return esc(r.username ? ("@" + r.username) : r.user_tg_id); }},
            {key: "amount_rub", label: "₽", sortable: true, align: "right"},
            {key: "method", label: "Метод", hideOnMobile: true},
            {key: "details", label: "Реквизиты", hideOnMobile: true},
            {key: "status", label: "Статус", render: function(r) { return badge(r.status); }},
            {key: "created_at", label: "Дата", sortable: true, render: function(r) { return fmtDate(r.created_at); }}
        ]
    });
}
function bulkApproveWd(ids) {
    return confirmDialog({title: "Одобрить " + ids.length + " заявок?"}).then(function(ok) {
        if (!ok) return;
        return Promise.all(ids.map(function(id) { return api("/api/admin/withdrawals/" + id + "/action", {method: "POST", body: JSON.stringify({action: "approve", reason: "bulk"})}).catch(function() {}); }))
            .then(function() { toast("success", "Готово"); });
    });
}
function openWithdrawal(row) {
    var actions = "";
    if (row.status === "pending") actions = '<button class="btn btn-primary" onclick="wdDo(' + row.id + ",\'approve\')\">Одобрить</button> <button class=\"btn btn-danger\" onclick=\"wdDo(" + row.id + ",\'reject\')\">Отклонить</button>";
    else if (row.status === "approved") actions = '<button class="btn btn-primary" onclick="wdDo(' + row.id + ",\'paid\')\">Выплачено</button>";
    openModal('<h3 class="modal-title">Вывод #' + row.id + ' ' + badge(row.status) + '</h3>' +
        '<div class="kv"><b>Юзер:</b> ' + esc(row.username ? ("@" + row.username) : row.user_tg_id) + '</div>' +
        '<div class="kv"><b>Сумма:</b> ' + row.amount_rub + ' ₽</div>' +
        '<div class="kv"><b>Метод:</b> ' + esc(row.method) + '</div>' +
        '<div class="kv"><b>Реквизиты:</b> ' + esc(row.details) + '</div>' +
        '<div class="kv"><b>Создан:</b> ' + fmtDate(row.created_at) + '</div>' +
        '<div style="margin-top:16px">' + actions + '</div>');
}
function wdDo(id, action) {
    var ask = action === "reject"
        ? formModal({title: "Причина отклонения", fields: [{name: "reason", label: "Причина", type: "textarea", required: true}], submitLabel: "Отклонить"}).then(function(v) { return v ? v.reason : null; })
        : confirmDialog({title: "Подтвердить?"}).then(function(ok) { return ok ? "" : null; });
    ask.then(function(reason) {
        if (reason === null) return;
        api("/api/admin/withdrawals/" + id + "/action", {method: "POST", body: JSON.stringify({action: action, reason: reason})})
            .then(function(d) { toast("success", "Статус: " + d.status); closeModal(); window._wdTable.reload(); }).catch(apiError);
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
var TF_MODELS = ["NanoBanana PRO", "Seedance 2.0", "Grok Imagine 1.5", "Kling 3.0", "Kling Motion 3.0", "Suno V5.5"];
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
    if (window.authToken) h["X-Auth-Token"] = window.authToken;
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

// ── Referrals ──
var refQuery = "";
function loadReferrals(offset) {
    var mc = document.getElementById("main-content");
    var q = refQuery;
    var url = "/api/admin/referrals?limit=" + PAGE_SIZE + "&offset=" + offset + (q ? "&q=" + encodeURIComponent(q) : "");
    mc.innerHTML = '<div class="toolbar"><input class="search-input" id="ref-search" placeholder="Поиск по tg_id или username" value="' + esc(q) + '" onkeydown="if(event.key===\'Enter\'){refQuery=this.value;loadReferrals(0)}"><button class="btn btn-outline btn-sm" onclick="refQuery=document.getElementById(\'ref-search\').value;loadReferrals(0)">Найти</button></div><p style="color:var(--tx2)">Загрузка...</p>';
    api(url).then(function(d) {
        var rows = d.items.map(function(u) {
            return '<tr class="clickable" onclick="loadReferralDetail(' + u.tg_id + ')"><td>' + u.tg_id + '</td><td>' + esc(u.username) + '</td><td>' + esc(u.first_name) + '</td><td>' + u.invites + '</td><td>' + Number(u.total_earned).toFixed(2) + ' ₽</td><td>' + Number(u.ref_balance).toFixed(2) + ' ₽</td><td>' + fmtDate(u.created_at) + '</td></tr>';
        }).join("");
        mc.innerHTML = '<div class="toolbar"><input class="search-input" id="ref-search" placeholder="Поиск по tg_id или username" value="' + esc(q) + '" onkeydown="if(event.key===\'Enter\'){refQuery=this.value;loadReferrals(0)}"><button class="btn btn-outline btn-sm" onclick="refQuery=document.getElementById(\'ref-search\').value;loadReferrals(0)">Найти</button></div>' +
            '<div class="tbl-wrap"><table class="tbl"><thead><tr><th>TG ID</th><th>Username</th><th>Имя</th><th>Приглашено</th><th>Заработано</th><th>Реф. баланс</th><th>Регистрация</th></tr></thead><tbody>' + (rows || '<tr><td colspan="7" style="text-align:center;color:var(--tx3)">Нет рефералов</td></tr>') + '</tbody></table></div>' +
            pagination(d.total, offset, "loadReferrals");
    });
}

function loadReferralDetail(tgId) {
    api("/api/admin/referrals/" + tgId).then(function(d) {
        var u = d.referrer;
        var html = '<h3>Реферер ' + u.tg_id + '</h3>' +
            '<div class="modal-row"><span class="modal-label">Username</span><span>' + esc(u.username) + '</span></div>' +
            '<div class="modal-row"><span class="modal-label">Имя</span><span>' + esc(u.first_name) + '</span></div>' +
            '<div class="modal-row"><span class="modal-label">Реф. баланс</span><span>' + Number(u.ref_balance).toFixed(2) + ' ₽</span></div>' +
            '<div class="modal-row"><span class="modal-label">Всего заработано</span><span>' + d.total_earned.toFixed(2) + ' ₽</span></div>' +
            '<div class="modal-row"><span class="modal-label">Приглашённых</span><span>' + d.invitees.length + '</span></div>';

        if (d.invitees.length) {
            html += '<div class="modal-section"><h4>Приглашённые пользователи</h4><div class="tbl-wrap"><table class="tbl"><thead><tr><th>TG ID</th><th>Username</th><th>Имя</th><th>Оплатил</th><th>Регистрация</th></tr></thead><tbody>';
            d.invitees.forEach(function(inv) {
                html += '<tr><td>' + inv.tg_id + '</td><td>' + esc(inv.username) + '</td><td>' + esc(inv.first_name) + '</td><td>' + Number(inv.total_paid).toFixed(2) + ' ₽</td><td>' + fmtDate(inv.created_at) + '</td></tr>';
            });
            html += '</tbody></table></div></div>';
        }

        if (d.earnings.length) {
            html += '<div class="modal-section"><h4>Начисления (последние 50)</h4><div class="tbl-wrap"><table class="tbl"><thead><tr><th>От кого</th><th>Username</th><th>Линия</th><th>Сумма</th><th>Дата</th></tr></thead><tbody>';
            d.earnings.forEach(function(e) {
                html += '<tr><td>' + e.referred_tg_id + '</td><td>' + esc(e.username) + '</td><td>L' + e.line + '</td><td>' + Number(e.amount_rub).toFixed(2) + ' ₽</td><td>' + fmtDate(e.created_at) + '</td></tr>';
            });
            html += '</tbody></table></div></div>';
        }

        openModal(html);
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

// ── Accounts management ──
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

// ── Init ──
window.authToken = resolveToken();
if (hasAuth()) {
    applyRole().then(function(){ showSection("dashboard"); });
} else {
    showLogin();
}
