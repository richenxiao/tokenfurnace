/* TokenFurnace 前端 —— 无框架、无构建，纯原生。 */
'use strict';

const $ = (id) => document.getElementById(id);
let S = null;                 // 服务端状态
let dirty = false;            // 用户正在编辑，暂停回填表单
let modelSig = '';            // 模型列表签名，变了才重建 DOM
let sel = {};                 // {modelId: {on, weight, ppk}}
let chartRange = 'live';      // live | all
let rateWin = 20;             // 即时速率统计窗口（秒）
// 渲染签名：内容没变就跳过 DOM 重建 / 画布重绘
let modelStateSig = '';
let sparkSig = '';
let hoverIdx = -1;
let chartCache = null;

/* ==================== 工具函数 ==================== */
function human(n, digits) {
  n = Number(n) || 0;
  const units = ['', 'K', 'M', 'B', 'T'];
  let i = 0;
  while (Math.abs(n) >= 1000 && i < units.length - 1) { n /= 1000; i++; }
  const d = digits === undefined ? (i === 0 ? 0 : 1) : digits;
  return n.toFixed(d) + units[i];
}
function num(n, d = 0) {
  return (Number(n) || 0).toLocaleString('zh-CN',
    { minimumFractionDigits: d, maximumFractionDigits: d });
}
function dur(sec) {
  sec = Math.max(0, Math.floor(sec || 0));
  const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
  const p = (x) => String(x).padStart(2, '0');
  return h ? `${h}:${p(m)}:${p(s)}` : `${p(m)}:${p(s)}`;
}
function hhmm(ts) {
  const d = new Date(ts * 1000);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}
function hhmmss(ts) {
  const d = new Date(ts * 1000);
  const p = (x) => String(x).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}
function toast(msg, kind = 'ok', ms = 4200) {
  const el = document.createElement('div');
  el.className = kind; el.textContent = msg;
  $('toast').appendChild(el);
  setTimeout(() => el.remove(), ms);
}
async function api(path, body, method = 'POST') {
  const opt = { method, headers: { 'Content-Type': 'application/json' } };
  if (body !== undefined) opt.body = JSON.stringify(body);
  const r = await fetch(path, opt);
  let j = {};
  try { j = await r.json(); } catch (e) { /* 非 JSON */ }
  if (!r.ok || j.ok === false) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
}

/* ==================== 分段控件 ==================== */
const SEG_HIDDEN = {
  modeSeg: 'modeSel', keySourceSeg: 'keySource',
  protoSeg: 'protocol',
};

function setSeg(id, v) {
  const box = $(id);
  if (!box) return;
  [...box.querySelectorAll('button[data-v]')].forEach(b =>
    b.classList.toggle('on', b.dataset.v === v));
  const hid = SEG_HIDDEN[id];
  if (hid) $(hid).value = v;
}
function segVal(id) {
  const b = $(id) && $(id).querySelector('button.on');
  return b ? b.dataset.v : '';
}
function initSeg(id, cb) {
  const box = $(id);
  if (!box) return;
  box.addEventListener('click', (e) => {
    const b = e.target.closest('button[data-v]');
    if (!b) return;
    setSeg(id, b.dataset.v);
    if (cb) cb(b.dataset.v);
  });
}

/* ==================== 数字步进器 ==================== */
function initSteppers() {
  document.querySelectorAll('.stepper').forEach(st => {
    const input = st.querySelector('input');
    if (!input) return;
    st.addEventListener('click', (e) => {
      const b = e.target.closest('button[data-d]');
      if (!b) return;                       // #btnCalib 没有 data-d，自动跳过
      const min = st.dataset.min !== undefined ? parseFloat(st.dataset.min) : -Infinity;
      const max = st.dataset.max !== undefined ? parseFloat(st.dataset.max) : Infinity;
      const step = parseFloat(st.dataset.step || '1');
      let v = (parseFloat(input.value) || 0) + parseFloat(b.dataset.d) * step;
      v = Math.min(max, Math.max(min, v));
      const dec = (String(step).split('.')[1] || '').length;
      input.value = v.toFixed(dec);
      dirty = true;
      input.dispatchEvent(new Event('change', { bubbles: true }));
    });
  });
}

/* ==================== 表单读写 ==================== */
const NUM_FIELDS = ['concurrency', 'timeout', 'inputChars', 'maxTokens', 'retry', 'safety',
  'maxPoints', 'maxTokensCap', 'maxRequests', 'duration', 'limit5h', 'limitWeek', 'ppk'];

function readSpec() {
  return {
    mode: $('modeSel').value,
    concurrency: +$('concurrency').value || 1,
    timeout: +$('timeout').value || 180,
    input_chars: +$('inputChars').value || 380000,
    max_tokens: +$('maxTokens').value || 8,
    reasoning_effort: $('reasoning').value,
    retry: +$('retry').value || 5,
    safety_ratio: +$('safety').value || 0.97,
    instant_window: rateWin,
    cache_bust: $('cacheBust').checked,
    max_points: +$('maxPoints').value || 0,
    max_total_tokens: +$('maxTokensCap').value || 0,
    max_requests: +$('maxRequests').value || 0,
    duration_min: +$('duration').value || 0,
    enforce_windows: $('enforceWin').checked,
    limit_5h: +$('limit5h').value || 0,
    limit_week: +$('limitWeek').value || 0,
    pools: readPools(),
    points_per_1k: +$('ppk').value || 0,
    loop: $('loop').checked,
    models: Object.entries(sel).filter(([, v]) => v.on).map(([id, v]) => ({
      id, weight: +v.weight || 1, points_per_1k: +v.ppk || 0,
    })),
  };
}

function fillForm(cfg, active) {
  if (dirty) return;
  const p = (cfg.profiles || []).find(x => x.id === active) || (cfg.profiles || [])[0];
  if (!p) return;
  const d = cfg.engine_defaults || {};

  $('profName').value = p.name || '';
  $('baseUrl').value = p.base_url || '';
  const a = p.auth || {};
  setSeg('protoSeg', p.protocol || 'openai-chat');
  renderAuthOptions((S && S.auth_styles) || {}, a.style || 'bearer');
  // 密钥：三种来源各自独立取值，不要互相串
  keyCache = { inline: '', env: '', file: '' };
  const src = a.source || 'inline';
  if (src === 'inline') keyCache.inline = a.value || '';
  else keyCache[src] = a.ref || '';
  setSeg('keySourceSeg', src);
  $('keyValue').value = keyCache[src] || '';
  updateKeyUI();

  const lim = p.limits || {};
  $('limit5h').value = lim.window_5h ?? 60000;
  $('limitWeek').value = lim.week ?? 600000;
  renderPools(p.pools || []);

  setSeg('modeSeg', d.mode || 'prefill');
  $('modeTail').textContent = ({
    prefill: '大输入 + 极短输出，实测最快',
    decode: '小输入 + 长输出，慢约 80 倍',
    mixed: '以预填充为主，穿插生成',
  })[d.mode || 'prefill'];
  $('concurrency').value = d.concurrency ?? 4;
  $('timeout').value = d.timeout ?? 180;
  $('inputChars').value = d.input_chars ?? 380000;
  $('maxTokens').value = d.max_tokens ?? 8;
  $('reasoning').value = d.reasoning_effort || 'none';
  $('retry').value = d.retry ?? 5;
  $('safety').value = d.safety_ratio ?? 0.97;
  $('cacheBust').checked = d.cache_bust !== false;
  rateWin = +d.instant_window || 20;
  setSeg('winSeg', String(rateWin));

  $('maxPoints').value = d.max_points ?? 0;
  $('maxTokensCap').value = d.max_total_tokens ?? 0;
  $('maxRequests').value = d.max_requests ?? 0;
  $('duration').value = d.duration_min ?? 0;
  $('enforceWin').checked = !!d.enforce_windows;
  $('loop').checked = !!d.loop;
  const ppk = (p.points_per_1k !== undefined && p.points_per_1k !== null)
    ? p.points_per_1k : (d.points_per_1k ?? 0);
  $('ppk').value = ppk;

  // 勾选状态：保留用户已有编辑，只补新增模型
  const prev = sel;
  sel = {};
  (p.models || []).forEach(m => {
    sel[m] = prev[m] || { on: false, weight: 1, ppk: 0 };
  });
  (p.selected || []).forEach(s => {
    if (!s.id) return;
    sel[s.id] = sel[s.id] || { on: false, weight: 1, ppk: 0 };
    sel[s.id].on = true;
    sel[s.id].weight = s.weight || 1;
    sel[s.id].ppk = s.points_per_1k || 0;
  });
  renderModels(true);
  updateEstimate();
}

function updateKeyUI() {
  const src = $('keySource').value;
  const map = {
    inline: ['API Key', 'sk-...', '明文保存在本地 config.json'],
    env: ['环境变量名', 'MY_LLM_API_KEY', '只存变量名，运行时读取，不落盘'],
    file: ['密钥文件路径', 'C:/keys/llm.txt', '读取文件内容并 trim'],
  }[src] || ['API Key', 'sk-...', ''];
  $('keyLabel').textContent = map[0];
  $('keyValue').placeholder = map[1];
  $('keyHint').textContent = map[2];
}

/* 三种密钥来源各自独立存值。切换来源不该把上一个来源的内容带过去——
   否则把密钥切到「环境变量」时，输入框里会留着 sk-xxx 当成变量名。 */
let keyCache = { inline: '', env: '', file: '' };

function stashKeyInput() {
  keyCache[$('keySource').value] = $('keyValue').value;
}

function switchKeySource(next) {
  const cur = $('keySource').value;
  if (next === cur) return;
  keyCache[cur] = $('keyValue').value;    // 把当前输入存回它所属的来源
  setSeg('keySourceSeg', next);
  $('keyValue').value = keyCache[next] || '';
  updateKeyUI();
  dirty = true;
  renderSaveState();
}

/** 只在用户真的改过密钥时才回传明文，否则回传空串让服务端用已保存的那份。
    否则会把脱敏串（sk-abc********xyz）当成真密钥发出去，认证必然失败。 */
function collectKey() {
  if ($('keySource').value !== 'inline') return '';
  const v = $('keyValue').value.trim();
  return v.includes('*') ? '' : v;
}

/** 首次使用引导：陌生人打开页面时，明确告诉他还差哪几步。 */
function renderFirstRun() {
  const el = $('firstRun');
  if (!S) { el.innerHTML = ''; return; }
  const p = (S.config.profiles || []).find(x => x.id === S.active_profile);
  if (!p) { el.innerHTML = ''; return; }
  const steps = [];
  if (!p.base_url) steps.push('填 <b>Base URL</b> —— 服务商的 API 根地址，比如 https://api.example.com');
  if (!(p.auth && p.auth.has_key)) steps.push('填 <b>密钥</b> —— 直接填写，或改用环境变量（不落盘）');
  if (!(p.selected || []).length) steps.push('点 <b>拉取模型</b>，勾选你要消耗的模型（也可以手动填模型 ID）');
  if (!steps.length) { el.innerHTML = ''; return; }
  el.className = 'first-run';
  el.innerHTML =
    `<div class="fr-title">还差 ${steps.length} 步就能开始</div>
     <ol>${steps.map(s => `<li>${s}</li>`).join('')}</ol>
     <div class="fr-note">配好后点「测试连接」验证，再点「用当前配置开始消费」。</div>`;
}

/** 积分池编辑区。留空即单池，行为等同上面的 5 小时 / 周额度。 */
function renderPools(pools) {
  const box = $('poolList');
  box.innerHTML = '';
  (pools || []).forEach((p, i) => box.appendChild(poolRow(p, i)));
}

function poolRow(p, i) {
  const row = document.createElement('div');
  row.className = 'pool-row';
  row.dataset.i = i;
  row.innerHTML =
    `<input class="p-name" placeholder="池名称" value="${escapeHtml(p.name || '')}">
     <input class="p-models" placeholder="*flash-lite*" value="${escapeHtml((p.models || []).join(','))}">
     <input class="p-5h" type="number" placeholder="5h 上限" value="${p.limit_5h || 0}">
     <input class="p-week" type="number" placeholder="周上限" value="${p.limit_week || 0}">
     <button type="button" class="p-del" title="删除">×</button>`;
  row.querySelector('.p-del').addEventListener('click', () => row.remove());
  return row;
}

function readPools() {
  return [...$('poolList').querySelectorAll('.pool-row')].map(row => ({
    name: row.querySelector('.p-name').value.trim() || '积分池',
    models: row.querySelector('.p-models').value.split(',')
      .map(s => s.trim()).filter(Boolean),
    limit_5h: +row.querySelector('.p-5h').value || 0,
    limit_week: +row.querySelector('.p-week').value || 0,
  })).filter(p => p.models.length);
}

$('btnAddPool').addEventListener('click', () => {
  const box = $('poolList');
  box.appendChild(poolRow({ name: '', models: [], limit_5h: 60000, limit_week: 600000 },
                          box.children.length));
});

/** 配置档案条下方的状态行：改动是否已落盘、密钥是否齐备。 */
function renderSaveState() {
  const el = $('saveState');
  if (!S) { el.textContent = ''; return; }
  const prof = (S.config.profiles || []).find(p => p.id === S.active_profile) || {};
  if (dirty) {
    el.className = 'save-state dirty';
    el.textContent = '● 有未保存的改动';
  } else if (!(prof.auth && prof.auth.has_key)) {
    el.className = 'save-state warn';
    el.textContent = '● 未配置密钥，无法开始消费';
  } else {
    el.className = 'save-state';
    el.textContent = '● 已保存';
  }
}

/* ==================== 协议与认证字段 ==================== */
function renderAuthOptions(styles, current) {
  const el = $('authStyle');
  const sig = Object.keys(styles || {}).join('|');
  if (el.dataset.sig !== sig) {
    el.dataset.sig = sig;
    el.innerHTML = Object.entries(styles || {}).map(([k, v]) =>
      `<option value="${k}">${escapeHtml(v.label)}</option>`).join('');
  }
  if (current) el.value = current;
  updateAuthHint();
}
function updateAuthHint() {
  const a = (S && S.auth_styles) ? S.auth_styles[$('authStyle').value] : null;
  $('authHint').textContent = a ? a.hint : '';
}
function applyProtocolDefaults(proto) {
  // 切协议时把认证字段带到该协议的惯用值，之后仍可手动改
  const p = (S && S.protocols) ? S.protocols[proto] : null;
  if (p && p.auth) $('authStyle').value = p.auth;
  updateAuthHint();
}

/* ==================== 模型列表 ==================== */
function visibleModels() {
  const q = ($('modelFilter').value || '').trim().toLowerCase();
  return Object.keys(sel).filter(id => !q || id.toLowerCase().includes(q));
}

function renderModels(force) {
  const ids = Object.keys(sel);
  const sig = ids.join('|') + '::' + $('modelFilter').value;
  if (!force && sig === modelSig) { syncModelInputs(); return; }
  modelSig = sig;
  modelStateSig = '';

  const box = $('modelList');
  const shown = visibleModels();
  if (!ids.length) {
    box.innerHTML = '<div class="model-empty">还没有模型列表<br>点「拉取模型」，或先选一个服务商预设</div>';
    updateModelBadge(); return;
  }
  if (!shown.length) {
    box.innerHTML = `<div class="model-empty">没有匹配「${escapeHtml($('modelFilter').value)}」的模型</div>`;
    updateModelBadge(); return;
  }
  box.innerHTML =
    `<div class="model-head"><span></span><span>模型</span>
       <span class="c">权重</span><span class="c">积分/1K</span></div>` +
    shown.map(id => `
      <div class="model-item ${sel[id].on ? 'on' : ''}" data-m="${escapeHtml(id)}">
        <span class="tick">✓</span>
        <span class="name" title="${escapeHtml(id)}">${escapeHtml(id)}</span>
        <input type="number" data-m="${escapeHtml(id)}" data-r="weight" min="0" step="1"
               value="${sel[id].weight}" ${sel[id].on ? '' : 'disabled'}>
        <input type="number" data-m="${escapeHtml(id)}" data-r="ppk" min="0" step="0.1"
               value="${sel[id].ppk}" ${sel[id].on ? '' : 'disabled'}>
      </div>`).join('');

  box.querySelectorAll('.model-item').forEach(row => {
    row.addEventListener('click', (e) => {
      if (e.target.tagName === 'INPUT') return;      // 点输入框不切换勾选
      const m = row.dataset.m;
      sel[m].on = !sel[m].on;
      dirty = true;
      row.classList.toggle('on', sel[m].on);
      row.querySelectorAll('input').forEach(i => i.disabled = !sel[m].on);
      updateModelBadge(); updateEstimate();
    });
  });
  box.querySelectorAll('input').forEach(el => {
    el.addEventListener('change', () => {
      const m = el.dataset.m, r = el.dataset.r;
      sel[m][r] = parseFloat(el.value) || 0;
      dirty = true;
      updateEstimate();
    });
    el.addEventListener('click', e => e.stopPropagation());
  });
  updateModelBadge();
}

function syncModelInputs() {
  // 勾选/权重/系数都没变就不用碰 DOM——每秒一次的全表同步是纯浪费
  const sig = Object.entries(sel).map(([k, v]) => `${k}:${v.on?1:0}:${v.weight}:${v.ppk}`).join('|');
  if (sig === modelStateSig) return;
  modelStateSig = sig;
  document.querySelectorAll('#modelList .model-item').forEach(row => {
    const m = row.dataset.m;
    if (!sel[m]) return;
    row.classList.toggle('on', sel[m].on);
    row.querySelectorAll('input').forEach(el => {
      el.disabled = !sel[m].on;
      if (document.activeElement !== el) el.value = sel[m][el.dataset.r];
    });
  });
}

function updateModelBadge() {
  const on = Object.entries(sel).filter(([, v]) => v.on);
  const n = on.length;
  $('modelBadge').textContent = `${n} / ${Object.keys(sel).length} 个`;
  $('modelBadge').className = 'badge ' + (n ? 'green' : 'amber');
  const names = on.map(([k]) => k);
  $('pickHint').textContent = n === 0
    ? '未勾选任何模型——不勾选就无法开始消费'
    : `本次将消耗 ${n} 个模型：${names.slice(0, 3).join('、')}`
      + (n > 3 ? ` 等 ${n} 个` : '');
}

function updateEstimate() {
  const chars = +$('inputChars').value || 0;
  const outTok = +$('maxTokens').value || 0;
  const inTok = Math.round(chars / 2.07);
  const tok = inTok + outTok;
  $('inputHint').textContent = `≈ ${num(inTok)} tokens`;
  const ppk = +$('ppk').value || 0;
  let txt = `单请求约 ${num(tok)} tokens（输入 ${num(inTok)} + 输出 ${outTok}）`;
  if (ppk > 0) txt += ` · 约 ${(tok / 1000 * ppk).toFixed(2)} 积分`;
  const mp = +$('maxPoints').value || 0;
  if (mp > 0 && ppk > 0) {
    txt += ` · 跑满 ${num(mp)} 积分需约 ${human(mp * 1000 / ppk)} tokens`;
  }
  $('estHint').textContent = txt;
}

/* ==================== 批量启动 ==================== */
let batchPick = {};     // {profileId: true}
let batchBusy = false;

function renderBatchList(profiles) {
  profiles = profiles || [];
  const box = $('batchList');
  const sig = profiles.map(p =>
    `${p.id}:${p.name}:${(p.selected || []).length}:${p.auth && p.auth.has_key ? 1 : 0}`
    + `:${p.base_url ? 1 : 0}`).join('|');
  if (box.dataset.sig !== sig) {
    box.dataset.sig = sig;
    const ids = new Set(profiles.map(p => p.id));   // 清掉已不存在的选择
    Object.keys(batchPick).forEach(k => { if (!ids.has(k)) delete batchPick[k]; });
    box.innerHTML = profiles.map(p => {
      const nSel = (p.selected || []).length;
      const lim = p.limits || {};
      const hasKey = !!(p.auth && p.auth.has_key);
      // 没密钥 / 没选模型的配置根本起不来，直接禁掉并写明原因——
      // 光靠启动后弹一句会自动消失的提示，用户很容易漏看，还以为只跑了一个
      // Base URL 是最根本的——没填它，密钥和模型都无从谈起，所以先报这个
      const block = !p.base_url ? '没填 Base URL'
        : (!hasKey ? '没有可用的 API Key'
          : (!nSel ? '没有勾选任何模型' : ''));
      if (block) delete batchPick[p.id];
      const sub = [
        ((S.protocols || {})[p.protocol] || {}).label || p.protocol,
        nSel ? `${nSel} 个模型` : '未选模型',
        lim.window_5h ? `5h ${num(lim.window_5h)}` : '未设额度',
      ].filter(Boolean).join(' · ');
      return `<label class="switch batch-item${block ? ' blocked' : ''}">
        <input type="checkbox" data-pid="${escapeHtml(p.id)}"
               ${batchPick[p.id] ? 'checked' : ''} ${block ? 'disabled' : ''}>
        <span class="track"></span>
        <span class="txt">${escapeHtml(p.name)}<small>${escapeHtml(sub)}</small>
          ${block ? `<small class="block-reason">无法启动：${escapeHtml(block)}</small>` : ''}</span>
      </label>`;
    }).join('') || '<div class="sess-empty">还没有配置</div>';
    box.querySelectorAll('input[data-pid]').forEach(el => {
      el.addEventListener('change', () => {
        if (el.checked) batchPick[el.dataset.pid] = true;
        else delete batchPick[el.dataset.pid];
        updateBatchBadge();
      });
    });
  }
  updateBatchBadge();
}

function updateBatchBadge() {
  // 只统计**能启动的**配置，被禁掉的不算进分母
  const usable = $('batchList').querySelectorAll('input[data-pid]:not([disabled])').length;
  const n = Object.keys(batchPick).length;
  $('batchBadge').textContent = n ? `${n} / ${usable}` : (usable ? '未选' : '无可启动');
  $('batchBadge').className = 'badge ' + (n ? 'green' : (usable ? 'grey' : 'red'));
  $('btnBatchStart').disabled = !n;
}

/* ==================== 看板 ==================== */
function rateLevel(r) {
  if (!r || r < 1) return 'lv-idle';
  if (r < 2000) return 'lv-low';
  if (r < 8000) return 'lv-mid';
  return 'lv-high';
}

function renderSessions(L) {
  const list = L.sessions || [];
  $('sessBadge').textContent = list.length;
  $('sessWorkers').textContent = `并发 ${L.workers_in_use || 0} / ${L.max_total_workers || 32}`;
  const box = $('sessList');
  if (!list.length) {
    box.innerHTML = '<div class="sess-empty">没有运行中的会话。左侧配好后点「用当前配置开始消费」。</div>';
    return;
  }
  const LABEL = { running: '运行中', waiting: '等待窗口', stopping: '正在停止',
                  done: '已结束', error: '异常', idle: '空闲' };
  const CLS = { running: 'green', waiting: 'amber', stopping: 'amber',
                done: 'grey', error: 'red', idle: 'grey' };
  // 会话条数很少，直接整块重建；真正费的是日志那 120 行，那边才需要签名
  box.innerHTML = list.map(s => `
    <div class="sess ${s.running ? '' : 'done'}">
      <div>
        <div class="sess-name">${escapeHtml(s.name)}
          <span class="badge ${CLS[s.status] || 'grey'}">${LABEL[s.status] || s.status}</span></div>
        <div class="sess-meta">${escapeHtml(((S.protocols || {})[s.protocol] || {}).label || s.protocol)}
          · ${escapeHtml((s.models || []).join(', '))} · 并发 ${s.concurrency}
          ${s.avg_latency ? ` · 单请求 ${s.avg_latency.toFixed(0)} 秒` : ''}
          ${s.wait_reason ? ' · ' + escapeHtml(s.wait_reason) : ''}</div>
      </div>
      <div class="sess-stats">
        <span><b>${human(s.rate_instant, 0)}</b> tok/s</span>
        <span><b>${human(s.total_tokens, 1)}</b> tok</span>
        <span><b>${num(s.ok)}</b>/${num(s.requests)} 请求</span>
      </div>
      ${s.running
        ? `<button class="sm ghost" data-stop="${s.sid}">停止</button>`
        : '<button class="sm ghost" disabled>已结束</button>'}
    </div>`).join('');
  box.querySelectorAll('button[data-stop]').forEach(b =>
    b.addEventListener('click', async () => {
      b.disabled = true;
      try {
        await api('/api/run/stop', { sid: b.dataset.stop, reason: '手动停止' });
        toast('已发送停止指令');
      } catch (e) { toast(e.message, 'err'); b.disabled = false; }
    }));
}

function renderLive(L) {
  // —— 速率主卡 ——
  const inst = L.rate_instant || 0;
  $('sRate').innerHTML = `${human(inst, inst < 100 ? 1 : 0)}<span class="u">tokens/秒</span>`;
  $('sRate').className = 'value ' + rateLevel(inst);
  // 窗口被自动放宽时必须说明，否则用户会以为自己的设置没生效
  $('rateWinNote').textContent = L.window_auto
    ? `完成太稀疏，${Math.round(L.instant_window)} 秒窗口会频繁读成 0，`
      + `已自动放宽到 ${Math.round(L.effective_window)} 秒`
    : '';
  $('sRateAvg').textContent = human(L.rate || 0, 1);
  $('sRatePeak').textContent = human(L.rate_peak || 0, 1);
  $('sRps').textContent = (L.rps || 0).toFixed(2);
  drawSpark(L.spark || []);

  // —— 指标卡 ——
  $('sTokens').textContent = human(L.total_tokens, 1);
  $('sTokensSub').textContent = `输入 ${human(L.prompt_tokens, 1)} · 输出 ${human(L.completion_tokens, 1)}`;
  const hasCoef = (+$('ppk').value || 0) > 0 || Object.values(sel).some(v => v.ppk > 0);
  if (hasCoef) {
    $('sPoints').textContent = num(L.points, 1);
    $('sPointsSub').textContent = '按当前系数估算';
  } else {
    $('sPoints').textContent = '—';
    $('sPointsSub').textContent = '需填换算系数';
  }
  $('sReq').textContent = num(L.requests);
  $('sReqSub').textContent = `成功 ${num(L.ok)} · 失败 ${num(L.failed)}`;
  $('sElapsed').textContent = dur(L.elapsed);
  const dm = +$('duration').value || 0;
  $('sLeft').textContent = dm > 0
    ? `剩余 ${dur(Math.max(0, dm * 60 - L.elapsed))}`
    : (L.running ? '未设时长上限' : '—');
  $('sCached').textContent = human(L.cached_tokens, 1);

  // —— 状态 ——
  const st = L.running ? L.status : (L.status === 'idle' ? 'idle' : L.status);
  const dot = { idle: 'idle', running: 'running', waiting: 'waiting',
                stopping: 'waiting', done: 'done', error: 'error' }[st] || 'idle';
  const n = L.session_count || 0;
  const label = n > 1
    ? `${n} 个会话运行中`
    : ({ idle: '空闲', running: '运行中', waiting: '等待窗口',
         stopping: '正在停止（等在途请求结束）', done: '已结束', error: '异常' }[st] || st);
  $('statusText').innerHTML = `<span class="dot ${dot}"></span> ${label}` +
    (L.stop_reason ? ` · ${escapeHtml(L.stop_reason)}` : '');
  // 可以同时跑多个会话，所以「开始」不禁用；只有真在跑时才允许「全部停止」
  $('btnStart').disabled = false;
  $('btnStop').disabled = !L.running;

  renderSessions(L);

  // —— 窗口 ——
  const w = L.windows;
  const hasLimit = w && (w.limit_5h > 0 || w.limit_week > 0);
  const pools = (w && w.pools) || [];
  const badge = $('winBadge');
  if (w && w.enforce && !w.coef_set) {
    // 没设换算系数就没法把 token 折算成积分，限流形同虚设，必须说清楚
    badge.textContent = '缺换算系数';
    badge.className = 'badge red';
    $('winBody').innerHTML = '<div class="kv"><span>未设换算系数，无法按积分限流</span></div>'
      + `<div class="kv"><span>窗口内</span><span class="v">${human(w.tokens_5h, 1)} / ${human(w.tokens_week, 1)} tokens</span></div>`;
  } else if (pools.length) {
    badge.textContent = w.enforce ? '已启用' : '仅展示';
    badge.className = 'badge ' + (w.enforce ? 'green' : 'grey');
    $('winBody').innerHTML = pools.map(poolHtml).join('');
  } else if (hasLimit) {
    badge.textContent = w.enforce ? '已启用' : '仅展示';
    badge.className = 'badge ' + (w.enforce ? 'green' : 'grey');
    $('winBody').innerHTML = poolHtml({
      name: '合计', points_5h: w.points_5h, limit_5h: w.limit_5h,
      points_week: w.points_week, limit_week: w.limit_week, blocked: false,
    });
  } else if (w) {
    badge.textContent = '未设额度';
    badge.className = 'badge grey';
    $('winBody').innerHTML =
      `<div class="kv"><span>5 小时窗口</span><span class="v">${human(w.tokens_5h, 1)} tokens</span></div>`
      + `<div class="kv"><span>周窗口</span><span class="v">${human(w.tokens_week, 1)} tokens</span></div>`;
  } else {
    badge.textContent = '未设置';
    badge.className = 'badge grey';
    $('winBody').innerHTML = '<div class="kv"><span>—</span></div>';
  }
  if (L.wait_until && L.wait_until > Date.now() / 1000) {
    $('waitRow').style.display = '';
    $('waitText').textContent = `${L.wait_reason} · 还剩 ${dur(L.wait_until - Date.now() / 1000)}`;
  } else {
    $('waitRow').style.display = 'none';
  }

  // —— 趋势图 ——
  const series = chartRange === 'live'
    ? (L.series || []).map(p => ({ t: p.t, tokens: p.tokens, bw: 10 }))
    : (L.per_minute || []).map(p => ({ t: p.t, tokens: p.tokens, bw: 60 }));
  $('chartBadge').textContent = chartRange === 'live' ? '10 秒粒度' : '1 分钟粒度';
  drawChart(series);
}

/** 一个积分池的窗口占用：5 小时 + 周两条进度条。 */
function poolHtml(p) {
  const tag = p.blocked
    ? '<span class="badge red" style="margin-left:6px">已用满</span>' : '';
  const models = (p.models || []).join('、');
  return `<div class="pool-block">
      <div class="pool-head"><span class="pool-name">${escapeHtml(p.name || '')}</span>${tag}</div>
      ${p.limit_5h > 0 ? barRow('5 小时窗口', p.points_5h, p.limit_5h) : ''}
      ${p.limit_week > 0 ? barRow('周窗口', p.points_week, p.limit_week) : ''}
      ${models ? `<div class="pool-models">匹配模型：${escapeHtml(models)}</div>` : ''}
    </div>`;
}

function barRow(label, used, limit) {
  const pct = limit > 0 ? Math.min(100, (used || 0) / limit * 100) : 0;
  const cls = pct > 92 ? ' hot' : pct > 75 ? ' warn' : '';
  return `<div style="margin-top:6px">
      <div class="kv"><span>${label}</span><span class="v">${num(used || 0, 0)} / ${num(limit, 0)} 积分（${pct.toFixed(1)}%）</span></div>
      <div class="bar${cls}"><i style="width:${pct.toFixed(1)}%"></i></div>
    </div>`;
}

/* ==================== 迷你速率图 ==================== */
function drawSpark(data) {
  const sig = data.length + '|' + (data.length ? data[data.length - 1] : 0);
  if (sig === sparkSig) return;
  sparkSig = sig;
  const cv = $('spark');
  const dpr = window.devicePixelRatio || 1;
  const W = cv.clientWidth || 180, H = 62;
  if (cv.width !== W * dpr) { cv.width = W * dpr; cv.height = H * dpr; }
  const g = cv.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, W, H);

  const max = Math.max(...data, 1);
  const n = data.length;
  if (!n) return;
  const x = (i) => (n === 1 ? W / 2 : W * i / (n - 1));

  // 面积
  const grd = g.createLinearGradient(0, 0, 0, H);
  grd.addColorStop(0, '#3fb95055');
  grd.addColorStop(1, '#3fb95000');
  g.beginPath();
  g.moveTo(x(0), H - 2);
  data.forEach((v, i) => g.lineTo(x(i), H - 2 - (v / max) * (H - 6)));
  g.lineTo(x(n - 1), H - 2);
  g.closePath();
  g.fillStyle = grd; g.fill();

  // 线
  g.beginPath();
  data.forEach((v, i) => {
    const y = H - 2 - (v / max) * (H - 6);
    i ? g.lineTo(x(i), y) : g.moveTo(x(i), y);
  });
  g.strokeStyle = '#3fb950'; g.lineWidth = 1.6; g.lineJoin = 'round'; g.stroke();

  // 末端点
  const ly = H - 2 - (data[n - 1] / max) * (H - 6);
  g.beginPath(); g.arc(x(n - 1), ly, 2.6, 0, 7);
  g.fillStyle = '#3fb950'; g.fill();
}

/* ==================== 趋势图 ==================== */
function drawChart(series) {
  const cv = $('chart');
  const wrap = cv.parentElement;
  const tip = $('chartTip');
  const dpr = window.devicePixelRatio || 1;
  const W = cv.clientWidth || wrap.clientWidth, H = 170;
  if (cv.width !== W * dpr) { cv.width = W * dpr; cv.height = H * dpr; }
  const g = cv.getContext('2d');
  g.setTransform(dpr, 0, 0, dpr, 0, 0);
  g.clearRect(0, 0, W, H);

  const pts = (series || []).map(p => ({
    t: p.t, rate: (p.tokens || 0) / (p.bw || 60), tokens: p.tokens || 0,
  }));
  // 累计
  let acc = 0;
  pts.forEach(p => { acc += p.tokens; p.cum = acc; });
  chartCache = { pts, W, H, tip };

  if (pts.length < 2 || acc === 0) {
    $('chartStat').textContent = '';
    g.fillStyle = '#6e7681'; g.font = '12px sans-serif'; g.textAlign = 'center';
    g.fillText('开始消费后这里会显示趋势', W / 2, H / 2);
    tip.style.opacity = 0;
    return;
  }

  const pad = { l: 52, r: 58, t: 14, b: 22 };
  const iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
  const maxRate = Math.max(...pts.map(p => p.rate), 1);
  const maxCum = Math.max(acc, 1);
  const X = (i) => pad.l + iw * i / (pts.length - 1);
  const Yr = (v) => pad.t + ih - (v / maxRate) * ih;
  const Yc = (v) => pad.t + ih - (v / maxCum) * ih;

  // 网格 + 左轴（速率）
  g.font = '10px ui-monospace, monospace';
  for (let i = 0; i <= 4; i++) {
    const y = pad.t + ih * i / 4;
    g.strokeStyle = '#1e242c'; g.beginPath();
    g.moveTo(pad.l, y); g.lineTo(W - pad.r, y); g.stroke();
    g.fillStyle = '#8b949e'; g.textAlign = 'right';
    g.fillText(human(maxRate * (1 - i / 4), 0), pad.l - 6, y + 3);
    g.fillStyle = '#4d5560'; g.textAlign = 'left';
    g.fillText(human(maxCum * (1 - i / 4), 0), W - pad.r + 6, y + 3);
  }

  // 速率面积
  const grd = g.createLinearGradient(0, pad.t, 0, pad.t + ih);
  grd.addColorStop(0, '#3fb95045');
  grd.addColorStop(1, '#3fb95000');
  g.beginPath();
  g.moveTo(X(0), pad.t + ih);
  pts.forEach((p, i) => g.lineTo(X(i), Yr(p.rate)));
  g.lineTo(X(pts.length - 1), pad.t + ih);
  g.closePath();
  g.fillStyle = grd; g.fill();

  // 速率线
  g.beginPath();
  pts.forEach((p, i) => i ? g.lineTo(X(i), Yr(p.rate)) : g.moveTo(X(i), Yr(p.rate)));
  g.strokeStyle = '#3fb950'; g.lineWidth = 1.8; g.lineJoin = 'round'; g.stroke();

  // 累计线
  g.beginPath();
  pts.forEach((p, i) => i ? g.lineTo(X(i), Yc(p.cum)) : g.moveTo(X(i), Yc(p.cum)));
  g.strokeStyle = '#1f6feb'; g.lineWidth = 1.5; g.setLineDash([4, 3]); g.stroke();
  g.setLineDash([]);

  // 时间轴
  g.fillStyle = '#8b949e'; g.textAlign = 'center';
  const step = Math.max(1, Math.ceil(pts.length / 7));
  pts.forEach((p, i) => {
    if (i % step) return;
    g.fillText(hhmm(p.t), X(i), H - 6);
  });

  // 悬停
  if (hoverIdx >= 0 && hoverIdx < pts.length) {
    const p = pts[hoverIdx], px = X(hoverIdx);
    g.strokeStyle = '#4d5560'; g.lineWidth = 1;
    g.beginPath(); g.moveTo(px, pad.t); g.lineTo(px, pad.t + ih); g.stroke();
    g.beginPath(); g.arc(px, Yr(p.rate), 3.4, 0, 7); g.fillStyle = '#3fb950'; g.fill();
    g.beginPath(); g.arc(px, Yc(p.cum), 3.4, 0, 7); g.fillStyle = '#1f6feb'; g.fill();

    tip.style.opacity = 1;
    tip.innerHTML =
      `<div class="tt">${hhmmss(p.t)}</div>` +
      `<div><span class="tt">速率</span> <span class="tv g">${human(p.rate, 0)} tok/s</span></div>` +
      `<div><span class="tt">本段</span> <span class="tv">${human(p.tokens, 1)} tok</span></div>` +
      `<div><span class="tt">累计</span> <span class="tv">${human(p.cum, 1)} tok</span></div>`;
    const tw = tip.offsetWidth || 140;
    let lx = px + 12;
    if (lx + tw > W) lx = px - tw - 12;
    tip.style.left = Math.max(0, lx) + 'px';
    tip.style.top = Math.max(0, Yr(p.rate) - 30) + 'px';
  } else {
    tip.style.opacity = 0;
  }

  const peak = Math.max(...pts.map(p => p.rate));
  $('chartStat').textContent = `峰值 ${human(peak, 0)} tok/s · 共 ${human(acc, 1)} tok`;
}

/* ==================== 日志 / 历史 ==================== */
// 每秒轮询一次，但绝大多数帧内容没变。用签名挡掉无谓的 DOM 重建。
let logsSig = '';
let histSig = '';

function renderLogs(logs) {
  const sig = logs.length + '|' + (logs.length ? logs[logs.length - 1].ts : 0);
  if (sig === logsSig) return;
  logsSig = sig;
  const box = $('logs');
  const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 30;
  box.innerHTML = logs.map(l =>
    `<div class="${l.level}"><span class="t">${hhmm(l.ts)}</span> ${escapeHtml(l.msg)}</div>`).join('');
  if (atBottom) box.scrollTop = box.scrollHeight;
}

function renderHistory(rows) {
  const sig = rows.map(r => `${r.id}${r.status}${r.requests}${r.failed}${r.ended || 0}`).join(',');
  if (sig === histSig) return;
  histSig = sig;
  $('histBadge').textContent = rows.length;
  $('histBody').innerHTML = rows.map(r => {
    const cls = { done: 'green', running: 'blue', error: 'red', interrupted: 'amber' }[r.status] || 'grey';
    const txt = { done: '完成', running: '运行中', error: '异常', interrupted: '被中断' }[r.status] || r.status;
    const models = (r.models || '').split(',').filter(Boolean);
    return `<tr>
      <td>${new Date(r.started * 1000).toLocaleString('zh-CN',
        { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' })}</td>
      <td>${escapeHtml(r.profile_name || r.profile || '')}</td>
      <td title="${escapeHtml(models.join(', '))}">${models.length > 1 ? models.length + ' 个模型' : escapeHtml(models[0] || '')}</td>
      <td><span class="badge ${cls}">${txt}</span></td>
      <td class="num">${num(r.requests)}</td>
      <td class="num">${num(r.failed)}</td>
      <td class="num">${human((r.prompt_tokens || 0) + (r.completion_tokens || 0), 1)}</td>
      <td><button class="sm ghost" data-run="${r.id}">明细</button></td>
    </tr>`;
  }).join('') || '<tr><td colspan="8" style="color:var(--muted);padding:14px">还没有历史会话</td></tr>';
  $('histBody').querySelectorAll('button[data-run]').forEach(b =>
    b.addEventListener('click', () => showRunDetail(b.dataset.run)));
}

async function showRunDetail(runId) {
  try {
    const d = await api(`/api/run/detail?run=${encodeURIComponent(runId)}`, undefined, 'GET');
    const r = d.run || {};
    const models = (d.models || []).map(m => `${m.model}: ${human(m.p + m.c, 1)} tokens / ${num(m.n)} 次`).join('\n');
    const errs = (d.errors || []).map(e => `${e.kind}: ${num(e.n)} 次`).join('\n') || '无';
    toast(`会话 ${runId}\n请求 ${num(r.requests)}（失败 ${num(r.failed)}）\n` +
      `tokens ${human((r.prompt_tokens || 0) + (r.completion_tokens || 0), 1)}\n\n` +
      `按模型：\n${models || '—'}\n\n错误：\n${errs}`, 'ok', 12000);
  } catch (e) { toast('读取失败：' + e.message, 'err'); }
}

/* ==================== 主轮询 ==================== */
async function tick() {
  try {
    const st = await api('/api/state', undefined, 'GET');
    S = st;
    $('verBadge').textContent = 'v' + st.version;
    $('profileMeta').textContent = `${st.active_profile_name}${st.active_has_key ? '' : '（缺密钥）'}`;

    const selEl = $('profileSel');
    const sig = (st.config.profiles || []).map(p =>
      `${p.id}:${p.name}:${p.protocol}:${p.auth && p.auth.has_key ? 1 : 0}`).join('|');
    if (selEl.dataset.sig !== sig) {
      selEl.dataset.sig = sig;
      // 下拉只显示用户自己起的名字，不擅自追加协议/状态标签——
      // 密钥是否齐备由下方状态行说明
      selEl.innerHTML = (st.config.profiles || []).map(p =>
        `<option value="${p.id}">${escapeHtml(p.name)}</option>`).join('');
    }
    if (selEl.value !== st.active_profile) selEl.value = st.active_profile;

    fillForm(st.config, st.active_profile);
    renderFirstRun();
    renderBatchList(st.config.profiles);
    renderSaveState();
    renderLive(st.live);
    renderLogs(st.logs || []);
    renderHistory(st.history || []);
  } catch (e) {
    $('statusText').innerHTML = `<span class="dot error"></span> 与服务失联：${escapeHtml(e.message)}`;
  }
}

/* ==================== 事件 ==================== */
function bind() {
  initSteppers();
  initSeg('modeSeg', (v) => {
    dirty = true;
    $('modeTail').textContent = ({
      prefill: '大输入 + 极短输出，实测最快',
      decode: '小输入 + 长输出，慢约 80 倍',
      mixed: '以预填充为主，穿插生成',
    })[v];
    updateEstimate();
  });
  // 密钥来源要自己接管：切换时需要先把当前输入存回原来源，所以不能用 initSeg
  $('keySourceSeg').addEventListener('click', (e) => {
    const b = e.target.closest('button[data-v]');
    if (b) switchKeySource(b.dataset.v);
  });
  initSeg('protoSeg', (v) => { dirty = true; applyProtocolDefaults(v); });
  $('authStyle').addEventListener('change', () => { dirty = true; updateAuthHint(); });
  initSeg('rangeSeg', (v) => {
    chartRange = v;
    hoverIdx = -1;
    if (S) renderLive(S.live);
  });
  initSeg('winSeg', async (v) => {
    rateWin = +v;
    dirty = true; renderSaveState();
    try {
      // 立刻通知服务端，不用等下次启动会话
      await api('/api/config/window', { seconds: rateWin });
      if (S) renderLive(S.live);
    } catch (e) { toast('窗口设置失败：' + e.message, 'err'); }
  });

  document.querySelectorAll('.col input, .col select').forEach(el => {
    ['input', 'change'].forEach(ev => el.addEventListener(ev, () => {
      if (['profileSel', 'modelFilter'].includes(el.id)) return;
      dirty = true; updateEstimate(); renderSaveState();
    }));
  });
  NUM_FIELDS.forEach(id => { if ($(id)) $(id).addEventListener('change', updateEstimate); });
  $('modelFilter').addEventListener('input', () => renderModels(true));

  // 图表悬停
  const cv = $('chart');
  cv.addEventListener('mousemove', (e) => {
    if (!chartCache || !chartCache.pts.length) return;
    const { pts, W } = chartCache;
    const pad = { l: 52, r: 58 };
    const rect = cv.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const iw = W - pad.l - pad.r;
    const i = Math.round((mx - pad.l) / iw * (pts.length - 1));
    hoverIdx = Math.min(pts.length - 1, Math.max(0, i));
    drawChart(chartRange === 'live'
      ? (S.live.series || []).map(p => ({ t: p.t, tokens: p.tokens, bw: 10 }))
      : (S.live.per_minute || []).map(p => ({ t: p.t, tokens: p.tokens, bw: 60 })));
  });
  cv.addEventListener('mouseleave', () => {
    hoverIdx = -1;
    if (S) renderLive(S.live);
  });

  $('baseUrl').addEventListener('blur', async () => {
    const v = $('baseUrl').value.trim();
    if (!v) return;
    // 只在该配置还没拉到模型时自动猜协议，避免覆盖用户的手动选择
    if (Object.keys(sel).length) return;
    try {
      const r = await api('/api/guess_protocol', { base_url: v });
      if (r.protocol && r.protocol !== $('protocol').value) {
        setSeg('protoSeg', r.protocol);
        applyProtocolDefaults(r.protocol);
        toast(`已按地址自动选择协议：${(S.protocols[r.protocol] || {}).label || r.protocol}`,
          'ok', 3000);
      }
    } catch (e) { /* 猜不出来就算了 */ }
  });

  $('profileSel').addEventListener('change', async () => {
    if (dirty && !confirm('当前配置有未保存的改动，切换后会丢失。\n确定切换？')) {
      $('profileSel').value = S.active_profile;      // 回滚选择
      return;
    }
    await api('/api/profile/activate', { id: $('profileSel').value });
    dirty = false; modelSig = ''; sel = {}; toast('已切换配置');
    tick();
  });

  $('btnTest').addEventListener('click', async () => {
    const b = $('btnTest'); b.disabled = true; b.textContent = '测试中…';
    try {
      const r = await api('/api/test', {
        profile_id: S.active_profile, base_url: $('baseUrl').value,
        protocol: $('protocol').value, auth_style: $('authStyle').value,
        api_key: collectKey(),
      });
      const res = r.result;
      if (res.models_ok && res.chat_ok) {
        $('connBadge').textContent = '连通'; $('connBadge').className = 'badge green';
        toast(`连接正常 · ${res.protocol} · 发现 ${res.models.length} 个模型 · 首包 ${res.chat_latency.toFixed(2)}s`);
      } else if (res.models_ok) {
        $('connBadge').textContent = '部分可用'; $('connBadge').className = 'badge amber';
        toast(`模型列表可用，但对话请求失败：${res.chat_error}`, 'warn', 8000);
      } else {
        $('connBadge').textContent = '失败'; $('connBadge').className = 'badge red';
        toast(`连接失败：${res.models_error}`, 'err', 8000);
      }
    } catch (e) { toast('测试失败：' + e.message, 'err'); }
    finally { b.disabled = false; b.textContent = '测试连接'; }
  });

  $('btnModels').addEventListener('click', async () => {
    const b = $('btnModels'); b.disabled = true; b.textContent = '拉取中…';
    try {
      const r = await api('/api/models', {
        profile_id: S.active_profile, base_url: $('baseUrl').value,
        protocol: $('protocol').value, auth_style: $('authStyle').value,
        api_key: collectKey(),
      });
      const keep = sel;
      sel = {};
      (r.models || []).forEach(m => { sel[m.id] = keep[m.id] || { on: false, weight: 1, ppk: 0 }; });
      modelSig = ''; renderModels(true);
      toast(`拉到 ${(r.models || []).length} 个模型`);
    } catch (e) { toast('拉取失败：' + e.message, 'err'); }
    finally { b.disabled = false; b.textContent = '拉取模型'; }
  });

  $('btnSave').addEventListener('click', async () => {
    try { await saveProfile(); dirty = false; toast('配置已保存'); tick(); }
    catch (e) { toast('保存失败：' + e.message, 'err'); }
  });

  $('btnNewProf').addEventListener('click', async () => {
    if (dirty && !confirm('当前配置有未保存的改动，新建后会丢失。\n确定新建？')) return;
    // 继承协议、认证字段、换算系数和窗口额度——新配置通常是同一家服务商的另一套密钥，
    // 不继承的话系数会静默变成 0、额度变成不限，窗口限流就形同虚设
    await api('/api/profile/save', { profile: {
      name: '新配置',
      protocol: $('protocol').value,
      base_url: '',
      points_per_1k: +$('ppk').value || 0,
      limits: { window_5h: +$('limit5h').value || 0,
                week: +$('limitWeek').value || 0, unit: 'points' },
      pools: readPools(),
      auth: { source: 'env', ref: 'MY_LLM_API_KEY', style: $('authStyle').value },
    } });
    dirty = false; modelSig = ''; sel = {};
    toast('已新建配置');
    tick();
  });

  $('btnDupProf').addEventListener('click', async () => {
    if (dirty && !confirm('当前配置有未保存的改动，复制出的副本不含这些改动。\n确定继续？')) return;
    try {
      await api('/api/profile/duplicate', { id: S.active_profile });
      dirty = false; modelSig = ''; sel = {};
      toast('已复制为新配置');
      tick();
    } catch (e) { toast('复制失败：' + e.message, 'err'); }
  });
  $('btnDelProf').addEventListener('click', async () => {
    if (!confirm('确定删除当前配置？该操作不可撤销。')) return;
    try { await api('/api/profile/delete', { id: S.active_profile }); toast('已删除'); dirty = false; tick(); }
    catch (e) { toast(e.message, 'err'); }
  });
  $('btnExportCfg').addEventListener('click', () => { location.href = '/api/export/config.json'; });
  $('btnImportCfg').addEventListener('click', () => $('importFile').click());
  $('importFile').addEventListener('change', async (e) => {
    const f = e.target.files[0]; if (!f) return;
    try {
      const txt = await f.text();
      const r = await api('/api/profile/import', { data: JSON.parse(txt) });
      toast(`导入 ${r.imported} 个配置`); dirty = false; tick();
    } catch (err) { toast('导入失败：' + err.message, 'err'); }
    e.target.value = '';
  });

  // 批量操作只作用于**当前筛选出来的**模型——列表可能有几百个，
  // 无差别「全选」会把不想消耗的也一起带上
  $('btnSelAll').addEventListener('click', () => {
    visibleModels().forEach(m => { if (sel[m]) sel[m].on = true; });
    dirty = true; renderModels(true); updateEstimate();
  });
  $('btnSelNone').addEventListener('click', () => {
    visibleModels().forEach(m => { if (sel[m]) sel[m].on = false; });
    dirty = true; renderModels(true); updateEstimate();
  });
  $('btnSelClear').addEventListener('click', () => {
    Object.values(sel).forEach(v => v.on = false);
    dirty = true; renderModels(true); updateEstimate();
  });

  // 手动加模型：/models 不一定返回全部（有些网关只列部分），
  // 想消耗哪个就填哪个，不受列表限制
  const addModel = () => {
    const id = $('newModel').value.trim();
    if (!id) return;
    const existed = !!sel[id];
    if (!existed) sel[id] = { on: true, weight: 1, ppk: 0 };
    else sel[id].on = true;
    $('newModel').value = '';
    dirty = true; modelSig = ''; renderModels(true); updateEstimate();
    toast(existed ? `「${id}」已在列表里，已勾选` : `已加入并勾选「${id}」`);
  };
  $('btnAddModel').addEventListener('click', addModel);
  $('newModel').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); addModel(); }
  });

  $('btnCalib').addEventListener('click', async (e) => {
    e.stopPropagation();
    try {
      const r = await api('/api/calibrate/auto', { profile_id: S.active_profile, limit_5h: +$('limit5h').value });
      $('ppk').value = r.points_per_1k.toFixed(4);
      dirty = true; updateEstimate();
      toast(`按 5h 窗口反推：窗口内 ${num(r.tokens)} tokens ÷ 上限 ${num(r.limit_5h)} 积分\n` +
        `→ ${r.points_per_1k.toFixed(4)} 积分/1K tokens\n` +
        `（前提：该窗口额度确实被打满过，否则这只是上界）`, 'warn', 11000);
    } catch (e2) { toast(e2.message, 'err'); }
  });

  $('btnRecalc').addEventListener('click', async (e) => {
    e.stopPropagation();
    const coef = +$('ppk').value || 0;
    if (coef <= 0) return toast('请先填换算系数', 'err');
    if (!confirm(`把全部历史记录按 ${coef} 积分/1K tokens 重新折算？\n\n`
      + '这是给「系数之前填错了」用的。正常调参不需要点这个——'
      + '窗口积分本来就是逐请求落库的，不会随系数自动变。')) return;
    try {
      const r = await api('/api/points/recalc',
        { profile_id: S.active_profile, points_per_1k: coef });
      toast(`已按 ${coef} 重算 ${num(r.rows)} 条历史记录的积分`, 'ok');
      await tick();
    } catch (e2) { toast(e2.message, 'err'); }
  });

  $('btnStart').addEventListener('click', async () => {
    const spec = readSpec();
    if (!spec.models.length) return toast('请至少勾选一个模型', 'err');
    if (!$('baseUrl').value.trim()) return toast('请先填写 Base URL', 'err');
    if ($('keySource').value === 'inline' && !$('keyValue').value.trim())
      return toast('请填写 API Key', 'err');
    try {
      await saveProfile();
      dirty = false;
      const r = await api('/api/run/start', { spec, profile_id: S.active_profile });
      toast(`已启动新会话 · ${S.active_profile_name} · ${r.sid}`);
      tick();
    } catch (e) { toast('启动失败：' + e.message, 'err'); }
  });

  $('btnStop').addEventListener('click', async () => {
    $('btnStop').disabled = true;
    try { await api('/api/run/stop', { reason: '手动停止' }); toast('已发送停止指令'); }
    catch (e) { toast(e.message, 'err'); }
  });

  $('btnBatchStart').addEventListener('click', async () => {
    if (batchBusy) return;
    const ids = Object.keys(batchPick);
    if (!ids.length) return toast('先勾选至少一个配置', 'err');
    batchBusy = true;
    const res = $('batchResult');
    res.className = 'hint';
    res.textContent = '正在启动…';
    try {
      // 先存当前配置，保证表单里刚改的策略参数被带上
      await saveProfile();
      dirty = false;
      const r = await api('/api/run/start_batch', {
        profile_ids: ids, spec: readSpec(),
      });
      const ok = r.started.length, bad = r.failed.length;
      const detail = r.failed.map(f => `· ${f.name}：${f.error}`).join('\n');
      // 结果同时写进页面，不只弹 toast——toast 会消失，漏看就以为只跑了一个
      if (ok && !bad) {
        res.className = 'hint ok';
        res.textContent = `已启动 ${ok} 个会话：${r.started.map(s => s.name).join('、')}`;
      } else if (ok) {
        res.className = 'hint warn';
        res.textContent = `已启动 ${ok} 个，${bad} 个没起来：\n${detail}`;
      } else {
        res.className = 'hint err';
        res.textContent = `全部没起来：\n${detail}`;
      }
      tick();
    } catch (e) {
      res.className = 'hint err';
      res.textContent = '启动失败：' + e.message;
    } finally { batchBusy = false; }
  });

  $('btnBatchClear').addEventListener('click', () => {
    batchPick = {};
    $('batchList').dataset.sig = '';
    $('batchResult').textContent = '';
    renderBatchList((S && S.config && S.config.profiles) || []);
  });

  $('btnExpReq').addEventListener('click', () => {
    const q = (S && S.live && S.live.run_id) ? `?run=${S.live.run_id}` : '';
    location.href = '/api/export/requests.csv' + q;
  });
  $('btnExpRun').addEventListener('click', () => { location.href = '/api/export/runs.csv'; });

  window.addEventListener('resize', () => { if (S) renderLive(S.live); });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && S && S.live.running) $('btnStop').click();
  });
}

async function saveProfile() {
  stashKeyInput();
  const src = $('keySource').value;
  const auth = { source: src, style: $('authStyle').value };
  if (src === 'inline') auth.value = keyCache.inline;
  else auth.ref = keyCache[src];
  const spec = readSpec();
  await api('/api/profile/save', { profile: {
    id: S.active_profile,
    name: $('profName').value,
    protocol: $('protocol').value,
    base_url: $('baseUrl').value.trim(),
    auth,
    models: Object.keys(sel),
    selected: spec.models,
    limits: { window_5h: spec.limit_5h, week: spec.limit_week, unit: 'points' },
    pools: readPools(),
    points_per_1k: spec.points_per_1k,
  } });
  await api('/api/config/engine', { defaults: {
    mode: spec.mode, concurrency: spec.concurrency, input_chars: spec.input_chars,
    max_tokens: spec.max_tokens, reasoning_effort: spec.reasoning_effort,
    timeout: spec.timeout, retry: spec.retry, safety_ratio: spec.safety_ratio,
    instant_window: spec.instant_window,
    cache_bust: spec.cache_bust, max_points: spec.max_points,
    max_total_tokens: spec.max_total_tokens, max_requests: spec.max_requests,
    duration_min: spec.duration_min, enforce_windows: spec.enforce_windows,
    points_per_1k: spec.points_per_1k, loop: spec.loop,
  } });
}

/* ==================== 启动 ==================== */
bind();
tick();

// 自适应轮询：标签页切到后台就降到 5 秒一次，回到前台立刻补一帧。
// 否则挂在后台的页面会一直每秒全量拉取 + 渲染，纯属白烧 CPU。
let pollTimer = null;
function scheduleTick() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(async () => {
    if (!document.hidden) await tick();
    scheduleTick();
  }, document.hidden ? 5000 : 1000);
}
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) tick();
  scheduleTick();
});
scheduleTick();
