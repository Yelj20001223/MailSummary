/* P0/P2 前端：原生 JS 单页应用，无构建依赖 */
const CATEGORY_LIST = ['合同法务', '财务发票', '订单采购', '人事招聘', '客户往来',
  '系统通知', '验证码安全', '市场广告', '社交', '其他'];
const ALL_TAGS = ['待回复', '待付款', '合同', '发票', '招聘', '通知', '广告', '垃圾'];
const RULE_FIELDS = ['subject', 'body', 'from', 'domain', 'attachment'];
const RULE_OPS = ['contains', 'in_list', 'regex', 'equals'];
/* 邮件列表分页：单次拉取条数，配合「加载更多」累加 offset */
const PAGE_SIZE = 50;

/* 当前分类名（来自后端统计，可自定义；未加载时回退内置默认） */
function categoryNames() {
  const names = (state.stats.categories || []).map((c) => c.name);
  return names.length ? names : CATEGORY_LIST;
}

const state = {
  accounts: [],
  providers: [],
  view: 'all',          // all | inbox | junk | other | unread | starred | companies | company | digest
  accountId: null,      // null = 全部邮箱
  category: null,       // 业务分类筛选
  tag: null,            // 标签筛选
  search: '',
  messages: [],
  total: 0,             // 当前筛选下的邮件总数（用于「加载更多」）
  unreadTotal: 0,
  loadingMore: false,
  selected: new Set(),
  activeMessageId: null,
  detailBody: { mode: 'html', images: false },  // 详情正文：富文本/纯文本、是否显示外链图片
  stats: { categories: [], tags: [] },
  ruleRows: [],
  deletedRuleIds: [],
  companies: [],        // 公司卡片列表
  companyId: null,      // 选中公司
  companyDetail: null,  // 公司详情
  llmConfig: null,      // 摘要 LLM 配置（P4）
};

/* ---------------- 基础工具 ---------------- */
async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) { /* ignore */ }
    throw new Error(detail);
  }
  if (res.status === 204) return null;
  return res.json();
}

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function toast(msg, ms = 2200) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.remove('hidden');
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.add('hidden'), ms);
}

function fmtDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const pad = (n) => String(n).padStart(2, '0');
  if (sameDay) return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function scoreClass(score) {
  if (score >= 7) return 'important';
  if (score >= 4) return 'todo';
  return '';
}

/* ---------------- 账号 ---------------- */
async function loadAccounts() {
  state.accounts = await api('/api/accounts');
  renderAccountTabs();
  renderSideAccounts();
}

function renderAccountTabs() {
  const box = document.getElementById('account-tabs');
  const all = state.accounts.length;
  const tabs = [{
    id: null, email: '全部邮箱', color: '#4f46e5',
    unread: state.accounts.reduce((s, a) => s + (a.unread_count || 0), 0),
    status: '',
  }, ...state.accounts.map((a) => ({
    id: a.id, email: a.email, color: a.color,
    unread: a.unread_count || 0, status: a.last_sync_status,
    needsAuth: a.auth_mode !== 'imap' && !a.oauth_connected,
  }))];

  box.innerHTML = tabs.map((t) => `
    <button class="account-tab ${state.accountId === t.id ? 'active' : ''}" data-acc="${t.id ?? ''}">
      <span class="dot" style="background:${esc(t.color)}"></span>
      <span>${esc(t.email)}</span>
      ${t.unread ? `<span class="badge">${t.unread}</span>` : ''}
      ${t.needsAuth ? `<span class="oauth-badge" data-oauth="${t.id}" title="待完成 OAuth 授权，点击授权">授权</span>` : ''}
      ${t.status ? `<span class="status-dot ${esc(t.status)}" title="${esc(t.status)}"></span>` : ''}
    </button>
  `).join('');
  box.querySelectorAll('[data-oauth]').forEach((el) => {
    el.onclick = (ev) => {
      ev.stopPropagation();
      authorizeAccount(state.accounts.find((a) => a.id === Number(el.dataset.oauth)));
    };
  });
  box.querySelectorAll('.account-tab').forEach((el) => {
    el.onclick = () => {
      const v = el.dataset.acc;
      state.accountId = v === '' ? null : Number(v);
      renderAccountTabs();
      renderSideAccounts();
      loadStats();
      loadMessages();
    };
  });
  if (all === 0) {
    box.insertAdjacentHTML('afterend', '');
  }
}

function renderSideAccounts() {
  const box = document.getElementById('side-accounts');
  if (!state.accounts.length) {
    box.innerHTML = '<div class="muted">尚无账号，请点击右上「添加账号」</div>';
    return;
  }
  box.innerHTML = state.accounts.map((a) => `
    <div class="side-row">
      <button class="side-item ${state.accountId === a.id ? 'active' : ''}" data-side-acc="${a.id}">
        ${esc(a.name)}<div class="muted">${esc(a.email)}</div>
      </button>
      <span class="side-actions">
        <button class="side-act" data-del-acc="${a.id}" title="删除账号">✕</button>
      </span>
    </div>
  `).join('');
  box.querySelectorAll('[data-side-acc]').forEach((el) => {
    el.onclick = () => {
      state.accountId = Number(el.dataset.sideAcc);
      renderAccountTabs();
      renderSideAccounts();
      loadStats();
      loadMessages();
    };
  });
  box.querySelectorAll('[data-del-acc]').forEach((el) => {
    el.onclick = (ev) => {
      ev.stopPropagation();
      deleteAccount(Number(el.dataset.delAcc));
    };
  });
}

/* 删除账号：二次确认后级联删除该账号的邮件与摘要（不可恢复） */
async function deleteAccount(id) {
  const acc = state.accounts.find((a) => a.id === id);
  if (!acc) return;
  const ok = await appConfirm(
    '删除账号',
    `确认删除「${acc.name}（${acc.email}）」？该账号下的邮件与摘要将一并删除，且不可恢复。`,
  );
  if (!ok) return;
  try {
    await api(`/api/accounts/${id}`, { method: 'DELETE' });
    if (state.accountId === id) state.accountId = null;
    state.selected.clear();
    state.companyId = null;
    if (state.view === 'company') state.view = 'all';
    closeDetail();
    toast('账号已删除');
    await loadAccounts();
    await loadCompanies();
    await loadStats();
    if (state.view === 'companies') renderCompanyGrid();
    else await loadMessages();
  } catch (e) {
    toast(`删除失败：${e.message}`, 3200);
  }
}

/* ---------------- 分类 / 标签树 ---------------- */
async function loadStats() {
  const params = new URLSearchParams();
  if (state.accountId) params.set('account_id', state.accountId);
  try {
    state.stats = await api(`/api/classify/stats?${params.toString()}`);
  } catch (e) {
    state.stats = { categories: [], tags: [] };
  }
  renderSideCategories();
  renderSideTags();
}

function renderSideCategories() {
  const box = document.getElementById('side-categories');
  const cats = state.stats.categories || [];
  const totalCount = cats.reduce((s, c) => s + c.count, 0);
  const head = `
    <button class="side-item ${state.category === null ? 'active' : ''}" data-cat="">
      全部分类 ${totalCount ? `<span class="side-count">${totalCount}</span>` : ''}
    </button>`;
  const rows = cats.map((c) => `
    <div class="side-row">
      <button class="side-item ${state.category === c.name ? 'active' : ''}" data-cat="${esc(c.name)}">
        ${esc(c.name)} ${c.count ? `<span class="side-count">${c.count}</span>` : ''}
      </button>
      <span class="side-actions">
        <button class="side-act" data-rename="${esc(c.name)}" title="重命名">✎</button>
        <button class="side-act" data-del="${esc(c.name)}" title="删除">✕</button>
      </span>
    </div>`).join('');
  box.innerHTML = head + rows;
  box.querySelectorAll('[data-cat]').forEach((el) => {
    el.onclick = () => {
      state.category = el.dataset.cat === '' ? null : el.dataset.cat;
      // 分类与标签互斥：选中某个分类时清掉标签筛选（否则两个维度叠加会出现「有数字但列表为空」）
      if (state.category !== null) state.tag = null;
      ensureListView();
      renderSideCategories();
      renderSideTags();
      loadMessages();
    };
  });
  box.querySelectorAll('[data-rename]').forEach((el) => {
    el.onclick = (ev) => { ev.stopPropagation(); renameCategory(el.dataset.rename); };
  });
  box.querySelectorAll('[data-del]').forEach((el) => {
    el.onclick = (ev) => { ev.stopPropagation(); deleteCategory(el.dataset.del); };
  });
}

/* 通用输入/确认弹窗：替代原生 prompt/confirm，兼容桌面 WebView 引擎 */
function _askModal(opts) {
  const { title = '提示', msg = '', value = '', withInput = true, okText = '确定' } = opts;
  return new Promise((resolve) => {
    const mask = document.getElementById('ask-modal');
    const inputEl = document.getElementById('ask-input');
    document.getElementById('ask-title').textContent = title;
    document.getElementById('ask-msg').textContent = msg;
    document.getElementById('ask-field').classList.toggle('hidden', !withInput);
    document.getElementById('ask-ok').textContent = okText;
    inputEl.value = value;

    const done = (result) => {
      mask.classList.add('hidden');
      document.getElementById('ask-ok').onclick = null;
      document.getElementById('ask-cancel').onclick = null;
      document.getElementById('ask-close').onclick = null;
      resolve(result);
    };
    document.getElementById('ask-ok').onclick = () => done(withInput ? inputEl.value : true);
    document.getElementById('ask-cancel').onclick = () => done(null);
    document.getElementById('ask-close').onclick = () => done(null);
    mask.classList.remove('hidden');
    if (withInput) setTimeout(() => { inputEl.focus(); inputEl.select(); }, 0);
  });
}

function appPrompt(title, value = '', msg = '') {
  return _askModal({ title, msg, value, withInput: true });
}

function appConfirm(title, msg = '') {
  return _askModal({ title, msg, withInput: false, okText: '确定' });
}

async function addCategory() {
  const name = ((await appPrompt('新增分类', '', '请输入新的分类名称：')) || '').trim();
  if (!name) return;
  try {
    await api('/api/classify/categories', { method: 'POST', body: JSON.stringify({ name }) });
    toast('分类已新增');
    await loadStats();
  } catch (e) {
    toast(`新增失败：${e.message}`, 3200);
  }
}

async function renameCategory(oldName) {
  const name = ((await appPrompt('重命名分类', oldName, `将「${oldName}」重命名为：`)) || '').trim();
  if (!name || name === oldName) return;
  try {
    await api(`/api/classify/categories/${encodeURIComponent(oldName)}`, {
      method: 'PUT', body: JSON.stringify({ name }),
    });
    if (state.category === oldName) state.category = name;
    toast('分类已重命名');
    await loadStats();
    await loadMessages();
  } catch (e) {
    toast(`重命名失败：${e.message}`, 3200);
  }
}

async function deleteCategory(name) {
  const ok = await appConfirm('删除分类', `确认删除「${name}」？该分类下的邮件将移入「其他」。`);
  if (!ok) return;
  try {
    await api(`/api/classify/categories/${encodeURIComponent(name)}`, { method: 'DELETE' });
    if (state.category === name) state.category = null;
    toast('分类已删除');
    await loadStats();
    await loadMessages();
  } catch (e) {
    toast(`删除失败：${e.message}`, 3200);
  }
}

function renderSideTags() {
  const box = document.getElementById('side-tags');
  const tags = state.stats.tags || [];
  box.innerHTML = tags.length
    ? tags.map((t) => `
      <button class="side-item ${state.tag === t.name ? 'active' : ''}" data-tag="${esc(t.name)}">
        ${esc(t.name)} <span class="side-count">${t.count}</span>
      </button>`).join('')
    : '<div class="muted">暂无标签</div>';
  box.querySelectorAll('[data-tag]').forEach((el) => {
    el.onclick = () => {
      state.tag = state.tag === el.dataset.tag ? null : el.dataset.tag;
      // 分类与标签互斥：选中某个标签时清掉分类筛选（否则两个维度叠加会出现「有数字但列表为空」）
      if (state.tag !== null) state.category = null;
      ensureListView();
      renderSideCategories();
      renderSideTags();
      loadMessages();
    };
  });
}

/* 点击分类/标签时，若当前处在「公司」「按公司」「摘要」或「收件箱/垃圾邮件/其他邮件」
   等带附加过滤的视图，先退回「全部邮件」：侧栏数字是按全部邮件统计的，否则会继续沿用
   company_id 或 folder/inference 过滤，导致列表为空或与侧栏数字对不上。 */
function ensureListView() {
  const withExtraFilter = ['companies', 'company', 'digest', 'inbox', 'junk', 'other'];
  if (!withExtraFilter.includes(state.view)) return;
  state.view = 'all';
  state.companyId = null;
  document.querySelectorAll('[data-view]').forEach((x) => x.classList.remove('active'));
  const allBtn = document.querySelector('[data-view="all"]');
  if (allBtn) allBtn.classList.add('active');
  renderSideCompanies();
  updateToolbar();
}

/* ---------------- 邮件列表 ---------------- */
/* 列表筛选参数：loadMessages / loadMore 共用同一份拼装逻辑 */
function listQueryParams() {
  const params = new URLSearchParams();
  if (state.accountId) params.set('account_id', state.accountId);
  if (state.view === 'company' && state.companyId) params.set('company_id', state.companyId);
  if (state.view === 'inbox') params.set('folder', 'INBOX');
  if (state.view === 'junk') params.set('folder', 'Junk');
  if (state.view === 'other') { params.set('folder', 'INBOX'); params.set('inference', 'other'); }
  if (state.view === 'unread') params.set('unread_only', 'true');
  if (state.view === 'starred') params.set('starred_only', 'true');
  if (state.category) params.set('category', state.category);
  if (state.tag) params.set('tag', state.tag);
  if (state.search) params.set('search', state.search);
  return params;
}

async function fetchMessagePage(offset) {
  const params = listQueryParams();
  params.set('limit', PAGE_SIZE);
  params.set('offset', offset);
  return api(`/api/messages?${params.toString()}`);
}

async function loadMessages(opts = {}) {
  if (state.view === 'companies') { renderCompanyGrid(); return; }
  if (state.view === 'digest') { await renderDigest(); return; }
  const data = await fetchMessagePage(0);
  state.messages = data.items;
  state.total = data.total;
  state.unreadTotal = data.unread_total;
  if (!opts.silent) state.selected.clear();
  renderList();
}

/* 加载更多：保留已加载内容，按 offset 追加下一页 */
async function loadMore() {
  if (state.loadingMore || state.messages.length >= state.total) return;
  state.loadingMore = true;
  const btn = document.getElementById('btn-load-more');
  if (btn) { btn.disabled = true; btn.textContent = '加载中…'; }
  try {
    const data = await fetchMessagePage(state.messages.length);
    state.messages = state.messages.concat(data.items);
    state.total = data.total;
    state.unreadTotal = data.unread_total;
    renderList();
  } catch (e) {
    toast(`加载更多失败：${e.message}`, 3200);
    if (btn) { btn.disabled = false; btn.textContent = '加载更多'; }
  } finally {
    state.loadingMore = false;
  }
}

function renderList() {
  document.getElementById('list-count').textContent =
    `共 ${state.total} 封 · 未读 ${state.unreadTotal}`;
  const box = document.getElementById('message-list');
  if (!state.messages.length) {
    box.innerHTML = '<div class="muted" style="padding:24px;text-align:center">暂无邮件，点击「立即同步」拉取</div>';
    return;
  }
  const remain = state.total - state.messages.length;
  box.innerHTML = state.messages.map((m) => `
    <div class="message-item ${m.is_read ? '' : 'unread'} ${state.activeMessageId === m.id ? 'selected' : ''}" data-id="${m.id}">
      <input type="checkbox" data-check="${m.id}" ${state.selected.has(m.id) ? 'checked' : ''} />
      <div>
        <div class="mi-subject">
          ${m.is_starred ? '<span>★</span>' : ''}
          <span>${esc(m.subject)}</span>
          ${m.has_attachment ? '<span class="chip">附件</span>' : ''}
          ${m.tags.map((t) => `<span class="chip">${esc(t)}</span>`).join('')}
        </div>
        <div class="mi-from">${esc(m.from_name || m.from_email)} &lt;${esc(m.from_email)}&gt;
          ${m.account_email ? `· ${esc(m.account_email)}` : ''}</div>
        <div class="mi-preview">${esc(m.preview)}</div>
      </div>
      <div class="mi-right">
        <div>${fmtDate(m.date_utc)}</div>
        <div class="badge-score ${scoreClass(m.importance_score)}">${esc(m.category)} ${m.importance_score}</div>
      </div>
    </div>
  `).join('') + (remain > 0
    ? `<div class="load-more-row"><button class="btn" id="btn-load-more">加载更多（还有 ${remain} 封）</button></div>`
    : '');

  box.querySelectorAll('.message-item').forEach((el) => {
    el.onclick = (ev) => {
      if (ev.target.dataset.check !== undefined) return;
      openDetail(Number(el.dataset.id));
    };
  });
  box.querySelectorAll('[data-check]').forEach((el) => {
    el.onclick = (ev) => {
      ev.stopPropagation();
      const id = Number(el.dataset.check);
      if (el.checked) state.selected.add(id); else state.selected.delete(id);
    };
  });
  const more = document.getElementById('btn-load-more');
  if (more) more.onclick = loadMore;
}

async function openDetail(id) {
  state.activeMessageId = id;
  expandDetail();
  renderList();
  const m = await api(`/api/messages/${id}`);
  let summaryHtml = '';
  try {
    const s = await api(`/api/summaries/${id}`);
    summaryHtml = renderSummaryBlock(s, 'detail-summary');
  } catch (e) { summaryHtml = ''; }
  const box = document.getElementById('detail');
  box.classList.remove('empty');
  box.innerHTML = `
    <button class="btn btn-sm detail-close" onclick="closeDetail()">关闭</button>
    <h2>${esc(m.subject)}</h2>
    <div class="meta">
      <div><b>${esc(m.from_name || m.from_email)}</b> &lt;${esc(m.from_email)}&gt;</div>
      <div>收件：${m.to.map((t) => esc(t.email)).join(', ') || '-'}</div>
      <div>时间：${fmtDate(m.date_utc)} · 分类：${esc(m.category)} · 评分：${m.importance_score}</div>
      ${m.account_email ? `<div>账号：${esc(m.account_email)}</div>` : ''}
    </div>
    ${summaryHtml}
    ${m.body_html ? `<div class="body-toolbar">
      <button class="btn btn-sm" id="btn-body-mode"></button>
      <button class="btn btn-sm" id="btn-body-images"></button>
    </div>` : ''}
    <div id="detail-body"></div>
    ${m.attachments.length ? `<div class="att-list"><div class="muted">附件（仅元数据）</div>${
      m.attachments.map((a) => `<div class="att">📎 ${esc(a.filename)} · ${(a.size / 1024).toFixed(1)} KB · ${esc(a.mime)}</div>`).join('')
    }</div>` : ''}
  `;
  bindSummaryBox('detail-summary', id);
  // 每封邮件都从「富文本 + 不加载图片」开始，避免上一封的开关状态串台
  state.detailBody = { mode: 'html', images: false };
  renderDetailBody(m);
  const modeBtn = document.getElementById('btn-body-mode');
  const imgBtn = document.getElementById('btn-body-images');
  if (modeBtn) {
    modeBtn.onclick = () => {
      state.detailBody.mode = state.detailBody.mode === 'html' ? 'text' : 'html';
      renderDetailBody(m);
    };
  }
  if (imgBtn) {
    imgBtn.onclick = () => {
      state.detailBody.images = !state.detailBody.images;
      renderDetailBody(m);
    };
  }
}

/* 详情正文渲染：
   - 纯文本走 esc() 转义后直接插入；
   - HTML 正文（后端已白名单消毒）放进 sandbox iframe + CSP 隔离，
     默认只允许 data:/cid: 图片（拦截外链追踪像素），可手动「显示图片」。 */
function renderDetailBody(m) {
  const host = document.getElementById('detail-body');
  if (!host) return;
  const { mode, images } = state.detailBody;
  const modeBtn = document.getElementById('btn-body-mode');
  const imgBtn = document.getElementById('btn-body-images');
  if (modeBtn) modeBtn.textContent = mode === 'html' ? '纯文本' : '显示排版';
  if (imgBtn) {
    imgBtn.classList.toggle('hidden', mode === 'text');
    imgBtn.textContent = images ? '隐藏图片' : '显示图片';
  }
  if (!m.body_html || mode === 'text') {
    host.innerHTML = `<div class="body">${esc(m.body_text || '（无正文）')}</div>`;
    return;
  }
  const style = getComputedStyle(document.getElementById('detail'));
  const csp = "default-src 'none'; style-src 'unsafe-inline'; font-src data:; "
    + (images ? "img-src https: http: data: cid:;" : "img-src data: cid:;");
  const doc = '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8" />'
    + `<meta http-equiv="Content-Security-Policy" content="${csp}" />`
    + '<base target="_blank" />'
    + '<style>'
    + 'html,body{margin:0;padding:0;background:transparent;}'
    + `body{font:${style.fontSize || '14px'}/1.8 ${style.fontFamily || 'sans-serif'};`
    + `color:${style.color};word-break:break-word;overflow-wrap:anywhere;}`
    + 'img{max-width:100%;height:auto;}table{max-width:100%;}'
    + 'a{color:inherit;}'
    + '</style></head><body>' + m.body_html + '</body></html>';

  host.innerHTML = '';
  const frame = document.createElement('iframe');
  frame.className = 'body-frame';
  frame.setAttribute('sandbox', 'allow-same-origin allow-popups allow-popups-to-escape-sandbox');
  frame.setAttribute('referrerpolicy', 'no-referrer');
  frame.setAttribute('title', '邮件正文');
  frame.srcdoc = doc;
  frame.addEventListener('load', () => {
    try {
      const h = frame.contentDocument.documentElement.scrollHeight;
      frame.style.height = `${Math.max(120, Math.min(h + 8, 4000))}px`;
    } catch (e) {
      frame.style.height = '520px';
    }
  });
  host.appendChild(frame);
}

/* ---------------- 公司聚合（P3） ---------------- */
async function loadCompanies() {
  const params = new URLSearchParams();
  if (state.search) params.set('search', state.search);
  try {
    state.companies = await api(`/api/companies?${params.toString()}`);
  } catch (e) {
    state.companies = [];
  }
  renderSideCompanies();
}

function renderSideCompanies() {
  const box = document.getElementById('side-companies');
  if (!box) return;
  if (!state.companies.length) {
    box.innerHTML = '<div class="muted">暂无公司，点击「立即同步」或公司视图里的「重跑公司归并」</div>';
    return;
  }
  box.innerHTML = state.companies.slice(0, 40).map((c) => `
    <button class="side-item ${state.companyId === c.id ? 'active' : ''}" data-company="${c.id}">
      ${c.starred ? '★ ' : ''}${esc(c.name)}
      <span class="side-count">${c.message_count}</span>
    </button>`).join('');
  box.querySelectorAll('[data-company]').forEach((el) => {
    el.onclick = () => selectCompany(Number(el.dataset.company));
  });
}

function renderCompanyGrid() {
  const box = document.getElementById('message-list');
  const toolbar = `<div class="grid-toolbar">
    <span class="muted">共 ${state.companies.length} 家公司（点卡片查看邮件与往来摘要）</span>
    <div class="spacer"></div>
    <button class="btn btn-sm" id="btn-reaggregate">重跑公司归并</button>
  </div>`;
  if (!state.companies.length) {
    box.innerHTML = toolbar + '<div class="muted" style="padding:24px;text-align:center">暂无公司。请先同步邮件，或点击「重跑公司归并」。</div>';
  } else {
    box.innerHTML = toolbar + `<div class="company-grid">${state.companies.map((c) => `
      <div class="company-card ${state.companyId === c.id ? 'selected' : ''}" data-company="${c.id}">
        <div class="cc-head">
          <span class="cc-name">${c.starred ? '★ ' : ''}${esc(c.name)}</span>
          <span class="side-count">${c.message_count}</span>
        </div>
        <div class="muted">${esc((c.domains || []).join(', ')) || '（无域名）'}</div>
        <div class="muted">最近往来：${fmtDate(c.last_message_at) || '—'}</div>
        <div class="cc-foot">
          ${c.unread ? `<span class="chip">未读 ${c.unread}</span>` : ''}
          ${(c.aliases || []).length ? `<span class="chip">别名 ${c.aliases.length}</span>` : ''}
        </div>
      </div>`).join('')}</div>`;
  }
  box.querySelectorAll('.company-card').forEach((el) => {
    el.onclick = () => selectCompany(Number(el.dataset.company), true);
  });
  box.querySelector('#btn-reaggregate').onclick = reaggregateCompanies;
}

async function selectCompany(id, loadList = false) {
  state.companyId = id;
  state.view = 'company';
  document.querySelectorAll('[data-view]').forEach((x) => x.classList.remove('active'));
  renderSideCompanies();
  updateToolbar();
  await openCompanyDetail(id);
  if (loadList) await loadMessages();
}

async function openCompanyDetail(id) {
  const box = document.getElementById('detail');
  expandDetail();
  box.classList.remove('empty');
  box.innerHTML = '<div class="muted">加载公司详情…</div>';
  let d;
  try {
    d = await api(`/api/companies/${id}`);
  } catch (e) {
    box.innerHTML = `<div class="muted">加载失败：${esc(e.message)}</div>`;
    return;
  }
  state.companyDetail = d;
  const c = d.company;
  const s = d.summary;
  const ent = s.key_entities || {};
  box.innerHTML = `
    <button class="btn btn-sm detail-close" onclick="closeDetail()">关闭</button>
    <div class="company-head">
      <h2>${c.starred ? '★ ' : ''}${esc(c.name)}</h2>
      <div class="meta">
        <div>域名：${esc((c.domains || []).join(', ')) || '（无）'}</div>
        ${c.industry ? `<div>行业：${esc(c.industry)}</div>` : ''}
        ${(c.aliases || []).length ? `<div>别名：${esc(c.aliases.join('、'))}</div>` : ''}
      </div>
      <div class="company-actions">
        <button class="btn btn-sm" data-ca="star">${c.starred ? '取消星标' : '加星'}</button>
        <button class="btn btn-sm" data-ca="rename">改名</button>
        <button class="btn btn-sm" data-ca="industry">行业</button>
        <button class="btn btn-sm" data-ca="notes">备注</button>
        <button class="btn btn-sm" data-ca="merge">合并到…</button>
        <button class="btn btn-sm" data-ca="filter">只看该公司邮件</button>
      </div>
    </div>

    <div class="summary-box">
      <div class="summary-title">往来摘要</div>
      <div class="summary-text">${esc(s.text)}</div>
      <div class="summary-meta muted">共 ${s.total} 封 · 未读 ${s.unread} · 附件 ${s.attachment_count} 个${
        s.first_at ? ` · ${fmtDate(s.first_at)} ~ ${fmtDate(s.last_at)}` : ''}</div>
      ${(ent.amounts || []).length ? `<div class="muted">金额：${esc(ent.amounts.join('、'))}</div>` : ''}
      ${(ent.orders || []).length ? `<div class="muted">订单号：${esc(ent.orders.join('、'))}</div>` : ''}
      ${c.notes ? `<div class="muted">备注：${esc(c.notes)}</div>` : ''}
    </div>

    ${d.contacts.length ? `<div class="block"><div class="muted">联系人（${d.contacts.length}）</div>${
      d.contacts.map((p) => `<div class="att">${esc(p.name || p.email)} · ${esc(p.email)} · ${p.message_count} 封</div>`).join('')
    }</div>` : ''}

    ${d.attachments.length ? `<div class="block"><div class="muted">附件清单（${d.attachments.length}）</div>${
      d.attachments.map((a) => `<div class="att">📎 ${esc(a.filename)} · ${(a.size / 1024).toFixed(1)} KB · ${esc(a.mime)}</div>`).join('')
    }</div>` : ''}

    <div class="block"><div class="muted">往来邮件（${d.messages.length}）</div>
      <div class="mini-list">${d.messages.map((m) => `
        <div class="mini-item" data-mid="${m.id}">
          <div class="mi-subject">${m.is_starred ? '<span>★</span>' : ''}${esc(m.subject)}</div>
          <div class="mi-from">${esc(m.from_name || m.from_email)} · ${esc(m.account_email || '')} · ${fmtDate(m.date_utc)}</div>
        </div>`).join('')}</div>
    </div>
  `;
  box.querySelectorAll('[data-mid]').forEach((el) => {
    el.onclick = () => openDetail(Number(el.dataset.mid));
  });
  box.querySelectorAll('[data-ca]').forEach((el) => {
    el.onclick = () => companyAction(el.dataset.ca);
  });
}

function closeDetail() {
  const box = document.getElementById('detail');
  box.classList.add('empty');
  box.innerHTML = '<div class="muted">选择一封邮件或一家公司查看详情</div>';
  state.activeMessageId = null;
}

async function refreshCompanyAll() {
  await loadCompanies();
  if (state.view === 'companies') renderCompanyGrid();
  if (state.view === 'company') {
    await loadMessages();
    if (state.companyId) await openCompanyDetail(state.companyId);
  }
}

async function updateCompany(id, payload, okMsg) {
  await api(`/api/companies/${id}`, { method: 'PUT', body: JSON.stringify(payload) });
  if (okMsg) toast(okMsg);
  await refreshCompanyAll();
}

async function companyAction(action) {
  const d = state.companyDetail;
  if (!d) return;
  const id = d.company.id;
  if (action === 'star') {
    await updateCompany(id, { starred: !d.company.starred }, d.company.starred ? '已取消星标' : '已加星');
  } else if (action === 'rename') {
    const name = prompt('新的公司名称', d.company.name);
    if (name && name.trim()) await updateCompany(id, { name: name.trim() }, '已改名');
  } else if (action === 'industry') {
    const industry = prompt('行业（留空清除）', d.company.industry || '');
    if (industry !== null) await updateCompany(id, { industry }, '已更新行业');
  } else if (action === 'notes') {
    const notes = prompt('备注', d.company.notes || '');
    if (notes !== null) await updateCompany(id, { notes }, '已更新备注');
  } else if (action === 'filter') {
    state.view = 'company';
    document.querySelectorAll('[data-view]').forEach((x) => x.classList.remove('active'));
    updateToolbar();
    await loadMessages();
  } else if (action === 'merge') {
    openMergeModal();
  }
}

function openMergeModal() {
  const d = state.companyDetail;
  if (!d) return;
  document.getElementById('merge-title').textContent = `合并到「${d.company.name}」`;
  const others = state.companies.filter((c) => c.id !== d.company.id);
  document.getElementById('merge-list').innerHTML = others.length
    ? others.map((c) => `<label class="chk"><input type="checkbox" data-merge="${c.id}" /> ${esc(c.name)} <span class="muted">${c.message_count} 封</span></label>`).join('')
    : '<div class="muted">没有可合并的其它公司</div>';
  document.getElementById('merge-modal').classList.remove('hidden');
}

async function confirmMerge() {
  const d = state.companyDetail;
  if (!d) return;
  const ids = [...document.querySelectorAll('[data-merge]')].filter((el) => el.checked).map((el) => Number(el.dataset.merge));
  if (!ids.length) { toast('请先勾选要合并的公司'); return; }
  await api('/api/companies/merge', {
    method: 'POST',
    body: JSON.stringify({ primary_id: d.company.id, merge_ids: ids }),
  });
  document.getElementById('merge-modal').classList.add('hidden');
  toast(`已合并 ${ids.length} 家公司`);
  await refreshCompanyAll();
}

async function reaggregateCompanies() {
  try {
    const r = await api('/api/companies/reaggregate', { method: 'POST' });
    toast(`已重跑公司归并：处理 ${r.processed} 封邮件`);
    await loadCompanies();
    await loadStats();
    if (state.view === 'companies') renderCompanyGrid();
    if (state.view === 'company') await loadMessages();
  } catch (e) {
    toast(`重跑失败：${e.message}`, 4000);
  }
}

function updateToolbar() {
  const hide = state.view === 'companies' || state.view === 'digest';
  document.querySelector('.list-toolbar').classList.toggle('hidden', hide);
}

/* ---------------- 摘要（P4） ---------------- */
function entChips(entities) {
  const ent = entities || {};
  const chips = [];
  ['amounts', 'orders', 'deadlines', 'captcha', 'phones', 'dates'].forEach((k) => {
    (ent[k] || []).forEach((v) => chips.push(String(v)));
  });
  return chips;
}

function renderSummaryBlock(s, boxId) {
  const chips = entChips(s.entities);
  return `<div id="${boxId}" class="summary-box summary-inline">
    <div class="summary-title">摘要
      <span class="muted">${s.source === 'llm' ? 'AI' : '规则'}${s.cached ? ' · 缓存' : ''}</span>
      <span class="urgency urgency-${esc(s.urgency)}">${esc(s.urgency)}</span>
    </div>
    <div class="summary-text">${esc(s.summary)}</div>
    ${(s.action_items || []).length ? `<div class="muted">待办：${s.action_items.map(esc).join('；')}</div>` : ''}
    ${chips.length ? `<div class="summary-chips">${chips.map((c) => `<span class="chip">${esc(c)}</span>`).join('')}</div>` : ''}
    <div class="company-actions">
      <button class="btn btn-sm" data-sum="regen">重新生成</button>
      ${state.llmConfig && state.llmConfig.enabled ? '<button class="btn btn-sm" data-sum="llm">AI 摘要</button>' : ''}
    </div>
  </div>`;
}

function bindSummaryBox(boxId, messageId) {
  const box = document.getElementById(boxId);
  if (!box) return;
  box.querySelectorAll('[data-sum]').forEach((el) => {
    el.onclick = () => regenerateSummary(messageId, el.dataset.sum === 'llm', boxId);
  });
}

async function regenerateSummary(messageId, useLlm, boxId) {
  try {
    const s = await api(`/api/summaries/${messageId}/generate?use_llm=${useLlm ? 'true' : 'false'}`, { method: 'POST' });
    const el = document.getElementById(boxId);
    if (el) {
      el.outerHTML = renderSummaryBlock(s, boxId);
      bindSummaryBox(boxId, messageId);
    }
    if (useLlm) toast(s.source === 'llm' ? 'AI 摘要已生成' : 'LLM 不可用，已用规则摘要');
    else toast('已重新生成摘要');
  } catch (e) {
    toast(`生成失败：${e.message}`, 4000);
  }
}

async function renderDigest() {
  const box = document.getElementById('message-list');
  let items = [];
  try {
    items = await api('/api/summaries/digest?limit=50');
  } catch (e) {
    box.innerHTML = `<div class="muted">加载失败：${esc(e.message)}</div>`;
    return;
  }
  const toolbar = `<div class="grid-toolbar">
    <span class="muted">重要邮件摘要（${items.length}）</span>
    <div class="spacer"></div>
    <button class="btn btn-sm" id="btn-digest-batch">为重要邮件生成摘要</button>
  </div>`;
  if (!items.length) {
    box.innerHTML = toolbar + '<div class="muted" style="padding:24px;text-align:center">暂无重要邮件。评分达到「重要阈值」的邮件会自动生成摘要。</div>';
    box.querySelector('#btn-digest-batch').onclick = digestBatch;
    return;
  }
  box.innerHTML = toolbar + items.map((it) => {
    const m = it.message;
    const chips = entChips(it.entities);
    return `<div class="digest-card" data-mid="${m.id}">
      <div class="dc-head">
        <span class="dc-subject">${m.is_starred ? '★ ' : ''}${esc(m.subject)}</span>
        <span class="urgency urgency-${esc(it.urgency)}">${esc(it.urgency)}</span>
      </div>
      <div class="mi-from">${esc(m.from_name || m.from_email)} · ${esc(m.account_email || '')} · ${fmtDate(m.date_utc)} · ${esc(m.category)}</div>
      <div class="dc-summary">${esc(it.summary)}</div>
      ${(it.action_items || []).length ? `<div class="muted">待办：${it.action_items.map(esc).join('；')}</div>` : ''}
      ${chips.length ? `<div class="summary-chips">${chips.map((c) => `<span class="chip">${esc(c)}</span>`).join('')}</div>` : ''}
    </div>`;
  }).join('');
  box.querySelectorAll('[data-mid]').forEach((el) => {
    el.onclick = () => openDetail(Number(el.dataset.mid));
  });
  box.querySelector('#btn-digest-batch').onclick = digestBatch;
}

async function digestBatch() {
  const btn = document.getElementById('btn-digest-batch');
  if (btn) { btn.disabled = true; btn.textContent = '生成中…'; }
  try {
    const r = await api('/api/summaries/batch', {
      method: 'POST',
      body: JSON.stringify({ important_only: true, limit: 100 }),
    });
    toast(`已生成 ${r.generated} 条摘要`);
    await renderDigest();
  } catch (e) {
    toast(`生成失败：${e.message}`, 4000);
  } finally {
    const b = document.getElementById('btn-digest-batch');
    if (b) { b.disabled = false; b.textContent = '为重要邮件生成摘要'; }
  }
}

async function loadLlmConfig() {
  try {
    state.llmConfig = await api('/api/summaries/config');
  } catch (e) {
    state.llmConfig = { enabled: false };
  }
}

function summaryMsg(text, ok) {
  const el = document.getElementById('summary-msg');
  el.textContent = text;
  el.className = 'form-msg ' + (ok ? 'ok' : 'err');
}

async function openSummaryCfg() {
  try {
    const cfg = await api('/api/summaries/config');
    state.llmConfig = cfg;
    document.getElementById('llm-enabled').checked = !!cfg.enabled;
    document.getElementById('llm-provider').value = cfg.provider || 'openai';
    document.getElementById('llm-base').value = cfg.base_url || '';
    document.getElementById('llm-model').value = cfg.model || '';
    const keyEl = document.getElementById('llm-key');
    keyEl.value = '';
    keyEl.placeholder = cfg.has_api_key ? `已保存（${cfg.api_key_masked}），留空保持不变` : '留空不设置';
    document.getElementById('llm-maxchars').value = cfg.max_chars || 4000;
    document.getElementById('llm-timeout').value = cfg.timeout || 30;
    summaryMsg('', true);
    document.getElementById('summary-modal').classList.remove('hidden');
  } catch (e) {
    toast(`加载摘要设置失败：${e.message}`, 4000);
  }
}

async function saveSummaryCfg() {
  const payload = {
    enabled: document.getElementById('llm-enabled').checked,
    provider: document.getElementById('llm-provider').value,
    base_url: document.getElementById('llm-base').value.trim(),
    model: document.getElementById('llm-model').value.trim(),
    max_chars: Number(document.getElementById('llm-maxchars').value) || 4000,
    timeout: Number(document.getElementById('llm-timeout').value) || 30,
  };
  const key = document.getElementById('llm-key').value;
  if (key) payload.api_key = key;
  try {
    state.llmConfig = await api('/api/summaries/config', { method: 'PUT', body: JSON.stringify(payload) });
    summaryMsg('已保存', true);
    toast('摘要设置已保存');
  } catch (e) {
    summaryMsg(`保存失败：${e.message}`, false);
  }
}

/* ---------------- 批量操作 ---------------- */
async function bulk(action) {
  const ids = [...state.selected];
  if (!ids.length) { toast('请先勾选邮件'); return; }
  const map = { read: 'read', star: 'star', archive: 'archive' };
  await api(`/api/messages/actions/${map[action]}`, {
    method: 'POST',
    body: JSON.stringify({ ids, value: true }),
  });
  toast(`已处理 ${ids.length} 封`);
  await loadAccounts();
  await loadStats();
  await loadMessages();
}

/* ---------------- 批量打标签 ---------------- */
async function bulkTag(value) {
  const ids = [...state.selected];
  if (!ids.length) { toast('请先勾选邮件'); return; }
  const tag = document.getElementById('tag-select').value;
  if (!tag) { toast('请先选择标签'); return; }
  await api('/api/messages/actions/tag', {
    method: 'POST',
    body: JSON.stringify({ ids, tag, value }),
  });
  toast(`${value ? '已添加' : '已移除'}标签「${tag}」：${ids.length} 封`);
  await loadStats();
  await loadMessages();
}

/* ---------------- 一键已读 / 一键删除（按当前筛选，不分页） ---------------- */
/* 与 loadMessages() 的筛选拼装保持一致，供「按筛选批量操作」接口使用 */
function currentFilters() {
  const f = {};
  if (state.accountId) f.account_id = state.accountId;
  if (state.view === 'company' && state.companyId) f.company_id = state.companyId;
  if (state.view === 'inbox') f.folder = 'INBOX';
  if (state.view === 'junk') f.folder = 'Junk';
  if (state.view === 'other') { f.folder = 'INBOX'; f.inference = 'other'; }
  if (state.view === 'unread') f.unread_only = true;
  if (state.view === 'starred') f.starred_only = true;
  if (state.category) f.category = state.category;
  if (state.tag) f.tag = state.tag;
  if (state.search) f.search = state.search;
  return f;
}

async function bulkAll(action) {
  if (state.view === 'companies' || state.view === 'digest') {
    toast('请先切到邮件列表再操作');
    return;
  }
  const filters = currentFilters();
  if (action === 'delete') {
    const narrowed = Object.keys(filters).length > 0;
    const ok = await appConfirm(
      '一键删除',
      narrowed
        ? '将删除当前筛选出的全部邮件（仅从本工具移除，不影响邮箱服务器）。此操作不可撤销，确定继续？'
        : '当前无筛选，将删除本地全部邮件（仅从本工具移除，不影响邮箱服务器）。此操作不可撤销，确定继续？',
    );
    if (!ok) return;
  }
  try {
    const res = await api('/api/messages/actions/bulk', {
      method: 'POST',
      body: JSON.stringify({ action, ...filters }),
    });
    const n = res.deleted ?? res.updated ?? 0;
    toast(action === 'delete' ? `已删除 ${n} 封邮件` : `已将 ${n} 封邮件标记为已读`);
    await loadAccounts();
    await loadStats();
    if (state.view === 'company') await loadCompanies();
    await loadMessages();
  } catch (e) {
    toast(`操作失败：${e.message}`, 3200);
  }
}

/* ---------------- 规则配置 ---------------- */
function rulesMsg(text, ok) {
  const el = document.getElementById('rules-msg');
  el.textContent = text;
  el.className = 'form-msg ' + (ok ? 'ok' : 'err');
}

function ruleRowHtml(rule, idx) {
  const fieldOpts = RULE_FIELDS.map((f) => `<option value="${f}" ${rule.field === f ? 'selected' : ''}>${f}</option>`).join('');
  const opOpts = RULE_OPS.map((o) => `<option value="${o}" ${rule.op === o ? 'selected' : ''}>${o}</option>`).join('');
  const catList = [...new Set([...categoryNames(), rule.category].filter((c) => c))];
  const catOpts = ['', ...catList].map((c) => `<option value="${c}" ${rule.category === c ? 'selected' : ''}>${c || '（无）'}</option>`).join('');
  const tagOpts = ['', ...ALL_TAGS].map((t) => `<option value="${t}" ${rule.tag === t ? 'selected' : ''}>${t || '（无）'}</option>`).join('');
  return `
    <div class="rule-row" data-idx="${idx}" data-id="${rule.id ?? ''}">
      <label class="chk"><input type="checkbox" data-k="enabled" ${rule.enabled ? 'checked' : ''} /></label>
      <input data-k="name" value="${esc(rule.name)}" placeholder="规则名" />
      <select data-k="field">${fieldOpts}</select>
      <select data-k="op">${opOpts}</select>
      <input data-k="value" value="${esc(rule.value)}" placeholder="关键词（in_list 用逗号分隔）" />
      <input data-k="score" type="number" value="${rule.score}" />
      <select data-k="tag">${tagOpts}</select>
      <select data-k="category">${catOpts}</select>
      <button class="btn btn-sm" data-del="${idx}">删除</button>
    </div>`;
}

function renderRules() {
  const box = document.getElementById('rules-table');
  box.innerHTML = state.ruleRows.map((r, i) => ruleRowHtml(r, i)).join('');
  box.querySelectorAll('.rule-row').forEach((row) => {
    const idx = Number(row.dataset.idx);
    row.querySelectorAll('[data-k]').forEach((el) => {
      el.onchange = () => {
        const k = el.dataset.k;
        let v = el.type === 'checkbox' ? el.checked : el.value;
        if (k === 'score') v = Number(v) || 0;
        state.ruleRows[idx][k] = v;
      };
    });
    row.querySelector('[data-del]').onclick = () => {
      const r = state.ruleRows[idx];
      if (r.id) state.deletedRuleIds.push(r.id);
      state.ruleRows.splice(idx, 1);
      renderRules();
    };
  });
}

async function openRules() {
  try {
    const [rules, cfg] = await Promise.all([api('/api/rules'), api('/api/classify/config')]);
    state.ruleRows = rules.map((r) => ({ ...r }));
    state.deletedRuleIds = [];
    renderRules();
    const th = cfg.thresholds || {};
    document.getElementById('cfg-important').value = th.important ?? 7;
    document.getElementById('cfg-todo').value = th.todo ?? 4;
    const w = cfg.weights || {};
    ['whitelist', 'entities', 'reply', 'to_me', 'attachment'].forEach((k) => {
      document.getElementById(`w-${k}`).value = w[k] ?? 0;
    });
    document.getElementById('cfg-whitelist').value = (cfg.whitelist || []).join('\n');
    rulesMsg('', true);
    document.getElementById('rules-modal').classList.remove('hidden');
  } catch (e) {
    toast(`加载规则失败：${e.message}`, 4000);
  }
}

async function saveRules() {
  try {
    // 1) 删除
    for (const id of state.deletedRuleIds) {
      await api(`/api/rules/${id}`, { method: 'DELETE' });
    }
    // 2) 更新 / 新增
    for (const r of state.ruleRows) {
      const payload = {
        name: r.name, enabled: r.enabled, field: r.field, op: r.op,
        value: r.value, score: Number(r.score) || 0,
        tag: r.tag || null, category: r.category || null,
      };
      if (r.id) await api(`/api/rules/${r.id}`, { method: 'PUT', body: JSON.stringify(payload) });
      else await api('/api/rules', { method: 'POST', body: JSON.stringify(payload) });
    }
    // 3) 全局配置
    await api('/api/classify/config', {
      method: 'PUT',
      body: JSON.stringify({
        whitelist: document.getElementById('cfg-whitelist').value.split('\n').map((s) => s.trim()).filter(Boolean),
        thresholds: {
          important: Number(document.getElementById('cfg-important').value) || 0,
          todo: Number(document.getElementById('cfg-todo').value) || 0,
        },
        weights: Object.fromEntries(['whitelist', 'entities', 'reply', 'to_me', 'attachment']
          .map((k) => [k, Number(document.getElementById(`w-${k}`).value) || 0])),
      }),
    });
    rulesMsg('已保存。建议点击「重新分类全部邮件」让历史邮件生效。', true);
    toast('规则已保存');
    await loadStats();
    await loadMessages();
  } catch (e) {
    rulesMsg(`保存失败：${e.message}`, false);
  }
}

async function resetRules() {
  try {
    await api('/api/rules/reset', { method: 'POST' });
    await api('/api/classify/reset', { method: 'POST' });
    toast('已恢复默认规则与阈值');
    await openRules();
    await loadStats();
    await loadMessages();
  } catch (e) {
    rulesMsg(`恢复失败：${e.message}`, false);
  }
}

async function reclassify() {
  const btn = document.getElementById('btn-reclassify');
  btn.disabled = true;
  btn.textContent = '分类中…';
  try {
    const r = await api('/api/classify/reclassify', {
      method: 'POST',
      body: JSON.stringify({ account_id: state.accountId || null }),
    });
    toast(`已重新分类，更新 ${r.updated} 封`);
    await loadStats();
    await loadMessages();
  } catch (e) {
    rulesMsg(`重新分类失败：${e.message}`, false);
  } finally {
    btn.disabled = false;
    btn.textContent = '重新分类全部邮件';
  }
}

/* ---------------- 同步 ---------------- */
async function syncNow() {
  const btn = document.getElementById('btn-sync');
  btn.disabled = true;
  btn.textContent = '同步中…';
  try {
    const payload = { account_ids: state.accountId ? [state.accountId] : null };
    const results = await api('/api/sync', { method: 'POST', body: JSON.stringify(payload) });
    const ok = results.filter((r) => r.status === 'ok').length;
    const inserted = results.reduce((s, r) => s + r.inserted, 0);
    toast(`同步完成：${ok}/${results.length} 个账号成功，新增 ${inserted} 封`);
    const err = results.find((r) => r.error);
    if (err) toast(`错误：${err.error}`, 4000);
  } catch (e) {
    toast(`同步失败：${e.message}`, 4000);
  } finally {
    btn.disabled = false;
    btn.textContent = '立即同步';
    await loadAccounts();
    await loadStats();
    await loadCompanies();
    await loadMessages();
  }
}

/* ---------------- 添加账号 ---------------- */
async function loadProviders() {
  state.providers = await api('/api/providers');
  const sel = document.getElementById('f-provider');
  sel.innerHTML = state.providers.map((p) => `<option value="${esc(p.provider)}">${esc(p.label)}</option>`).join('');
  sel.onchange = applyProviderHint;
  applyProviderHint();
}

function currentProvider() {
  const v = document.getElementById('f-provider').value;
  return state.providers.find((p) => p.provider === v);
}

function applyProviderHint() {
  const p = currentProvider();
  if (!p) return;
  const oauth = p.auth_mode !== 'imap';
  document.getElementById('f-hint').textContent = `${p.credential_hint}（默认服务器 ${p.imap_host}:${p.imap_port}）`;
  document.getElementById('f-cred-label').textContent = p.credential_label;
  document.getElementById('f-oauth').classList.toggle('hidden', !oauth);
  // 走 OAuth2 时授权码可留空（用于 IMAP 降级时才必填）
  document.getElementById('f-secret').required = !oauth;
  // 微软只接受 https 或 http://localhost 的回调地址，不接受 127.0.0.1；
  // 桌面端 location.origin 是 127.0.0.1，这里统一提示成 localhost 形式，避免填错。
  const origin = location.origin.replace('//127.0.0.1', '//localhost');
  document.getElementById('f-redirect').textContent = oauth
    ? `回调地址（需填入 OAuth 应用白名单）：${origin}/api/oauth/${p.provider}/callback`
    : '';
}

function formPayload() {
  const p = currentProvider();
  return {
    provider: p.provider,
    name: document.getElementById('f-name').value.trim(),
    email: document.getElementById('f-email').value.trim(),
    secret: document.getElementById('f-secret').value,
    imap_host: document.getElementById('f-host').value.trim() || null,
    imap_port: Number(document.getElementById('f-port').value) || null,
  };
}

function setFormMsg(text, ok) {
  const el = document.getElementById('form-msg');
  el.textContent = text;
  el.className = 'form-msg ' + (ok ? 'ok' : 'err');
}

async function testConnection() {
  setFormMsg('测试中…', true);
  try {
    const r = await api('/api/accounts/test', { method: 'POST', body: JSON.stringify(formPayload()) });
    setFormMsg(r.message, r.ok);
  } catch (e) {
    setFormMsg(`测试失败：${e.message}`, false);
  }
}

async function startOAuth(accountId, provider, clientId, clientSecret) {
  try {
    const r = await api(`/api/oauth/${provider}/authorize`, {
      method: 'POST',
      body: JSON.stringify({ account_id: accountId, client_id: clientId, client_secret: clientSecret || '' }),
    });
    window.open(r.authorize_url, '_blank', 'noopener');
    toast('已打开授权页面，完成后回到本页即可', 4000);
  } catch (e) {
    toast(`授权启动失败：${e.message}`, 4000);
  }
}

async function authorizeAccount(acc) {
  if (!acc) return;
  const cid = await appPrompt('OAuth Client ID', acc.oauth_client_id || '',
    '填写你在 Google Cloud（Gmail）或 Azure 应用注册（Outlook）拿到的 Client ID');
  if (!cid || !cid.trim()) return;
  const cs = await appPrompt('OAuth Client Secret', '',
    '公开客户端可留空，直接点确定') || '';
  await startOAuth(acc.id, acc.provider, cid.trim(), cs);
}

async function saveAccount(ev) {
  ev.preventDefault();
  const p = currentProvider();
  const clientId = document.getElementById('f-client-id').value.trim();
  const clientSecret = document.getElementById('f-client-secret').value;
  try {
    const created = await api('/api/accounts', { method: 'POST', body: JSON.stringify(formPayload()) });
    toast('账号已保存');
    document.getElementById('modal').classList.add('hidden');
    document.getElementById('account-form').reset();
    setFormMsg('', true);
    await loadAccounts();
    if (p && p.auth_mode !== 'imap') {
      if (clientId) {
        await startOAuth(created.id, p.provider, clientId, clientSecret);
      } else {
        toast('请点击账号旁的『授权』完成 OAuth 授权', 4000);
      }
    }
  } catch (e) {
    setFormMsg(`保存失败：${e.message}`, false);
  }
}

/* ---------------- 事件绑定 ---------------- */
function bind() {
  document.querySelectorAll('[data-view]').forEach((el) => {
    el.onclick = async () => {
      state.view = el.dataset.view;
      state.category = null;
      state.tag = null;
      document.querySelectorAll('[data-view]').forEach((x) => x.classList.remove('active'));
      el.classList.add('active');
      renderSideCategories();
      renderSideTags();
      updateToolbar();
      if (state.view === 'companies') {
        await loadCompanies();
        renderCompanyGrid();
      } else {
        await loadMessages();
      }
    };
  });
  document.querySelectorAll('[data-bulk]').forEach((el) => {
    el.onclick = () => bulk(el.dataset.bulk);
  });
  document.getElementById('btn-all-read').onclick = () => bulkAll('read');
  document.getElementById('btn-all-delete').onclick = () => bulkAll('delete');
  document.getElementById('check-all').onclick = (ev) => {
    state.selected.clear();
    if (ev.target.checked) state.messages.forEach((m) => state.selected.add(m.id));
    renderList();
  };
  let searchTimer;
  document.getElementById('search').oninput = (ev) => {
    state.search = ev.target.value.trim();
    clearTimeout(searchTimer);
    searchTimer = setTimeout(async () => {
      await loadCompanies();
      if (state.view === 'companies') renderCompanyGrid();
      else await loadMessages();
    }, 300);
  };
  document.getElementById('btn-sync').onclick = syncNow;
  document.getElementById('btn-add').onclick = () => document.getElementById('modal').classList.remove('hidden');
  document.getElementById('modal-close').onclick = () => document.getElementById('modal').classList.add('hidden');
  document.getElementById('btn-test').onclick = testConnection;
  document.getElementById('account-form').onsubmit = saveAccount;

  // 公司合并弹窗
  document.getElementById('merge-close').onclick = () => document.getElementById('merge-modal').classList.add('hidden');
  document.getElementById('merge-confirm').onclick = confirmMerge;

  // 标签下拉 + 批量打标签
  document.getElementById('tag-select').innerHTML =
    ALL_TAGS.map((t) => `<option value="${t}">${t}</option>`).join('');
  document.getElementById('btn-tag-add').onclick = () => bulkTag(true);
  document.getElementById('btn-tag-remove').onclick = () => bulkTag(false);

  // 分类：新增
  document.getElementById('btn-cat-add').onclick = addCategory;

  // 规则配置
  document.getElementById('btn-rules').onclick = openRules;
  document.getElementById('rules-close').onclick = () => document.getElementById('rules-modal').classList.add('hidden');
  document.getElementById('rule-add').onclick = () => {
    state.ruleRows.push({ id: null, name: '新规则', enabled: true, field: 'subject', op: 'contains', value: '', score: 1, tag: null, category: null });
    renderRules();
  };
  document.getElementById('rules-save').onclick = saveRules;
  document.getElementById('rules-reset').onclick = resetRules;
  document.getElementById('btn-reclassify').onclick = reclassify;

  // 摘要设置（LLM 可选）
  document.getElementById('btn-summary-cfg').onclick = openSummaryCfg;
  document.getElementById('summary-close').onclick = () => document.getElementById('summary-modal').classList.add('hidden');
  document.getElementById('summary-save').onclick = saveSummaryCfg;

  document.querySelector('[data-view="all"]').classList.add('active');
}

async function handleOAuthReturn() {
  const params = new URLSearchParams(location.search);
  const status = params.get('oauth');
  if (!status) return;
  history.replaceState({}, '', location.pathname);
  if (status === 'ok') {
    toast('OAuth 授权成功');
    await loadAccounts();
  } else {
    toast(`OAuth 授权失败：${params.get('reason') || '未知原因'}`, 5000);
  }
}

/* ---------- 主题：浅色 / 深色 / 跟随系统 ---------- */
const THEME_KEY = 'mail-summary-theme';

function _systemDark() {
  return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches;
}

function applyTheme(pref) {
  const dark = pref === 'dark' || (pref === 'auto' && _systemDark());
  document.documentElement.classList.toggle('theme-dark', dark);
}

function initTheme() {
  // 默认浅色：桌面端 WebView 的 prefers-color-scheme 会跟随系统深色，
  // 若默认"跟随系统"则每次打开都是深色。用户仍可在设置里改为深色/跟随系统。
  const pref = localStorage.getItem(THEME_KEY) || 'light';
  applyTheme(pref);
  const sel = document.getElementById('theme-select');
  if (sel) {
    sel.value = pref;
    sel.onchange = () => {
      localStorage.setItem(THEME_KEY, sel.value);
      applyTheme(sel.value);
    };
  }
  if (window.matchMedia) {
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
      if ((localStorage.getItem(THEME_KEY) || 'auto') === 'auto') applyTheme('auto');
    });
  }
}

/* ---------- 外观：设计风格 / 详情收起 / 正文字号 ---------- */
const DETAIL_KEY = 'mail-summary-detail-collapsed';
const FONT_KEY = 'mail-summary-font-size';
const DESIGN_KEY = 'mail-summary-design';
const DESIGNS = ['flow', 'editorial', 'glass', 'terminal'];

/* 通过 <html data-design="..."> 切换整体设计语言（不改动 DOM 结构与 JS 逻辑） */
function applyDesign(name) {
  document.documentElement.dataset.design = DESIGNS.includes(name) ? name : 'flow';
}

function applyDetailCollapsed(collapsed) {
  const layout = document.querySelector('.layout');
  if (layout) layout.classList.toggle('detail-collapsed', collapsed);
  const btn = document.getElementById('detail-toggle');
  if (btn) {
    btn.textContent = collapsed ? '❮' : '❯';
    btn.title = collapsed ? '展开详情' : '收起详情';
  }
}

/* 打开邮件/公司详情时，若详情面板处于收起状态则自动展开 */
function expandDetail() {
  const layout = document.querySelector('.layout');
  if (layout && layout.classList.contains('detail-collapsed')) {
    localStorage.setItem(DETAIL_KEY, '0');
    applyDetailCollapsed(false);
  }
}

function applyFontSize(px) {
  document.documentElement.style.setProperty('--mail-font-size', `${px}px`);
}

function initAppearance() {
  // 设计风格（编辑部 / 玻璃拟态 / 终端控制台）
  const dsel = document.getElementById('design-select');
  if (dsel) {
    const design = localStorage.getItem(DESIGN_KEY) || 'flow';
    dsel.value = design;
    applyDesign(design);
    dsel.onchange = () => {
      localStorage.setItem(DESIGN_KEY, dsel.value);
      applyDesign(dsel.value);
    };
  }
  // 详情面板收起 / 展开
  applyDetailCollapsed(localStorage.getItem(DETAIL_KEY) === '1');
  const dbtn = document.getElementById('detail-toggle');
  if (dbtn) {
    dbtn.onclick = () => {
      const next = !document.querySelector('.layout').classList.contains('detail-collapsed');
      localStorage.setItem(DETAIL_KEY, next ? '1' : '0');
      applyDetailCollapsed(next);
    };
  }
  // 邮件正文字号
  const fsel = document.getElementById('font-size-select');
  if (fsel) {
    const px = localStorage.getItem(FONT_KEY) || '14';
    fsel.value = px;
    applyFontSize(px);
    fsel.onchange = () => {
      localStorage.setItem(FONT_KEY, fsel.value);
      applyFontSize(fsel.value);
    };
  }
}

/* ---------- 侧栏分组折叠（分类 / 标签 / 公司） ---------- */
const SIDE_GROUP_KEY = 'mail-summary-side-collapsed';

function readCollapsedGroups() {
  try { return JSON.parse(localStorage.getItem(SIDE_GROUP_KEY)) || {}; }
  catch (e) { return {}; }
}

function applySideCollapse() {
  const collapsed = readCollapsedGroups();
  document.querySelectorAll('.side-group[data-group]').forEach((g) => {
    g.classList.toggle('collapsed', !!collapsed[g.dataset.group]);
  });
}

function toggleSideGroup(key) {
  const collapsed = readCollapsedGroups();
  collapsed[key] = !collapsed[key];
  try { localStorage.setItem(SIDE_GROUP_KEY, JSON.stringify(collapsed)); } catch (e) { /* ignore */ }
  applySideCollapse();
}

function initSideCollapse() {
  applySideCollapse();
  document.querySelectorAll('.side-title[data-toggle]').forEach((el) => {
    el.onclick = (ev) => {
      if (ev.target.closest('.side-add')) return; // 「＋ 新增分类」不触发折叠
      toggleSideGroup(el.dataset.toggle);
    };
  });
}

async function main() {
  initTheme();
  initAppearance();
  initSideCollapse();
  bind();
  try {
    await loadProviders();
    await loadAccounts();
    await loadStats();
    await loadCompanies();
    await loadLlmConfig();
    await loadMessages();
  } catch (e) {
    toast(`初始化失败：${e.message}`, 4000);
  }
  handleOAuthReturn();
  // 后端每 30 秒自动同步新邮件；前端每 15 秒静默刷新列表，让新邮件自动出现。
  // 窗口隐藏时不刷新；「公司视图 / 摘要视图」由各自逻辑自行更新。
  setInterval(() => {
    if (document.hidden) return;
    if (state.view === 'companies' || state.view === 'digest') return;
    loadMessages({ silent: true }).catch(() => { /* 后台刷新失败静默忽略 */ });
  }, 15000);
}

main();