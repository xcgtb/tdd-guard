/* ═══════════ 状态 ═══════════ */
var MENU = [
  { id: 'dashboard',  name: '治理总览', icon: 'grid' },
  { id: 'explore',    name: '影视探索', icon: 'film' },
  { id: 'governance', name: '双库治理', icon: 'scale' },
  { id: 'mapping',    name: '片库映射', icon: 'map' },
  { id: 'subscribe',  name: '追更订阅', icon: 'bell' },
  { id: 'morning',    name: '每日晨报', icon: 'sun' },
  { id: 'daily',      name: '每日汇报', icon: 'file' },
  { id: 'history',    name: '执行记录', icon: 'bookmark' },
  { id: 'settings',   name: '规则设置', icon: 'gear' }
];

var currentPlan = null;
var govData = { loc: [], shr: [], keep: [], exempt: [] };
var currentGovFilter = 'all';
var statsAutoLoaded = false;
var revealed = {};
var currentStrategy = { decision: 'quality_first', multi_season_protect: true, tie_keep_local: false, exempt_keywords: [], special_action: 'keep' };
var currentSubs = [];
var currentGovItems = [];

/* 缓存 Emby 主机地址（由 /api/dashboard 或 /api/emby/library 填充） */
window.__embyHost = '';

function $(id){ return document.getElementById(id); }
function esc(s){ return String(s).replace(/[&<>"']/g, function(c){ return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }

function toast(msg, type, ms){
  if (typeof type === 'number'){ ms = type; type = ''; }
  ms = ms || 2600;
  var t = $('toast');
  var prefix = type === 'success' ? icon('check') : (type === 'error' ? icon('x') : '');
  t.className = 'toast ' + (type || '');
  t.innerHTML = prefix + '<span>' + msg + '</span>';
  t.classList.add('show');
  clearTimeout(t._h); t._h = setTimeout(function(){ t.classList.remove('show'); }, ms);
}
function showTaskHint(text){ $('taskHintText').textContent = text || '任务中...'; $('taskHint').classList.add('show'); }
function hideTaskHint(){ $('taskHint').classList.remove('show'); }
function toggleSidebar(open){ var sb = $('sidebar'); if(open){ sb.classList.add('open'); $('mask').classList.add('show'); } else { sb.classList.remove('open'); $('mask').classList.remove('show'); } }
function openModal(html){ $('modalBody').innerHTML = html; $('modalBg').classList.add('show'); }
function closeModal(){ $('modalBg').classList.remove('show'); }

function getHeaders(){
  return { 'Content-Type': 'application/json' };
}
async function api(path, opts){
  opts = opts || {};
  var headers = Object.assign(getHeaders(), opts.headers || {});
  var r = await fetch(path, Object.assign({}, opts, { headers: headers }));
  if (r.status === 401) {
    // 浏览器在收到 401 + WWW-Authenticate 时会自动弹原生登录框并重试；
    // 走到这里说明用户取消了登录框或凭证仍然错误。
    throw new Error('未授权：用户名或密码错误');
  }
  var j = await r.json().catch(function(){ return { detail: '响应解析失败' }; });
  if (!r.ok) throw new Error(j.detail || j.message || ('HTTP ' + r.status));
  return j;
}
async function pollTask(taskId, hintText){
  showTaskHint(hintText || '任务中...');
  for(;;){
    var t = await api('/api/task/' + taskId);
    if (t.status !== 'running') { hideTaskHint(); return t; }
    await new Promise(function(r){ setTimeout(r, 1200); });
  }
}
function statusOk(txt){ return '<span class="status-badge ok">' + icon('check') + txt + '</span>'; }
function statusNo(txt){ return '<span class="status-badge no">' + icon('x') + txt + '</span>'; }
function statusDim(txt){ return '<span class="status-badge dim">' + txt + '</span>'; }

/* ═══════════ 主题切换 ═══════════ */
var THEME_KEY = 'tdd-theme';

function getCurrentTheme(){
  var saved = '';
  try { saved = localStorage.getItem(THEME_KEY) || ''; } catch(e){}
  if (saved === 'dark' || saved === 'light') return saved;
  // 未设置：跟随系统
  return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
}

function updateThemeIcon(){
  var btn = $('themeBtn');
  if (!btn) return;
  var isDark = getCurrentTheme() === 'dark';
  // 月亮图标：当前是深色时，按钮显示太阳（点了变亮）
  btn.innerHTML = isDark
    ? '<svg class="ic" viewBox="0 0 24 24"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M4.93 19.07l1.41-1.41M17.66 6.34l1.41-1.41"/></svg>'
    : '<svg class="ic" viewBox="0 0 24 24"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';
  btn.title = isDark ? '切换到浅色模式' : '切换到深色模式';
}

function toggleTheme(){
  var cur = getCurrentTheme();
  var next = cur === 'dark' ? 'light' : 'dark';
  try { localStorage.setItem(THEME_KEY, next); } catch(e){}
  document.documentElement.setAttribute('data-theme', next);
  updateThemeIcon();
  toast(next === 'dark' ? '已切换到深色模式' : '已切换到浅色模式', 'success', 1500);
}

function initTheme(){
  // <head> 里的内联脚本已经设了 data-theme（防闪烁），这里只更新图标
  updateThemeIcon();
  // 监听系统主题变化（仅在用户未手动选择时生效）
  if (window.matchMedia) {
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function(){
      var saved = '';
      try { saved = localStorage.getItem(THEME_KEY) || ''; } catch(e){}
      if (!saved) {
        document.documentElement.removeAttribute('data-theme');
        updateThemeIcon();
      }
    });
  }
}

/* ═══════════ 带鉴权的海报加载 ═══════════ */
/* <img src> 不会自动带 Basic Auth 头，必须走 fetch 拿 blob 再赋值。
   HTML 里统一用 data-emby-src 标记需要鉴权加载的图片。 */
async function hydratePosters(root){
  var scope = root || document;
  var imgs = scope.querySelectorAll('img[data-emby-src]:not([data-loaded])');
  for (var i = 0; i < imgs.length; i++) {
    var img = imgs[i];
    img.setAttribute('data-loaded', '1');
    try {
      var r = await fetch(img.getAttribute('data-emby-src'));
      if (!r.ok) throw new Error('HTTP ' + r.status);
      var blob = await r.blob();
      img.src = URL.createObjectURL(blob);
    } catch (e) {
      var ph = document.createElement('div');
      ph.className = 'no-img';
      ph.textContent = '无图';
      if (img.parentNode) img.parentNode.replaceChild(ph, img);
    }
  }
}

function renderMenu(){
  var nav = $('menu');
  nav.innerHTML = MENU.map(function(m){
    var active = m.id === 'dashboard' ? 'active' : '';
    return '<a class="'+active+'" data-tab="'+m.id+'" onclick="switchTab(\''+m.id+'\')">' +
           '<span class="icon">'+icon(m.icon)+'</span>' + m.name + '</a>';
  }).join('');
}
function switchTab(id){
  document.querySelectorAll('.sidebar nav a').forEach(function(a){ a.classList.toggle('active', a.dataset.tab === id); });
  document.querySelectorAll('.tab').forEach(function(s){ s.classList.remove('active'); });
  var tab = $('tab-' + id); if(tab) tab.classList.add('active');
  var m = MENU.find(function(x){ return x.id === id; });
  $('tabTitle').textContent = m ? m.name : 'TDD Guard';
  toggleSidebar(false);

  if (id === 'dashboard')  loadDashboard();
  if (id === 'explore')    loadExplore();
  if (id === 'settings')   { loadConfig(); loadBotStatus(); loadStrategy(); loadIngest(); }
  if (id === 'mapping')    ensureEmbyLoaded();
  if (id === 'subscribe')  loadSubscriptions();
  if (id === 'morning')    loadMorning();
  if (id === 'history')    loadRecords();
  if (id === 'daily' && !statsAutoLoaded) { statsAutoLoaded = true; loadStats(false, false); }
}

/* ═══════════ 治理总览 ═══════════ */
async function loadDashboard(){
  try {
    var d = await api('/api/dashboard');
    $('stat-local').textContent = d.localCount || '-';
    $('stat-share').textContent = d.shareCount || '-';
    var embyOk = d.services && d.services.emby && d.services.emby.ok;
    $('svc-emby').innerHTML = embyOk ? statusOk('在线') : statusNo('离线');
    $('svc-emby-host').textContent = (d.services && d.services.emby && d.services.emby.host) || '—';
    /* 缓存 Emby 主机地址，供 showEmbyDetailById 里的"在 Emby 中打开"链接使用 */
    if (d.services && d.services.emby && d.services.emby.host) {
      window.__embyHost = d.services.emby.host;
    }
    var tmdbOk = d.services && d.services.tmdb && d.services.tmdb.ok;
    $('svc-tmdb').innerHTML = tmdbOk ? statusOk('已配置') : statusNo('未配置');
    $('svc-tmdb-sub').textContent = tmdbOk ? '影视探索可用' : '去「规则设置」配置';

    if (d.subscriptions) {
      var ss = d.subscriptions;
      $('stat-sub').textContent = (ss.enabled || 0) + ' / ' + (ss.total || 0) + ' 部';
      $('stat-sub-sub').textContent = (ss.enabled || 0) > 0 ? '轮询中' : '点击查看';
    }
    if (d.morningReport) {
      var mr = d.morningReport;
      $('stat-morning').textContent = mr.enabled ? mr.time : '未开启';
      $('stat-morning-sub').textContent = mr.last_date ? ('上次 ' + mr.last_date) : ('预扫 ' + (mr.prescan_min || 5) + ' 分钟');
    }
    if (d.ingest) {
      var ing = d.ingest;
      $('stat-ingest-mov').textContent = (ing.movies || 0) + ' / ' + (ing.series || 0) + ' 部';
      var age = ing.cache_age_sec;
      if (age == null) $('stat-ingest-age').textContent = '无缓存';
      else if (age < 60) $('stat-ingest-age').textContent = age + ' 秒前';
      else if (age < 3600) $('stat-ingest-age').textContent = Math.floor(age/60) + ' 分钟前';
      else $('stat-ingest-age').textContent = Math.floor(age/3600) + ' 小时前';
    }

    // 策略快照
    var st = await api('/api/strategy');
    var s = st.strategy || {};
    var decisionLabel = { quality_first: '画质优先', keep_local: '保留本地', keep_share: '保留分享', balanced: '严格画质' }[s.decision] || s.decision;
    var spLabel = { keep: '保留', ignore: '忽略', delete: '清理' }[s.special_action] || '保留';
    $('strategySnapshot').innerHTML =
      '<div class="path-row"><span class="name">决策模型</span><span class="path" style="color:#10b981;border:none">' + decisionLabel + '</span></div>' +
      '<div class="path-row"><span class="name">多季合集保护</span><span class="path" style="color:' + (s.multi_season_protect ? '#3b82f6' : '#9ca3af') + ';border:none">' + (s.multi_season_protect ? '已开启' : '已关闭') + '</span></div>' +
      '<div class="path-row"><span class="name">特别篇策略</span><span class="path" style="color:#8b5cf6;border:none">' + spLabel + '</span></div>' +
      '<div class="path-row"><span class="name">平局策略</span><span class="path" style="color:#6b7280;border:none">' + (s.tie_keep_local ? '保留本地' : '保留分享') + '</span></div>' +
      '<div class="path-row"><span class="name">白名单</span><span class="path" style="border:none;color:#6b7280">' + (s.exempt_keywords || []).join(', ') + '</span></div>';
  } catch(e){ console.warn('dashboard 失败', e); }
}

/* ═══════════ 扫描 ═══════════ */
async function scanLibrary(){
  var btn1 = $('scanBtn'), btn2 = $('govScanBtn');
  [btn1, btn2].forEach(function(b){ if(b) b.disabled = true; });
  if (btn1) btn1.innerHTML = '<span class="spin"></span><span>扫描中...</span>';
  if (btn2) btn2.innerHTML = '<span class="spin"></span><span>扫描中...</span>';
  try {
    var res = await api('/api/check', { method: 'POST' });
    var t = await pollTask(res.task_id, '扫描双库中...');
    if (t.status !== 'success') throw new Error(t.error || '诊断失败');
    var r = t.result;
    currentPlan = r.plan_id;
    if ($('govExecBtn')) $('govExecBtn').disabled = !currentPlan;
    govData = {
      loc: r.del_local_items || [],
      shr: r.del_share_items || [],
      keep: r.protected_items || [],
      exempt: r.exempted_items || []
    };
    $('govStats').style.display = '';
    $('gov-keep').textContent  = govData.keep.length;
    $('gov-loc').textContent   = govData.loc.length;
    $('gov-shr').textContent   = govData.shr.length;
    $('gov-exempt').textContent = govData.exempt.length;
    renderGovList();
    toast('扫描完成：待处理 ' + (r.total_clean_cnt || 0) + ' 项', 'success');
  } catch(e){ toast(e.message, 'error', 4000); }
  finally {
    [btn1, btn2].forEach(function(b){ if(b) b.disabled = false; });
    if (btn1){ btn1.removeAttribute('data-icon-done'); btn1.innerHTML = '立即扫描双库'; btn1.setAttribute('data-icon','refresh'); }
    if (btn2){ btn2.removeAttribute('data-icon-done'); btn2.innerHTML = '重新扫描';   btn2.setAttribute('data-icon','refresh'); }
    injectIcons();
  }
}
function setGovFilter(f){
  currentGovFilter = f;
  document.querySelectorAll('#govFilter button').forEach(function(b){ b.classList.toggle('active', b.dataset.f === f); });
  renderGovList();
}
function renderGovList(){
  var items = [];
  if (currentGovFilter === 'all' || currentGovFilter === 'loc')
    govData.loc.forEach(function(t){ items.push({type:'loc', data:t}); });
  if (currentGovFilter === 'all' || currentGovFilter === 'shr')
    govData.shr.forEach(function(t){ items.push({type:'shr', data:t}); });
  if (currentGovFilter === 'all' || currentGovFilter === 'keep')
    govData.keep.forEach(function(t){ items.push({type:'keep', data:t}); });
  if (currentGovFilter === 'all' || currentGovFilter === 'exempt')
    govData.exempt.forEach(function(t){ items.push({type:'exempt', data:t}); });
  currentGovItems = items;
  var list = $('planList');
  if (!items.length) {
    list.innerHTML = '<div class="list-empty">' + ((govData.loc.length + govData.shr.length + govData.keep.length + govData.exempt.length) ? '当前筛选下无匹配项' : '无待治理项') + '</div>';
    return;
  }
  list.innerHTML = items.map(function(it, i){
    return '<div class="list-item '+it.type+' clickable" onclick="showGovDetailAt('+i+')">' +
           '<div class="t">' + esc(it.data.text) + '</div>' +
           '<div class="d">' + esc(it.data.detail) + '</div>' +
           '</div>';
  }).join('');
}
function showGovDetailAt(i){
  var it = currentGovItems[i];
  if (!it) return;
  showGovDetail(it.data);
}
function showGovDetail(d){
  var meta = d.meta || {};
  var rows = [];
  rows.push('<div class="meta-row"><span class="k">标题</span><span class="v">' + esc(d.text) + '</span></div>');
  if (d.reason_label) rows.push('<div class="meta-row"><span class="k">原因</span><span class="v">' + esc(d.reason_label) + '</span></div>');
  if (meta.title) rows.push('<div class="meta-row"><span class="k">剧名</span><span class="v">' + esc(meta.title) + '</span></div>');
  if (meta.season != null) rows.push('<div class="meta-row"><span class="k">季</span><span class="v">S' + String(meta.season).padStart(2,'0') + '</span></div>');
  if (meta.local_seasons != null) rows.push('<div class="meta-row"><span class="k">本地季数</span><span class="v">' + meta.local_seasons + '</span></div>');
  if (meta.share_seasons != null) rows.push('<div class="meta-row"><span class="k">分享季数</span><span class="v">' + meta.share_seasons + '</span></div>');
  if (meta.local_quality != null) rows.push('<div class="meta-row"><span class="k">本地画质分</span><span class="v">' + meta.local_quality + '</span></div>');
  if (meta.share_quality != null) rows.push('<div class="meta-row"><span class="k">分享画质分</span><span class="v">' + meta.share_quality + '</span></div>');
  if (meta.keywords) rows.push('<div class="meta-row"><span class="k">命中关键词</span><span class="v">' + esc((meta.keywords || []).join(', ')) + '</span></div>');
  rows.push('<div class="meta-row"><span class="k">涉及文件数</span><span class="v">' + (d.files_count || 0) + '</span></div>');
  var html = '<h3>' + icon('info') + '详情</h3>' + rows.join('') +
             '<div style="margin-top:16px;text-align:right"><button class="btn gray" onclick="closeModal()">关闭</button></div>';
  openModal(html);
}
async function runClean(){
  if (!currentPlan) return toast('请先扫描双库', 'error');
  if (!(await showModalConfirm('确认执行清理？将按清单删除文件'))) return;
  if ($('govExecBtn')) $('govExecBtn').disabled = true;
  try {
    var res = await api('/api/clean', { method: 'POST', body: JSON.stringify({ plan_id: currentPlan, dry_run: false }) });
    var t = await pollTask(res.task_id, '执行清理中...');
    if (t.status !== 'success') throw new Error(t.error || '执行失败');
    var lines = (t.result.detail || []).join('\n');
    showModalAlert('【执行结果】\n\n' + lines + '\n\n释放本地: ' + t.result.loc_cnt + '  淘汰分享: ' + t.result.sh_cnt);
    currentPlan = null;
    if ($('govExecBtn')) $('govExecBtn').disabled = true;
    govData = { loc: [], shr: [], keep: [], exempt: [] };
    $('govStats').style.display = 'none';
    $('planList').innerHTML = '<div class="list-empty">已执行完毕</div>';
    loadDashboard();
  } catch(e){ toast(e.message, 'error', 4000); }
}

/* ═══════════ 影视探索 ═══════════ */
var exploreState = { region: 'all', media: 'movie', year: '', sort: 'popularity', page: 1, totalPages: 1, q: '' };
var exploreLoading = false;
var exploreFiltersOpen = false;

var REGION_LABEL = { all: '全部地区', cn: '大陆', hk: '香港', tw: '台湾', jp: '日本', kr: '韩国', us: '欧美' };
var MEDIA_LABEL  = { movie: '电影', tv: '剧集' };
var SORT_LABEL   = { popularity: '热度', release: '最新', rating: 'TMDB 评分' };

function updateFilterSummaryText(){
  var parts = [
    REGION_LABEL[exploreState.region] || '全部地区',
    MEDIA_LABEL[exploreState.media]   || '电影',
    exploreState.year ? (exploreState.year + ' 年') : '全部年份',
    SORT_LABEL[exploreState.sort]     || '热度'
  ];
  var el = $('filterSummaryText');
  if (el) el.textContent = parts.join(' · ');
}

function toggleExploreFilters(){
  exploreFiltersOpen = !exploreFiltersOpen;
  var panel = $('exploreFilters');
  var summary = $('filterSummary');
  var toggleText = $('filterToggleText');
  if (!panel) return;
  if (exploreFiltersOpen) {
    panel.classList.remove('hidden');
    if (summary) summary.classList.add('open');
    if (toggleText) toggleText.textContent = '收起筛选';
  } else {
    panel.classList.add('hidden');
    if (summary) summary.classList.remove('open');
    if (toggleText) toggleText.textContent = '展开筛选';
  }
}

function setupChipGroup(id, key){
  var el = $(id);
  if (!el) return;
  el.addEventListener('click', function(e){
    var btn = e.target.closest('.chip');
    if (!btn) return;
    el.querySelectorAll('.chip').forEach(function(b){ b.classList.remove('active'); });
    btn.classList.add('active');
    exploreState[key] = btn.dataset.v;
    exploreState.page = 1;
    exploreState.q = '';
    $('exploreSearch').value = '';
    updateFilterSummaryText();
    // 选完自动收起（移动端体验好；桌面端也一致）
    if (exploreFiltersOpen) toggleExploreFilters();
    loadExplore();
  });
}
function doExploreSearch(){
  var q = $('exploreSearch').value.trim();
  exploreState.q = q; exploreState.page = 1;
  // 搜索时始终收起筛选栏
  if (exploreFiltersOpen) toggleExploreFilters();
  loadExplore();
}
async function loadExplore(){
  if (exploreLoading) return;
  exploreLoading = true;
  var grid = $('exploreGrid');
  grid.innerHTML = '<div class="list-empty">加载中...</div>';
  try {
    var qs = 'region=' + encodeURIComponent(exploreState.region)
           + '&year=' + encodeURIComponent(exploreState.year)
           + '&sort=' + encodeURIComponent(exploreState.sort)
           + '&media=' + encodeURIComponent(exploreState.media)
           + '&page=' + exploreState.page
           + '&q=' + encodeURIComponent(exploreState.q);
    var r = await api('/api/explore?' + qs);
    if (r.status !== 'success') throw new Error(r.message || '加载失败');
    exploreState.totalPages = r.total_pages || 1;
    renderExploreCards(r.cards || []);
    if (r.cards && r.cards.length) {
      $('explorePager').classList.remove('hidden');
      var label = r.is_search ? '搜索结果' : '第 ' + exploreState.page + ' / ' + exploreState.totalPages + ' 页';
      $('explorePageInfo').textContent = label + ' · 共 ' + (r.total_results || 0) + ' 条';
    } else { $('explorePager').classList.add('hidden'); }
  } catch(e){
    grid.innerHTML = '<div class="list-empty">' + esc(e.message) + '</div>';
  } finally { exploreLoading = false; }
}
function _subscribedTmdbIds(){
  var set = {};
  currentSubs.forEach(function(s){ if (s.tmdb_id) set[String(s.tmdb_id)] = true; });
  return set;
}
function renderExploreCards(cards){
  var grid = $('exploreGrid');
  if (!cards.length) { grid.innerHTML = '<div class="list-empty">无结果</div>'; return; }
  var subSet = _subscribedTmdbIds();
  grid.innerHTML = cards.map(function(c){
    var status, cls;
    if (c.in_local && c.in_share) { status = '两库全入库'; cls = 'ok'; }
    else if (c.in_local) { status = '仅本地'; cls = 'local'; }
    else if (c.in_share) { status = '仅分享'; cls = 'share'; }
    else { status = '未入库'; cls = 'no'; }
    var typeLabel = c.type === 'tv' ? '剧集' : '电影';
    /* poster 有两种来源：
       1) TMDB 图片 → 直接 <img src>
       2) /api/emby/poster/... → 需要 Basic Auth，走 data-emby-src，由 hydratePosters() 拉 blob */
    var posterHtml;
    if (!c.poster) {
      posterHtml = '<div class="no-img">暂无海报</div>';
    } else if (c.poster.indexOf('/api/emby/poster/') === 0) {
      posterHtml = '<img data-emby-src="' + esc(c.poster) + '" alt="" loading="lazy">';
    } else {
      posterHtml = '<img src="' + esc(c.poster) + '" alt="" loading="lazy" onerror="this.style.display=\'none\';this.parentNode.innerHTML=\'<div class=&quot;no-img&quot;>暂无海报</div>\'">';
    }
    var isSub = subSet[String(c.tmdb_id)];
    var subBtn = c.type === 'tv'
      ? '<button class="sub-btn ' + (isSub ? 'on' : '') + '" onclick="event.stopPropagation();toggleSubscribe(\'' + esc(c.tmdb_id) + '\',\'' + esc(c.title).replace(/'/g, '&#39;') + '\',\'' + esc(c.poster || '').replace(/'/g, '&#39;') + '\')">' + (isSub ? '✓ 已订阅' : '+ 订阅') + '</button>'
      : '';
    return '<div class="poster-card">'
      + '<div class="poster-wrap">' + posterHtml
      + '<span class="badge-type">' + typeLabel + '</span>'
      + (c.rating ? '<span class="badge-rate">' + icon('star') + c.rating + '</span>' : '')
      + '<span class="badge-status ' + cls + '">' + status + '</span>'
      + '</div>'
      + '<div class="info">'
      + '<div class="t" title="' + esc(c.title) + '">' + esc(c.title) + '</div>'
      + '<div class="meta"><span>' + (c.year || '—') + ' · TMDB</span>' + subBtn + '</div>'
      + '</div>'
      + '</div>';
  }).join('');
  hydratePosters(grid);
}
async function toggleSubscribe(tmdbId, title, poster){
  var idx = currentSubs.findIndex(function(s){ return String(s.tmdb_id) === String(tmdbId); });
  if (idx >= 0) {
    if (!(await showModalConfirm('取消订阅《' + title + '》？'))) return;
    currentSubs.splice(idx, 1);
  } else {
    currentSubs.push({ id: tmdbId, tmdb_id: tmdbId, name: title, poster: poster || '', enabled: true });
    toast('已订阅《' + title + '》', 'success');
  }
  try {
    await api('/api/subscriptions', { method: 'POST', body: JSON.stringify({ subscriptions: currentSubs }) });
    loadExplore();
  } catch(e){ toast('保存失败: ' + e.message, 'error'); }
}
function explorePage(delta){
  var p = exploreState.page + delta;
  if (p < 1 || p > exploreState.totalPages) return;
  exploreState.page = p;
  loadExplore();
  window.scrollTo({ top: 0, behavior: 'smooth' });
}
function switchExploreTab_init(){
  setupChipGroup('filterRegion', 'region');
  setupChipGroup('filterMedia', 'media');
  setupChipGroup('filterYear', 'year');
  setupChipGroup('filterSort', 'sort');
  $('exploreSearch').addEventListener('keydown', function(e){ if (e.key === 'Enter') doExploreSearch(); });
  updateFilterSummaryText();
}

/* ═══════════ 片库映射 ═══════════ */
var embyData = { series: [], movies: [], stats: {} };
var embyFilterState = { type: 'series', filter: 'all' };
var embyLoaded = false;
function ensureEmbyLoaded(){ if (!embyLoaded) loadEmbyLibrary(false, false); }
var tmdbProgressTimer = null;

async function pollTmdbProgress(){
  try {
    var r = await api('/api/tmdb/progress');
    var p = r.progress || {};
    if (p.running) {
      document.getElementById('tmdbProgressWrap').classList.remove('hidden');
      document.getElementById('tmdbProgressFill').style.width = p.percent + '%';
      var stage = p.stage || '对照中...';
      if (p.total > 0) stage += ' (' + p.done + '/' + p.total + ')';
      document.getElementById('tmdbProgressStage').textContent = stage;
      var etaText = '';
      if (p.elapsed_sec) {
        etaText = '已用 ' + p.elapsed_sec + 's';
        if (p.eta_sec != null && p.eta_sec > 0) etaText += ' · 预计还需 ' + p.eta_sec + 's';
      }
      document.getElementById('tmdbProgressEta').textContent = etaText;
    } else {
      document.getElementById('tmdbProgressWrap').classList.add('hidden');
      if (tmdbProgressTimer) { clearInterval(tmdbProgressTimer); tmdbProgressTimer = null; }
      if (p.finished_at && !p.error) loadEmbyLibrary(false, true);
      else if (p.error) toast('TMDB 对照失败: ' + p.error, 'error', 5000);
    }
  } catch(e) {}
}

function startTmdbProgressWatch(){
  if (tmdbProgressTimer) return;
  tmdbProgressTimer = setInterval(pollTmdbProgress, 2000);
  pollTmdbProgress();
}

async function loadEmbyLibrary(force, withTmdb){
  if (force && withTmdb) {
    startTmdbProgressWatch();
    try {
      var r0 = await api('/api/emby/library?force=1&with_tmdb=1');
      if (r0 && (r0.status === 'started' || r0.status === 'running')) {
        $('embyList').innerHTML = '<div class="list-empty">TMDB 对照进行中，请稍候...</div>';
        return;
      }
    } catch(e) {}
  }
  var msg = withTmdb ? '正在拉取 Emby + TMDB（首次可能较慢）...' : '正在拉取 Emby 库...';
  $('embyList').innerHTML = '<div class="list-empty">' + msg + '</div>';
  try {
    var url = '/api/emby/library?force=' + (force ? 1 : 0) + '&with_tmdb=' + (withTmdb ? 1 : 0);
    var r = await api(url);
    if (r.status !== 'success') throw new Error(r.message || '失败');
    embyData.series = r.series || [];
    embyData.movies = r.movies || [];
    embyData.stats = r.stats || {};
    /* 缓存 Emby 主机地址（后端在返回里带了 emby_host），供"在 Emby 中打开"链接使用 */
    if (r.emby_host) window.__embyHost = r.emby_host;
    window.__embySeriesMap = {};
    embyData.series.forEach(function(x){ if (x.id) window.__embySeriesMap[x.id] = x; });
    embyLoaded = true;
    var st = embyData.stats;
    $('embyStats').style.display = '';
    $('embyStats2').style.display = '';
    $('emby-total').textContent      = st.total_series || 0;
    $('emby-aligned').textContent    = st.aligned || 0;
    $('emby-missing').textContent    = st.missing || 0;
    $('emby-extra').textContent      = st.extra || 0;
    $('emby-ongoing').textContent    = st.ongoing || 0;
    $('emby-unmatched').textContent  = (st.unmatched || 0) + (st.no_tmdb || 0);
    $('emby-movies').textContent     = st.total_movies || 0;
    $('emby-tmdb-errors').textContent = r.tmdb_errors || 0;
    renderEmbyList();
  } catch(e){ $('embyList').innerHTML = '<div class="list-empty">' + esc(e.message) + '</div>'; }
}
function setEmbyType(t){
  embyFilterState.type = t;
  document.querySelectorAll('#embyTypeFilter button').forEach(function(b){ b.classList.toggle('active', b.dataset.t === t); });
  var f = $('embyFilter');
  if (t === 'movies') {
    for (var i = 1; i <= 4; i++) f.children[i].style.display = 'none';
    if (embyFilterState.filter !== 'all') {
      embyFilterState.filter = 'all';
      document.querySelectorAll('#embyFilter button').forEach(function(b){ b.classList.toggle('active', b.dataset.f === 'all'); });
    }
  } else {
    for (var i = 1; i <= 4; i++) f.children[i].style.display = '';
  }
  renderEmbyList();
}
function setEmbyFilter(f){
  embyFilterState.filter = f;
  document.querySelectorAll('#embyFilter button').forEach(function(b){ b.classList.toggle('active', b.dataset.f === f); });
  renderEmbyList();
}
function renderEmbyList(){
  var list = $('embyList');
  var keyword = ($('embySearch').value || '').trim().toLowerCase();
  var arr;
  if (embyFilterState.type === 'movies') {
    arr = embyData.movies.slice();
    if (keyword) arr = arr.filter(function(x){ return (x.name||'').toLowerCase().indexOf(keyword) >= 0; });
    if (!arr.length) { list.innerHTML = '<div class="list-empty">无匹配项</div>'; return; }
    list.innerHTML = arr.slice(0, 500).map(renderMovieCard).join('');
    hydratePosters(list);
    return;
  }
  arr = embyData.series.slice();
  if (embyFilterState.filter === 'unmatched') {
    arr = arr.filter(function(x){
      var st = (x.tmdb_info || {}).match_status;
      return st === 'unmatched' || st === 'no_tmdb';
    });
  } else if (embyFilterState.filter !== 'all') {
    arr = arr.filter(function(x){ return (x.tmdb_info || {}).match_status === embyFilterState.filter; });
  }
  if (keyword) arr = arr.filter(function(x){ return (x.name||'').toLowerCase().indexOf(keyword) >= 0; });
  if (!arr.length) { list.innerHTML = '<div class="list-empty">无匹配项</div>'; return; }
  var prio = { missing: 0, extra: 1, unmatched: 2, no_tmdb: 2, ongoing: 3, aligned: 4 };
  arr.sort(function(a, b){
    var sa = (a.tmdb_info || {}).match_status || 'unmatched';
    var sb = (b.tmdb_info || {}).match_status || 'unmatched';
    var pa = prio[sa] != null ? prio[sa] : 9;
    var pb = prio[sb] != null ? prio[sb] : 9;
    if (pa !== pb) return pa - pb;
    var da = Math.abs((a.tmdb_info || {}).diff || 0);
    var db = Math.abs((b.tmdb_info || {}).diff || 0);
    if (da !== db) return db - da;
    return (a.name || '').localeCompare(b.name || '');
  });
  list.innerHTML = arr.slice(0, 500).map(renderSeriesCard).join('');
  hydratePosters(list);
}
function renderSeriesCard(s){
  var ti = s.tmdb_info || {};
  var mst = ti.match_status || 'unmatched';
  var stCls = 'st-' + mst;
  var badges = '';
  if (s.in_local && s.in_share) badges += '<span class="emby-badge loc">本地+分享</span>';
  else if (s.in_local) badges += '<span class="emby-badge loc">本地</span>';
  else if (s.in_share) badges += '<span class="emby-badge shr">分享</span>';

  var tsl = ti.tmdb_status || '';
  if (mst === 'aligned') badges += '<span class="emby-badge ok">' + icon('check') + ' 对齐</span>';
  else if (mst === 'missing') badges += '<span class="emby-badge miss">' + icon('x') + ' 缺 ' + Math.abs(ti.diff || 0) + ' 集</span>';
  else if (mst === 'extra') badges += '<span class="emby-badge extra">' + icon('alert') + ' 超 ' + (ti.diff || 0) + ' 集</span>';
  else if (mst === 'ongoing') badges += '<span class="emby-badge ongoing">' + icon('clock') + ' 在更</span>';
  else if (mst === 'unmatched') badges += '<span class="emby-badge dim">' + icon('info') + ' 未匹配</span>';
  else if (mst === 'no_tmdb') badges += '<span class="emby-badge dim">' + icon('info') + ' 无 TMDB ID</span>';
  else if (mst === 'pending') badges += '<span class="emby-badge gen">' + icon('info') + ' 待对照</span>';
  if (tsl === 'Ended') badges += '<span class="emby-badge gen">已完结</span>';
  else if (tsl === 'Returning Series') badges += '<span class="emby-badge gen">连载中</span>';
  if (s.year) badges += '<span class="emby-badge gen">' + s.year + '</span>';

  // ── 顶部对比摘要 ──
  var summary = '';
  if (ti.tmdb_total != null) {
    var localSeasons = (ti.seasons || []).filter(function(x){ return x.local > 0; }).length;
    var tmdbSeasons = (ti.seasons || []).filter(function(x){ return x.tmdb > 0; }).length;
    var diffCls = ti.diff < 0 ? 'miss' : (ti.diff > 0 ? 'extra' : 'ok');
    var diffTxt = ti.diff < 0 ? ('缺 ' + Math.abs(ti.diff) + ' 集')
                : (ti.diff > 0 ? ('超 ' + ti.diff + ' 集') : '完全对齐');
    summary = '<div class="season-summary">'
      + '<div class="item"><span class="k">本地</span><span class="v">' + localSeasons + ' 季 · ' + ti.local_total + ' 集</span></div>'
      + '<div class="item"><span class="k">TMDB</span><span class="v">' + tmdbSeasons + ' 季 · ' + ti.tmdb_total + ' 集</span></div>'
      + '<div class="item ' + diffCls + '"><span class="k">差异</span><span class="v">' + diffTxt + '</span></div>'
      + '</div>';
  } else if (mst === 'pending') {
    summary = '<div class="season-summary"><div class="item"><span class="k">本地</span><span class="v">' + (s.total_seasons||0) + ' 季 · ' + (s.total_episodes||0) + ' 集</span></div><div class="item"><span class="k">TMDB</span><span class="v" style="color:#9ca3af">点击「重新对照」拉取</span></div></div>';
  } else {
    summary = '<div class="season-summary"><div class="item"><span class="k">本地</span><span class="v">' + (s.total_seasons||0) + ' 季 · ' + (s.total_episodes||0) + ' 集</span></div></div>';
  }

  // ── 季详情列表 ──
  var seasonList = '';
  if (ti.seasons && ti.seasons.length) {
    seasonList = '<div class="season-list">' + ti.seasons.map(function(se){
      var cls = se.status === 'aligned' ? 'ok' : (se.status === 'missing' ? 'miss' : (se.status === 'extra' ? 'extra' : 'ok'));
      var tag = '';
      if (se.tmdb == null) {
        tag = '<span class="tag" style="background:#f3f4f6;color:#6b7280">—</span>';
        se.tmdb = '?';
      } else if (se.diff === 0) {
        tag = '<span class="tag">✓ 完整</span>';
      } else if (se.diff > 0) {
        tag = '<span class="tag">超 ' + se.diff + '</span>';
      } else {
        tag = '<span class="tag">缺 ' + Math.abs(se.diff) + '</span>';
      }
      return '<div class="season-row ' + cls + '">'
        + '<span class="s">S' + String(se.season).padStart(2,'0') + '</span>'
        + '<span class="d">本地 <b>' + se.local + '</b> 集 / TMDB <b>' + se.tmdb + '</b> 集</span>'
        + tag
        + '</div>';
    }).join('') + '</div>';
  }

  /* 海报改为 data-emby-src，由 hydratePosters() 带鉴权拉取 */
  var poster = s.has_image
    ? '<img data-emby-src="/api/emby/poster/' + esc(s.id) + '" loading="lazy">'
    : '无图';

  return '<div class="emby-card ' + stCls + '" onclick="showEmbyDetailById(\'' + s.id + '\')" style="cursor:pointer">'
    + '<div class="emby-head">'
    + '<div class="emby-poster">' + poster + '</div>'
    + '<div class="emby-body">'
    + '<div class="emby-title">' + esc(s.name || '?') + '</div>'
    + '<div class="emby-badges">' + badges + '</div>'
    + summary
    + seasonList
    + '</div>'
    + '</div>'
    + '</div>';
}

function showEmbyDetailById(id){
  var s = (window.__embySeriesMap || {})[id];
  if (!s) return;
  var ti = s.tmdb_info || {};
  var mst = ti.match_status || 'unmatched';
  var html = '';

  html += '<div style="display:flex;gap:16px;margin-bottom:16px">';
  html += '<div style="width:110px;height:165px;border-radius:8px;background:#f3f4f6;overflow:hidden;flex-shrink:0;display:flex;align-items:center;justify-content:center;color:#9ca3af;font-size:11px">';
  /* 海报改为 data-emby-src，由 hydratePosters() 带鉴权拉取 */
  html += s.has_image
    ? '<img data-emby-src="/api/emby/poster/' + esc(s.id) + '" style="width:100%;height:100%;object-fit:cover">'
    : '无图';
  html += '</div>';
  html += '<div style="flex:1;min-width:0">';
  html += '<h3 style="margin:0 0 8px;font-size:16px;line-height:1.3;word-break:break-word">' + esc(s.name || '?') + '</h3>';
  html += '<div class="emby-badges">';
  if (s.in_local && s.in_share) html += '<span class="emby-badge loc">本地+分享</span>';
  else if (s.in_local) html += '<span class="emby-badge loc">本地</span>';
  else if (s.in_share) html += '<span class="emby-badge shr">分享</span>';
  if (mst === 'aligned') html += '<span class="emby-badge ok">✓ 对齐</span>';
  else if (mst === 'missing') html += '<span class="emby-badge miss">缺 ' + Math.abs(ti.diff || 0) + ' 集</span>';
  else if (mst === 'extra') html += '<span class="emby-badge extra">超 ' + (ti.diff || 0) + ' 集</span>';
  else if (mst === 'ongoing') html += '<span class="emby-badge ongoing">在更</span>';
  else if (mst === 'unmatched') html += '<span class="emby-badge dim">未匹配</span>';
  else if (mst === 'no_tmdb') html += '<span class="emby-badge dim">无 TMDB</span>';
  else if (mst === 'pending') html += '<span class="emby-badge gen">待对照</span>';
  if (s.year) html += '<span class="emby-badge gen">' + s.year + '</span>';
  html += '</div>';
  html += '<div style="margin-top:8px;font-size:12px;color:#6b7280">';
  html += '本地 ' + (s.total_seasons || 0) + ' 季 · ' + (s.total_episodes || 0) + ' 集';
  if (ti.tmdb_total != null) html += '　|　TMDB ' + ti.tmdb_total + ' 集';
  html += '</div>';
  html += '</div></div>';

  if (ti.seasons && ti.seasons.length) {
    html += '<h4 style="margin:16px 0 8px;font-size:13px;color:#374151">季集对比</h4>';
    html += '<table style="width:100%;border-collapse:collapse;font-size:12px">';
    html += '<thead><tr style="color:#6b7280"><th style="text-align:left;padding:6px 4px">季</th><th style="text-align:right;padding:6px 4px">本地</th><th style="text-align:right;padding:6px 4px">TMDB</th><th style="text-align:right;padding:6px 4px">差异</th><th style="text-align:left;padding:6px 4px">状态</th></tr></thead><tbody>';
    ti.seasons.forEach(function(se){
      var cls = se.status === 'aligned' ? '#10b981' : (se.status === 'missing' ? '#ef4444' : '#f59e0b');
      var statusTxt = '';
      if (se.tmdb == null) statusTxt = '—';
      else if (se.diff === 0) statusTxt = '✓ 完整';
      else if (se.diff > 0) statusTxt = '超 ' + se.diff;
      else statusTxt = '缺 ' + Math.abs(se.diff);
      var diffTxt = (se.diff == null) ? '—' : (se.diff > 0 ? '+' + se.diff : '' + se.diff);
      html += '<tr style="border-top:1px solid #f3f4f6">';
      html += '<td style="padding:6px 4px;font-weight:600">S' + String(se.season).padStart(2, '0') + '</td>';
      html += '<td style="text-align:right;padding:6px 4px">' + se.local + '</td>';
      html += '<td style="text-align:right;padding:6px 4px">' + (se.tmdb != null ? se.tmdb : '?') + '</td>';
      html += '<td style="text-align:right;padding:6px 4px;color:' + cls + '">' + diffTxt + '</td>';
      html += '<td style="padding:6px 4px;color:' + cls + '">' + statusTxt + '</td>';
      html += '</tr>';
    });
    html += '</tbody></table>';
  }

  if (ti.seasons && ti.seasons.length) {
    var missingHtml = '';
    ti.seasons.forEach(function(se){
      if (se.missing && se.missing.length) {
        missingHtml += '<div style="font-size:12px;color:#991b1b;margin:4px 0">S' + String(se.season).padStart(2,'0') + ' 缺 ' + se.missing.map(function(n){return 'E' + String(n).padStart(2,'0')}).join(', ') + '</div>';
      }
    });
    if (missingHtml) {
      html += '<h4 style="margin:16px 0 8px;font-size:13px;color:#374151">缺集详情</h4>';
      html += '<div style="background:#fef2f2;border:1px solid #fee2e2;border-radius:8px;padding:10px">' + missingHtml + '</div>';
    }
  }

  /* "在 Emby 中打开"链接改为读配置，不再硬编码 IP */
  var embyHost = (window.__embyHost || '').replace(/\/+$/, '');
  html += '<div style="margin-top:16px;display:flex;gap:8px;justify-content:flex-end">';
  if (s.id && embyHost) {
    html += '<a href="' + esc(embyHost) + '/web/index.html#!/item?id=' + esc(s.id) + '" target="_blank" rel="noopener" style="text-decoration:none" class="btn gray">在 Emby 中打开</a>';
  }
  html += '<button class="btn gray" onclick="closeModal()">关闭</button>';
  html += '</div>';

  openModal(html);
  hydratePosters($('modalBody'));
}

function renderMovieCard(m){
  var badges = '';
  if (m.in_local && m.in_share) badges += '<span class="emby-badge loc">本地+分享</span>';
  else if (m.in_local) badges += '<span class="emby-badge loc">本地</span>';
  else if (m.in_share) badges += '<span class="emby-badge shr">分享</span>';
  if (m.year) badges += '<span class="emby-badge gen">' + m.year + '</span>';
  /* 海报改为 data-emby-src，由 hydratePosters() 带鉴权拉取 */
  var poster = m.has_image
    ? '<img data-emby-src="/api/emby/poster/' + esc(m.id) + '" loading="lazy">'
    : '无图';
  return '<div class="emby-card"><div class="emby-head">'
    + '<div class="emby-poster">' + poster + '</div>'
    + '<div class="emby-body">'
    + '<div class="emby-title">' + esc(m.name || '?') + '</div>'
    + '<div class="emby-meta">电影</div>'
    + '<div class="emby-badges">' + badges + '</div>'
    + '</div></div></div>';
}

/* ═══════════ 追更订阅 ═══════════ */
async function loadSubscriptions(){
  try {
    var r = await api('/api/subscriptions');
    if (r.status !== 'success') throw new Error(r.message || '失败');
    currentSubs = r.subscriptions || [];
    $('subEnabled').checked = !!r.enabled;
    $('subCheckTmdb').checked = !!r.check_tmdb;
    $('subInterval').value = r.interval_min || 30;
    $('subCount').textContent = currentSubs.length + ' 部';

    if (!currentSubs.length) {
      $('subList').innerHTML = '<div class="list-empty">暂无订阅。去「影视探索」搜索剧集，点「+ 订阅」</div>';
    } else {
      $('subList').innerHTML = currentSubs.map(function(s, i){
        /* 订阅卡的海报也可能是 Emby poster URL，走 data-emby-src；TMDB 图片直接 src */
        var poster;
        if (!s.poster) {
          poster = '';
        } else if (s.poster.indexOf('/api/emby/poster/') === 0) {
          poster = '<img data-emby-src="' + esc(s.poster) + '" loading="lazy">';
        } else {
          poster = '<img src="' + esc(s.poster) + '" loading="lazy" onerror="this.style.display=\'none\'">';
        }
        var tmdbExtra = s.tmdb_total ? ' · TMDB ' + s.tmdb_total + ' 集' : '';
        var st = s.tmdb_status ? ' · ' + s.tmdb_status : '';
        return '<div class="sub-item">'
          + '<div class="p">' + poster + '</div>'
          + '<div class="body">'
          + '<div class="name">' + esc(s.name) + '</div>'
          + '<div class="meta">最新集: <code>' + (s.latest_ep || '未检查') + '</code>' + tmdbExtra + st + '</div>'
          + '</div>'
          + '<div class="actions">'
          + '<button class="btn sm ' + (s.enabled ? 'green' : 'gray') + '" onclick="toggleSubEnabled(' + i + ')">' + (s.enabled ? '启用' : '暂停') + '</button>'
          + '<button class="btn sm red" onclick="removeSub(' + i + ')">删</button>'
          + '</div>'
          + '</div>';
      }).join('');
      hydratePosters($('subList'));
    }
  } catch(e){ toast('加载订阅失败: ' + e.message, 'error', 4000); }
}
async function saveSubSettings(){
  try {
    await api('/api/subscriptions/settings', { method: 'POST', body: JSON.stringify({
      enabled: $('subEnabled').checked,
      check_tmdb: $('subCheckTmdb').checked,
      interval_min: parseInt($('subInterval').value) || 30,
    })});
    toast('设置已保存', 'success');
  } catch(e){ toast('保存失败: ' + e.message, 'error'); }
}
async function toggleSubEnabled(i){
  currentSubs[i].enabled = !currentSubs[i].enabled;
  try {
    await api('/api/subscriptions', { method: 'POST', body: JSON.stringify({ subscriptions: currentSubs }) });
    loadSubscriptions();
  } catch(e){ toast(e.message, 'error'); }
}
async function removeSub(i){
  if (!(await showModalConfirm('删除订阅《' + currentSubs[i].name + '》？'))) return;
  currentSubs.splice(i, 1);
  try {
    await api('/api/subscriptions', { method: 'POST', body: JSON.stringify({ subscriptions: currentSubs }) });
    loadSubscriptions();
  } catch(e){ toast(e.message, 'error'); }
}
async function checkSubsNow(){
  toast('正在检查...');
  try {
    var r = await api('/api/subscriptions/check', { method: 'POST' });
    if (r.status !== 'success') throw new Error(r.message || '失败');
    var ups = r.updates || [];
    if (!ups.length) {
      toast('检查完成：暂无变化', 'success');
      $('subUpdatesPanel').style.display = 'none';
    } else {
      toast('发现 ' + ups.length + ' 部变化', 'success');
      $('subUpdatesPanel').style.display = '';
      $('subUpdates').innerHTML = ups.map(function(u){
        var parts = ['📺 《' + esc(u.name) + '》'];
        if (u.new_ep) parts.push('🆕 ' + esc(u.new_ep.old) + ' → <b>' + esc(u.new_ep.new) + '</b>');
        if (u.missing) parts.push('⚠️ 缺 ' + u.missing.diff + ' 集（TMDB 已播 ' + u.missing.tmdb_total + '）');
        return '<div class="list-item exempt"><div class="t">' + parts[0] + '</div>'
          + '<div class="d">' + parts.slice(1).join(' · ') + '</div></div>';
      }).join('');
    }
    loadSubscriptions();
  } catch(e){ toast('检查失败: ' + e.message, 'error'); }
}

/* ═══════════ 每日晨报 ═══════════ */
async function loadMorning(){
  try {
    var r = await api('/api/morning');
    var mr = r.morning || {};
    $('mrEnabled').checked = !!mr.enabled;
    $('mrHour').value = mr.hour != null ? mr.hour : 9;
    $('mrMinute').value = mr.minute != null ? mr.minute : 0;
    $('mrPrescan').value = mr.prescan_min != null ? mr.prescan_min : 5;
    var items = mr.items || [];
    $('mrItemStats').checked = items.indexOf('stats') >= 0;
    $('mrItemSubs').checked = items.indexOf('subscriptions') >= 0;
    $('mrItemGap').checked = items.indexOf('emby_gap') >= 0;
  } catch(e){ toast('加载晨报设置失败: ' + e.message, 'error'); }
}
async function saveMorning(){
  var items = [];
  if ($('mrItemStats').checked) items.push('stats');
  if ($('mrItemSubs').checked)  items.push('subscriptions');
  if ($('mrItemGap').checked)   items.push('emby_gap');
  try {
    await api('/api/morning', { method: 'POST', body: JSON.stringify({
      enabled: $('mrEnabled').checked,
      hour: parseInt($('mrHour').value) || 0,
      minute: parseInt($('mrMinute').value) || 0,
      prescan_min: parseInt($('mrPrescan').value) || 0,
      items: items,
    })});
    toast('已保存', 'success');
  } catch(e){ toast('保存失败: ' + e.message, 'error'); }
}
async function previewMorning(force){
  var items = [];
  if ($('mrItemStats').checked) items.push('stats');
  if ($('mrItemSubs').checked)  items.push('subscriptions');
  if ($('mrItemGap').checked)   items.push('emby_gap');
  try {
    if (force) toast('现场扫描中，可能稍慢...');
    var r = await api('/api/morning/preview', { method: 'POST', body: JSON.stringify({ items: items, force: force }) });
    if (r.status !== 'success') throw new Error(r.message || '失败');
    $('mrPreviewPanel').style.display = '';
    $('mrPreview').textContent = r.text || '(空)';
  } catch(e){ toast(e.message, 'error'); }
}
async function sendMorningNow(){
  if (!(await showModalConfirm('立即发送晨报到 Telegram？（会现场扫描 Emby）'))) return;
  toast('生成并发送中...');
  try {
    var r = await api('/api/morning/send', { method: 'POST', body: JSON.stringify({ force: true }) });
    toast(r.message || '完成', r.status === 'success' ? 'success' : 'error');
  } catch(e){ toast(e.message, 'error'); }
}

/* ═══════════ 每日汇报 ═══════════ */

async function loadRecords(){
  var n = parseInt($('recordN').value) || 50;
  $('recordsList').innerHTML = '<div class="list-empty">加载中...</div>';
  try {
    var r = await api('/api/records?n=' + n);
    var list = r.records || [];
    if (!list.length) {
      $('recordsList').innerHTML = '<div class="list-empty">暂无记录</div>';
      return;
    }
    var html = '';
    list.forEach(function(rec){
      var cat = rec.category || '';
      var cls = '';
      if (cat.indexOf('清理') >= 0) cls = 'record-clean';
      else if (cat.indexOf('巡检') >= 0 || cat.indexOf('扫描') >= 0) cls = 'record-scan';
      else if (cat.indexOf('配置') >= 0) cls = 'record-config';
      else if (cat.indexOf('搜片') >= 0) cls = 'record-search';

      html += '<div class="record-item ' + cls + '">';
      html += '<div class="record-time">' + esc(rec.ts) + '</div>';
      html += '<div class="record-body">';
      html += '<div class="record-head"><span class="record-tag">' + esc(cat) + '</span>' + esc(rec.title) + '</div>';
      if (rec.details && rec.details.length) {
        html += '<div class="record-details">';
        rec.details.slice(0, 15).forEach(function(d){
          html += '<div>' + esc(d) + '</div>';
        });
        if (rec.details.length > 15) {
          html += '<div class="dim">… 共 ' + rec.details.length + ' 条详情</div>';
        }
        html += '</div>';
      }
      html += '</div></div>';
    });
    $('recordsList').innerHTML = html;
  } catch(e) {
    $('recordsList').innerHTML = '<div class="list-empty">加载失败: ' + esc(e.message) + '</div>';
  }
}

async function loadStats(full, force){
  var kw = (full ? 'full ' : '') + (force ? 'force' : '');
  var url = '/api/ingest?full=' + (full?1:0) + '&force=' + (force?1:0);
  $('statsOut').textContent = force ? '正在现场扫描 Emby（约 10-30 秒）...' : '读取缓存中...';
  $('statsCacheHint').textContent = '';
  try {
    var r = await api(url);
    $('statsOut').textContent = r.text || JSON.stringify(r, null, 2);
    var age = r.cache_ts ? Math.round((Date.now()/1000) - r.cache_ts) : null;
    var ageStr = age == null ? '—' : (age < 60 ? age + ' 秒前' : (age < 3600 ? Math.floor(age/60) + ' 分钟前' : Math.floor(age/3600) + ' 小时前'));
    $('statsCacheHint').textContent = (r.from_cache ? '📦 来自缓存' : '🔄 现场扫描') + ' · ' + ageStr;
  } catch(e){ $('statsOut').textContent = '错误: ' + e.message; }
}

/* ═══════════ 治理策略 ═══════════ */
async function loadStrategy(){
  try {
    var r = await api('/api/strategy');
    currentStrategy = r.strategy || currentStrategy;
    applyStrategyUI();
  } catch(e){ console.warn(e); }
}
function applyStrategyUI(){
  document.querySelectorAll('.strategy-option[data-v]').forEach(function(el){
    el.classList.toggle('on', el.dataset.v === currentStrategy.decision);
  });
  document.querySelectorAll('.strategy-option[data-sp]').forEach(function(el){
    el.classList.toggle('on', el.dataset.sp === (currentStrategy.special_action || 'keep'));
  });
  $('strMultiProtect').checked = !!currentStrategy.multi_season_protect;
  $('strTieLocal').checked = !!currentStrategy.tie_keep_local;
  renderExempt();
}
function pickStrategy(v){
  currentStrategy.decision = v;
  applyStrategyUI();
  saveStrategy();
}
function pickSpecial(v){
  currentStrategy.special_action = v;
  applyStrategyUI();
  saveStrategy();
}
async function saveStrategy(){
  try {
    var body = {
      decision: currentStrategy.decision,
      multi_season_protect: $('strMultiProtect').checked,
      tie_keep_local: $('strTieLocal').checked,
      special_action: currentStrategy.special_action || 'keep',
      exempt_keywords: currentStrategy.exempt_keywords || [],
    };
    var r = await api('/api/strategy', { method: 'POST', body: JSON.stringify(body) });
    currentStrategy = r.strategy || currentStrategy;
    applyStrategyUI();
    toast('策略已保存', 'success');
  } catch(e){ toast('保存失败: ' + e.message, 'error'); }
}

/* ═══════════ 白名单 ═══════════ */
function renderExempt(){
  var kws = currentStrategy.exempt_keywords || [];
  $('exemptList').innerHTML = kws.length
    ? kws.map(function(k){
        return '<span class="tag">' + esc(k) + '<span class="rm" onclick="removeExempt(\'' + esc(k).replace(/'/g, '&#39;') + '\')">×</span></span>';
      }).join('')
    : '<span style="color:#9ca3af;font-size:12px">暂无关键词</span>';
}
function addExempt(){
  var v = $('exemptInput').value.trim();
  if (!v) return;
  var kws = currentStrategy.exempt_keywords || [];
  if (kws.indexOf(v) < 0) kws.push(v);
  currentStrategy.exempt_keywords = kws;
  $('exemptInput').value = '';
  saveStrategyExempt();
}
function removeExempt(k){
  var kws = currentStrategy.exempt_keywords || [];
  var i = kws.indexOf(k);
  if (i >= 0) kws.splice(i, 1);
  currentStrategy.exempt_keywords = kws;
  saveStrategyExempt();
}
async function saveStrategyExempt(){
  try {
    await api('/api/strategy', { method: 'POST', body: JSON.stringify({ exempt_keywords: currentStrategy.exempt_keywords }) });
    renderExempt();
    toast('白名单已更新', 'success');
  } catch(e){ toast(e.message, 'error'); }
}
async function scanExempt(){
  $('exemptMatches').innerHTML = '<div class="list-empty">扫描中...</div>';
  try {
    var r = await api('/api/exempt/scan');
    if (r.status !== 'success') throw new Error(r.message || '失败');
    var m = r.matches || [];
    if (!m.length) {
      $('exemptMatches').innerHTML = '<div class="list-empty">双库中未命中白名单</div>';
    } else {
      $('exemptMatches').innerHTML = m.map(function(x){
        var seasons = (x.seasons || []).map(function(s){
          return s === 0 ? 'S00(特别篇)' : 'S' + String(s).padStart(2,'0');
        }).join(', ');
        return '<div class="list-item exempt">'
          + '<div class="t">🛡️ 《' + esc(x.title) + '》</div>'
          + '<div class="d">命中「' + esc(x.keyword) + '」 · 库: ' + (x.libs || []).join('/') + (seasons ? ' · 季: ' + seasons : '') + '</div>'
          + '</div>';
      }).join('');
    }
  } catch(e){ $('exemptMatches').innerHTML = '<div class="list-empty">扫描失败: ' + esc(e.message) + '</div>'; }
}

/* ═══════════ 入库监控 ═══════════ */
async function loadIngest(){
  try {
    var r = await api('/api/ingest/settings');
    if (r.status !== 'success') throw new Error(r.message || '失败');
    var s = r.settings || {};
    $('ingEnabled').checked = !!s.enabled;
    $('ingInterval').value = s.interval_min || 5;
    // 显示缓存状态
    try {
      var st = await api('/api/ingest/status');
      if (st.has_cache) {
        var age = st.age_sec;
        var ageStr = age < 60 ? age + ' 秒前' : (age < 3600 ? Math.floor(age/60) + ' 分钟前' : Math.floor(age/3600) + ' 小时前');
        var stats = st.stats || {};
        $('ingStatus').textContent = '📦 缓存于 ' + ageStr + ' · 电影 ' + (stats.movies||0) + ' / 剧集 ' + (stats.series||0) + ' 部 / ' + (stats.episodes||0) + ' 集';
      } else {
        $('ingStatus').textContent = '⚪ 暂无缓存，等待后台首次刷新';
      }
    } catch(e){ $('ingStatus').textContent = ''; }
  } catch(e){ toast('加载入库设置失败: ' + e.message, 'error'); }
}
async function saveIngest(){
  try {
    await api('/api/ingest/settings', { method: 'POST', body: JSON.stringify({
      enabled: $('ingEnabled').checked,
      interval_min: parseInt($('ingInterval').value) || 5,
    })});
    toast('已保存', 'success');
    loadIngest();
  } catch(e){ toast('保存失败: ' + e.message, 'error'); }
}

/* ═══════════ 服务配置 ═══════════ */
async function loadConfig(){
  try {
    revealed = {};
    var r = await api('/api/config');
    var c = r.config || {};
    $('cfg-emby-host').value = c.emby_host || '';
    $('cfg-emby-key').value  = c.emby_key || '';
    $('cfg-tmdb-key').value  = c.tmdb_key || '';
    $('cfg-tg-token').value  = c.telegram_bot_token || '';
    $('cfg-tg-chat').value   = c.telegram_chat_id || '';
    $('cfg-tg-users').value  = c.telegram_allowed_users || '';
    ['emby_key','tmdb_key','telegram_bot_token'].forEach(function(k){
      var btn = document.querySelector('.eye-btn[data-eye="' + k + '"]');
      if (btn) { btn.classList.remove('on'); btn.innerHTML = icon('eye') + '<span>查看</span>'; }
      var row = document.getElementById('row-' + k.replace('_key','-key').replace('telegram_bot_token','tg-token'));
      if (row) row.classList.remove('revealed');
    });
  } catch(e){ toast('加载配置失败: ' + e.message, 'error', 4000); }
}
async function toggleReveal(key){
  var btn = document.querySelector('.eye-btn[data-eye="' + key + '"]');
  var inputId = { emby_key:'cfg-emby-key', tmdb_key:'cfg-tmdb-key', telegram_bot_token:'cfg-tg-token' }[key];
  var rowId = { emby_key:'row-emby-key', tmdb_key:'row-tmdb-key', telegram_bot_token:'row-tg-token' }[key];
  var input = $(inputId);
  if (!input) return;
  if (revealed[key]) {
    revealed[key] = false;
    try { var r = await api('/api/config'); input.value = (r.config || {})[key] || ''; } catch(e){}
    if (btn) { btn.classList.remove('on'); btn.innerHTML = icon('eye') + '<span>查看</span>'; }
    if (rowId && $(rowId)) $(rowId).classList.remove('revealed');
  } else {
    if (!(await showModalConfirm('⚠️ 即将显示完整密钥，请确保周围环境安全。继续？'))) return;
    try {
      var r = await api('/api/config?reveal=1');
      input.value = (r.config || {})[key] || '';
      revealed[key] = true;
      if (btn) { btn.classList.add('on'); btn.innerHTML = icon('eye-off') + '<span>隐藏</span>'; }
      if (rowId && $(rowId)) $(rowId).classList.add('revealed');
      toast('已显示，60 秒后自动重新脱敏', 'success');
      setTimeout(function(){ if (revealed[key]) toggleReveal(key); }, 60000);
    } catch(e){ toast('加载失败: ' + e.message, 'error'); }
  }
}
async function loadBotStatus(){
  try {
    var r = await api('/api/bot/status');
    var b = r.bot || {};
    var running = b.running;
    var uname = b.bot_username ? '@' + b.bot_username : '未连接';
    var lastAgo = b.last_poll_ago != null ? (b.last_poll_ago + ' 秒前') : '—';
    var err = b.last_error ? ('\n⚠️ ' + b.last_error) : '';
    $('botStatus').innerHTML =
      '<div class="bot-status"><span class="dot ' + (running ? 'on' : 'off') + '"></span>' +
      (running ? 'Bot 运行中 ' + uname : 'Bot 未运行') +
      '</div>' +
      '<div style="margin-top:6px;color:#9ca3af">上次轮询: ' + lastAgo + err + '</div>';
  } catch(e){ $('botStatus').textContent = '加载失败: ' + e.message; }
}
async function saveConfig(){
  var body = {
    emby_host: $('cfg-emby-host').value.trim(),
    emby_key:  $('cfg-emby-key').value.trim(),
    tmdb_key:  $('cfg-tmdb-key').value.trim(),
    telegram_bot_token:     $('cfg-tg-token').value.trim(),
    telegram_chat_id:       $('cfg-tg-chat').value.trim(),
    telegram_allowed_users: $('cfg-tg-users').value.trim(),
  };
  try {
    var r = await api('/api/config', { method: 'POST', body: JSON.stringify(body) });
    if (r.status === 'success') {
      toast('配置已保存并生效', 'success');
      loadDashboard(); embyLoaded = false; loadConfig();
      setTimeout(loadBotStatus, 1500);
    } else toast('保存失败: ' + (r.message || ''), 'error', 4000);
  } catch(e){ toast(e.message, 'error', 4000); }
}
async function testEmby(){
  toast('测试中...');
  try {
    var r = await api('/api/config/test/emby', { method: 'POST', body: JSON.stringify({ emby_host: $('cfg-emby-host').value.trim(), emby_key: $('cfg-emby-key').value.trim() }) });
    showModalAlert(r.message);
  } catch(e){ showModalAlert(e.message); }
}
async function testTmdb(){
  toast('测试中...');
  try {
    var r = await api('/api/config/test/tmdb', { method: 'POST', body: JSON.stringify({ tmdb_key: $('cfg-tmdb-key').value.trim() }) });
    showModalAlert(r.message);
  } catch(e){ showModalAlert(e.message); }
}
async function testTelegram(){
  toast('发送中...');
  try {
    var r = await api('/api/config/test/telegram', { method: 'POST', body: JSON.stringify({ telegram_bot_token: $('cfg-tg-token').value.trim(), telegram_chat_id: $('cfg-tg-chat').value.trim() }) });
    showModalAlert(r.message);
  } catch(e){ showModalAlert(e.message); }
}

/* ═══════════ 初始化 ═══════════ */
document.addEventListener('DOMContentLoaded', async function(){
  initTheme();
  var st = $('sideTitle');
  if (st && !st.querySelector('svg')) st.insertAdjacentHTML('afterbegin', icon('shield'));
  injectIcons();
  renderMenu();
  switchExploreTab_init();
  // 预加载订阅列表（供探索页订阅按钮用）
  try { var r = await api('/api/subscriptions'); currentSubs = r.subscriptions || []; } catch(e){}
  loadDashboard();
  pollTmdbProgress();
});