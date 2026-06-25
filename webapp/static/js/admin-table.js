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
        }).catch(function(e){ apiError(e); mount.querySelector(".dt-body").innerHTML = '<div class="dt-error">Не удалось загрузить. <button class="btn btn-outline btn-sm dt-retry">Повторить</button></div>'; var rb=mount.querySelector(".dt-retry"); if(rb) rb.onclick=load; });
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
