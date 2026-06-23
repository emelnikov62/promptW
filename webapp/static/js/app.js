var tg = window.Telegram.WebApp;
tg.ready(); tg.expand();
tg.setHeaderColor("#16130F"); tg.setBackgroundColor("#16130F");   // warm --bg (design system), not cold near-black

// Fullscreen mode — hides Telegram's centered "name / mini app" title, leaving just floating Close/menu.
try { if (tg.requestFullscreen) tg.requestFullscreen(); } catch (e) {}
try { if (tg.disableVerticalSwipes) tg.disableVerticalSwipes(); } catch (e) {}
function applySafeArea() {
    var top = ((tg.safeAreaInset && tg.safeAreaInset.top) || 0) +
              ((tg.contentSafeAreaInset && tg.contentSafeAreaInset.top) || 0);
    document.documentElement.style.setProperty("--tg-top", top + "px");
}
applySafeArea();
function syncHeaderH(){var h=document.getElementById("main-header");if(h)document.documentElement.style.setProperty("--header-h",h.offsetHeight+"px");}
requestAnimationFrame(syncHeaderH);
if (tg.onEvent) {
    tg.onEvent("safeAreaChanged", function(){applySafeArea();syncHeaderH();});
    tg.onEvent("contentSafeAreaChanged", function(){applySafeArea();syncHeaderH();});
    tg.onEvent("fullscreenChanged", function(){applySafeArea();syncHeaderH();});
}

// Haptic Feedback helpers (no-op if unsupported)
var haptic = {
    impact: function (style) { try { if (tg.HapticFeedback) tg.HapticFeedback.impactOccurred(style || "light"); } catch (e) {} },
    notify: function (type) { try { if (tg.HapticFeedback) tg.HapticFeedback.notificationOccurred(type); } catch (e) {} },
    select: function () { try { if (tg.HapticFeedback) tg.HapticFeedback.selectionChanged(); } catch (e) {} }
};
// Light tap feedback on any interactive element (delegated, capture phase)
document.addEventListener("click", function (e) {
    var t = e.target;
    if (!t || !t.closest) return;
    if (t.closest(".tbtn, .pkg, .rtab, .dd-item, .lang-item, .cnt, .ni")) { haptic.select(); return; }
    if (t.closest("button, .acard, .trend-t, .act-tile, .info-card, .up-btn, .tpl-up-add, [data-nav], [data-tpl]")) { haptic.impact("light"); }
}, true);

// Toasts — slide-in notice with a colored status stripe (replaces native alerts)
var TOAST_ICO = {
    success: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>',
    error: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>',
    info: '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="11"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg>'
};
function toast(msg, type) {
    type = type || "info";
    var wrap = document.getElementById("toast-wrap");
    if (!wrap) { wrap = document.createElement("div"); wrap.id = "toast-wrap"; document.body.appendChild(wrap); }
    var el = document.createElement("div");
    el.className = "toast " + type;
    el.innerHTML = '<span class="toast-ico">' + (TOAST_ICO[type] || "") + '</span><span class="toast-msg">' + escHtml(msg) + '</span>';
    wrap.appendChild(el);
    if (type === "error") haptic.notify("error");
    else if (type === "success") haptic.notify("success");
    requestAnimationFrame(function () { el.classList.add("show"); });
    var hide = function () { el.classList.remove("show"); setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 280); };
    var timer = setTimeout(hide, 2800);
    el.addEventListener("click", function () { clearTimeout(timer); hide(); });
}

var galleryItems = [];
var videoThumbs = {};           // url -> captured mid-frame dataURL (static history preview)
var videoThumbPending = {};     // url -> true while a capture is in flight
var HISTORY_PAGE = 30;          // how many more rows each "Показать ещё" reveals
var historyLimit = HISTORY_PAGE; // current window size requested from the server
var historyHasMore = false;     // server returned a full window → older rows may exist
var historyLoading = false;     // a /history fetch is in flight (guards infinite-scroll)
var ratios = ["1:1","3:4","4:3","9:16","16:9"];
var quals = ["1K","2K","4K"];
var state = { pRatio:1, pQual:1 };
var PHOTO_BASE_COST = 30;               // фото: 30 W за генерацию в любом качестве
// 1 кредит KIE = $0.005 ≈ 0.37 ₽; 1 токен ≈ 1.06 ₽. M=2.2 → маржа ~84% (list) / ~80% (макс. скидка)
var CREDIT_TO_TOKEN = 2.2;
// mascot logos shown on the "active model" tile per neural network (transparent PNGs
// extracted from the brand sheet → /static/img/models/<slug>.png). MODEL_LOGOS below
// stays as the text-monogram fallback for any model without a logo file.
var MODEL_LOGO_VER = 1; // bump when a logo PNG is re-exported (cache-bust)
var MODEL_LOGO_IMG = {
    "NanoBanana PRO":"nanobanana-pro","NanoBanana 2":"nanobanana-2","GPT Image 2":"gpt-image-2",
    "Seedream 4.5":"seedream","Kling 3.0":"kling","Kling Motion 3.0":"kling-motion",
    "Grok Imagine 1.5":"grok","Veo 3.1 Fast":"veo","Seedance 2.0":"seedance",
    "Suno V5.5":"suno"
};
// short brand monograms — fallback when no logo PNG is mapped
var MODEL_LOGOS = {
    "NanoBanana PRO":"NB","NanoBanana 2":"N2","GPT Image 2":"G2","Seedream 4.5":"SD",
    "Kling Motion 3.0":"KL","Grok Imagine 1.5":"GK","Kling 3.0":"KL","Veo 3.1 Fast":"VO","Seedance 2.0":"S2",
    "Suno V5.5":"SU"
};
function applyModelLogo(icoId, modelName){
    var el = document.getElementById(icoId); if (!el) return;
    var img = MODEL_LOGO_IMG[modelName];
    if (img){
        el.classList.add("has-logo");
        el.innerHTML = '<img class="model-logo" src="/static/img/models/' + img + '.png?v=' + MODEL_LOGO_VER + '" alt="">';
        return;
    }
    el.classList.remove("has-logo");
    var m = MODEL_LOGOS[modelName];
    if (m) el.innerHTML = '<span class="model-mono">' + m + '</span>';
}
function updatePhotoCost(){
    var c = document.querySelector(".cnt.active");
    var n = c ? parseInt(c.textContent) : 1;
    var el = document.getElementById("photo-cost");
    if (el) el.textContent = PHOTO_BASE_COST * n;
    refreshGenButtons();
}
// audio (Suno) — KIE credits × CREDIT_TO_TOKEN. ESTIMATE — confirm exact KIE Suno credit cost.
var AUDIO_CREDITS = { "Suno V5.5": 16 };
function getActiveAudioModel(){
    var dd = document.getElementById("audio-model-dd");
    var a = dd ? dd.querySelector(".dd-item.active") : null;
    return a ? a.dataset.model : "Suno V5.5";
}
function updateAudioCost(){
    var cr = AUDIO_CREDITS[getActiveAudioModel()];
    var el = document.getElementById("audio-cost");
    if (el && cr != null) el.textContent = Math.round(cr * CREDIT_TO_TOKEN);
    refreshGenButtons();
}

// Generate buttons flip to "Пополнить" when the balance can't cover the cost,
// and tapping them then routes to top-up instead of failing on a 402.
function _genCost(type){
    var id = type==="image"?"photo-cost":(type==="audio"?"audio-cost":"video-cost");
    var el = document.getElementById(id);
    return el ? (parseInt(el.textContent,10)||0) : 0;
}
// Optimistic balance: the server reserves tokens at the START of a generation (atomic
// try_charge before the job, refund on failure), but the displayed balance only changed
// on the final response. Mirror the reservation in the UI immediately so the user sees
// tokens taken at once and the gen buttons flip to "пополнить" if funds run out; restore
// on failure (the server refunded), or overwrite with the authoritative balance on success.
function applyBalanceDelta(d){
    if(!currentUser || currentUser.balance == null) return;
    currentUser.balance = Math.max(0, currentUser.balance + d);
    document.querySelectorAll(".user-balance").forEach(function(el){ el.textContent = currentUser.balance; });
    refreshGenButtons();
}
function refreshGenButtons(){
    [["gen-photo","image"],["gen-video","video"],["gen-audio","audio"]].forEach(function(p){
        var btn = document.getElementById(p[0]); if(!btn) return;
        var lbl = btn.querySelector("span[data-i18n]");
        var bal = (currentUser && currentUser.balance != null) ? currentUser.balance : null;
        var need = bal != null && bal < _genCost(p[1]);
        btn.classList.toggle("topup-needed", need);
        if(lbl){ lbl.setAttribute("data-i18n", need?"topupGen":"generate"); lbl.textContent = t(need?"topupGen":"generate"); }
    });
}
var currentLang = "ru";
var currentUser = null;
var currentVideoModel = "Kling 3.0";
var currentVideoDuration = null;
var currentVideoQuality = null;
var motionVideoRawSec = null;   // measured length of the uploaded motion clip (sec)

// Bot-issued fallback auth token from the WebApp URL (?tgauth=). Used when the
// client doesn't expose initData (some Telegram Desktop builds). Persisted so it
// survives in-app navigation that drops the query string.
var pwAuthToken = (function(){
    try {
        var u = new URLSearchParams(location.search).get("tgauth");
        if (u) { localStorage.setItem("pwAuthToken", u); return u; }
        return localStorage.getItem("pwAuthToken");
    } catch (e) { return null; }
})();

var _tgIdCache = null;
function getTgId() {
    if (tg.initDataUnsafe && tg.initDataUnsafe.user && tg.initDataUnsafe.user.id) return tg.initDataUnsafe.user.id;
    if (_tgIdCache) return _tgIdCache;
    // Some clients (notably Telegram Desktop/Web) leave initDataUnsafe.user empty
    // while still providing the signed initData string — parse the id out of it.
    try {
        if (tg && tg.initData) {
            var raw = new URLSearchParams(tg.initData).get("user");
            if (raw) { var u = JSON.parse(raw); if (u && u.id) { _tgIdCache = u.id; return u.id; } }
        }
    } catch (e) {}
    // Last resort: the tg_id is the first segment of the signed fallback token.
    if (pwAuthToken) {
        var tid = parseInt(pwAuthToken.split(".")[0], 10);
        if (tid) { _tgIdCache = tid; return tid; }
    }
    return null;
}

// Attach the signed Telegram initData (and the fallback token) so the server can
// verify the user.
function authHeaders(extra) {
    var h = extra || {};
    if (tg && tg.initData) h["X-Init-Data"] = tg.initData;
    if (pwAuthToken) h["X-Auth-Token"] = pwAuthToken;
    return h;
}

async function loadUserProfile() {
    var tgId = getTgId();
    if (!tgId) {
        // Diagnostic: surface what the client actually exposes when no id resolves.
        var pe = document.getElementById("prof-id");
        if (pe) pe.textContent = "no-id · plat:" + (tg.platform || "?") +
            " v:" + (tg.version || "?") + " init:" + (tg.initData ? tg.initData.length : 0) +
            " u:" + ((tg.initDataUnsafe && tg.initDataUnsafe.user) ? 1 : 0);
        return;
    }
    try {
        var res = await fetch("/api/user/" + tgId, { headers: authHeaders() });
        if (res.ok) {
            currentUser = await res.json();
            var idEl = document.getElementById("prof-id");
            if (idEl) idEl.textContent = currentUser.tg_id;
            var balEls = document.querySelectorAll(".user-balance");
            balEls.forEach(function(el){ el.textContent = currentUser.balance; });
            refreshGenButtons();
            if (currentUser.lang && currentUser.lang !== currentLang) {
                applyLang(currentUser.lang);
            }
            var nm = document.getElementById("notif-marketing-tg");
            if (nm) nm.checked = currentUser.notif_marketing !== false;
        }
    } catch(e) { console.error("Failed to load profile", e); }
}

async function setNotifMarketing(on) {
    var tgId = getTgId();
    if (!tgId) return;
    try {
        await fetch("/api/user/" + tgId + "/notif", {
            method: "POST",
            headers: authHeaders({ "Content-Type": "application/json" }),
            body: JSON.stringify({ on: on }),
        });
        if (currentUser) currentUser.notif_marketing = on;
    } catch(e) { console.error("Failed to set notif pref", e); }
}

async function loadUserHistory() {
    var tgId = getTgId();
    if (!tgId) return;
    historyLoading = true;
    try {
        var res = await fetch("/api/user/" + tgId + "/history?limit=" + historyLimit, { headers: authHeaders() });
        if (res.ok) {
            var items = await res.json();
            // A full window back means the server likely has older rows beyond it.
            historyHasMore = items.length >= historyLimit;
            galleryItems = [];
            items.forEach(function(i){
                if (!i.result_url) return;
                var s = i.settings || {};
                if (typeof s === "string") { try { s = JSON.parse(s); } catch(e) { s = {}; } }
                var type = (i.gen_type === "photo" || i.gen_type === "audio") ? i.gen_type : "video";
                // A multi-image gen stores the full set in result_urls — expand each into
                // its own gallery card (sharing the row id; deleting one deletes the row).
                var urls = (i.result_urls && i.result_urls.length) ? i.result_urls : [i.result_url];
                urls.forEach(function(u){
                    if (!u) return;
                    galleryItems.push({
                        id: i.id, url: u, type: type,
                        prompt: i.prompt, model: i.model,
                        settings: s,
                        cost: i.cost || 0, created_at: i.created_at
                    });
                });
            });
            // Still-running jobs (status pending, no result yet) → placeholder cards that
            // survive reloads. Skip stale rows so a crashed job doesn't spin forever.
            var now = Date.now();
            serverPending = items.filter(function(i){
                if (i.result_url || i.status !== "pending") return false;
                var ts = i.created_at ? new Date(i.created_at).getTime() : now;
                return (now - ts) < PENDING_MAX_AGE_MS;
            }).map(function(i){
                return { id: "s" + i.id, type: (i.gen_type === "photo" || i.gen_type === "audio") ? i.gen_type : "video", model: i.model || "" };
            });
            recomputePending();
            updateHistory();
            // If the image-source picker is open on the History tab, APPEND the newly
            // loaded photos to the existing grid instead of re-rendering it. A full
            // re-render recreates every <img>, which flashes black while they re-decode
            // and looks like the whole gallery reloads. Appending leaves drawn images alone.
            var ov = document.getElementById("imgsrc-overlay");
            if (imgSrcView === "history" && ov && !ov.classList.contains("hidden")){
                var grid = document.querySelector("#imgsrc-body .imgsrc-grid");
                var photos = galleryItems.filter(function(g){ return g.type === "photo" && g.url; }).map(function(g){ return g.url; });
                var have = grid ? grid.querySelectorAll(".imgsrc-cell").length : -1;
                if (grid && photos.length >= have){
                    if (photos.length > have) grid.insertAdjacentHTML("beforeend", photos.slice(have).map(imgSrcCell).join(""));
                    var more = document.getElementById("imgsrc-more");
                    if (!historyHasMore){ if (more) more.remove(); }
                    else if (more){ more.textContent = ""; }   // clear "loading", keep sentinel
                } else {
                    imgSrcGrid("history");   // unexpected shape (e.g. list shrank) → safe rebuild
                }
            }
            if (serverPending.length) pendingPoll();
        }
    } catch(e) { console.error("Failed to load history", e); }
    finally { historyLoading = false; }
}

// ── Transactions / statistics ──
function txLabel(tx){
    var d=(tx.description||"");
    if(tx.tx_type==="bonus") return t("txBonus");
    if(tx.tx_type==="topup") return t("txTopup");
    if(d.indexOf("refund")===0) return t("txRefund");
    if(d.indexOf("photo:")===0) return t("txGenPhoto");
    if(d.indexOf("video:")===0) return t("txGenVideo");
    if(d.indexOf("audio:")===0) return t("txGenAudio");
    if(d.indexOf("chat")===0) return t("txChat");
    return tx.amount>=0 ? t("txTopup") : t("txSpend");
}
function txModel(tx){
    var d=tx.description||"";
    if(d.indexOf("photo:")===0||d.indexOf("video:")===0||d.indexOf("audio:")===0){ return d.slice(d.indexOf(":")+1); }
    return "";
}
function txDate(iso){
    if(!iso) return "";
    var dt=new Date(iso); if(isNaN(dt.getTime())) return "";
    function p(n){ return (n<10?"0":"")+n; }
    return p(dt.getDate())+"."+p(dt.getMonth()+1)+" "+p(dt.getHours())+":"+p(dt.getMinutes());
}
var statsPeriod="month"; // week | month | all (default: last month)
function periodCutoff(){
    if(statsPeriod==="all") return 0;
    var days=statsPeriod==="week"?7:30;
    return Date.now()-days*86400000;
}
function setStatsPeriod(p){
    statsPeriod=p;
    var tabs=document.querySelectorAll("#period-tabs .period-tab");
    tabs.forEach(function(b){ b.classList.toggle("active", b.dataset.period===p); });
    loadTransactions();
}
async function loadTransactions(){
    var tgId=getTgId(); if(!tgId) return;
    var list=document.getElementById("tx-list"); if(!list) return;
    try {
        var res=await fetch("/api/user/"+tgId+"/transactions", { headers: authHeaders() });
        if(!res.ok) return;
        var txs=await res.json();
        var cutoff=periodCutoff();
        if(cutoff>0){ txs=txs.filter(function(tx){ var dt=new Date(tx.created_at); return !isNaN(dt.getTime()) && dt.getTime()>=cutoff; }); }
        var inSum=0, outSum=0;
        txs.forEach(function(tx){ if(tx.amount>=0) inSum+=tx.amount; else outSum+=(-tx.amount); });
        var ie=document.getElementById("stat-in"), oe=document.getElementById("stat-out");
        if(ie) ie.textContent=inSum; if(oe) oe.textContent=outSum;
        if(!txs.length){ list.innerHTML='<div class="tx-empty">'+t("txEmpty")+'</div>'; return; }
        list.innerHTML=txs.map(function(tx){
            var credit=tx.amount>=0;
            var model=txModel(tx);
            var ico=credit
                ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="19" x2="12" y2="5"/><polyline points="5 12 12 5 19 12"/></svg>'
                : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><polyline points="5 12 12 19 19 12"/></svg>';
            return '<div class="tx-item">'+
                '<div class="tx-ico '+(credit?"plus":"minus")+'">'+ico+'</div>'+
                '<div class="tx-main"><span class="tx-title">'+escHtml(txLabel(tx))+(model?' <span class="tx-sub">· '+escHtml(model)+'</span>':'')+'</span>'+
                '<span class="tx-date">'+txDate(tx.created_at)+'</span></div>'+
                '<span class="tx-amt '+(credit?"plus":"minus")+'">'+(credit?"+":"−")+Math.abs(tx.amount)+' W</span>'+
            '</div>';
        }).join("");
    } catch(e){ /* keep previous */ }
}

if (tg.initDataUnsafe && tg.initDataUnsafe.user) {
    var u = tg.initDataUnsafe.user;
    var idEl = document.getElementById("prof-id");
    if (idEl) idEl.textContent = u.id;
}

// ── i18n ──
function t(key) {
    var dict = I18N[currentLang] || I18N.ru;
    return dict[key] || (I18N.ru[key] || key);
}

function applyLang(lang) {
    currentLang = lang;
    document.documentElement.lang = lang;
    document.querySelectorAll("[data-i18n]").forEach(function(el) {
        el.textContent = t(el.dataset.i18n);
    });
    document.querySelectorAll("[data-i18n-placeholder]").forEach(function(el) {
        el.placeholder = t(el.dataset.i18nPlaceholder);
    });
    document.getElementById("lang-btn").textContent = langFlags[lang] || langFlags.ru;
    renderVideoSettings(currentVideoModel);
    if (typeof setModelDesc === "function") { setModelDesc("photo"); setModelDesc("audio"); }
    if (typeof renderTemplatesHome === "function") renderTemplatesHome();   // re-caption gallery
    if (typeof fixObBonus === "function") fixObBonus();   // keep onboarding bonus {n} substituted
    if (typeof renderRewardsState === "function") renderRewardsState();   // re-apply "done" labels after re-localizing

    var tgId = getTgId();
    if (tgId) {
        fetch("/api/user/" + tgId + "/lang", {
            method: "POST",
            headers: authHeaders({"Content-Type": "application/json"}),
            body: JSON.stringify({lang: lang})
        }).catch(function(){});
    }
}

// ── Navigation ──
// ── Native Telegram BackButton ──
var NATIVE_UI = !!(tg.isVersionAtLeast && tg.isVersionAtLeast("6.1"));
var IS_MOBILE = (tg.platform === "android" || tg.platform === "ios");
// We use our own in-page fixed CTA on all platforms (the native MainButton is
// text-only and renders differently per platform); only BackButton is native.
if (NATIVE_UI) {
    document.body.classList.add("tg-back");
    if (tg.BackButton && tg.BackButton.onClick) tg.BackButton.onClick(function(){ var b = document.getElementById("back-btn"); if (b) b.click(); });
    try { tg.MainButton && tg.MainButton.hide(); tg.BackButton && tg.BackButton.hide(); } catch (e) {}
}

var SUB_PAGES = ["topup","partner","info","gen-detail","tpl-detail","rewards","refguide","stats","terms","privacy","offer","support"];
var subOrigin = "home";   // page to return to when leaving a header-opened sub-page
function showPage(name) {
    if (typeof genViewOpen !== "undefined" && genViewOpen) genOvClose();
    var isSub = SUB_PAGES.indexOf(name) >= 0;
    // Remember which main page we came from so Back returns there, not always Profile.
    if (isSub) {
        var curEl = document.querySelector(".page.active");
        if (curEl) {
            var curName = curEl.id.replace("page-", "");
            if (SUB_PAGES.indexOf(curName) < 0) subOrigin = curName;
        }
    }
    // header (brand + balance + пополнить) stays on every page; show back arrow on sub-pages
    document.getElementById("back-btn").classList.toggle("hidden", !isSub);
    // bottom nav stays visible on every page (never hide it)
    document.querySelectorAll(".page").forEach(function(p){p.classList.remove("active")});
    var pg = document.getElementById("page-" + name);
    if (pg) pg.classList.add("active");
    if (!isSub) {
        document.querySelectorAll(".ni").forEach(function(n){n.classList.remove("active")});
        var nb = document.querySelector('[data-page="'+name+'"]');
        if (nb) nb.classList.add("active");
    }
    if (name === "history") loadUserHistory();
    if (name === "profile") { loadUserProfile(); loadReferences(); }
    if (name === "stats") { setStatsPeriod("month"); }
    if (name === "partner") loadPartner();
    if (name === "text") initChat();
    if (name === "rewards") loadRewards();
    if (name === "support") initSupport();
    if (name !== "support" && supPollTimer) { clearInterval(supPollTimer); supPollTimer = null; }
    if (NATIVE_UI && tg.BackButton) { if (isSub) tg.BackButton.show(); else tg.BackButton.hide(); }
    window.scrollTo(0,0);
}

function resetCreateForm(){
    uploadedFiles={};
    motionVideoRawSec=null;
    var pp=document.getElementById("prompt-photo");if(pp)pp.value="";
    var pa=document.getElementById("prompt-audio");if(pa)pa.value="";
    state.pRatio=1;state.pQual=1;
    var ri=document.getElementById("p-ratio");if(ri)ri.textContent=ratios[1];
    var qi=document.getElementById("p-qual");if(qi)qi.textContent=quals[1];
    document.querySelectorAll(".cnt").forEach(function(b){b.classList.toggle("active",b.dataset.c==="1")});
    initPhotoUpload();
    renderVideoSettings(currentVideoModel);
    document.querySelectorAll("#form-audio .pill-row").forEach(function(row){
        row.querySelectorAll(".pill").forEach(function(p,i){p.classList.toggle("active",i===0)});
    });
    var lw=document.getElementById("audio-lyrics-wrap");if(lw)lw.classList.add("hidden");
    var al=document.getElementById("audio-lyrics");if(al)al.value="";
    refreshGenButtons();
}
// bottom nav
document.querySelectorAll(".ni").forEach(function(b){b.addEventListener("click",function(){if(b.dataset.page==="create")resetCreateForm();showPage(b.dataset.page)})});
// back
document.getElementById("back-btn").addEventListener("click",function(){
    var cur = document.querySelector(".page.active");
    if (cur && cur.id === "page-gen-detail") { showPage("history"); }
    else if (cur && cur.id === "page-tpl-detail") { showPage("home"); }
    else if (cur && cur.id === "page-rewards") { showPage("home"); }
    else if (cur && cur.id === "page-refguide") { showPage(refguideFrom); }
    else { showPage(subOrigin); }   // topup/partner/info/stats → wherever we opened it from
});

// Photo reference guide
var refguideFrom = "create";
function openRefguide(from) { refguideFrom = from || "create"; showPage("refguide"); }
var createRefHint = document.querySelector("#form-image [data-i18n='refHint']");
if (createRefHint) createRefHint.addEventListener("click", function(){ openRefguide("create"); });
// action cards
document.querySelectorAll("[data-nav]").forEach(function(el){el.addEventListener("click",function(e){if(el.closest("label")){e.preventDefault();e.stopPropagation();}showPage(el.dataset.nav)})});
// "По шаблону" → scroll to the templates showcase on the home page
(function(){ var b=document.getElementById("hook-tpl-btn"); if(b) b.addEventListener("click",function(){ var t=document.getElementById("home-tpl"); if(t) t.scrollIntoView({behavior:"smooth",block:"start"}); }); })();
// stats period tabs
document.querySelectorAll("#period-tabs .period-tab").forEach(function(b){b.addEventListener("click",function(){setStatsPeriod(b.dataset.period)})});
// topup buttons
["topup-btn-h","topup-btn-p"].forEach(function(id){
    var el=document.getElementById(id); if(el) el.addEventListener("click",function(){showPage("topup")});
});
// feed tabs
// ── Create: type toggle ──
function setCreateType(type){
    var tt = document.querySelector(".type-toggle");
    tt.classList.remove("seg-video","seg-audio");
    if (type === "video") tt.classList.add("seg-video");
    if (type === "audio") tt.classList.add("seg-audio");
    document.querySelectorAll(".create-form").forEach(function(f){f.classList.add("hidden")});
    var form = document.getElementById("form-"+type);
    if (form) form.classList.remove("hidden");
    // Keep the segmented tab highlight in sync (deep-links call this directly).
    document.querySelectorAll(".tbtn").forEach(function(b){ b.classList.toggle("active", b.dataset.type === type); });
    refreshGenButtons();
}
document.querySelectorAll(".tbtn").forEach(function(b){
    b.addEventListener("click",function(){ setCreateType(b.dataset.type); });
});

// ── Video model configs ── (KIE credit costs, 1 credit = $0.005)
var VIDEO_MODELS = {
    "Kling 3.0": {
        cost: 90,
        desc: "vmDescKling",
        // Kling 3.0 не поддерживает конечный кадр — с двумя кадрами не генерит вовсе,
        // поэтому показываем только стартовый (см. kie.py: шлём один image_urls).
        uploads: [
            { id: "v-start-frame", title: "startFrame", hint: "startFrameHint", type: "image", label: "addPhoto" }
        ],
        // Соотношение сторон: при прикреплённом кадре Kling берёт формат из самого фото,
        // поэтому показываем «как на фото» и блокируем выбор; без кадра — обычный выбор.
        ratios: ["16:9", "9:16", "1:1"],
        ratioFromFrame: true,
        qualities: ["720p", "1080p"],
        duration: { min: 3, max: 15, default: 5 },
        sound: true,
        // фикс. сетка W/сек: 720p 18/24, 1080p 24/32 (без/со звуком) — маржа ~87-88%
        // KIE-кредиты: 720p 6/9, 1080p 8/12 (1 кр = $0.005)
        tokenPerSec: { "720p": { silent: 18, sound: 24 }, "1080p": { silent: 24, sound: 32 } }
    },
    "Veo 3.1 Fast": {
        cost: 69,
        desc: "vmDescVeo",
        uploads: [
            { id: "v-start-frame", title: "startFrame", hint: "startFrameHint", type: "image", label: "addPhoto" },
            { id: "v-end-frame", title: "endFrame", hint: "endFrameHint", type: "image", label: "addFrame" }
        ],
        ratios: ["16:9", "9:16", "Auto"],
        qualities: ["720p", "1080p", "4K"],
        qualityLabel: "resolution",
        duration: { min: 4, max: 8, default: 8, steps: [4, 6, 8] },
        tokenPerCredit: 0.867,   // 60% маржа (только Veo Fast)
        // Veo 3.1 Fast ≈ $0.05/s (10 cr/s) @720p; 1080p/4K — оценка (4K включает апскейл)
        creditPerSec: { "720p": { silent: 10 }, "1080p": { silent: 15 }, "4K": { silent: 26 } }
    },
    "Seedance 2.0": {
        cost: 48,
        desc: "vmDescSeedance",
        uploads: [
            { id: "v-start-frame", title: "startFrame", hint: "startFrameHint", type: "image", label: "addPhoto" },
            { id: "v-end-frame", title: "endFrame", hint: "endFrameHint", type: "image", label: "addFrame" }
        ],
        refImages: { max: 9 },
        refVideos: { max: 3 },
        refAudio: { max: 3 },
        mode: ["Standard", "Fast"],
        ratios: ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"],
        qualities: ["480p", "720p"],
        duration: { min: 4, max: 15, default: 4 },
        sound: true,
        tokenPerCredit: 0.99,   // 65% маржа
        // KIE Seedance 2.0 кр/с: Standard 12/28 (480p/720p), Fast 10/22
        creditPerSecByMode: {
            "Standard": { "480p": 12, "720p": 28 },
            "Fast": { "480p": 10, "720p": 22 }
        }
    },
    "Kling Motion 3.0": {
        cost: 139,
        desc: "vmDescMotion",
        uploads: [
            { id: "v-char-photo", title: "charPhoto", hint: "charPhotoHint", type: "image", label: "addPhoto" },
            { id: "v-motion-video", title: "motionVideo", hint: "motionVideoHint", type: "video", label: "addVideo" }
        ],
        qualities: ["720p", "1080p"],   // KIE "mode"
        orientation: ["byVideo", "byPhoto"],   // KIE character_orientation: video / image
        fixedSeconds: 10,               // длина = длине входного видео; для цены берём ~10с
        tokenPerCredit: 1.156,          // 70% маржа
        // KIE motion-control: 12 cr/s @720p ($0.06), 20 cr/s @1080p ($0.10)
        creditPerSec: { "720p": { silent: 12 }, "1080p": { silent: 20 } }
    },
    "Grok Imagine 1.5": {
        cost: 132,
        desc: "vmDescGrok",
        uploads: [
            { id: "v-grok15-photo", title: "grokRefPhoto", hint: "grokRefPhotoHint", type: "image", required: true, label: "addPhoto" }
        ],
        ratios: ["Auto", "16:9", "9:16", "1:1", "3:2", "2:3"],
        qualities: ["480p", "720p"],
        duration: { min: 6, max: 30, default: 6 },   // KIE grok image-to-video: 6–30s
        // xAI: $0.05/s @480p (10 cr/s), $0.07/s @720p (14 cr/s)
        creditPerSec: { "480p": { silent: 10 }, "720p": { silent: 14 } }
    },
};

var plusSvg = '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>';
var uploadedFiles = {};

function pickFile(accept, callback) {
    var inp = document.createElement("input");
    inp.type = "file"; inp.accept = accept; inp.style.display = "none";
    document.body.appendChild(inp);
    inp.onchange = function() { if (inp.files[0]) callback(inp.files[0]); document.body.removeChild(inp); };
    inp.click();
}

function showSinglePreview(el) {
    var uid = el.dataset.uid;
    var file = uploadedFiles[uid];
    if (!file) return;
    if (file.type.startsWith("image/")) {
        var reader = new FileReader();
        reader.onload = function(e) {
            el.innerHTML = '<img src="' + e.target.result + '" class="up-thumb"><button class="up-x">×</button>';
            el.classList.add("has-img");   // show the WHOLE frame (contain), not a cropped zoom
        };
        reader.readAsDataURL(file);
    } else {
        var name = file.name.length > 18 ? file.name.substring(0, 15) + "…" : file.name;
        el.innerHTML = '<span class="up-fname">' + name + '</span><button class="up-x">×</button>';
    }
    syncRatioFromFrame();   // "Как на фото" when a Kling start frame is attached
}

function renderRefChips(area) {
    var uid = area.dataset.uid;
    var files = uploadedFiles[uid] || [];
    var max = parseInt(area.dataset.max) || 9;
    var html = "";
    files.forEach(function(f, i) {
        if (f.type && f.type.startsWith("image/")) {
            var objUrl = URL.createObjectURL(f);
            html += '<div class="ref-chip ref-chip-img" data-idx="' + i + '"><img src="' + objUrl + '" class="ref-chip-thumb" onload="URL.revokeObjectURL(this.src)"><button class="ref-chip-x">×</button></div>';
        } else {
            var name = f.name.length > 12 ? f.name.substring(0, 9) + "…" : f.name;
            html += '<div class="ref-chip" data-idx="' + i + '"><span class="ref-chip-name">' + name + '</span><button class="ref-chip-x">×</button></div>';
        }
    });
    if (files.length < max) {
        html += '<div class="up-btn ref-add">' + plusSvg + '</div>';
    }
    area.innerHTML = html;
}

function initUploads() {
    uploadedFiles = {};
    motionVideoRawSec = null;
    var vs = document.getElementById("video-settings");
    if (!vs) return;
    if (vs.dataset.bound) return;   // attach the delegated click listener only once
    vs.dataset.bound = "1";
    vs.addEventListener("click", function(e) {
        if (e.target.closest(".up-x")) {
            e.stopPropagation();
            var up = e.target.closest(".frame-up[data-uid]");
            if (up) {
                delete uploadedFiles[up.dataset.uid];
                up.classList.remove("has-img");
                up.innerHTML = plusSvg + '<span>' + up.dataset.lbl + '</span>';
                if (up.dataset.uid === "v-motion-video") { motionVideoRawSec = null; updateMotionDuration(); }
                syncRatioFromFrame();   // frame removed -> restore the ratio picker
            }
            return;
        }
        if (e.target.closest(".ref-chip-x")) {
            e.stopPropagation();
            var chip = e.target.closest(".ref-chip");
            var area = chip.closest(".ref-area");
            var uid = area.dataset.uid;
            var idx = parseInt(chip.dataset.idx);
            if (uploadedFiles[uid]) { uploadedFiles[uid].splice(idx, 1); if (!uploadedFiles[uid].length) delete uploadedFiles[uid]; }
            renderRefChips(area);
            return;
        }
        var frameUp = e.target.closest(".frame-up[data-uid]");
        if (frameUp && !uploadedFiles[frameUp.dataset.uid]) {
            var fAccept = frameUp.dataset.accept || "image/*";
            var fOnFile = function(file) {
                uploadedFiles[frameUp.dataset.uid] = file;
                showSinglePreview(frameUp);
                if (frameUp.dataset.uid === "v-motion-video") {
                    measureVideoDuration(file, function(sec) { motionVideoRawSec = sec; updateMotionDuration(); });
                }
            };
            // Image frames get the phone/history/reference chooser; video files stay native.
            if (fAccept.indexOf("image") === 0) openImageSource(fOnFile); else pickFile(fAccept, fOnFile);
            return;
        }
        var refAdd = e.target.closest(".ref-add");
        if (refAdd) {
            var area = refAdd.closest(".ref-area");
            var uid = area.dataset.uid;
            var max = parseInt(area.dataset.max) || 9;
            if (uploadedFiles[uid] && uploadedFiles[uid].length >= max) return;
            var rAccept = area.dataset.accept || "image/*";
            var rOnFile = function(file) {
                if (!uploadedFiles[uid]) uploadedFiles[uid] = [];
                uploadedFiles[uid].push(file);
                renderRefChips(area);
            };
            if (rAccept.indexOf("image") === 0) openImageSource(rOnFile); else pickFile(rAccept, rOnFile);
            return;
        }
    });
}

// ── Saved reference photos ("Мой референс") ──
var REF_MAX = 6;
var referenceList = null;   // cache of {id, file_url, title}

function refCellHtml(r, mode){
    var del = mode === "manage" ? '<button class="ref-x" data-id="' + r.id + '">×</button>' : '';
    return '<div class="ref-cell" data-id="' + r.id + '" data-url="' + escHtml(r.file_url) + '">' +
           '<img src="' + escHtml(r.file_url) + '" class="ref-img" alt="">' + del + '</div>';
}
function renderRefLib(container, mode){
    if (!container) return;
    var arr = referenceList || [];
    var html = arr.map(function(r){ return refCellHtml(r, mode); }).join("");
    if (arr.length < REF_MAX){
        html += '<button class="ref-add-tile" data-ref-add="1">' + plusSvg + '<span>' + t("refAdd") + '</span></button>';
    }
    container.innerHTML = html;
}
function renderAllRefLibs(){
    renderRefLib(document.getElementById("ref-lib"), "manage");
    // Refresh the chooser's "Мой референс" grid if it's the one currently open.
    if (typeof imgSrcView !== "undefined" && imgSrcView === "ref") imgSrcGrid("ref");
}
async function loadReferences(force){
    var tgId = getTgId(); if (!tgId) return;
    if (referenceList && !force){ renderAllRefLibs(); return; }
    try {
        var res = await fetch("/api/user/" + tgId + "/references", { headers: authHeaders() });
        if (!res.ok) return;
        referenceList = await res.json();
        renderAllRefLibs();
    } catch(e){}
}
function refAddClick(){
    if ((referenceList || []).length >= REF_MAX){ toast(t("refLimit"), "info"); return; }
    pickFile("image/*", uploadReference);
}
async function uploadReference(file){
    var tgId = getTgId(); if (!tgId) return;
    toast(t("refUploading"), "info");
    var fd = new FormData(); fd.append("file", file);
    try {
        var res = await fetch("/api/references", { method: "POST", headers: authHeaders(), body: fd });
        var d = await res.json().catch(function(){ return {}; });
        if (res.ok && d.id){
            if (!referenceList) referenceList = [];
            referenceList.unshift(d);
            renderAllRefLibs();
            toast(t("refAdded"), "success");
        } else if (res.status === 409){ toast(t("refLimit"), "info"); }
        else if (res.status === 400){ toast(t("refBadFile"), "error"); }
        else { toast(t("refError"), "error"); }
    } catch(e){ toast(t("refError"), "error"); }
}
async function deleteRef(id){
    try {
        var res = await fetch("/api/references/" + id, { method: "DELETE", headers: authHeaders() });
        if (res.ok){ referenceList = (referenceList || []).filter(function(r){ return String(r.id) !== String(id); }); renderAllRefLibs(); }
    } catch(e){}
}
(function(){
    var lib = document.getElementById("ref-lib");
    if (lib) lib.addEventListener("click", function(e){
        var x = e.target.closest(".ref-x");
        if (x){ e.stopPropagation(); deleteRef(x.dataset.id); return; }
        if (e.target.closest("[data-ref-add]")){ refAddClick(); return; }
    });
    var gl = document.getElementById("ref-guide-link"); if (gl) gl.addEventListener("click", function(){ openRefguide("profile"); });
})();

// ── Flexible image-source chooser (phone / history / saved reference) ──
// One bottom sheet reused for every photo upload (Create + templates). The caller
// passes a callback that receives a File, regardless of where the user picked it.
var imgSrcOnFile = null;
var imgSrcView = "menu";   // "menu" | "history" | "ref" — which chooser screen is open
var IMGSRC_PHONE ='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9a2 2 0 0 0-2-2h-3l-2.5-3z"/><circle cx="12" cy="13" r="3.2"/></svg>';
var IMGSRC_HIST = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15.5 14"/></svg>';
var IMGSRC_REF = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>';
function imgSrcOpt(k, ic, title, sub){
    return '<button class="imgsrc-opt" data-src="'+k+'"><span class="imgsrc-opt-ic">'+ic+'</span>'+
           '<span class="imgsrc-opt-tx"><span class="imgsrc-opt-title">'+title+'</span><span class="imgsrc-opt-sub">'+sub+'</span></span></button>';
}
function imgSrcMenu(){
    imgSrcView = "menu";
    document.getElementById("imgsrc-back").classList.add("hidden");
    document.getElementById("imgsrc-title").textContent = t("imgSrcTitle");
    document.getElementById("imgsrc-body").innerHTML = '<div class="imgsrc-menu">'+
        imgSrcOpt("phone", IMGSRC_PHONE, t("imgSrcPhone"), t("imgSrcPhoneSub"))+
        imgSrcOpt("history", IMGSRC_HIST, t("imgSrcHistory"), t("imgSrcHistorySub"))+
        imgSrcOpt("ref", IMGSRC_REF, t("imgSrcRef"), t("imgSrcRefSub"))+
    '</div>';
}
function imgSrcCell(u){
    return '<div class="imgsrc-cell" data-url="'+escHtml(u)+'"><img src="'+escHtml(u)+'" loading="lazy"></div>';
}
function imgSrcGrid(kind){
    imgSrcView = kind;
    document.getElementById("imgsrc-back").classList.remove("hidden");
    var title = document.getElementById("imgsrc-title"), body = document.getElementById("imgsrc-body");
    if (kind === "history"){
        title.textContent = t("imgSrcHistory");
        var photos = galleryItems.filter(function(g){ return g.type === "photo" && g.url; }).map(function(g){ return g.url; });
        // Mirror the History page: the picker draws the same galleryItems window, so it
        // needs its own "show more" to reach older photos (else scrolling dead-ends at the
        // current limit and the gallery seems to stop loading).
        body.innerHTML = photos.length
            ? '<div class="imgsrc-grid">'+photos.map(imgSrcCell).join("")+'</div>'
              + (historyHasMore ? '<div class="imgsrc-loadmore" id="imgsrc-more">'+(historyLoading ? t("loading") : "")+'</div>' : "")
            : '<div class="imgsrc-empty">'+t("imgSrcEmptyHist")+'</div>';
        return;
    }
    // "Мой референс" — always offer a "+ добавить" tile, even when empty.
    title.textContent = t("imgSrcRef");
    var refs = (referenceList || []).map(function(r){ return r.file_url; });
    var addTile = '<button class="imgsrc-cell imgsrc-add" data-imgsrc-add="1"><span class="imgsrc-add-plus">+</span><span class="imgsrc-add-t">'+t("refAdd")+'</span></button>';
    var html = '<div class="imgsrc-grid">'+refs.map(imgSrcCell).join("")+addTile+'</div>';
    if (!refs.length) html = '<div class="imgsrc-empty">'+t("imgSrcEmptyRef")+'</div>'+html;
    body.innerHTML = html;
}
function imgSrcClose(){
    var o = document.getElementById("imgsrc-overlay"); if (!o) return;
    o.classList.remove("show");
    setTimeout(function(){ o.classList.add("hidden"); }, 300);
    imgSrcOnFile = null;
}
// Our own S3 media is cross-origin; iOS Telegram's webview fails cross-origin
// blob fetches intermittently (CORS / Vary cache quirks), so route remote URLs
// through the same-origin /api/media-proxy. Same-origin / relative URLs pass through.
function mediaBlobUrl(url){
    if (/^https?:\/\//i.test(url) && url.indexOf(location.origin) !== 0)
        return "/api/media-proxy?url=" + encodeURIComponent(url);
    return url;
}
// Fetch a media URL and hand it back as a File for FormData upload.
function urlToFile(url, cb){
    fetch(mediaBlobUrl(url)).then(function(r){ if(!r.ok) throw 0; return r.blob(); }).then(function(blob){
        var m = url.split("?")[0].match(/\.[a-z0-9]+$/i);
        cb(new File([blob], "image"+(m ? m[0] : ".jpg"), { type: blob.type || "image/jpeg" }));
    }).catch(function(){ toast(t("genFailed"), "error"); });
}
function openImageSource(onFile){
    var o = document.getElementById("imgsrc-overlay");
    if (!o){ pickFile("image/*", onFile); return; }   // graceful fallback
    imgSrcOnFile = onFile;
    imgSrcMenu();
    o.classList.remove("hidden");
    requestAnimationFrame(function(){ o.classList.add("show"); });
    loadReferences();   // warm the saved-reference cache for the "Мой референс" tab
    haptic.impact("light");
}
(function setupImgSrc(){
    var o = document.getElementById("imgsrc-overlay"); if (!o) return;
    o.addEventListener("click", function(e){ if (e.target === o) imgSrcClose(); });
    document.getElementById("imgsrc-back").addEventListener("click", imgSrcMenu);
    // Infinite scroll for the History grid: pull the next window as the user nears the
    // bottom (no manual "show more"). The scroll happens on the inner .imgsrc-grid
    // (overflow-y), and scroll events don't bubble — so listen in the CAPTURE phase.
    // historyLoading guards against duplicate fetches.
    document.getElementById("imgsrc-body").addEventListener("scroll", function(e){
        if (imgSrcView !== "history" || !historyHasMore || historyLoading) return;
        var b = e.target;
        if (!b || !b.classList || !b.classList.contains("imgsrc-grid")) return;
        if (b.scrollTop + b.clientHeight >= b.scrollHeight - 160){
            historyLimit += HISTORY_PAGE;
            var more = document.getElementById("imgsrc-more"); if (more) more.textContent = t("loading");
            loadUserHistory();   // re-renders this grid on completion, keeping scroll
        }
    }, true);
    document.getElementById("imgsrc-body").addEventListener("click", function(e){
        if (e.target.closest("[data-imgsrc-add]")){
            if ((referenceList || []).length >= REF_MAX){ toast(t("refLimit"), "info"); return; }
            pickFile("image/*", uploadReference);   // uploads, then renderAllRefLibs refreshes this grid
            return;
        }
        var opt = e.target.closest(".imgsrc-opt");
        if (opt){
            var k = opt.dataset.src;
            haptic.select();
            if (k === "phone"){ var cb = imgSrcOnFile; imgSrcClose(); pickFile("image/*", cb); }
            else if (k === "history"){ imgSrcGrid("history"); if (!galleryItems.length) loadUserHistory(); }
            else { imgSrcGrid("ref"); loadReferences().then(function(){ imgSrcGrid("ref"); }); }
            return;
        }
        var cell = e.target.closest(".imgsrc-cell");
        if (cell){ var cb2 = imgSrcOnFile, url = cell.dataset.url; haptic.impact("light"); imgSrcClose(); urlToFile(url, cb2); }
    });
})();

function initPhotoUpload() {
    var area = document.querySelector("#form-image .up-area");
    if (!area) return;
    area.classList.add("ref-area");
    area.dataset.uid = "photo-refs";
    area.dataset.accept = "image/*";
    area.dataset.max = "8";
    area.innerHTML = '<div class="up-btn ref-add">' + plusSvg + '</div>';
    area.addEventListener("click", function(e) {
        if (e.target.closest(".ref-chip-x")) {
            e.stopPropagation();
            var chip = e.target.closest(".ref-chip");
            var idx = parseInt(chip.dataset.idx);
            if (uploadedFiles["photo-refs"]) { uploadedFiles["photo-refs"].splice(idx, 1); if (!uploadedFiles["photo-refs"].length) delete uploadedFiles["photo-refs"]; }
            renderRefChips(area);
            return;
        }
        if (e.target.closest(".ref-add")) {
            var max = 8;
            if (uploadedFiles["photo-refs"] && uploadedFiles["photo-refs"].length >= max) return;
            openImageSource(function(file) {
                if (!uploadedFiles["photo-refs"]) uploadedFiles["photo-refs"] = [];
                uploadedFiles["photo-refs"].push(file);
                renderRefChips(area);
            });
        }
    });
}

function vmPickCol(id, label, valText) {
    return '<button class="set-col set-pick" data-vm="' + id + '"><span class="set-lbl">' + label + '</span>' +
        '<div class="set-ctrl"><span class="set-val" id="' + id + '">' + valText + '</span>' +
        '<svg class="set-chev" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg></div></button>';
}

// Real KIE credit cost for the current options, in KIE credits.
//   creditPerSecByMode: { mode -> { quality -> creditsPerSec } }
//   creditPerSec:       { quality -> {silent, sound} }   (per second, e.g. Kling)
function videoCredits(cfg) {
    if (!cfg) return null;
    if (cfg.creditPerSecByMode) {
        var mb = document.querySelector(".vm-mode .pill.active");
        var mode = mb ? mb.dataset.v : Object.keys(cfg.creditPerSecByMode)[0];
        var rates = cfg.creditPerSecByMode[mode] || cfg.creditPerSecByMode[Object.keys(cfg.creditPerSecByMode)[0]];
        var perM = rates ? rates[currentVideoQuality] : null;
        if (perM == null) return null;
        var secM = currentVideoDuration || (cfg.duration ? cfg.duration.default : 1);
        return perM * secM;
    }
    if (cfg.creditPerSec) {
        var rate = cfg.creditPerSec[currentVideoQuality] || cfg.creditPerSec[Object.keys(cfg.creditPerSec)[0]];
        if (!rate) return null;
        var se = document.getElementById("vm-sound");
        var per = (se && se.checked && rate.sound != null) ? rate.sound : rate.silent;
        var sec = currentVideoDuration || cfg.fixedSeconds || (cfg.duration ? cfg.duration.default : 1);
        return per * sec;
    }
    return null;
}
// tokens (W) = round(KIE credits × CREDIT_TO_TOKEN); fallback to fixed cfg.cost
function computeVideoCost(cfg) {
    // explicit W-per-second grid (overrides credit math) — e.g. Kling 3.0
    if (cfg && cfg.tokenPerSec) {
        var rate = cfg.tokenPerSec[currentVideoQuality] || cfg.tokenPerSec[Object.keys(cfg.tokenPerSec)[0]];
        if (rate) {
            var se = document.getElementById("vm-sound");
            var per = (se && se.checked && rate.sound != null) ? rate.sound : rate.silent;
            var sec = currentVideoDuration || (cfg.duration ? cfg.duration.default : 1);
            return Math.round(per * sec);
        }
    }
    var cr = videoCredits(cfg);
    if (cr != null) return Math.round(cr * (cfg.tokenPerCredit || CREDIT_TO_TOKEN));
    return cfg ? (cfg.cost || 0) : 0;
}
function updateVideoCost() {
    var cfg = VIDEO_MODELS[currentVideoModel] || {};
    var costEl = document.getElementById("video-cost");
    if (costEl) costEl.textContent = computeVideoCost(cfg);
    refreshGenButtons();
}
// read the duration of an uploaded video file (seconds)
function measureVideoDuration(file, cb) {
    try {
        var v = document.createElement("video");
        v.preload = "metadata";
        v.onloadedmetadata = function() { var d = v.duration; URL.revokeObjectURL(v.src); cb(isFinite(d) ? d : null); };
        v.onerror = function() { cb(null); };
        v.src = URL.createObjectURL(file);
    } catch (e) { cb(null); }
}
// motion-control models (fixedSeconds): price by the real length of the uploaded clip,
// clamped to KIE limits (3–30s; ≤10s when character_orientation = image/"По фото")
function updateMotionDuration() {
    var cfg = VIDEO_MODELS[currentVideoModel] || {};
    if (!cfg.fixedSeconds) return;
    var note = document.getElementById("motion-note");
    if (motionVideoRawSec == null) {
        currentVideoDuration = null;
        if (note) note.textContent = t("motionDurHint");
        updateVideoCost();
        return;
    }
    var orientEl = document.getElementById("vm-orient");
    var isImage = orientEl && orientEl.textContent === t("byPhoto");
    var maxSec = isImage ? 10 : 30;
    var sec = Math.max(3, Math.min(maxSec, Math.ceil(motionVideoRawSec)));
    currentVideoDuration = sec;
    if (note) note.textContent = t("motionDurDetected") + " " + sec + " " + t("sec");
    updateVideoCost();
}

function renderVideoSettings(model) {
    var cfg = VIDEO_MODELS[model];
    if (!cfg) return;
    currentVideoModel = model;
    var container = document.getElementById("video-settings");
    var html = "";

    if (cfg.desc) {
        html += '<div class="ccard vm-desc" style="color:var(--tx2);font-size:13px;line-height:1.45">💡 ' + t(cfg.desc) + '</div>';
    }

    if (cfg.uploads && cfg.uploads.length) {
        html += '<div class="ccard"><div class="frames-row">';
        cfg.uploads.forEach(function(u) {
            var accept = u.type === "image" ? "image/*" : u.type === "video" ? "video/mp4,video/quicktime" : "audio/*";
            html += '<div class="frame-col"><span class="frame-title">' + t(u.title) + '</span>';
            html += '<span class="frame-hint">' + t(u.hint) + '</span>';
            html += '<div class="frame-up" data-uid="' + u.id + '" data-accept="' + accept + '" data-lbl="' + t(u.label) + '">' + plusSvg + '<span>' + t(u.label) + '</span></div></div>';
        });
        html += '</div></div>';
    }

    if (cfg.refImages) {
        html += '<div class="ccard"><span class="up-title">' + t("refImages") + ' (' + t("upTo") + ' ' + cfg.refImages.max + ')</span>';
        html += '<div class="up-area ref-area" data-uid="ref-images" data-accept="image/*" data-max="' + cfg.refImages.max + '"><div class="up-btn ref-add">' + plusSvg + '</div></div></div>';
    }

    if (cfg.refVideos) {
        html += '<div class="ccard"><span class="up-title">' + t("refVideos") + ' (' + t("upTo") + ' ' + cfg.refVideos.max + ', mp4/mov)</span>';
        html += '<div class="up-area ref-area" data-uid="ref-videos" data-accept="video/mp4,video/quicktime" data-max="' + cfg.refVideos.max + '"><div class="up-btn ref-add">' + plusSvg + '</div></div></div>';
    }

    if (cfg.refAudio) {
        html += '<div class="ccard"><span class="up-title">' + t("refAudioLabel") + ' (' + t("upTo") + ' ' + cfg.refAudio.max + ', mp3/wav)</span>';
        html += '<div class="up-area ref-area" data-uid="ref-audio" data-accept="audio/*" data-max="' + cfg.refAudio.max + '"><div class="up-btn ref-add">' + plusSvg + '</div></div></div>';
    }

    html += '<div class="ccard">';

    if (cfg.mode) {
        html += '<span class="prm-lbl">' + t("mode") + '</span>';
        html += '<div class="pill-row vm-mode">';
        cfg.mode.forEach(function(m, i) {
            html += '<button class="pill' + (i === 0 ? ' active' : '') + '" data-v="' + m + '">' + m + '</button>';
        });
        html += '</div>';
    }

    var settingCols = [];
    if (cfg.ratios) settingCols.push(vmPickCol("vm-ratio", t("aspectRatio"), cfg.ratios[0]));
    if (cfg.qualities) {
        currentVideoQuality = cfg.qualities[0];
        settingCols.push(vmPickCol("vm-qual", t(cfg.qualityLabel || "quality"), currentVideoQuality));
    } else {
        currentVideoQuality = null;
    }
    if (cfg.duration) {
        currentVideoDuration = cfg.duration.default;
        settingCols.push(vmPickCol("vm-dur", t("duration"), cfg.duration.default + " " + t("sec")));
    } else {
        currentVideoDuration = null;
    }
    if (cfg.orientation) settingCols.push(vmPickCol("vm-orient", t("orientationLabel"), t(cfg.orientation[0])));

    if (settingCols.length) {
        var cls = settingCols.length >= 3 ? " three" : "";
        html += '<div class="set-row' + cls + '">';
        html += settingCols.join('<div class="set-div"></div>');
        html += '</div>';
    }

    if (cfg.fixedSeconds) {
        html += '<p class="field-hint" id="motion-note" style="margin-top:10px">' + t("motionDurHint") + '</p>';
    }

    if (cfg.sound) {
        html += '<div class="sound-row"><span>' + t("sound") + '</span><label class="switch"><input type="checkbox" id="vm-sound"><span class="slider-toggle"></span></label><span class="muted" id="vm-sound-label">' + t("soundOff") + '</span></div>';
    }

    html += '<div class="field"><textarea id="prompt-video" class="field-area" rows="4" placeholder=" "></textarea><label class="field-label" for="prompt-video">' + t("prompt") + '</label></div>';
    html += '<span class="field-hint">' + t("promptVideoPlc") + '</span>';
    html += '</div>';

    container.innerHTML = html;

    bindVideoSettingsEvents(cfg);
    initUploads();
    updateVideoCost();
    syncRatioFromFrame();
}

// Models where the output aspect follows the start frame (Kling): with a frame attached,
// show "Как на фото" and lock the ratio picker; with no frame, restore the user's choice.
function syncRatioFromFrame() {
    var cfg = VIDEO_MODELS[currentVideoModel] || {};
    if (!cfg.ratioFromFrame || !cfg.ratios) return;
    var col = document.querySelector('#video-settings .set-col[data-vm="vm-ratio"]');
    var val = document.getElementById("vm-ratio");
    if (!col || !val) return;
    if (uploadedFiles["v-start-frame"]) {
        val.textContent = t("ratioByPhoto");
        col.classList.add("ratio-locked");
    } else {
        col.classList.remove("ratio-locked");
        if (val.textContent === t("ratioByPhoto")) val.textContent = cfg.ratios[0];
    }
}

function bindVideoSettingsEvents(cfg) {
    if (cfg.sound) {
        var st = document.getElementById("vm-sound");
        if (st) st.addEventListener("change", function() {
            document.getElementById("vm-sound-label").textContent = st.checked ? t("soundOn") : t("soundOff");
            updateVideoCost();
        });
    }

    document.querySelectorAll(".vm-mode .pill").forEach(function(p) {
        p.addEventListener("click", function() {
            p.parentElement.querySelectorAll(".pill").forEach(function(x) { x.classList.remove("active"); });
            p.classList.add("active");
            updateVideoCost();
        });
    });

    // ratio / quality / duration / orientation open a bottom sheet (like the photo page)
    document.querySelectorAll("#video-settings .set-pick").forEach(function(b) {
        b.addEventListener("click", function() {
            var id = b.dataset.vm;
            if (id === "vm-ratio" && cfg.ratios) {
                if (cfg.ratioFromFrame && uploadedFiles["v-start-frame"]) { haptic.impact("light"); toast(t("ratioByPhotoHint"), "info"); return; }
                openSheet(t("aspectRatio"), cfg.ratios.map(function(v){return {value:v,label:v}}),
                    document.getElementById("vm-ratio").textContent, function(v) {
                        document.getElementById("vm-ratio").textContent = v;
                    });
            } else if (id === "vm-qual" && cfg.qualities) {
                openSheet(t(cfg.qualityLabel || "quality"), cfg.qualities.map(function(v){return {value:v,label:v}}),
                    currentVideoQuality, function(v) {
                        currentVideoQuality = v;
                        document.getElementById("vm-qual").textContent = v;
                        updateVideoCost();
                    });
            } else if (id === "vm-orient" && cfg.orientation) {
                var opts = cfg.orientation.map(function(o){ return {value:t(o), label:t(o)} });
                openSheet(t("orientationLabel"), opts, document.getElementById("vm-orient").textContent, function(v) {
                    document.getElementById("vm-orient").textContent = v;
                    updateMotionDuration();
                });
            } else if (id === "vm-dur" && cfg.duration) {
                var secs = [];
                if (cfg.duration.steps) {
                    cfg.duration.steps.forEach(function(s){ secs.push({ value: String(s), label: s + " " + t("sec") }); });
                } else {
                    for (var s = cfg.duration.min; s <= cfg.duration.max; s++) {
                        secs.push({ value: String(s), label: s + " " + t("sec") });
                    }
                }
                openSheet(t("duration"), secs, String(currentVideoDuration), function(v) {
                    currentVideoDuration = parseInt(v);
                    document.getElementById("vm-dur").textContent = v + " " + t("sec");
                    updateVideoCost();
                });
            }
        });
    });
}

// ── Bottom sheet ──
var sheetOnSelect = null;
function openSheet(title, options, current, onSelect) {
    var overlay = document.getElementById("sheet-overlay");
    document.getElementById("sheet-title").textContent = title;
    document.getElementById("sheet-list").innerHTML = options.map(function(o) {
        var active = o.value === current;
        return '<button class="sheet-item' + (active ? ' active' : '') + '" data-val="' + escHtml(o.value) + '">' +
               '<span>' + escHtml(o.label) + '</span>' +
               (active ? '<svg class="sheet-check" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>' : '') +
               '</button>';
    }).join("");
    sheetOnSelect = onSelect;
    overlay.classList.remove("hidden");
    requestAnimationFrame(function(){ overlay.classList.add("show"); });
}
function closeSheet() {
    var overlay = document.getElementById("sheet-overlay");
    overlay.classList.remove("show");
    setTimeout(function(){ overlay.classList.add("hidden"); }, 300);
}
(function setupSheet() {
    var overlay = document.getElementById("sheet-overlay");
    var sheet = document.getElementById("sheet");
    if (!overlay) return;
    overlay.addEventListener("click", function(e){ if (e.target === overlay) closeSheet(); });
    document.getElementById("sheet-list").addEventListener("click", function(e){
        var it = e.target.closest(".sheet-item"); if (!it) return;
        if (sheetOnSelect) sheetOnSelect(it.dataset.val);
        closeSheet();
    });
    // swipe-down to dismiss
    var startY = 0, curY = 0, dragging = false;
    sheet.addEventListener("touchstart", function(e){ startY = e.touches[0].clientY; curY = 0; dragging = true; sheet.style.transition = "none"; }, {passive:true});
    sheet.addEventListener("touchmove", function(e){
        if (!dragging) return;
        curY = e.touches[0].clientY - startY;
        if (curY > 0) sheet.style.transform = "translateY(" + curY + "px)";
    }, {passive:true});
    sheet.addEventListener("touchend", function(){
        dragging = false; sheet.style.transition = "";
        if (curY > 90) { closeSheet(); }
        sheet.style.transform = "";
    });
})();

// ── Text chat (OpenRouter: ChatGPT / Gemini) ──
var CHAT_MODELS_UI = ["ChatGPT", "Gemini", "Grok"];
var chatModel = "ChatGPT";
var chats = [];
var curChatId = null;
var curMessages = [];
var chatBusy = false;
var chatInited = false;

function chatTime(ts){
    var d = ts ? new Date(ts) : new Date();
    if(isNaN(d.getTime())) d = new Date();
    var h = d.getHours(), m = d.getMinutes();
    return (h<10?"0":"")+h+":"+(m<10?"0":"")+m;
}
function chatDayLabel(ts){
    var d = ts ? new Date(ts) : new Date();
    if(isNaN(d.getTime())) return "";
    var now = new Date();
    var sameDay = d.toDateString() === now.toDateString();
    if(sameDay) return chatTime(ts);
    var yest = new Date(now); yest.setDate(now.getDate()-1);
    if(d.toDateString() === yest.toDateString()) return t("chatYesterday");
    return d.toLocaleDateString(currentLang === "ru" ? "ru-RU" : (currentLang==="es"?"es-ES":"en-US"), { day:"2-digit", month:"2-digit" });
}
function chatRenderContent(text){
    var esc = escHtml(text);
    esc = esc.replace(/```([\s\S]*?)```/g, function(_m, c){ return "<pre><code>" + c.replace(/^\n/, "") + "</code></pre>"; });
    esc = esc.replace(/`([^`\n]+)`/g, "<code>$1</code>");
    esc = esc.replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>");
    return esc;
}
function chatScrollBottom(){ window.scrollTo({ top: document.body.scrollHeight }); }
function chatRender(keepScroll){
    var list = document.getElementById("chat-list");
    var empty = document.getElementById("chat-empty");
    if(!list) return;
    if(!curMessages.length){ if(empty) empty.classList.remove("hidden"); list.innerHTML = ""; return; }
    if(empty) empty.classList.add("hidden");
    list.innerHTML = curMessages.map(function(m, i){
        var prev = curMessages[i-1];
        var grouped = prev && prev.role === m.role;   // tighten gap for same-sender runs
        var cls = "msg " + (m.role === "user" ? "user" : "bot") + (grouped ? " grouped" : "") + (m.err ? " err" : "");
        return '<div class="' + cls + '"><div class="msg-c">' + chatRenderContent(m.content) + '</div>' +
               '<span class="msg-t">' + chatTime(m.ts) + '</span></div>';
    }).join("");
    if(!keepScroll) chatScrollBottom();
}
function chatTyping(on){
    var list = document.getElementById("chat-list");
    var tp = document.getElementById("chat-typing");
    if(on){ if(!tp && list){ list.insertAdjacentHTML("beforeend", '<div class="chat-typing" id="chat-typing"><span></span><span></span><span></span></div>'); chatScrollBottom(); } }
    else if(tp){ tp.remove(); }
}
function chatGrow(el){ el.style.height = "auto"; el.style.height = Math.min(120, el.scrollHeight) + "px"; }
function chatModelLabel(){ var n = document.getElementById("chat-model-name"); if(n) n.textContent = chatModel; }
function chatSendEnabled(on){ var b = document.getElementById("chat-send"); if(b) b.disabled = !on; }
function chatNew(){ curChatId = null; curMessages = []; chatRender(); var i = document.getElementById("chat-input"); if(i){ i.value = ""; chatGrow(i); i.focus(); } }
async function chatLoadDialogs(){
    if(!getTgId()) { chats = []; return; }
    try {
        var res = await fetch("/api/chats", { headers: authHeaders() });
        if(res.ok) chats = await res.json();
    } catch(e){ /* keep previous list */ }
}
async function chatOpen(id){
    id = Number(id);
    try {
        var res = await fetch("/api/chats/" + id, { headers: authHeaders() });
        if(!res.ok) return;
        var d = await res.json();
        curChatId = d.id;
        chatModel = d.model || "ChatGPT";
        curMessages = (d.messages || []).map(function(m){ return { role: m.role, content: m.content, ts: m.created_at }; });
        chatModelLabel(); chatRender();
    } catch(e){}
}
async function chatSend(){
    if(chatBusy) return;
    var input = document.getElementById("chat-input");
    if(!input) return;
    var text = input.value.trim();
    if(!text) return;
    curMessages.push({ role: "user", content: text, ts: Date.now() });
    input.value = ""; chatGrow(input);
    chatRender();
    chatBusy = true; chatSendEnabled(false); chatTyping(true); haptic.impact("light");
    try {
        var res = await fetch("/api/chat", { method: "POST", headers: authHeaders({ "Content-Type": "application/json" }), body: JSON.stringify({ model: chatModel, dialog_id: curChatId, messages: curMessages }) });
        var data = await res.json();
        chatTyping(false);
        if(res.status===402) throw new Error(t("errNoBalance"));
        if(!res.ok) throw new Error(data.error || t("chatErrorReq"));
        if(data.balance!=null){ document.querySelectorAll(".user-balance").forEach(function(el){el.textContent=data.balance;}); if(currentUser) currentUser.balance=data.balance; refreshGenButtons(); }
        if(data.dialog_id) curChatId = data.dialog_id;
        if(data.user_at) curMessages[curMessages.length-1].ts = data.user_at;
        curMessages.push({ role: "assistant", content: data.reply || t("chatEmptyReply"), ts: data.assistant_at || Date.now() });
        haptic.notify("success");
        chatLoadDialogs();
    } catch(e){
        chatTyping(false);
        curMessages.push({ role: "assistant", content: (e.message || t("chatError")), ts: Date.now(), err: true });
        haptic.notify("error");
    }
    chatRender();
    chatBusy = false; chatSendEnabled(true);
}
function chatModelSheet(){
    openSheet(t("chooseModel"), CHAT_MODELS_UI.map(function(m){ return { value: m, label: m }; }), chatModel, function(v){ chatModel = v; chatModelLabel(); });
}
async function chatDialogsSheet(){
    await chatLoadDialogs();
    var overlay = document.getElementById("sheet-overlay");
    document.getElementById("sheet-title").textContent = t("chatDialogs");
    var html = '<button class="dlg-new" id="dlg-new"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>' + t("chatNewDialog") + '</button>';
    if(!chats.length){ html += '<p class="muted center" style="padding:6px 0 4px">' + t("chatNoDialogs") + '</p>'; }
    html += chats.map(function(c){
        return '<div class="dlg-item' + (c.id === curChatId ? " active" : "") + '" data-open="' + c.id + '">' +
               '<div class="dlg-main"><div class="dlg-title">' + escHtml(c.title || t("chatDialogDefault")) + '</div><div class="dlg-sub">' + escHtml(c.model || "") + ' · ' + chatDayLabel(c.updated_at) + '</div></div>' +
               '<button class="dlg-del" data-del="' + c.id + '"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg></button>' +
               '</div>';
    }).join("");
    document.getElementById("sheet-list").innerHTML = html;
    sheetOnSelect = null;
    overlay.classList.remove("hidden");
    requestAnimationFrame(function(){ overlay.classList.add("show"); });
}
async function chatDeleteDialog(id){
    id = Number(id);
    try { await fetch("/api/chats/" + id, { method: "DELETE", headers: authHeaders() }); } catch(e){}
    if(curChatId === id) chatNew();
    chats = chats.filter(function(x){ return x.id !== id; });
}
function initChat(){
    chatModelLabel();
    chatRender();
    chatLoadDialogs();
    if(chatInited) return;
    chatInited = true;
    document.getElementById("chat-send").addEventListener("click", chatSend);
    document.getElementById("chat-new").addEventListener("click", chatNew);
    document.getElementById("chat-model").addEventListener("click", chatModelSheet);
    document.getElementById("chat-dialogs").addEventListener("click", chatDialogsSheet);
    var input = document.getElementById("chat-input");
    input.addEventListener("input", function(){ chatGrow(input); });
    input.addEventListener("keydown", function(e){ if(e.key === "Enter" && !e.shiftKey && !IS_MOBILE){ e.preventDefault(); chatSend(); } });
    document.getElementById("sheet-list").addEventListener("click", function(e){
        if(e.target.closest("#dlg-new")){ chatNew(); closeSheet(); return; }
        var del = e.target.closest(".dlg-del");
        if(del){ e.stopPropagation(); chatDeleteDialog(del.dataset.del).then(chatDialogsSheet); return; }
        var it = e.target.closest(".dlg-item");
        if(it){ chatOpen(it.dataset.open); closeSheet(); }
    });
}

// ── Model selection (bottom sheet) ──
function setupModelDD(btnId, ddId, nameId, onChange) {
    var btn = document.getElementById(btnId);
    var dd = document.getElementById(ddId);
    var type = ddId.split("-")[0];
    btn.addEventListener("click", function(){
        var options = Array.prototype.map.call(dd.querySelectorAll(".dd-item"), function(it){
            return { value: it.dataset.model, label: it.dataset.model };
        });
        var activeEl = dd.querySelector(".dd-item.active");
        var current = activeEl ? activeEl.dataset.model : null;
        openSheet(t("chooseModel"), options, current, function(model){
            selectModel(type, model);
            if (onChange) onChange(model);
        });
    });
}
setupModelDD("photo-model-btn","photo-model-dd","photo-model-name");
setupModelDD("video-model-btn","video-model-dd","video-model-name", function(model) {
    renderVideoSettings(model);
});
setupModelDD("audio-model-btn","audio-model-dd","audio-model-name");
// audio style picker (bottom sheet, same UX as model/ratio pickers)
var AUDIO_STYLES=[
{v:"Energetic pop song with catchy melody, upbeat rhythm, feel-good summer vibes",l:"Pop — летний, энергичный"},
{v:"Atmospheric lo-fi hip-hop beat, mellow piano, warm vinyl crackle, relaxing study mood",l:"Lo-Fi — спокойный, для учёбы"},
{v:"Epic cinematic orchestral soundtrack, dramatic strings, powerful brass, heroic theme",l:"Кинематограф — эпичный оркестр"},
{v:"Smooth R&B with soulful vocals, groovy bassline, romantic night atmosphere",l:"R&B — романтичный, soulful"},
{v:"Hard-hitting trap beat, heavy 808 bass, dark hi-hats, aggressive energy",l:"Trap — агрессивный бит"},
{v:"Acoustic folk ballad, warm guitar fingerpicking, gentle vocals, nostalgic autumn mood",l:"Folk — тёплая акустика"},
{v:"Deep house electronic track, pulsing bassline, dreamy synth pads, club atmosphere",l:"Deep House — клубный, глубокий"},
{v:"Chill indie dream-pop, reverb guitars, soft ethereal vocals, pastel sunset aesthetic",l:"Indie — мечтательный dream-pop"},
{v:"Powerful rock anthem, distorted electric guitars, driving drums, stadium energy",l:"Рок — мощный, стадионный"},
{v:"Jazzy bossa nova, smooth saxophone, gentle rhythm guitar, evening café ambiance",l:"Jazz — вечерний bossa nova"},
{v:"Dark synthwave, retro 80s synths, neon-lit atmosphere, cyberpunk driving beat",l:"Synthwave — ретро 80-х, неон"},
{v:"Classical piano sonata, elegant and emotional, flowing arpeggios, concert hall resonance",l:"Классика — фортепианная соната"},
{v:"Reggaeton party track, Latin rhythm, infectious dembow beat, tropical summer club",l:"Reggaeton — латинский, танцевальный"},
{v:"Ambient meditation music, soft drones, nature textures, peaceful and calming atmosphere",l:"Ambient — медитативный, спокойный"},
{v:"Funky disco groove, slap bass, wah guitar, retro 70s dance floor energy",l:"Disco/Funk — танцевальный ретро"}
];
(function(){
var btn=document.getElementById("audio-style-btn");
if(!btn) return;
btn.addEventListener("click",function(){
    var cur=document.getElementById("prompt-audio").value;
    openSheet(t("audioStyleLabel"),AUDIO_STYLES.map(function(s){return{value:s.v,label:s.l}}),cur,function(val){
        document.getElementById("prompt-audio").value=val;
        var s=AUDIO_STYLES.find(function(x){return x.v===val;});
        document.getElementById("audio-style-val").textContent=s?s.l:val;
    });
});
})();
// initial model logos
applyModelLogo("photo-model-ico","NanoBanana PRO");
applyModelLogo("video-model-ico","Kling 3.0");
applyModelLogo("audio-model-ico","Suno V5.5");
updateAudioCost();

// close dropdowns on outside click
document.addEventListener("click",function(e){
    ["photo-model-dd","video-model-dd","audio-model-dd","lang-dd"].forEach(function(id){
        var dd=document.getElementById(id);
        if(!dd) return;
        var btn=dd.previousElementSibling || document.getElementById(id.replace("-dd","-btn"));
        if(!dd.classList.contains("hidden") && !dd.contains(e.target)){
            var trigger = dd.parentElement.querySelector(".model-change") || document.getElementById("lang-btn");
            if(!trigger || !trigger.contains(e.target)) dd.classList.add("hidden");
        }
    });
});

// ── Count buttons ──
document.querySelectorAll(".cnt").forEach(function(b){
    b.addEventListener("click",function(){
        document.querySelectorAll(".cnt").forEach(function(x){x.classList.remove("active")}); b.classList.add("active");
        updatePhotoCost();
    });
});
updatePhotoCost();

// ── Photo ratio/quality pickers (bottom sheet) ──
document.querySelectorAll("#form-image .set-pick").forEach(function(b){
    b.addEventListener("click",function(){
        var tid=b.dataset.sheet;
        if(tid==="p-ratio"){
            openSheet(t("aspectRatio"), ratios.map(function(r){return {value:r,label:r}}), ratios[state.pRatio], function(v){
                state.pRatio=ratios.indexOf(v); document.getElementById("p-ratio").textContent=v;
            });
        } else if(tid==="p-qual"){
            openSheet(t("quality"), quals.map(function(q){return {value:q,label:q}}), quals[state.pQual], function(v){
                state.pQual=quals.indexOf(v); document.getElementById("p-qual").textContent=v;
            });
        }
    });
});

// ── Audio form: mode + content toggles ──
document.querySelectorAll("#audio-mode .pill").forEach(function(b){
    b.addEventListener("click",function(){
        document.querySelectorAll("#audio-mode .pill").forEach(function(x){x.classList.remove("active")}); b.classList.add("active");
        document.getElementById("audio-lyrics-wrap").classList.toggle("hidden", b.dataset.v!=="custom");
    });
});
document.querySelectorAll("#audio-content .pill").forEach(function(b){
    b.addEventListener("click",function(){
        document.querySelectorAll("#audio-content .pill").forEach(function(x){x.classList.remove("active")}); b.classList.add("active");
    });
});


// ── Language dropdown ──
document.getElementById("lang-btn").addEventListener("click",function(){
    document.getElementById("lang-dd").classList.toggle("hidden");
});
document.querySelectorAll(".lang-item").forEach(function(it){
    it.addEventListener("click",function(){
        document.querySelectorAll(".lang-item").forEach(function(x){x.classList.remove("active")});
        it.classList.add("active");
        document.getElementById("lang-dd").classList.add("hidden");
        applyLang(it.dataset.lang);
    });
});

// ── Partner / referral cabinet ──
var BOT_USERNAME = "promptW_bot";
var partnerData = { balance: 0, total: 0 };

function referralLink(){
    var id = getTgId();
    return "https://t.me/" + BOT_USERNAME + (id ? ("?start=r" + id) : "");
}

async function loadPartner(){
    var linkEl = document.getElementById("paff-link");
    if (linkEl) linkEl.value = referralLink();
    var tgId = getTgId(); if (!tgId) return;
    try {
        var res = await fetch("/api/user/" + tgId + "/referrals", { headers: authHeaders() });
        if (!res.ok) return;
        var d = await res.json();
        // Authoritative id from the server (it only 200s with valid initData) —
        // guarantees a personalized link even if the client's initDataUnsafe is empty.
        if (d.tg_id) { _tgIdCache = d.tg_id; if (linkEl) linkEl.value = "https://t.me/" + BOT_USERNAME + "?start=r" + d.tg_id; }
        partnerData.balance = d.balance || 0;
        partnerData.total = d.total_earned || 0;
        setText("paff-bal", fmtRub(partnerData.balance));
        setText("paff-total", fmtRub(partnerData.total));
        var grid = {d: "ref-d", w: "ref-w", m: "ref-m", a: "ref-a"};
        var per = {d: d.day, w: d.week, m: d.month, a: d.all};
        Object.keys(grid).forEach(function(k){
            var p = per[k] || {people: 0, earned: 0};
            setText(grid[k] + "-people", p.people || 0);
            setText(grid[k] + "-earn", fmtRub(p.earned) + " ₽");
        });
        var c1 = d.line1 || 0, c2 = d.line2 || 0;
        var l1 = document.querySelector('.rtab[data-rt="l1"]'), l2 = document.querySelector('.rtab[data-rt="l2"]');
        if (l1) l1.textContent = t("line1Tab").replace(/\(\d+\)/, "(" + c1 + ")");
        if (l2) l2.textContent = t("line2Tab").replace(/\(\d+\)/, "(" + c2 + ")");
    } catch(e){}
}
function setText(id, v){ var e = document.getElementById(id); if (e) e.textContent = v; }
function fmtRub(v){ var n = Math.round((parseFloat(v) || 0) * 100) / 100; return (n % 1 === 0) ? String(n) : n.toFixed(2); }

// referral link: copy + invite via Telegram share
var copyBtn = document.getElementById("paff-copy-btn");
if (copyBtn) copyBtn.addEventListener("click", function(){
    var link = referralLink();
    if (navigator.clipboard) navigator.clipboard.writeText(link).then(function(){ toast(t("copied"), "success"); }).catch(function(){});
    else { var el = document.getElementById("paff-link"); el.select(); try{ document.execCommand("copy"); toast(t("copied"),"success"); }catch(e){} }
});
var inviteBtn = document.getElementById("paff-invite-btn");
if (inviteBtn) inviteBtn.addEventListener("click", function(){
    var url = "https://t.me/share/url?url=" + encodeURIComponent(referralLink()) + "&text=" + encodeURIComponent(t("inviteShareText"));
    if (tg && tg.openTelegramLink) tg.openTelegramLink(url);
    else window.open(url, "_blank");
});

// ── Referral tabs ──
document.querySelectorAll(".rtab").forEach(function(tab){
    tab.addEventListener("click",function(){
        document.querySelectorAll(".rtab").forEach(function(x){x.classList.remove("active")}); tab.classList.add("active");
        var msgKeys = {tx:"refMsgTx",l1:"refMsgL1",l2:"refMsgL2"};
        document.querySelector(".ref-content").innerHTML = '<p class="muted center">' + t(msgKeys[tab.dataset.rt]) + '</p>';
    });
});

// ── Withdrawal sheet ──
var WD_MIN = { card: 1000, crypto: 500 };
var wdMethod = "card";
function wdOpen(){
    var ov = document.getElementById("wd-overlay"); if (!ov) return;
    setText("wd-avail", fmtRub(partnerData.balance));
    document.getElementById("wd-amount").value = "";
    document.getElementById("wd-details").value = "";
    wdSetMethod("card");
    wdValidate();
    ov.classList.remove("hidden");
}
function wdClose(){ var ov = document.getElementById("wd-overlay"); if (ov) ov.classList.add("hidden"); }
function wdSetMethod(m){
    wdMethod = m;
    document.querySelectorAll(".wd-method").forEach(function(b){ b.classList.toggle("active", b.dataset.wm === m); });
    var lbl = document.getElementById("wd-details-lbl"), inp = document.getElementById("wd-details");
    if (m === "card"){ lbl.textContent = t("wmCardField"); inp.placeholder = t("wmCardPh"); }
    else { lbl.textContent = t("wmCryptoField"); inp.placeholder = t("wmCryptoPh"); }
    wdValidate();
}
function wdValidate(){
    var note = document.getElementById("wd-note");
    var btn = document.getElementById("wd-submit");
    var amt = parseInt(document.getElementById("wd-amount").value, 10) || 0;
    var min = WD_MIN[wdMethod];
    note.className = "wd-note";
    if (partnerData.balance < min){ note.textContent = t("wdNeedMin").replace("{min}", min); btn.disabled = true; return; }
    if (amt > 0 && amt < min){ note.className = "wd-note err"; note.textContent = t("wdMinAmt").replace("{min}", min); btn.disabled = true; return; }
    if (amt > partnerData.balance){ note.className = "wd-note err"; note.textContent = t("wdTooMuch"); btn.disabled = true; return; }
    note.textContent = t("wdMinHint").replace("{min}", min);
    btn.disabled = !(amt >= min);
}
(function(){
    var wb = document.getElementById("paff-withdraw-btn"); if (wb) wb.addEventListener("click", wdOpen);
    var wc = document.getElementById("wd-close"); if (wc) wc.addEventListener("click", wdClose);
    var ov = document.getElementById("wd-overlay"); if (ov) ov.addEventListener("click", function(e){ if (e.target === ov) wdClose(); });
    document.querySelectorAll(".wd-method").forEach(function(b){ b.addEventListener("click", function(){ wdSetMethod(b.dataset.wm); }); });
    var amtIn = document.getElementById("wd-amount"); if (amtIn) amtIn.addEventListener("input", wdValidate);
    var mx = document.getElementById("wd-max"); if (mx) mx.addEventListener("click", function(){ document.getElementById("wd-amount").value = Math.floor(partnerData.balance); wdValidate(); });
    var sb = document.getElementById("wd-submit"); if (sb) sb.addEventListener("click", async function(){
        var amt = parseInt(document.getElementById("wd-amount").value, 10) || 0;
        var details = document.getElementById("wd-details").value.trim();
        var note = document.getElementById("wd-note");
        if (amt < WD_MIN[wdMethod]) return;
        if (!details){ note.className = "wd-note err"; note.textContent = t("wdNeedDetails"); return; }
        sb.disabled = true;
        try {
            var res = await fetch("/api/withdraw", {
                method: "POST",
                headers: authHeaders({"Content-Type": "application/json"}),
                body: JSON.stringify({ method: wdMethod, details: details, amount: amt })
            });
            var d = await res.json().catch(function(){ return {}; });
            if (res.ok && d.ok){
                haptic.notify("success");
                toast(t("wdRequested"), "success");
                partnerData.balance = Math.max(0, partnerData.balance - amt);
                wdClose();
                loadPartner();
            } else if (res.status === 402){
                note.className = "wd-note err"; note.textContent = t("wdTooMuch");
            } else if (res.status === 400 && d.min){
                note.className = "wd-note err"; note.textContent = t("wdMinAmt").replace("{min}", d.min);
            } else if (res.status === 409){
                note.className = "wd-note err"; note.textContent = t("wdPending");
            } else {
                note.className = "wd-note err"; note.textContent = t("wdError");
            }
        } catch(e){ note.className = "wd-note err"; note.textContent = t("wdError"); }
        sb.disabled = false;
    });
})();

// ── Generate ──
function getActiveModel(type) {
    var ddId = type + "-model-dd";
    var dd = document.getElementById(ddId);
    if (!dd) return null;
    var active = dd.querySelector(".dd-item.active");
    return active ? active.dataset.model : null;
}

function getSettings(type) {
    if (type === "image") {
        return { ratio: ratios[state.pRatio], quality: quals[state.pQual],
                 count: document.querySelector(".cnt.active") ? parseInt(document.querySelector(".cnt.active").textContent) : 1 };
    }
    if (type === "audio") {
        var modeEl = document.querySelector("#audio-mode .pill.active");
        var contentEl = document.querySelector("#audio-content .pill.active");
        var custom = modeEl && modeEl.dataset.v === "custom";
        var s = { custom_mode: !!custom, instrumental: !!(contentEl && contentEl.dataset.v === "instrumental") };
        if (custom) { var ly = document.getElementById("audio-lyrics"); if (ly && ly.value.trim()) s.lyrics = ly.value.trim(); }
        return s;
    }
    if (type === "video") {
        var cfg = VIDEO_MODELS[currentVideoModel] || {};
        var s = {};
        var ratioEl = document.getElementById("vm-ratio");
        if (ratioEl && ratioEl.textContent !== "Auto" && ratioEl.textContent !== t("ratioByPhoto")) s.ratio = ratioEl.textContent;
        var qualEl = document.getElementById("vm-qual");
        if (qualEl) s.quality = qualEl.textContent;
        if (currentVideoDuration && !cfg.fixedSeconds) s.duration = currentVideoDuration;
        var soundEl = document.getElementById("vm-sound");
        if (soundEl) s.sound = soundEl.checked;
        var modeBtn = document.querySelector(".vm-mode .pill.active");
        if (modeBtn) s.mode = modeBtn.dataset.v;
        var orientEl = document.getElementById("vm-orient");
        if (orientEl && cfg.orientation) {
            var labels = cfg.orientation.map(function(o) { return t(o); });
            var idx = labels.indexOf(orientEl.textContent);
            s.orientation = cfg.orientation[idx >= 0 ? idx : 0];
        }
        return s;
    }
    return {};
}

function buildFormData(type, prompt) {
    var fd = new FormData();
    fd.append("prompt", prompt || "generated content");
    fd.append("tg_id", getTgId() || "");
    fd.append("model", getActiveModel(type === "image" ? "photo" : type) || "");
    fd.append("settings", JSON.stringify(getSettings(type)));
    Object.keys(uploadedFiles).forEach(function(uid) {
        var val = uploadedFiles[uid];
        if (Array.isArray(val)) {
            val.forEach(function(f) { fd.append(uid, f); });
        } else if (val instanceof File) {
            fd.append(uid, val);
        }
    });
    return fd;
}

// ── Generation result overlay ──
var genViewOpen = false;     // is the result overlay currently shown
var genLastReq = null;       // {type, prompt} for the "again" button
var genRepeatFn = null;      // unified "Повторить" handler (Create runGenerate OR template tplGenerate)

function genOvOpen(){
    var o=document.getElementById("gen-overlay");
    var hdr=document.getElementById("main-header");
    var nav=document.getElementById("bnav");
    var hb=hdr?Math.round(hdr.getBoundingClientRect().bottom):56;
    var nh=nav?Math.round(nav.getBoundingClientRect().height):60;
    // Cover the full column (incl. BEHIND the frosted header, which is a higher z-index) so the
    // page never bleeds through the translucent header; push the overlay's own content below it.
    o.style.top="0px";
    o.style.bottom=nh+"px";
    var card=o.querySelector(".gen-ov-card");
    if(card) card.style.paddingTop=hb+"px";
    o.classList.remove("hidden");
    requestAnimationFrame(function(){ o.classList.add("show"); });
    document.body.classList.add("noscroll");
    genViewOpen=true;
}
function genOvClose(){
    var o=document.getElementById("gen-overlay");
    o.classList.remove("show");
    setTimeout(function(){ o.classList.add("hidden"); },200);
    document.body.classList.remove("noscroll");
    genViewOpen=false;
}
function genOvLoading(type, model){
    document.getElementById("gen-ov-title").textContent=t("generating");
    var waitKey = type==="video"?"genWaitVideo":(type==="audio"?"genWaitAudio":"genWaitPhoto");
    var note = (type==="video" ? '<p class="gen-load-note">'+t("genVideoTime")+'</p>' : '')+
               '<p class="gen-load-note gen-bg-note"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="16" x2="12" y2="12"/><line x1="12" y1="8" x2="12.01" y2="8"/></svg><span>'+t("genBgNote")+'</span></p>';
    document.getElementById("gen-ov-body").innerHTML=
        '<div class="gen-load"><div class="gen-spin"></div>'+
        '<p class="gen-load-t">'+t(waitKey)+'</p>'+
        '<p class="gen-load-s">'+escHtml(model||"")+'</p>'+note+'</div>';
    document.getElementById("gen-ov-actions").innerHTML="";
}
// ── Custom audio player (the native <audio controls> ignores the dark theme) ──
function apFmt(s){
    if(!isFinite(s) || s<0) s=0;
    var m=Math.floor(s/60), sec=Math.floor(s%60);
    return m+":"+(sec<10?"0":"")+sec;
}
function audioPlayerHtml(url){
    return '<div class="aplayer">'+
        '<button type="button" class="aplayer-play" aria-label="play">'+
            '<svg class="ap-ico-play" viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg>'+
            '<svg class="ap-ico-pause" viewBox="0 0 24 24" fill="currentColor"><path d="M6 5h3.5v14H6zM14.5 5H18v14h-3.5z"/></svg>'+
        '</button>'+
        '<span class="aplayer-cur">0:00</span>'+
        '<div class="aplayer-seek"><div class="aplayer-fill"></div><div class="aplayer-knob"></div></div>'+
        '<span class="aplayer-dur">0:00</span>'+
        '<audio src="'+escHtml(url)+'" preload="metadata"></audio>'+
    '</div>';
}
function bindAudioPlayers(root){
    (root||document).querySelectorAll(".aplayer").forEach(function(p){
        if(p.dataset.bound) return; p.dataset.bound="1";
        var audio=p.querySelector("audio"), btn=p.querySelector(".aplayer-play"),
            seek=p.querySelector(".aplayer-seek"), fill=p.querySelector(".aplayer-fill"),
            knob=p.querySelector(".aplayer-knob"), cur=p.querySelector(".aplayer-cur"),
            dur=p.querySelector(".aplayer-dur");
        function paint(){
            var d=audio.duration||0, c=audio.currentTime||0, r=d?Math.min(1,c/d):0;
            fill.style.width=(r*100)+"%"; knob.style.left=(r*100)+"%"; cur.textContent=apFmt(c);
        }
        audio.addEventListener("loadedmetadata",function(){ dur.textContent=apFmt(audio.duration); paint(); });
        audio.addEventListener("timeupdate",paint);
        audio.addEventListener("play",function(){
            document.querySelectorAll(".aplayer audio").forEach(function(a){ if(a!==audio) a.pause(); });
            p.classList.add("playing");
        });
        audio.addEventListener("pause",function(){ p.classList.remove("playing"); });
        audio.addEventListener("ended",function(){ p.classList.remove("playing"); audio.currentTime=0; paint(); });
        btn.addEventListener("click",function(){ if(audio.paused) audio.play(); else audio.pause(); haptic.impact("light"); });
        var seeking=false;
        function seekTo(x){
            var rect=seek.getBoundingClientRect();
            var r=Math.min(1,Math.max(0,(x-rect.left)/rect.width));
            if(audio.duration){ audio.currentTime=r*audio.duration; paint(); }
        }
        seek.addEventListener("pointerdown",function(e){ seeking=true; try{seek.setPointerCapture(e.pointerId);}catch(_){} seekTo(e.clientX); });
        seek.addEventListener("pointermove",function(e){ if(seeking) seekTo(e.clientX); });
        seek.addEventListener("pointerup",function(e){ seeking=false; try{seek.releasePointerCapture(e.pointerId);}catch(_){} });
        seek.addEventListener("pointercancel",function(){ seeking=false; });
    });
}
function genOvResult(mtype, data){
    var html;
    if(mtype==="photo"){
        var urls=(data.file_urls&&data.file_urls.length)?data.file_urls:[data.file_url];
        html = urls.length>1
            ? '<div class="gen-res-grid">'+urls.map(function(u){return '<img src="'+escHtml(u)+'" alt="result">';}).join("")+'</div>'
            : '<div class="gen-res-media"><img src="'+escHtml(urls[0])+'" alt="result"></div>';
    } else if(mtype==="audio"){
        html='<div class="gen-res-media gen-res-audio">'+audioPlayerHtml(data.file_url)+'</div>';
    } else {
        html='<div class="gen-res-media"><video src="'+escHtml(data.file_url)+'" controls autoplay></video></div>';
    }
    document.getElementById("gen-ov-title").textContent=t("genDone");
    document.getElementById("gen-ov-body").innerHTML=html;
    bindAudioPlayers(document.getElementById("gen-ov-body"));
    document.getElementById("gen-ov-actions").innerHTML=
        '<button class="gen-ov-again" id="gen-ov-again"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>'+t("detailRepeat")+'</button>'+
        '<button class="gen-ov-save" id="gen-ov-save">'+t("detailSave")+'</button>';
    var saveUrl=(data.file_urls&&data.file_urls.length)?data.file_urls[0]:data.file_url;
    document.getElementById("gen-ov-save").addEventListener("click",function(){
        var b=this, tgId=getTgId(); if(!tgId) return;
        b.disabled=true; b.textContent="...";
        fetch("/api/send-media",{method:"POST",headers:authHeaders({"Content-Type":"application/json"}),body:JSON.stringify({tg_id:tgId,file_url:saveUrl,media_type:(mtype==="photo"?"photo":mtype)})})
          .then(function(r){return r.json()}).then(function(d){ b.disabled=false; b.textContent=d.ok?("✓ "+t("savedToChat")):t("detailSave"); if(d.ok) haptic.notify("success"); })
          .catch(function(){ b.disabled=false; b.textContent=t("detailSave"); });
    });
    document.getElementById("gen-ov-again").addEventListener("click",function(){ if(genRepeatFn) genRepeatFn(); });
}
function genOvError(msg){
    document.getElementById("gen-ov-title").textContent=t("genFailed");
    document.getElementById("gen-ov-body").innerHTML='<div class="gen-load"><div class="gen-err-ic"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg></div><p class="gen-load-t">'+escHtml(msg||t("genFailed"))+'</p></div>';
    document.getElementById("gen-ov-actions").innerHTML='<button class="gen-ov-again" id="gen-ov-again"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/></svg>'+t("detailRepeat")+'</button>';
    document.getElementById("gen-ov-again").addEventListener("click",function(){ if(genRepeatFn) genRepeatFn(); });
}

async function runGenerate(type, prompt){
    genLastReq={ type:type, prompt:prompt };
    genRepeatFn=function(){ runGenerate(type, prompt); };
    var model=getActiveModel(type==="image"?"photo":type)||"";
    var settings=getSettings(type);
    genOvOpen(); genOvLoading(type, model);
    haptic.impact("medium");
    var apiType=type==="image"?"image":type;
    var pendType=type==="image"?"photo":type;
    var pendId=addPendingGen(pendType, model);
    var genCost=_genCost(type);
    applyBalanceDelta(-genCost);   // reserve in the UI immediately (server already charged at start)
    try {
        var fd=buildFormData(type, prompt);
        var res=await fetch("/api/generate/"+apiType, { method:"POST", headers:authHeaders(), body:fd });
        var data=await res.json();
        if(res.status===402){ applyBalanceDelta(genCost); removePendingGen(pendId); updateHistory(); genOvClose(); toast(t("errNoBalance"),"error"); haptic.notify("error"); showPage("topup"); return; }
        if(res.status===202 || (data && data.pending)){
            // Server kept the job running (foreground poll timed out / app was backgrounded
            // mid-request). It's NOT a failure — the reconciler delivers it to History + TG.
            // Keep the charge (still running), drop the local card, let the server row drive.
            if(getTgId()){ loadUserHistory(); pendingPoll(); }
            removePendingGen(pendId);
            updateHistory();
            if(genViewOpen) genOvClose();
            toast(t("genStillRunning"),"info");
            return;
        }
        if(!res.ok) throw new Error(data.error||"Generation failed");
        if(data.balance!=null){ document.querySelectorAll(".user-balance").forEach(function(el){el.textContent=data.balance;}); if(currentUser) currentUser.balance=data.balance; refreshGenButtons(); }
        var mtype=data.media_type==="photo"?"photo":(data.media_type==="audio"?"audio":"video");
        function makeItem(url){ return { url:url, type:mtype, prompt:prompt, model:model, settings:settings, cost:(data.cost||0), created_at:new Date().toISOString() }; }
        if(mtype==="photo"){
            var urls=(data.file_urls&&data.file_urls.length)?data.file_urls:[data.file_url];
            urls.slice().reverse().forEach(function(u){ galleryItems.unshift(makeItem(u)); });
        } else {
            galleryItems.unshift(makeItem(data.file_url));
        }
        removePendingGen(pendId);
        updateHistory();
        if(getTgId()) loadUserHistory();   // reconcile with the server (clears its pending row)
        haptic.notify("success");
        if(genViewOpen) genOvResult(mtype, data);
        else toast(t("genSavedToHistory"),"success");
        // Face-verify: gentle, brand-voice nudge when similarity stayed low after retries.
        if(data.face_tip) setTimeout(function(){ toast(t(data.face_tip),"info"); }, 600);
    } catch(err){
        // Transport error (not a server failure): the webview was backgrounded / lost
        // connection mid-request — iOS Safari throws TypeError "Load failed". The job is
        // still running server-side (charged, task_id persisted, sweep delivers it to
        // History + Telegram), so DON'T refund or show the scary error. Reconcile instead.
        var msg = (err && err.message) || "";
        var transient = (err instanceof TypeError) || /load failed|failed to fetch|network/i.test(msg);
        if(transient){
            if(getTgId()){ loadUserHistory(); pendingPoll(); }   // server pending row drives the card
            removePendingGen(pendId);   // drop the local optimistic card; server state takes over
            updateHistory();
            if(genViewOpen) genOvClose();
            toast(t("genStillRunning"),"info");
            return;
        }
        applyBalanceDelta(genCost);   // real failure — server refunds, restore the UI
        removePendingGen(pendId);
        updateHistory();
        haptic.notify("error");
        if(genViewOpen) genOvError(err.message);
        else toast(t("genFailed"),"error");
    }
}

function bindGen(btnId, promptId, type) {
    var btn = document.getElementById(btnId);
    if(!btn) return;
    btn.addEventListener("click", function(){
        // Not enough tokens → take the user straight to top-up.
        if(btn.classList.contains("topup-needed")){ haptic.impact("light"); showPage("topup"); return; }
        var promptEl = document.getElementById(promptId);
        var prompt = promptEl ? promptEl.value.trim() : "";
        if(!prompt && type !== "video" && type !== "audio"){ toast(t("alertPrompt"),"error"); return; }
        if(!prompt && type === "audio" && AUDIO_STYLES.length){ prompt = AUDIO_STYLES[0].v; document.getElementById("prompt-audio").value = prompt; }
        runGenerate(type, prompt);
    });
}
bindGen("gen-photo","prompt-photo","image");
bindGen("gen-video","prompt-video","video");
bindGen("gen-audio","prompt-audio","audio");
(function(){
    var c=document.getElementById("gen-ov-close");
    if(c) c.addEventListener("click", genOvClose);
})();

// Keyboard handling: when a text field is focused the on-screen keyboard opens and iOS
// repositions the fixed bottom nav ABOVE the keyboard, where it overlaps the input the
// user is typing into. Hide the bottom nav (body.kb-open) while any text field is focused.
(function(){
    function isTextField(el){
        if(!el) return false;
        if(el.tagName==="TEXTAREA" || el.isContentEditable) return true;
        if(el.tagName==="INPUT"){
            var ty=(el.getAttribute("type")||"text").toLowerCase();
            return ["text","search","url","email","tel","number","password",""].indexOf(ty)>=0;
        }
        return false;
    }
    document.addEventListener("focusin", function(e){
        if(isTextField(e.target)) document.body.classList.add("kb-open");
    });
    document.addEventListener("focusout", function(){
        // Defer: moving focus between two text fields fires focusout→focusin; only clear
        // once focus has actually left every text field (keyboard dismissed).
        setTimeout(function(){
            if(!isTextField(document.activeElement)) document.body.classList.remove("kb-open");
        }, 60);
    });
})();

// In-progress generations shown as animated placeholder cards at the top of History.
// Two sources, merged so a single job never shows twice:
//  - serverPending: rows with status 'pending' from /history (authoritative; survive a
//    reload; we ignore ones older than ~15 min so a crashed job can't spin forever).
//  - localPending: optimistic cards added the instant a request fires, for immediate
//    feedback in the firing tab before the server row is fetched.
var pendingGens=[], serverPending=[], localPending=[], pendingSeq=0, pendingPollT=null;
var PENDING_MAX_AGE_MS=15*60*1000;
function recomputePending(){
    // Server rows win; add only the local cards a server row doesn't already cover (by type).
    var left={}; serverPending.forEach(function(p){ left[p.type]=(left[p.type]||0)+1; });
    var extras=[]; localPending.forEach(function(p){ if(left[p.type]>0){ left[p.type]--; } else extras.push(p); });
    pendingGens=serverPending.concat(extras);
}
function addPendingGen(type, model){ var id=++pendingSeq; localPending.push({id:id,type:type,model:model||""}); recomputePending(); updateHistory(); return id; }
function removePendingGen(id){ localPending=localPending.filter(function(p){return p.id!==id;}); recomputePending(); }
function pendingPoll(){
    if(pendingPollT) return;
    pendingPollT=setInterval(function(){
        if(!serverPending.length){ clearInterval(pendingPollT); pendingPollT=null; return; }
        loadUserHistory();
    }, 5000);
}
function pendingGenHtml(p){
    var tag='<span class="gal-tag gal-tag-'+p.type+'">'+t(p.type)+'</span>';
    var meta='<div class="gal-meta"><span class="gal-model">'+escHtml(p.model)+'</span></div>';
    return '<div class="gal-item gal-pending gal-pending-'+p.type+'">'+tag+
        '<div class="gal-gen"><div class="gal-gen-spin"></div><span class="gal-gen-t">'+t("genInProgress")+'</span></div>'+
        meta+'</div>';
}

function updateHistory(){
    var list=document.getElementById("history-list");
    if(!galleryItems.length && !pendingGens.length){list.innerHTML='<div class="empty-state"><div class="empty-ico"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg></div><p class="empty-title">' + t("historyEmpty") + '</p></div>';return}
    var pendingHtml=pendingGens.map(pendingGenHtml).join("");
    list.innerHTML=pendingHtml+galleryItems.map(function(item, idx){
        var media = item.type==="photo"
            ? '<img src="'+escHtml(item.url)+'" alt="g">'
            : item.type==="audio"
            ? '<div class="gal-audio"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg></div>'
            // Static mid-frame preview captured once into an <img>. A live <video>
            // flickered because updateHistory() rebuilds innerHTML on every poll/reconcile,
            // reloading each element from black; a cached image re-renders instantly.
            : (videoThumbs[item.url]
                ? '<img class="gal-vthumb" src="'+videoThumbs[item.url]+'" alt="v">'
                : '<img class="gal-vthumb gal-vthumb-load" data-vthumb="'+escHtml(item.url)+'" alt="v">')
              + '<div class="gal-play"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M8 5v14l11-7z"/></svg></div>';
        var cost = item.cost ? '<span class="gal-cost">'+item.cost+' W</span>' : '';
        var meta = '<div class="gal-meta"><span class="gal-model">'+escHtml(item.model||"")+'</span>'+cost+'</div>';
        var tagKey = item.type==="photo"?"photo":(item.type==="audio"?"audio":"video");
        var tag = '<span class="gal-tag gal-tag-'+tagKey+'">'+t(tagKey)+'</span>';
        return '<div class="gal-item" data-idx="'+idx+'">'+tag+media+meta+'</div>';
    }).join("")
    + (historyHasMore ? '<button class="gal-more" id="hist-more">'+t("loadMore")+'</button>' : "");
    list.querySelectorAll(".gal-item[data-idx]").forEach(function(el){
        el.addEventListener("click",function(){ showGenDetail(galleryItems[parseInt(el.dataset.idx)]); });
    });
    list.querySelectorAll("img[data-vthumb]").forEach(function(img){ makeVideoThumb(img.getAttribute("data-vthumb")); });
    var more=document.getElementById("hist-more");
    if(more) more.addEventListener("click",function(){
        more.disabled=true; more.textContent=t("loading");
        historyLimit+=HISTORY_PAGE;
        loadUserHistory();
    });
}

// Capture a static mid-video frame once and cache it as a dataURL. Media is same-origin
// (/media/...), so the canvas isn't tainted and toDataURL works (incl. iOS WebView, which
// needs muted+playsinline and a real seek before a frame can be drawn).
function makeVideoThumb(url){
    if(videoThumbs[url] || videoThumbPending[url]) return;
    videoThumbPending[url]=true;
    var v=document.createElement("video");
    // preload="metadata" (not "auto"): don't eagerly download the whole clip — we only
    // need the very start. Combined with an early-frame seek below, the browser fetches
    // just the file head (a small range) instead of half the video → near-instant thumb.
    v.muted=true; v.playsInline=true; v.preload="metadata"; v.crossOrigin="anonymous"; v.src=mediaBlobUrl(url);
    var done=false;
    function finish(){ if(!done){ done=true; delete videoThumbPending[url]; } }
    v.addEventListener("loadedmetadata",function(){
        var d=v.duration;
        // Near the first frame, not the middle: seeking to ~0.15s only needs the file head,
        // not half the download. For image-to-video the first frame is the source photo, so
        // it's a great (and far faster) thumbnail.
        var t=(isFinite(d)&&d>0) ? Math.min(0.15, d-0.05) : 0.1;
        try{ v.currentTime=t; }catch(e){ finish(); }
    });
    v.addEventListener("seeked",function(){
        if(done) return;
        try{
            var c=document.createElement("canvas");
            c.width=v.videoWidth||320; c.height=v.videoHeight||320;
            c.getContext("2d").drawImage(v,0,0,c.width,c.height);
            videoThumbs[url]=c.toDataURL("image/jpeg",0.72);
            finish();
            // Patch any currently-rendered placeholders in place (no full re-render → no flicker).
            document.querySelectorAll('img[data-vthumb]').forEach(function(img){
                if(img.getAttribute("data-vthumb")===url){
                    img.src=videoThumbs[url];
                    img.classList.remove("gal-vthumb-load");
                    img.removeAttribute("data-vthumb");
                }
            });
        }catch(e){ finish(); }
    });
    v.addEventListener("error",finish);
    setTimeout(finish, 9000); // give up so a bad URL never wedges the pending flag
}

// ── Helpers ──
function escHtml(s){var d=document.createElement("div");d.textContent=s;return d.innerHTML}

function formatDetailDate(iso){
    if(!iso)return "—";
    var d=new Date(iso);
    var months=["янв","фев","мар","апр","май","июн","июл","авг","сен","окт","ноя","дек"];
    if(currentLang==="en") months=["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"];
    if(currentLang==="es") months=["ene","feb","mar","abr","may","jun","jul","ago","sep","oct","nov","dic"];
    return d.getDate()+" "+months[d.getMonth()]+" "+d.getFullYear()+", "+String(d.getHours()).padStart(2,"0")+":"+String(d.getMinutes()).padStart(2,"0");
}

// ── Gen Detail ──
function detailCell(label, value, wide, valCls){
    return '<div class="detail-cell'+(wide?" wide":"")+'"><div class="detail-k">'+label+'</div>'+
           '<div class="detail-v'+(valCls?" "+valCls:"")+'">'+value+'</div></div>';
}
// "Сохранить" — send the result into the chat with the bot (as before).
function saveMediaToChat(item, btn){
    var tgId=getTgId(); if(!tgId) return;
    haptic.impact("light");
    var lbl=btn?btn.querySelector("span"):null;
    if(lbl) lbl.textContent="…";
    fetch("/api/send-media",{
        method:"POST",headers:authHeaders({"Content-Type":"application/json"}),
        body:JSON.stringify({tg_id:tgId,file_url:item.url,media_type:item.type})
    }).then(function(r){return r.json()}).then(function(d){
        if(d&&d.ok){ haptic.notify("success"); toast(t("savedToChat"),"success"); }
        else { haptic.notify("error"); toast(t("genFailed"),"error"); }
        if(lbl) lbl.textContent=t("detailSave");
    }).catch(function(){ if(lbl) lbl.textContent=t("detailSave"); toast(t("genFailed"),"error"); });
}
// "Поделиться" — native share to ANY chat/friend via a prepared inline message.
function shareMedia(item, btn){
    var tgId=getTgId(); if(!tgId) return;
    haptic.impact("light");
    var lbl=btn?btn.querySelector("span"):null;
    if(lbl) lbl.textContent="…";
    function reset(){ if(lbl) lbl.textContent=t("share"); }
    fetch("/api/share-media",{
        method:"POST",headers:authHeaders({"Content-Type":"application/json"}),
        body:JSON.stringify({file_url:item.url,media_type:item.type})
    }).then(function(r){return r.json()}).then(function(d){
        reset();
        if(d&&d.id&&tg.shareMessage){
            tg.shareMessage(d.id, function(sent){ if(sent) haptic.notify("success"); });
        } else if(d&&d.id){
            toast(t("shareUnsupported"),"info");   // very old client without shareMessage
        } else {
            toast(t("genFailed"),"error");
        }
    }).catch(function(){ reset(); toast(t("genFailed"),"error"); });
}
function showGenDetail(item){
    var s=item.settings||{};
    var html="";
    if(item.type==="photo"){
        html+='<div class="detail-media"><img src="'+escHtml(item.url)+'" alt="result"></div>';
    } else if(item.type==="audio"){
        html+='<div class="detail-media detail-audio">'+audioPlayerHtml(item.url)+'</div>';
    } else {
        html+='<div class="detail-media"><video src="'+escHtml(item.url)+'#t=0.1" controls preload="metadata" playsinline></video></div>';
    }

    // "Оживить" — promote a photo into a Kling 3.0 video (amber = video zone wayfinding).
    if(item.type==="photo"){
        html+='<button class="detail-animate" id="d-animate"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 3 19 12 5 21 5 3"/></svg><span>'+t("animate")+'</span></button>';
    }

    // Compact action row right under the media (no scrolling to the bottom).
    html+='<div class="detail-actions">'+
        '<button class="dact" id="d-save"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg><span>'+t("detailSave")+'</span></button>'+
        '<button class="dact" id="d-repeat"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><polyline points="17 1 21 5 17 9"/><path d="M3 11V9a4 4 0 0 1 4-4h14"/><polyline points="7 23 3 19 7 15"/><path d="M21 13v2a4 4 0 0 1-4 4H3"/></svg><span>'+t("detailRepeat")+'</span></button>'+
        '<button class="dact" id="d-share"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><line x1="8.6" y1="13.5" x2="15.4" y2="17.5"/><line x1="15.4" y1="6.5" x2="8.6" y2="10.5"/></svg><span>'+t("share")+'</span></button>'+
        '<button class="dact dact-danger" id="d-delete"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg><span>'+t("detailDelete")+'</span></button>'+
    '</div>';

    // Template generations: keep the underlying prompt hidden (product secret).
    // Users' own create-page prompts are still shown.
    var fromTemplate = !!(item.settings && item.settings.tplId);
    if(item.prompt && !fromTemplate){
        var longPrompt=item.prompt.length>240;
        html+='<div class="ccard detail-prompt-card">'+
              '<div class="detail-prompt-head"><span class="clabel">'+t("prompt")+'</span>'+
              '<button class="detail-copy" id="detail-copy"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg><span id="detail-copy-t">'+t("copy")+'</span></button></div>'+
              '<p class="detail-prompt'+(longPrompt?" clamped":"")+'" id="detail-prompt">'+escHtml(item.prompt)+'</p>'+
              (longPrompt?'<button class="detail-more" id="detail-more"><span id="detail-more-t">'+t("expand")+'</span><svg class="detail-more-ic" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg></button>':'')+
              '</div>';
    }

    html+='<div class="ccard detail-info"><span class="clabel">'+t("detailParams")+'</span><div class="detail-grid">';
    html+=detailCell(t("detailModel"), escHtml(item.model||"—"));
    html+=detailCell(t("detailCost"), '<span class="coin">W</span> '+(item.cost||0), false, "cost");
    if(s.ratio) html+=detailCell(t("aspectRatio"), escHtml(s.ratio));
    if(s.quality) html+=detailCell(t("quality"), escHtml(s.quality));
    if(s.duration) html+=detailCell(t("duration"), s.duration+" "+t("sec"));
    if(s.sound!==undefined) html+=detailCell(t("sound"), s.sound?t("soundOn"):t("soundOff"));
    if(s.mode) html+=detailCell(t("mode"), escHtml(s.mode));
    html+=detailCell(t("detailDate"), formatDetailDate(item.created_at), true);
    html+='</div></div>';

    if(s.references){
        var refsHtml="",hasRefs=false;
        for(var key in s.references){
            var urls=Array.isArray(s.references[key])?s.references[key]:[s.references[key]];
            urls.forEach(function(url){
                hasRefs=true;
                if(url.match(/\.(png|jpg|jpeg|gif|webp)/i)){
                    refsHtml+='<img src="'+escHtml(url)+'" class="detail-ref-img" alt="ref">';
                } else if(url.match(/\.(mp4|mov|webm)/i)){
                    refsHtml+='<video src="'+escHtml(url)+'#t=0.1" class="detail-ref-img" preload="metadata" muted playsinline></video>';
                } else {
                    refsHtml+='<div class="detail-ref-file">'+escHtml(url.split("/").pop())+'</div>';
                }
            });
        }
        if(hasRefs){
            html+='<div class="ccard detail-info"><span class="clabel">'+t("detailRefs")+'</span><div class="detail-refs-grid">'+refsHtml+'</div></div>';
        }
    }

    var dc=document.getElementById("gen-detail-content");
    dc.className = item.type==="photo"?"z-photo":(item.type==="audio"?"z-audio":"z-video");
    dc.innerHTML=html;
    bindAudioPlayers(dc);

    var detailMediaEl=dc.querySelector(".detail-media:not(.detail-audio)");
    if(detailMediaEl) detailMediaEl.addEventListener("click",function(){
        var src=item.url;
        var lbContent=document.getElementById("lb-content");
        var closeBtn='<button class="lb-close" id="lb-close">&times;</button>';
        if(item.type==="photo"){
            lbContent.innerHTML=closeBtn+'<img src="'+escHtml(src)+'">';
        } else {
            lbContent.innerHTML=closeBtn+'<video src="'+escHtml(src)+'" controls autoplay playsinline></video>';
        }
        document.getElementById("lb-close").addEventListener("click",function(e){e.stopPropagation();closeLightbox();});
        document.getElementById("media-lightbox").classList.add("open");
    });

    var moreBtn=document.getElementById("detail-more");
    if(moreBtn) moreBtn.addEventListener("click",function(){
        var p=document.getElementById("detail-prompt");
        var clamped=p.classList.toggle("clamped");
        moreBtn.classList.toggle("open", !clamped);
        document.getElementById("detail-more-t").textContent = clamped ? t("expand") : t("collapse");
        haptic.select();
    });

    var copyBtn=document.getElementById("detail-copy");
    if(copyBtn) copyBtn.addEventListener("click",function(){
        var lbl=document.getElementById("detail-copy-t");
        function done(){ haptic.notify("success"); if(lbl){ lbl.textContent=t("copied"); setTimeout(function(){ lbl.textContent=t("copy"); },1500); } }
        if(navigator.clipboard && navigator.clipboard.writeText){ navigator.clipboard.writeText(item.prompt||"").then(done).catch(done); }
        else { var ta=document.createElement("textarea"); ta.value=item.prompt||""; document.body.appendChild(ta); ta.select(); try{ document.execCommand("copy"); }catch(e){} document.body.removeChild(ta); done(); }
    });

    var anBtn=document.getElementById("d-animate");
    if(anBtn) anBtn.addEventListener("click",function(){ haptic.impact("medium"); animatePhoto(item); });
    var svBtn=document.getElementById("d-save");
    if(svBtn) svBtn.addEventListener("click",function(){ saveMediaToChat(item, svBtn); });
    var rpBtn=document.getElementById("d-repeat");
    if(rpBtn) rpBtn.addEventListener("click",function(){ repeatGeneration(item); });
    var shBtn=document.getElementById("d-share");
    if(shBtn) shBtn.addEventListener("click",function(){ shareMedia(item, shBtn); });
    var delBtn=document.getElementById("d-delete");
    if(delBtn) delBtn.addEventListener("click",function(){ confirmDeleteGeneration(item); });

    showPage("gen-detail");
}

// Ask before deleting — native Telegram confirm when available, else the browser one.
function confirmDeleteGeneration(item){
    haptic.impact("light");
    var msg=t("deleteConfirm");
    if(tg && tg.showConfirm){
        tg.showConfirm(msg, function(ok){ if(ok) deleteGeneration(item); });
    } else if(window.confirm(msg)){
        deleteGeneration(item);
    }
}
function deleteGeneration(item){
    function removeLocally(){
        galleryItems = galleryItems.filter(function(g){ return item.id != null ? g.id !== item.id : g !== item; });
        updateHistory();
        haptic.notify("success");
        toast(t("deleted"),"success");
        showPage("history");
    }
    // Optimistic items may not have a server id yet — just drop them from the UI.
    if(item.id == null || !getTgId()){ removeLocally(); return; }
    fetch("/api/generation/delete",{
        method:"POST",headers:authHeaders({"Content-Type":"application/json"}),
        body:JSON.stringify({tg_id:getTgId(),id:item.id})
    }).then(function(r){ return r.json().then(function(d){ return {ok:r.ok,d:d}; }); })
      .then(function(res){
          if(res.ok || (res.d && res.d.error==="not_found")){ removeLocally(); }
          else { haptic.notify("error"); toast(t("genFailed"),"error"); }
      }).catch(function(){ haptic.notify("error"); toast(t("genFailed"),"error"); });
}

function selectModel(type,modelName){
    var dd=document.getElementById(type+"-model-dd");
    if(!dd) return;
    dd.querySelectorAll(".dd-item").forEach(function(it){
        it.classList.remove("active");it.textContent=it.dataset.model;
        if(it.dataset.model===modelName){it.classList.add("active");it.textContent="✓ "+it.dataset.model;}
    });
    document.getElementById(type+"-model-name").textContent=modelName;
    applyModelLogo(type+"-model-ico", modelName);
    setModelDesc(type, modelName);
    if(type==="audio") updateAudioCost();
}

// Per-model "what it's best for" hints (photo + audio; video uses VIDEO_MODELS.desc
// rendered inside renderVideoSettings).
var MODEL_DESC = {
    "NanoBanana PRO": "imDescNbPro",
    "NanoBanana 2": "imDescNb2",
    "GPT Image 2": "imDescGpt",
    "Seedream 4.5": "imDescSeedream",
    "Suno V5.5": "auDescSuno",
};

function setModelDesc(type, modelName) {
    var el = document.getElementById(type + "-model-desc");
    if (!el) return;   // video has no such element (hint lives in #video-settings)
    var name = modelName || (document.getElementById(type + "-model-name") || {}).textContent || "";
    var key = MODEL_DESC[name];
    el.innerHTML = key ? ("💡 " + t(key)) : "";
    el.style.display = key ? "" : "none";
}

async function loadRefsForRepeat(references){
    for(var key in references){
        var urls=Array.isArray(references[key])?references[key]:[references[key]];
        var files=[];
        for(var i=0;i<urls.length;i++){
            try{
                var res=await fetch(mediaBlobUrl(urls[i]));
                if(!res.ok) throw 0;
                var blob=await res.blob();
                var filename=urls[i].split("?")[0].split("/").pop();
                var file=new File([blob],filename,{type:blob.type});
                files.push(file);
            }catch(e){console.error("Failed to load ref",urls[i],e);}
        }
        var multiKeys=["photo-refs","ref-images","ref-videos","ref-audio"];
        if(files.length===1&&multiKeys.indexOf(key)<0){
            uploadedFiles[key]=files[0];
        }else if(files.length>0){
            uploadedFiles[key]=files;
        }
    }
}

// "Оживить": take a history PHOTO into the video flow — Create → Видео → Kling 3.0
// with the photo pre-attached as the start frame, so the user only writes a prompt
// and hits Generate. Reuses the same machinery as the repeat flow (urlToFile +
// showSinglePreview into the v-start-frame slot).
async function animatePhoto(item){
    uploadedFiles={};
    showPage("create");
    setCreateType("video");
    selectModel("video","Kling 3.0");
    renderVideoSettings("Kling 3.0");
    updateVideoCost();
    var pe=document.getElementById("prompt-video");
    if(pe) pe.value="";
    urlToFile(item.url, function(file){
        uploadedFiles["v-start-frame"]=file;
        var fu=document.querySelector('.frame-up[data-uid="v-start-frame"]');
        if(fu) showSinglePreview(fu);
        updateVideoCost();
        haptic.notify("success");
        toast(t("animateReady"),"info");
        if(pe) pe.focus();
    });
}

var _tplRepeatItem = null;
async function repeatGeneration(item){
    if(item.settings&&item.settings.tplId){ _tplRepeatItem=item; loadTemplateThen(item.settings.tplId); return; }
    var type=item.type==="photo"?"image":(item.type==="audio"?"audio":"video");
    uploadedFiles={};

    showPage("create");
    setCreateType(type);   // also syncs the active .tbtn

    if(type==="audio"){
        if(item.model) selectModel("audio",item.model);
        var pa=document.getElementById("prompt-audio");
        if(pa) pa.value=item.prompt||"";
        return;
    }

    if(type==="image"){
        if(item.model) selectModel("photo",item.model);
        document.getElementById("prompt-photo").value=item.prompt||"";
        var s=item.settings||{};
        if(s.ratio){var ri=ratios.indexOf(s.ratio);if(ri>=0){state.pRatio=ri;document.getElementById("p-ratio").textContent=ratios[ri]}}
        if(s.quality){var qi=quals.indexOf(s.quality);if(qi>=0){state.pQual=qi;document.getElementById("p-qual").textContent=quals[qi]}}
        if(s.count){
            document.querySelectorAll(".cnt").forEach(function(b){
                b.classList.remove("active");if(parseInt(b.textContent)===s.count) b.classList.add("active");
            });
        }
        if(s.references){
            await loadRefsForRepeat(s.references);
            initPhotoUpload();
            var area=document.querySelector("#form-image .up-area");
            if(area&&uploadedFiles["photo-refs"]) renderRefChips(area);
        }
    }else{
        if(item.model){selectModel("video",item.model);renderVideoSettings(item.model)}
        await new Promise(function(r){setTimeout(r,50)});
        var pe=document.getElementById("prompt-video");
        if(pe) pe.value=item.prompt||"";
        var s=item.settings||{};
        if(s.ratio){var re=document.getElementById("vm-ratio");if(re)re.textContent=s.ratio}
        if(s.quality){currentVideoQuality=s.quality;var qe=document.getElementById("vm-qual");if(qe)qe.textContent=s.quality}
        if(s.duration){
            currentVideoDuration=s.duration;
            var dl=document.getElementById("vm-dur");
            if(dl)dl.textContent=s.duration+" "+t("sec");
        }
        if(s.sound!==undefined){
            var se=document.getElementById("vm-sound"),sl=document.getElementById("vm-sound-label");
            if(se)se.checked=s.sound;if(sl)sl.textContent=s.sound?t("soundOn"):t("soundOff");
        }
        updateVideoCost();
        if(s.references){
            await loadRefsForRepeat(s.references);
            document.querySelectorAll(".frame-up[data-uid]").forEach(function(el){
                if(uploadedFiles[el.dataset.uid]) showSinglePreview(el);
            });
            document.querySelectorAll(".ref-area[data-uid]").forEach(function(area){
                if(uploadedFiles[area.dataset.uid]) renderRefChips(area);
            });
        }
    }
}

// ── Trend template detail ──
// Templates ("тренды") now live in the DB (table `templates`). The client keeps a lazy
// cache: fetchTemplate(id) loads the full definition from /api/templates/{id} on first
// open, so the heavy prompt/params payload never ships in the bundle. Home-screen cards
// are static HTML (data-tpl=...); editing prompt/params/cost is deploy-free via admin.
var TRENDS = {};            // id -> full template definition (lazy cache)

// Lazy-load a template definition from the DB on first open, then render its detail.
// Opens the detail page with a spinner so the tap feels instant despite the round-trip.
function loadTemplateThen(id) {
    var c = document.getElementById("tpl-detail-content");
    if (c) c.innerHTML = '<div class="tpl-loading"><div class="gal-gen-spin"></div></div>';
    showPage("tpl-detail");
    fetch("/api/templates/" + encodeURIComponent(id), { headers: authHeaders() })
        .then(function(r){ if (!r.ok) throw new Error("load"); return r.json(); })
        .then(function(tpl){ TRENDS[id] = tpl; showTplDetail(id); })
        .catch(function(){ toast(t("tplLoadErr"), "error"); showPage("home"); });
}

var TOKENS_WORD = { ru: "токенов", en: "tokens", es: "tokens" };
var tplFiles = [];
var tplRatio = null;   // selected aspect ratio for templates that expose `ratios`

function renderTplUploads(tpl) {
    var area = document.getElementById("tpl-uploads");
    if (!area) return;
    var max = tpl.maxPhotos || 8;
    var html = "";
    tplFiles.forEach(function(f, i) {
        var u = URL.createObjectURL(f);
        html += '<div class="tpl-up-thumb"><img src="' + u + '" onload="URL.revokeObjectURL(this.src)"><button class="tpl-up-x" data-i="' + i + '">×</button></div>';
    });
    if (tplFiles.length < max) html += '<div class="tpl-up-add" id="tpl-up-add">' + plusSvg + '</div>';
    area.innerHTML = html;
    var add = document.getElementById("tpl-up-add");
    if (add) add.addEventListener("click", function() {
        openImageSource(function(file) { tplFiles.push(file); renderTplUploads(tpl); });
    });
    area.querySelectorAll(".tpl-up-x").forEach(function(b) {
        b.addEventListener("click", function(e) {
            e.stopPropagation();
            tplFiles.splice(parseInt(b.dataset.i), 1);
            renderTplUploads(tpl);
        });
    });
}

var shareSvg = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8"/><polyline points="16 6 12 2 8 6"/><line x1="12" y1="2" x2="12" y2="15"/></svg>';
var chevSvg = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="6 9 12 15 18 9"/></svg>';

// ── Parameterized template prompts (gender/age/clothing/hairstyle) ──
var tplParamState = {};                                  // current selections, keyed by param id
function tplParamById(tpl, id){ var a=tpl.params||[]; for(var i=0;i<a.length;i++) if(a[i].id===id) return a[i]; return null; }
function tplOptionsFor(param, gender){                   // resolve gender-keyed option lists
    if(param.dependsOn==="gender" && param.options && !Array.isArray(param.options)){
        return param.options[gender] || param.options.female || [];
    }
    return param.options || [];
}
function tplFirstVal(param, gender){ var l=tplOptionsFor(param, gender); return l.length ? l[0].value : null; }
function clampAge(tpl, v){
    var p = tplParamById(tpl,"age") || {min:1,max:99,default:27};
    var n = parseInt(v,10);
    if(isNaN(n)) n = (p.default!=null?p.default:27);
    return Math.max(p.min||1, Math.min(p.max||99, n));
}
function tplFragFor(tpl, param, pv, gender){
    var list = tplOptionsFor(param, gender); if(!list.length) return "";
    var chosen = pv[param.id], opt = null;
    for(var i=0;i<list.length;i++){ if(list[i].value===chosen){ opt=list[i]; break; } }
    if(!opt) opt = list[0];
    return opt.frag || "";
}
// Pure, deterministic — assembles the final prompt from skeleton + chosen params. Never returns empty (defaults).
function buildTplPrompt(tpl, pv){
    if(!tpl.skeleton || !tpl.params) return tpl.prompt || "";
    pv = pv || {};
    var ctx = {}, gp = tplParamById(tpl,"gender"), gender = null;
    if(gp){
        gender = pv.gender || gp.default || tplFirstVal(gp, null);
        var gopt = null, opts = gp.options||[];
        for(var i=0;i<opts.length;i++){ if(opts[i].value===gender){ gopt=opts[i]; break; } }
        if(!gopt) gopt = opts[0] || {};
        var gr = gopt.grammar || {noun:"человек", adjE:"ый"};
        ctx.age = clampAge(tpl, pv.age);
        // Anchor the subject to the reference person — no idealizing adjective ("эффектный"
        // pushed NanoBanana to invent a generic handsome face instead of keeping the real one).
        // Age is woven into the subject only when the template exposes an age param (birthday);
        // templates without it (yacht) get a clean noun so no "~27 лет" is invented.
        var ageStr = tplParamById(tpl,"age") ? " ~"+ctx.age+" лет," : "";
        ctx.subject = gr.noun+ageStr+" тот же самый человек, что на загруженном фото-референсе, с его реальными чертами лица";
    } else {
        ctx.age = clampAge(tpl, pv.age);
    }
    (tpl.params||[]).forEach(function(p){
        if(p.id==="gender" || p.id==="age") return;
        ctx[p.id] = tplFragFor(tpl, p, pv, gender);
    });
    return tpl.skeleton.replace(/\{(\w+)\}/g, function(_, k){ return ctx[k]!=null ? String(ctx[k]) : ""; })
                       .replace(/\s+/g, " ").trim();
}
function initTplParamState(tpl, preset){
    preset = preset || {}; tplParamState = {};
    (tpl.params||[]).forEach(function(p){
        if(p.id==="age") tplParamState.age = preset.age!=null ? preset.age : (p.default!=null?p.default:27);
        else if(p.id==="gender") tplParamState.gender = preset.gender || p.default || tplFirstVal(p, null);
    });
    var g = tplParamState.gender;
    (tpl.params||[]).forEach(function(p){
        if(p.id==="gender" || p.id==="age") return;
        var list = tplOptionsFor(p, g), pre = preset[p.id];
        var ok = pre && list.some(function(o){ return o.value===pre; });
        tplParamState[p.id] = ok ? pre : tplFirstVal(p, g);
    });
}
function readTplParams(tpl){
    var o = {}; (tpl.params||[]).forEach(function(p){ o[p.id] = tplParamState[p.id]; });
    if(o.age!=null) o.age = clampAge(tpl, o.age);
    return o;
}
function renderTplParams(tpl){
    var L = currentLang, g = tplParamState.gender, html = "";
    (tpl.params||[]).forEach(function(p){
        var lbl = (p.label && (p.label[L]||p.label.ru)) || p.id;
        if(p.control==="pills"){
            html += '<h4 class="tpl-sec">'+lbl+'</h4><div class="pill-row tplp-pills" data-pid="'+p.id+'">'+
                (p.options||[]).map(function(o){
                    return '<button class="pill'+(o.value===tplParamState[p.id]?' active':'')+'" data-v="'+escHtml(o.value)+'">'+escHtml(o.label[L]||o.label.ru)+'</button>';
                }).join("")+'</div>';
        } else if(p.control==="age"){
            html += '<h4 class="tpl-sec">'+lbl+'</h4>'+
                '<div class="tplp-age-row">'+
                    '<button type="button" class="tplp-age-btn" data-age-step="-1" aria-label="−"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><line x1="5" y1="12" x2="19" y2="12"/></svg></button>'+
                    '<input type="number" inputmode="numeric" id="tplp-age" class="tplp-age-inp" min="'+(p.min||1)+'" max="'+(p.max||99)+'" value="'+escHtml(String(tplParamState.age))+'">'+
                    '<button type="button" class="tplp-age-btn" data-age-step="1" aria-label="+"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg></button>'+
                '</div>';
        } else if(p.control==="sheet"){
            var list = tplOptionsFor(p, g), cur = tplParamState[p.id], curOpt = null;
            for(var i=0;i<list.length;i++){ if(list[i].value===cur){ curOpt=list[i]; break; } }
            if(!curOpt) curOpt = list[0] || {label:{}};
            html += '<h4 class="tpl-sec">'+lbl+'</h4><div class="tpl-select tplp-sheet" data-pid="'+p.id+'"><span>'+escHtml((curOpt.label&&(curOpt.label[L]||curOpt.label.ru))||"")+'</span>'+chevSvg+'</div>';
        }
    });
    return html;
}
function updateTplPreview(tpl){
    var pv = document.getElementById("tpl-prompt-preview");
    if(pv) pv.textContent = buildTplPrompt(tpl, readTplParams(tpl));
}
function mountTplParams(tpl){
    var host = document.getElementById("tpl-params"); if(!host) return;
    host.innerHTML = renderTplParams(tpl);
    host.querySelectorAll(".tplp-pills").forEach(function(row){
        row.addEventListener("click", function(e){
            var b = e.target.closest(".pill"); if(!b) return;
            var pid = row.dataset.pid; if(tplParamState[pid]===b.dataset.v) return;
            tplParamState[pid] = b.dataset.v; haptic.select();
            if(pid==="gender"){
                // gender-keyed lists changed → reset them to the new gender's default and re-render
                (tpl.params||[]).forEach(function(p){ if(p.dependsOn==="gender") tplParamState[p.id] = tplFirstVal(p, b.dataset.v); });
                mountTplParams(tpl);
            } else {
                row.querySelectorAll(".pill").forEach(function(x){ x.classList.toggle("active", x===b); });
            }
            updateTplPreview(tpl);
        });
    });
    var ageEl = host.querySelector("#tplp-age");
    if(ageEl){
        ageEl.addEventListener("input", function(){ tplParamState.age = ageEl.value; updateTplPreview(tpl); });
        ageEl.addEventListener("blur", function(){ tplParamState.age = clampAge(tpl, ageEl.value); ageEl.value = tplParamState.age; updateTplPreview(tpl); });
        host.querySelectorAll(".tplp-age-btn").forEach(function(b){
            b.addEventListener("click", function(){
                var step = parseInt(b.dataset.ageStep, 10) || 0;
                tplParamState.age = clampAge(tpl, (parseInt(ageEl.value, 10) || tplParamById(tpl,"age").default) + step);
                ageEl.value = tplParamState.age;
                haptic.select(); updateTplPreview(tpl);
            });
        });
    }
    host.querySelectorAll(".tplp-sheet").forEach(function(sel){
        sel.addEventListener("click", function(){
            var pid = sel.dataset.pid, p = tplParamById(tpl, pid), L = currentLang;
            var list = tplOptionsFor(p, tplParamState.gender);
            var opts = list.map(function(o){ return {value:o.value, label:(o.label[L]||o.label.ru)}; });
            openSheet((p.label&&(p.label[L]||p.label.ru))||pid, opts, tplParamState[pid], function(v){
                tplParamState[pid] = v;
                var cur = null; for(var i=0;i<list.length;i++) if(list[i].value===v) cur = list[i];
                sel.querySelector("span").textContent = cur ? (cur.label[L]||cur.label.ru) : "";
                haptic.select(); updateTplPreview(tpl);
            });
        });
    });
}

function showTplDetail(id) {
    var tpl = TRENDS[id];
    if (!tpl) { loadTemplateThen(id); return; }   // lazy-fetch definition, then re-render
    tplFiles = [];
    var L = currentLang;
    var title = tpl.title[L] || tpl.title.ru;
    var desc = tpl.desc[L] || tpl.desc.ru;
    var tokWord = TOKENS_WORD[L] || TOKENS_WORD.ru;

    var media = tpl.type === "photo"
        ? '<img src="' + escHtml(tpl.full || tpl.preview) + '" alt="">'
        : '<video src="' + escHtml(tpl.preview) + '" autoplay muted loop playsinline></video>';
    var needBadge = tpl.needPhoto
        ? '<div class="tpl-needphoto"><span class="pill-ico"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m12 19-7-7 7-7M19 12H5"/></svg></span><span>' + t("trendNeedPhoto") + '</span></div>' : '';

    var afterUploads, setting;
    if (tpl.type === "photo") {
        afterUploads = '<button class="ref-pill" id="tpl-refhint">' + t("refHint") + '</button>';
        // Aspect ratio is fixed per template (sent via settings.ratio) — don't show a
        // non-interactive selector that looks tappable but isn't.
        setting = '';
    } else {
        afterUploads = '<p class="tpl-minmax">' + t("minPhoto") + ': ' + (tpl.minPhotos || 1) + '. ' + t("maxPhoto") + ': ' + (tpl.maxPhotos || 7) + '.</p>';
        setting = tpl.quality ? ('<h4 class="tpl-sec">' + t("quality") + '</h4>' +
                  '<div class="tpl-select">' + tpl.quality + chevSvg + '</div>') : '';
    }

    // Optional aspect-ratio picker (templates that list `ratios`). Otherwise the
    // ratio is fixed to tpl.ratio. tplGenerate reads the selected tplRatio.
    var ratioBlock = '';
    tplRatio = tpl.ratio || (tpl.ratios && tpl.ratios[0]) || null;
    if (tpl.ratios && tpl.ratios.length > 1) {
        ratioBlock = '<h4 class="tpl-sec">' + t("tplOrientation") + '</h4><div class="pill-row" id="tpl-ratio">' +
            tpl.ratios.map(function(r){ return '<button class="pill' + (r === tplRatio ? ' active' : '') + '" data-r="' + r + '">' + r + '</button>'; }).join('') +
            '</div>';
    }

    // Param mode shows only the key params — the underlying prompt is a product
    // secret and stays hidden from users (no reveal toggle).
    var promptBlock;
    if (tpl.params) {
        promptBlock = '<div id="tpl-params"></div>';
    } else if (tpl.hidePrompt) {
        promptBlock = '<p class="tpl-auto-note">' + t("tplPromptAuto") + '</p>';
    } else {
        promptBlock = '<h4 class="tpl-sec">' + t("prompt") + '</h4><textarea id="tpl-prompt" class="tpl-prompt" rows="5"></textarea>';
    }

    var html =
        '<div class="tpl-hero">' + media + '</div>' +
        needBadge +
        '<div class="tpl-head">' +
            '<h2 class="tpl-title">' + escHtml(title) + '</h2>' +
            '<span class="tpl-price"><span class="coin">W</span>' + tpl.cost + ' ' + tokWord + '</span>' +
            '<button class="tpl-share">' + shareSvg + '</button>' +
        '</div>' +
        '<p class="tpl-model">' + escHtml(tpl.model || "") + '</p>' +
        '<p class="tpl-desc">' + escHtml(desc) + '</p>' +
        '<h4 class="tpl-sec">' + t("yourPhotos") + '</h4>' +
        '<div class="tpl-uploads" id="tpl-uploads"></div>' +
        afterUploads +
        promptBlock +
        setting +
        ratioBlock +
        '<div class="gen-bar"><button class="gen-btn" id="tpl-gen"><span>' + t("generate") + '</span><div class="tok"><span class="coin">W</span>' + tpl.cost + '</div></button></div>';

    var repeatItem = _tplRepeatItem; _tplRepeatItem = null;
    var repeatSettings = repeatItem ? (repeatItem.settings || {}) : {};

    document.getElementById("tpl-detail-content").innerHTML = html;
    if (tpl.params) {
        initTplParamState(tpl, repeatSettings.tplParams);
        mountTplParams(tpl);
        var tgl = document.getElementById("tpl-prompt-toggle");
        if (tgl) tgl.addEventListener("click", function() {
            var pv = document.getElementById("tpl-prompt-preview");
            var open = !pv.classList.toggle("hidden");
            tgl.classList.toggle("open", open);
            document.getElementById("tpl-prompt-toggle-t").textContent = open ? t("tplHidePrompt") : t("tplShowPrompt");
            if (open) updateTplPreview(tpl);
            haptic.select();
        });
    } else if (!tpl.hidePrompt) {
        document.getElementById("tpl-prompt").value = repeatItem ? (repeatItem.prompt || tpl.prompt) : tpl.prompt;
    }
    renderTplUploads(tpl);

    if (repeatSettings.ratio) {
        tplRatio = repeatSettings.ratio;
        var rr = document.getElementById("tpl-ratio");
        if (rr) rr.querySelectorAll(".pill").forEach(function(p){ p.classList.toggle("active", p.dataset.r === tplRatio); });
    }

    if (repeatSettings.references) {
        (async function(){
            var refs = repeatSettings.references;
            for (var key in refs) {
                var urls = Array.isArray(refs[key]) ? refs[key] : [refs[key]];
                for (var i = 0; i < urls.length; i++) {
                    try {
                        var res = await fetch(mediaBlobUrl(urls[i]));
                        if (!res.ok) throw 0;
                        var blob = await res.blob();
                        var fname = urls[i].split("?")[0].split("/").pop();
                        tplFiles.push(new File([blob], fname, {type: blob.type}));
                    } catch(e) { console.error("Failed to load tpl ref", urls[i], e); }
                }
            }
            renderTplUploads(tpl);
        })();
    }

    var refBtn = document.getElementById("tpl-refhint");
    if (refBtn) refBtn.addEventListener("click", function() { openRefguide("tpl-detail"); });
    var ratioRow = document.getElementById("tpl-ratio");
    if (ratioRow) ratioRow.addEventListener("click", function(e) {
        var b = e.target.closest(".pill"); if (!b) return;
        tplRatio = b.dataset.r;
        ratioRow.querySelectorAll(".pill").forEach(function(x){ x.classList.toggle("active", x === b); });
        haptic.select();
    });
    document.getElementById("tpl-gen").addEventListener("click", function() { tplGenerate(tpl, this, id); });

    showPage("tpl-detail");
}

async function tplGenerate(tpl, btn, id) {
    var prompt;
    if (tpl.params) { prompt = buildTplPrompt(tpl, readTplParams(tpl)); }
    else { var promptEl = document.getElementById("tpl-prompt"); prompt = (promptEl ? promptEl.value : (tpl.prompt || "")).trim(); }
    if (!prompt) { toast(t("alertPrompt"), "error"); return; }
    if (tplFiles.length < (tpl.minPhotos || 1)) { toast(t("trendNeedPhoto"), "error"); return; }

    // Identical flow to Create: full-screen overlay (loading → result), not an inline skeleton.
    genRepeatFn = function(){ tplGenerate(tpl, btn, id); };
    var ovType = tpl.type === "video" ? "video" : "photo";
    genOvOpen(); genOvLoading(ovType, tpl.model);
    haptic.impact("medium");
    var pendId = addPendingGen(ovType, tpl.model);
    var genCost = tpl.cost || 0;
    applyBalanceDelta(-genCost);   // reserve in the UI immediately (server already charged at start)
    try {
        var fd = new FormData();
        fd.append("prompt", prompt);
        fd.append("tg_id", getTgId() || "");
        fd.append("model", tpl.model);
        // Always send tplId — the server charges the fixed template price by it (pricing.py
        // TEMPLATE_COST), so the charged amount matches the price shown in the UI.
        var settings = { ratio: (tplRatio || tpl.ratio), tplId: id };
        // Photo templates carry no explicit quality, so the server ran NanoBanana at its
        // default and history showed no "Качество". Default to the Create-page quality (2K)
        // so the gen is consistent AND the value is recorded for the history detail.
        if (tpl.type === "photo") settings.quality = tpl.quality || quals[1];
        else if (tpl.quality) settings.quality = tpl.quality;
        if (tpl.mode) settings.mode = tpl.mode;
        if (tpl.duration) settings.duration = tpl.duration;
        if (tpl.sound) settings.sound = true;
        if (tpl.params) settings.tplParams = readTplParams(tpl);
        fd.append("settings", JSON.stringify(settings));
        // Send the reference photo under the field name the generator reads. Photo ->
        // "photo-refs" (kie.generate_image). Video -> "v-first-frame" by default; a template
        // may override via tpl.refField (e.g. Seedance uses "ref-images" so the photo becomes
        // an @Image1 reference, not a literal first frame — those are mutually exclusive).
        var refField = tpl.refField || (tpl.type === "photo" ? "photo-refs" : "v-first-frame");
        tplFiles.forEach(function(f) { fd.append(refField, f); });

        var endpoint = "/api/generate/" + (tpl.type === "photo" ? "image" : "video");
        var res = await fetch(endpoint, { method: "POST", headers: authHeaders(), body: fd });
        var data = await res.json();
        if (res.status === 402) { applyBalanceDelta(genCost); removePendingGen(pendId); updateHistory(); genOvClose(); toast(t("errNoBalance"), "error"); haptic.notify("error"); showPage("topup"); return; }
        if (!res.ok) throw new Error(data.error || "Generation failed");
        if (data.balance != null) { document.querySelectorAll(".user-balance").forEach(function(el){el.textContent=data.balance;}); if(currentUser) currentUser.balance=data.balance; refreshGenButtons(); }

        var mtype = data.media_type === "photo" ? "photo" : (data.media_type === "audio" ? "audio" : "video");
        function makeItem(url){ return { url:url, type:mtype, prompt:prompt, model:tpl.model, settings:settings, cost:(data.cost != null ? data.cost : tpl.cost), created_at:new Date().toISOString() }; }
        if (mtype === "photo" && data.file_urls && data.file_urls.length) {
            data.file_urls.slice().reverse().forEach(function(u){ galleryItems.unshift(makeItem(u)); });
        } else {
            galleryItems.unshift(makeItem(data.file_url));
        }
        removePendingGen(pendId);
        updateHistory();
        if (getTgId()) loadUserHistory();   // reconcile with the server (clears its pending row)
        haptic.notify("success");
        if (genViewOpen) genOvResult(mtype, data); else toast(t("genSavedToHistory"), "success");
    } catch (err) {
        applyBalanceDelta(genCost);   // generation failed — server refunds, restore the UI
        removePendingGen(pendId);
        updateHistory();
        haptic.notify("error");
        if (genViewOpen) genOvError(err.message); else toast(t("genFailed"), "error");
    }
}

// ── Home template gallery (rendered from GET /api/templates) ──
// The home cards are now data-driven so admin-added templates appear without a deploy.
// Markup/classes mirror the former static layout 1:1 so the look is unchanged.
var templatesList = null;     // cached light list
var _tplListFetching = false;
var TPL_CAT_ORDER = ["girls", "men", "animals", "kids", "travel", "cinema", "jobs", "eras"];   // curated sections; others appended after
var TPL_PEOPLE_ICO = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/></svg>';
var TPL_CAMERA_ICO = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2" stroke-linejoin="round"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/></svg>';
var TPL_ARROW_ICO = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m12 19-7-7 7-7M19 12H5"/></svg>';

function tplCap(tt) { return escHtml((tt.title && (tt.title[currentLang] || tt.title.ru)) || ""); }

function tplTrendBadge(tt) {
    if (tt.need_photo) return '<span class="trend-badge trend-badge-pill"><span class="pill-ico">' + TPL_ARROW_ICO + '</span><span>' + t("trendNeedPhoto") + '</span></span>';
    if (tt.type === "photo") return '<span class="trend-badge trend-badge-ico">' + TPL_CAMERA_ICO + '</span>';
    return '';
}

function tplTrendCard(tt) {
    var media = tt.type === "video"
        ? '<video class="trend-media" src="' + escHtml(tt.preview) + '" autoplay muted loop playsinline preload="metadata"></video>'
        : '<img class="trend-media" src="' + escHtml(tt.preview) + '" alt="" loading="lazy">';
    return '<div class="trend-t has-media" data-tpl="' + escHtml(tt.id) + '">' + media + tplTrendBadge(tt) + '<span class="trend-cap">' + tplCap(tt) + '</span></div>';
}

function tplThumbCard(tt) {
    return '<div class="tpl-thumb has-media" data-tpl="' + escHtml(tt.id) + '"><img src="' + escHtml(tt.preview) + '" alt="" loading="lazy"><span class="tpl-thumb-cap">' + tplCap(tt) + '</span></div>';
}

function renderTrendsGrid() {
    var grid = document.getElementById("trends-grid");
    if (!grid || !templatesList) return;
    var feat = templatesList.filter(function(x){ return x.featured; });
    // Teasers removed (empty placeholder tiles not needed) — show only real featured templates.
    grid.innerHTML = feat.map(tplTrendCard).join("");
}

function renderTplCats() {
    var host = document.getElementById("tpl-cats");
    if (!host || !templatesList) return;
    var byCat = {};
    templatesList.forEach(function(x){
        if (!x.featured && x.category) (byCat[x.category] = byCat[x.category] || []).push(x);
    });
    var cats = TPL_CAT_ORDER.slice();
    Object.keys(byCat).forEach(function(c){ if (cats.indexOf(c) < 0) cats.push(c); });
    host.innerHTML = cats.map(function(c){
        var items = byCat[c] || [];
        // Curated empties (e.g. "Дети") show 4 placeholder thumbs; unknown empty cats are skipped.
        if (!items.length && TPL_CAT_ORDER.indexOf(c) < 0) return "";
        var thumbs = items.length
            ? items.map(tplThumbCard).join("")
            : '<div class="tpl-thumb"></div><div class="tpl-thumb"></div><div class="tpl-thumb"></div><div class="tpl-thumb"></div>';
        return '<div class="tpl-cat"><div class="tpl-hdr"><span class="tpl-hdr-ico">' + TPL_PEOPLE_ICO + '</span>' +
            '<b>' + escHtml(t(c)) + '</b><span class="muted">' + t("tplCount") + '</span>' +
            '<span class="gold ml-auto">' + t("tplAll") + '</span></div>' +
            '<div class="tpl-scroll">' + thumbs + '</div></div>';
    }).join("");
}

function renderTemplatesHome(force) {
    if (templatesList && !force) { renderTrendsGrid(); renderTplCats(); return; }
    if (_tplListFetching) return;
    _tplListFetching = true;
    fetch("/api/templates", { headers: authHeaders() })
        .then(function(r){ return r.ok ? r.json() : []; })
        .then(function(list){ templatesList = list; renderTrendsGrid(); renderTplCats(); })
        .catch(function(){})
        .then(function(){ _tplListFetching = false; });
}

// Delegated so dynamically-rendered cards work. Open detail on a [data-tpl] card;
// toggle a category's full grid via its "все →" link.
document.addEventListener("click", function(e) {
    var card = e.target.closest("[data-tpl]");
    if (card) { showTplDetail(card.dataset.tpl); return; }
    var link = e.target.closest(".tpl-hdr .gold");
    if (link) {
        var cat = link.closest(".tpl-cat");
        if (!cat) return;
        if (!cat.querySelector(".tpl-thumb.has-media")) { toast(t("tplSoon")); return; }
        var open = cat.classList.toggle("expanded");
        link.textContent = t(open ? "tplCollapse" : "tplAll");
        haptic.impact("light");
    }
});

renderTemplatesHome();   // initial fill on load

// ── Rewards (dot over the gift pulses while there are unclaimed tasks) ──
// Rewards are server-authoritative (was localStorage). rewardsData mirrors
// GET /api/rewards: [{id,type,amount,configured,claimed,channel_link?}].
var rewardsData = null;
function rwdById(id){ var a=rewardsData||[]; for(var i=0;i<a.length;i++){ if(a[i].id===id) return a[i]; } return null; }
async function loadRewards(){
    if(!getTgId()) return;
    try {
        var res = await fetch("/api/rewards", { headers: authHeaders() });
        if(!res.ok) return;
        rewardsData = await res.json();
    } catch(e){ return; }
    renderRewardsState();
}
function updateRewardsDot(){
    var dot=document.querySelector("#rewards-btn-h .dot-badge");
    if(!dot) return;
    // pulse if a configured reward is still unclaimed; hidden when all done/unavailable
    var active=(rewardsData||[]).some(function(r){ return r.configured && !r.claimed; });
    dot.classList.toggle("active", active);
}
function renderRewardsState(){
    document.querySelectorAll(".rwd-card[data-rwd-id]").forEach(function(card){
        var r=rwdById(card.dataset.rwdId);
        // Hide subscription cards whose channel isn't configured yet.
        card.style.display = (r && !r.configured) ? "none" : "";
        if(r && r.channel_link){
            card.querySelectorAll('.rwd-btn[data-rwd="open"]').forEach(function(b){ b.dataset.link=r.channel_link; });
        }
        var isDone=!!(r && r.claimed);
        card.classList.toggle("done", isDone);
        card.querySelectorAll('.rwd-btn[data-rwd="check"],.rwd-btn[data-rwd="do"]').forEach(function(btn){
            btn.disabled=isDone;
            if(isDone) btn.textContent="✓ "+t("rwdDone");
        });
    });
    updateRewardsDot();
}
async function rwdClaim(rid, btn){
    if(btn) btn.disabled=true;
    try {
        var res=await fetch("/api/rewards/claim", { method:"POST",
            headers: authHeaders({"Content-Type":"application/json"}),
            body: JSON.stringify({ reward_id: rid }) });
        var d=await res.json().catch(function(){ return {}; });
        if(res.ok && d.ok){
            var r=rwdById(rid); if(r) r.claimed=true;
            if(d.credited){ applyBalanceDelta(+d.credited); haptic.notify("success"); toast(t("rwdCredited").replace("{n}", d.credited), "success"); }
            else { toast(t("rwdAlready"), "info"); }
            renderRewardsState();
            return;
        }
        var err=(d && d.error) || "";
        if(err==="not_subscribed") toast(t("rwdNotSubscribed"), "error");
        else toast(t("rwdCheckFail"), "error");
    } catch(e){ toast(t("rwdCheckFail"), "error"); }
    if(btn) btn.disabled=false;
}
function rwdShareStory(rid, btn){
    var mediaUrl = location.origin + "/static/img/share-story.jpg?v=2";
    var shared=false;
    try {
        if(tg && tg.shareToStory){
            tg.shareToStory(mediaUrl, { text: t("rwdStoryText"),
                widget_link: { url: referralLink(), name: "PromptW" } });
            shared=true;
        } else if(tg && tg.openTelegramLink){
            tg.openTelegramLink("https://t.me/share/url?url=" + encodeURIComponent(referralLink()) + "&text=" + encodeURIComponent(t("inviteShareText")));
            shared=true;
        }
    } catch(e){}
    // Honor-based: credit once after the share editor is invoked (Telegram gives no
    // post-confirmation callback). Idempotent server-side.
    if(shared) rwdClaim(rid, btn);
    else toast(t("rwdCheckFail"), "error");
}
document.querySelectorAll(".rwd-card[data-rwd-id] [data-rwd]").forEach(function(b){
    b.addEventListener("click", function(){
        var act=b.dataset.rwd;
        var card=b.closest(".rwd-card[data-rwd-id]");
        var rid=card ? card.dataset.rwdId : "";
        if(act==="open"){
            if(b.dataset.link){ if(tg && tg.openTelegramLink) tg.openTelegramLink(b.dataset.link); else window.open(b.dataset.link, "_blank"); }
            return;
        }
        if(act==="do"){ rwdShareStory(rid, b); return; }   // share → shareToStory + honor credit
        if(act==="check"){ rwdClaim(rid, b); return; }      // subscription → getChatMember verify
    });
});

// ── Token packages → payment sheet (СБП / Карта РФ via ЮKassa) ──
var activePromoId = null;
var activePromoPct = 0;
(function(){
    var btn=document.getElementById("promo-activate");
    if(!btn) return;
    btn.addEventListener("click",async function(){
        var inp=document.getElementById("promo-input");
        var msg=document.getElementById("promo-msg");
        var code=(inp?inp.value:"").trim();
        if(!code){msg.textContent=t("promoEmpty");msg.className="promo-msg err";return;}
        btn.disabled=true;
        try{
            var res=await fetch("/api/promo/activate",{
                method:"POST",headers:authHeaders({"Content-Type":"application/json"}),
                body:JSON.stringify({code:code})
            });
            var d=await res.json().catch(function(){return {};});
            if(res.ok&&d.ok){
                if(d.type==="topup"){
                    msg.textContent=t("promoTopupOk").replace("{n}",d.tokens);
                    msg.className="promo-msg ok";
                    if(d.balance!=null){document.querySelectorAll(".user-balance").forEach(function(el){el.textContent=d.balance;});if(currentUser)currentUser.balance=d.balance;refreshGenButtons();}
                } else {
                    activePromoId=d.promo_id;
                    activePromoPct=d.value;
                    msg.textContent=t("promoBonusOk").replace("{n}",d.value);
                    msg.className="promo-msg ok";
                }
                inp.value="";
                haptic.notify("success");
            } else {
                var errKey={not_found:"promoNotFound",disabled:"promoDisabled",expired:"promoExpired",limit_reached:"promoLimit",already_used:"promoUsed"};
                msg.textContent=t(errKey[d.error]||"promoError");
                msg.className="promo-msg err";
                haptic.notify("error");
            }
        }catch(e){msg.textContent=t("promoError");msg.className="promo-msg err";}
        btn.disabled=false;
    });
})();
var selectedPkg = null, payMethod = "sbp", lastPayUrl = null;
var PKG_PRICE = {"100":106,"300":307,"500":498,"1000":954,"2000":1802,"5000":4240};
function tuNote(msg, cls){ var n = document.getElementById("tu-note"); if (n){ n.className = "wd-note" + (cls ? " " + cls : ""); n.textContent = msg; } }
// Opening must be synchronous to a tap (webviews block redirects after an await),
// so we render an explicit "Перейти к оплате" button as the reliable path.
function openPay(u){ if (!u) return; if (tg && tg.openLink) tg.openLink(u); else window.open(u, "_blank"); }
function tuClose(){ var ov = document.getElementById("tu-overlay"); if (ov) ov.classList.add("hidden"); }
function tuOpen(pkg){
    selectedPkg = pkg; lastPayUrl = null;
    var price = PKG_PRICE[pkg] || "";
    setText("tu-pkg-amt", pkg);
    setText("tu-sub-amt", pkg);
    setText("tu-price-sbp", price + " ₽");
    setText("tu-price-card", price + " ₽");
    var go = document.getElementById("tu-go"); if (go) go.classList.add("hidden");
    document.querySelectorAll("#tu-overlay .pay-m.primary").forEach(function(x){ x.style.pointerEvents = ""; x.style.opacity = ""; });
    tuNote("", "");
    var ov = document.getElementById("tu-overlay"); if (ov) ov.classList.remove("hidden");
}
async function startPay(method){
    var agree = document.getElementById("agree-chk");
    if (agree && !agree.checked){ tuNote(t("payNeedAgree"), "err"); return; }
    if (!selectedPkg) return;
    var rows = document.querySelectorAll("#tu-overlay .pay-m.primary");
    rows.forEach(function(x){ x.style.pointerEvents = "none"; x.style.opacity = ".6"; });
    tuNote(t("payCreating"), "");
    try {
        var res = await fetch("/api/topup/create", {
            method: "POST",
            headers: authHeaders({"Content-Type": "application/json"}),
            body: JSON.stringify({ package: selectedPkg, provider: "yoomoney", method: method, promo_id: activePromoId || undefined })
        });
        var d = await res.json().catch(function(){ return {}; });
        if (res.ok && d.url){
            lastPayUrl = d.url;
            if (d.order_id){ try { localStorage.setItem("pwPendingOrder", d.order_id); } catch(e){} }
            tuNote(t("payCreated"), "ok");
            var go = document.getElementById("tu-go"); if (go) go.classList.remove("hidden");
            openPay(d.url);   // best-effort auto-open; the button is the fallback
        } else if (res.status === 503){ tuNote(t("payUnavailable"), "err"); }
        else if (res.status === 401){ tuNote(t("payAuth"), "err"); }
        else { tuNote(t("payError"), "err"); }
    } catch(e){ tuNote(t("payError"), "err"); }
    rows.forEach(function(x){ x.style.pointerEvents = ""; x.style.opacity = ""; });
}
document.querySelectorAll("#page-topup [data-pkg]").forEach(function(p) {
    p.addEventListener("click", function() {
        document.querySelectorAll("#page-topup [data-pkg]").forEach(function(x) { x.classList.remove("sel"); });
        p.classList.add("sel");
        tuOpen(p.dataset.pkg);
    });
});
(function(){
    var ov = document.getElementById("tu-overlay");
    if (ov) ov.addEventListener("click", function(e){ if (e.target === ov) tuClose(); });
    var c = document.getElementById("tu-close"); if (c) c.addEventListener("click", tuClose);
    document.querySelectorAll("#tu-overlay .pay-m.primary").forEach(function(b){ b.addEventListener("click", function(){ startPay(b.dataset.pm); }); });
    var go = document.getElementById("tu-go");
    if (go) go.addEventListener("click", function(){ openPay(lastPayUrl); });
})();

// ── Confirm a top-up on return (no dependency on provider webhook) ──
// After paying, the user comes back to the app; we re-verify the order with the
// server (which re-fetches it from ЮKassa) and credit tokens if it succeeded.
// payChainActive stays true for the WHOLE retry chain (not just one fetch), so a
// visibilitychange mid-retry can't spawn a second chain / duplicate success toast.
var payChainActive = false;
function checkPendingPayment(){
    if (payChainActive) return;
    var order; try { order = localStorage.getItem("pwPendingOrder"); } catch(e){ order = null; }
    payChainActive = true;
    _pollPayment(order, 0);
}
function _pollPayment(order, attempt){
    // With a stored order we retry (active payment may still be settling); without
    // one we do a single reconcile of the user's latest pending top-up.
    var url = "/api/topup/status" + (order ? "?order_id=" + encodeURIComponent(order) : "");
    fetch(url, { headers: authHeaders({}) })
        .then(function(r){ return r.ok ? r.json() : (r.status === 404 ? {gone:true} : {}); })
        .then(function(d){
            if (d && d.paid){
                try { localStorage.removeItem("pwPendingOrder"); } catch(e){}
                tuClose();
                toast(t("payCredited").replace("{n}", d.tokens), "success");
                haptic.notify("success");
                loadUserProfile();
                payChainActive = false;
            } else if (d && d.gone){
                try { localStorage.removeItem("pwPendingOrder"); } catch(e){}
                payChainActive = false;
            } else if (order && attempt < 5){
                // ЮKassa may need a moment to flip to succeeded — retry a few times.
                setTimeout(function(){ _pollPayment(order, attempt + 1); }, 2500);
            } else {
                payChainActive = false;
            }
        })
        .catch(function(){ payChainActive = false; });
}
document.addEventListener("visibilitychange", function(){
    if (!document.hidden) checkPendingPayment();
});
checkPendingPayment();

// ── Header divider on scroll ──
window.addEventListener("scroll", function(){
    document.body.classList.toggle("scrolled", window.scrollY > 4);
}, { passive: true });

// History grid density toggle (2 per row default, 4 per row), remembered per device.
(function initGridToggle(){
    var toggle=document.getElementById("grid-toggle"), list=document.getElementById("history-list");
    if(!toggle||!list) return;
    var saved="2"; try{ saved=localStorage.getItem("promptw_grid_cols")||"2"; }catch(e){}
    function apply(cols){
        list.classList.toggle("cols-4", cols==="4");
        toggle.querySelectorAll(".gt-btn").forEach(function(b){ b.classList.toggle("active", b.dataset.cols===cols); });
    }
    apply(saved);
    toggle.querySelectorAll(".gt-btn").forEach(function(b){
        b.addEventListener("click",function(){
            apply(b.dataset.cols);
            try{ localStorage.setItem("promptw_grid_cols", b.dataset.cols); }catch(e){}
            haptic.select();
        });
    });
})();

// ── Init ──
renderVideoSettings(currentVideoModel);
setModelDesc("photo");
setModelDesc("audio");
initPhotoUpload();
loadUserProfile();
loadUserHistory();
loadRewards();   // server-authoritative; also drives the header dot pulse on home

// ── Heartbeat ──
// Lets the server know the user is in the app, so it can suppress TG-notification
// duplicates of in-app toasts (e.g. "generation ready"). Fire-and-forget.
function sendHeartbeat() {
    var tgId = getTgId();
    if (!tgId || document.hidden) return;
    try { fetch("/api/heartbeat", { method: "POST", headers: authHeaders() }); } catch (e) {}
}
sendHeartbeat();
setInterval(sendHeartbeat, 45000);
document.addEventListener("visibilitychange", function(){ if (!document.hidden) sendHeartbeat(); });

(function(){
    var nm = document.getElementById("notif-marketing-tg");
    if (nm) nm.addEventListener("change", function(){ setNotifMarketing(nm.checked); });
})();

// Deep-link from the bot menu: open the requested page (?p=video / start_param).
(function(){
    try {
        var p = new URLSearchParams(location.search).get("p");
        if (!p && tg.initDataUnsafe) p = tg.initDataUnsafe.start_param;
        if (!p) return;
        if (p === "image" || p === "video" || p === "audio") {
            showPage("create"); setCreateType(p);
        } else if (["create","home","history","topup","partner","info","text","profile","stats","rewards","support"].indexOf(p) >= 0) {
            showPage(p);
        }
    } catch (e) {}
})();

function closeLightbox(){
    var lb=document.getElementById("media-lightbox");
    if(!lb) return;
    lb.classList.remove("open");
    var v=lb.querySelector("video");
    if(v) v.pause();
    document.getElementById("lb-content").innerHTML='<button class="lb-close" id="lb-close">&times;</button>';
}
(function(){
    var lb=document.getElementById("media-lightbox");
    if(lb) lb.addEventListener("click",function(e){ if(e.target===lb) closeLightbox(); });
})();

// ── Onboarding ──
var WELCOME_BONUS = 60;              // keep in sync with the server welcome bonus (.env WELCOME_BONUS)
var ONBOARDING_CTA_TARGET = "home";  // swap to a starter template id when chosen
function fixObBonus(){
    var el = document.getElementById("ob4-title");
    if (el) el.textContent = t("ob4Title").replace("{n}", WELCOME_BONUS);
}
(function initOnboarding(){
    var ob = document.getElementById("onboarding");
    if (!ob) return;
    var track = document.getElementById("ob-track");
    var dotsWrap = document.getElementById("ob-dots");
    var nextBtn = document.getElementById("ob-next");
    var skipBtn = document.getElementById("ob-skip");
    var slides = track ? track.children.length : 0;
    var i = 0, x0 = null;
    for (var d = 0; d < slides; d++){ var dot = document.createElement("span"); dot.className = "ob-dot"; dotsWrap.appendChild(dot); }
    var dots = dotsWrap.children;
    function render(){
        track.style.transform = "translateX(-" + (i * 100) + "%)";
        for (var k = 0; k < slides; k++){ dots[k].classList.toggle("on", k === i); }
        var last = i === slides - 1;
        nextBtn.textContent = last ? t("obCtaStart") : t("obNext");
        nextBtn.classList.toggle("cta", last);
        skipBtn.style.visibility = last ? "hidden" : "visible";
    }
    function go(n){ var p = i; i = Math.max(0, Math.min(slides - 1, n)); if (i !== p) haptic.select(); render(); }
    function finish(){
        try { localStorage.setItem("pwOnboardingSeen", "1"); } catch(e){}
        ob.classList.add("hidden");
        document.body.classList.remove("ob-open");
        showPage(ONBOARDING_CTA_TARGET);
    }
    nextBtn.addEventListener("click", function(){ if (i < slides - 1) go(i + 1); else finish(); });
    skipBtn.addEventListener("click", finish);
    track.addEventListener("touchstart", function(e){ x0 = e.touches[0].clientX; }, { passive: true });
    track.addEventListener("touchend", function(e){ if (x0 === null) return; var dx = e.changedTouches[0].clientX - x0; if (dx < -40) go(i + 1); else if (dx > 40) go(i - 1); x0 = null; });
    window.showOnboarding = function(){ i = 0; fixObBonus(); render(); ob.classList.remove("hidden"); document.body.classList.add("ob-open"); };
    var replay = document.getElementById("ob-replay");
    if (replay) replay.addEventListener("click", function(){ window.showOnboarding(); });
})();
// First launch: show onboarding unless already seen or a deep-link page was requested.
(function(){
    var seen; try { seen = localStorage.getItem("pwOnboardingSeen"); } catch(e){ seen = "1"; }
    if (seen) return;
    var dl = null;
    try { dl = new URLSearchParams(location.search).get("p") || (tg.initDataUnsafe && tg.initDataUnsafe.start_param); } catch(e){}
    if (dl) return;   // honor the deep-link this time; onboarding will show on a later plain launch
    if (typeof window.showOnboarding === "function") window.showOnboarding();
})();

// ── Support Chat ──────────────────────────────────────────────────
var supMessages = [];
var supPollTimer = null;
var supLastId = 0;
var supBusy = false;
var supFile = null;

function initSupport() {
    supMessages = [];
    supLastId = 0;
    supFile = null;
    supHidePreview();
    supRender();
    supLoadMessages();
    clearInterval(supPollTimer);
    supPollTimer = setInterval(function() {
        var pg = document.querySelector(".page.active");
        if (pg && pg.id === "page-support") supPollNew();
    }, 5000);
}

function supRender() {
    var list = document.getElementById("sup-list");
    var empty = document.getElementById("sup-empty");
    if (!list) return;
    if (!supMessages.length) {
        if (empty) empty.classList.remove("hidden");
        list.innerHTML = "";
        return;
    }
    if (empty) empty.classList.add("hidden");
    list.innerHTML = supMessages.map(function(m, i) {
        var prev = supMessages[i - 1];
        var grouped = prev && prev.sender === m.sender;
        var cls = "msg " + (m.sender === "user" ? "user" : (m.sender === "system" ? "system" : "bot")) +
                  (grouped ? " grouped" : "");
        var img = "";
        if (m.image_url) {
            img = '<img class="sup-msg-img" src="' + escHtml(m.image_url) + '" loading="lazy">';
        }
        return '<div class="' + cls + '"><div class="msg-c">' +
               (m.content ? escHtml(m.content) : "") + img +
               '</div><span class="msg-t">' + chatTime(m.created_at) +
               '</span></div>';
    }).join("");
    var scroll = document.getElementById("sup-scroll");
    if (scroll) scroll.scrollTop = scroll.scrollHeight;
}

function supShowPreview(file) {
    var pv = document.getElementById("sup-preview");
    if (!pv) return;
    var url = URL.createObjectURL(file);
    pv.innerHTML = '<img src="' + url + '"><div class="sup-rm" id="sup-rm-btn">&times;</div>';
    pv.classList.remove("hidden");
    document.getElementById("sup-rm-btn").addEventListener("click", function() {
        supFile = null;
        supHidePreview();
        var fi = document.getElementById("sup-file");
        if (fi) fi.value = "";
    });
}

function supHidePreview() {
    var pv = document.getElementById("sup-preview");
    if (pv) { pv.innerHTML = ""; pv.classList.add("hidden"); }
}

async function supLoadMessages() {
    if (!getTgId()) return;
    try {
        var res = await fetch("/api/support/messages", { headers: authHeaders() });
        if (!res.ok) return;
        var data = await res.json();
        if (data.messages && data.messages.length) {
            supMessages = data.messages;
            supLastId = supMessages[supMessages.length - 1].id;
        }
        supRender();
    } catch (e) {}
}

async function supPollNew() {
    if (!getTgId()) return;
    try {
        var res = await fetch("/api/support/messages?after_id=" + supLastId,
                              { headers: authHeaders() });
        if (!res.ok) return;
        var data = await res.json();
        if (data.messages && data.messages.length) {
            supMessages = supMessages.concat(data.messages);
            supLastId = supMessages[supMessages.length - 1].id;
            supRender();
            if (typeof haptic !== "undefined" && haptic.notify) haptic.notify("success");
        }
    } catch (e) {}
}

async function supSend() {
    if (supBusy) return;
    var input = document.getElementById("sup-input");
    if (!input) return;
    var text = input.value.trim();
    if (!text && !supFile) return;
    var optimistic = { sender: "user", content: text, created_at: new Date().toISOString() };
    if (supFile) optimistic.image_url = URL.createObjectURL(supFile);
    supMessages.push(optimistic);
    input.value = "";
    chatGrow(input);
    supRender();
    supBusy = true;
    if (typeof haptic !== "undefined" && haptic.impact) haptic.impact("light");
    var btn = document.getElementById("sup-send");
    if (btn) btn.disabled = true;
    try {
        var res;
        if (supFile) {
            var fd = new FormData();
            fd.append("text", text);
            fd.append("image", supFile);
            res = await fetch("/api/support/send", {
                method: "POST",
                headers: authHeaders(),
                body: fd
            });
        } else {
            res = await fetch("/api/support/send", {
                method: "POST",
                headers: authHeaders({ "Content-Type": "application/json" }),
                body: JSON.stringify({ text: text })
            });
        }
        if (!res.ok) {
            var err = await res.json().catch(function(){ return {}; });
            toast(err.error || t("supError"), "error");
        } else {
            var msg = await res.json();
            supMessages[supMessages.length - 1] = msg;
            if (msg.id > supLastId) supLastId = msg.id;
        }
    } catch (e) {
        toast(t("supError"), "error");
    }
    supFile = null;
    supHidePreview();
    var fi = document.getElementById("sup-file");
    if (fi) fi.value = "";
    supBusy = false;
    if (btn) btn.disabled = false;
    supRender();
}

(function() {
    var sendBtn = document.getElementById("sup-send");
    if (sendBtn) sendBtn.addEventListener("click", supSend);
    var input = document.getElementById("sup-input");
    if (input) {
        input.addEventListener("input", function() { chatGrow(input); });
        input.addEventListener("keydown", function(e) {
            if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); supSend(); }
        });
    }
    var fileInput = document.getElementById("sup-file");
    if (fileInput) fileInput.addEventListener("change", function() {
        if (fileInput.files && fileInput.files[0]) {
            supFile = fileInput.files[0];
            supShowPreview(supFile);
        }
    });
})();
