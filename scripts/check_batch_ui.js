/* 用 jsdom 跑真实的前端代码，验证「勾选两个配置」到底发出了什么。
   这是唯一能确定前端逻辑对错的办法——不靠猜、不靠浏览器。

   NODE_PATH=<node workspace>/node_modules node scripts/check_batch_ui.js
*/
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const ROOT = path.join(__dirname, '..');
const html = fs.readFileSync(path.join(ROOT, 'tokenfurnace/web/index.html'), 'utf8');
const appJs = fs.readFileSync(path.join(ROOT, 'tokenfurnace/web/app.js'), 'utf8');

/* ---------------- 假后端 ---------------- */
const PROFILES = [
  { id: 'pa', name: '配置A', protocol: 'openai-chat', base_url: 'https://a.example.com/v1',
    auth: { source: 'inline', style: 'bearer', has_key: true, masked: 'sk-a***' },
    models: ['m1', 'm2'], selected: [{ id: 'm1', weight: 1, points_per_1k: 0 }],
    limits: { window_5h: 60000, week: 600000, unit: 'points' }, points_per_1k: 1.4, headers: {} },
  { id: 'pb', name: '配置B', protocol: 'anthropic', base_url: 'https://b.example.com',
    auth: { source: 'inline', style: 'x-api-key', has_key: true, masked: 'sk-b***' },
    models: ['m3'], selected: [{ id: 'm3', weight: 1, points_per_1k: 0 }],
    limits: { window_5h: 60000, week: 600000, unit: 'points' }, points_per_1k: 1.4, headers: {} },
  // 用户真实遇到的情况：新建的配置既没密钥也没选模型，根本起不来
  { id: 'pc', name: '新配置', protocol: 'openai-chat', base_url: '',
    auth: { source: 'env', style: 'bearer', has_key: false, masked: '' },
    models: [], selected: [],
    limits: { window_5h: 0, week: 0, unit: 'tokens' }, points_per_1k: 0, headers: {} },
];
const STATE = {
  version: '0.1.0', server_time: Date.now() / 1000, uptime: 1,
  active_profile: 'pa', active_profile_name: '配置A', active_has_key: true,
  config: {
    version: 2, active_profile: 'pa', ui: { poll_ms: 1000 }, profiles: PROFILES,
    engine_defaults: {
      mode: 'prefill', concurrency: 4, input_chars: 380000, max_tokens: 8,
      reasoning_effort: 'none', cache_bust: true, timeout: 180, retry: 5,
      instant_window: 20, safety_ratio: 0.97, max_points: 0, max_total_tokens: 0,
      max_requests: 0, duration_min: 0, enforce_windows: false, points_per_1k: 1.4,
      loop: false },
  },
  protocols: {
    'openai-chat': { label: 'OpenAI Chat', short: 'Chat', auth: 'bearer', hint: '' },
    anthropic: { label: 'Anthropic', short: 'Anthropic', auth: 'x-api-key', hint: '' },
  },
  auth_styles: {
    bearer: { label: 'Authorization: Bearer', hint: '' },
    'x-api-key': { label: 'x-api-key', hint: '' },
  },
  modes: { prefill: '预填充', decode: '生成', mixed: '混合' },
  windows: { h5: 18000, week: 604800 },
  live: {
    status: 'idle', running: false, sessions: [], session_count: 0,
    requests: 0, ok: 0, failed: 0, prompt_tokens: 0, completion_tokens: 0,
    total_tokens: 0, points: 0, errors: {}, by_model: {}, per_minute: [], spark: [],
    series: [], rate: 0, rate_instant: 0, rate_peak: 0, rps: 0, elapsed: 0,
    wait_until: 0, wait_reason: '', stop_reason: '', last_error: '', instant_window: 20,
    workers_in_use: 0, max_total_workers: 32,
  },
  logs: [], history: [], totals: { tokens: 0, requests: 0 }, presets: {},
};

const calls = [];
const dom = new JSDOM(html, {
  url: 'http://127.0.0.1:8760/',
  runScripts: 'outside-only',
  pretendToBeVisual: true,
});
const win = dom.window;
const doc = win.document;

// jsdom 没有 canvas，app.js 会调 getContext，这里补个空实现
const noop = () => ({ addColorStop() {} });
win.HTMLCanvasElement.prototype.getContext = () => new Proxy({}, {
  get: (t, k) => (k === 'canvas' ? {} : (k === 'createLinearGradient' ? () => ({ addColorStop() {} }) : () => {})),
  set: () => true,
});
win.confirm = () => true;
win.alert = () => {};
win.fetch = async (url, opt) => {
  const p = String(url).split('?')[0];
  let body = {};
  try { body = opt && opt.body ? JSON.parse(opt.body) : {}; } catch (e) { /* ignore */ }
  calls.push({ path: p, body });
  const reply = (o) => ({ ok: true, status: 200, json: async () => o });
  if (p === '/api/state') return reply(STATE);
  if (p === '/api/run/start_batch') return reply({ ok: true, started: [], failed: [] });
  return reply({ ok: true });
};

/* ---------------- 跑真实 app.js ---------------- */
win.eval(appJs);

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

(async () => {
  await sleep(150);   // 等首帧 tick

  const inputs = doc.querySelectorAll('#batchList input[data-pid]');
  console.log('① batchList 渲染出的配置开关数:', inputs.length);
  console.log('   batchBadge 初始值:', doc.getElementById('batchBadge').textContent);
  if (inputs.length !== 3) { console.log('❌ 期望 3 个开关'); process.exit(1); }

  // 起不来的配置必须被禁用，并写明原因
  const blocked = [...inputs].filter(i => i.disabled);
  console.log('   被禁用的开关:', blocked.map(i => i.dataset.pid).join(',') || '(无)');
  const reasons = [...doc.querySelectorAll('.block-reason')].map(e => e.textContent.trim());
  console.log('   禁用原因:', reasons.join(' | '));
  if (blocked.length !== 1 || blocked[0].dataset.pid !== 'pc') {
    console.log('❌ 没密钥/没模型的配置应该被禁用');
    process.exit(1);
  }
  if (!reasons.length) { console.log('❌ 缺少禁用原因说明'); process.exit(1); }

  // 试着点被禁用的那个：不该被选中
  blocked[0].closest('label').dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
  console.log(`   点了被禁用的「${blocked[0].dataset.pid}」-> checked=${blocked[0].checked}`);
  if (blocked[0].checked) { console.log('❌ 被禁用的配置不该能勾上'); process.exit(1); }

  // 勾选两个能跑的
  for (const inp of inputs) {
    if (inp.disabled) continue;
    inp.closest('label').dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
    console.log(`   点了「${inp.dataset.pid}」的 label -> checked=${inp.checked}`);
  }
  console.log('   batchBadge 勾选后:', doc.getElementById('batchBadge').textContent);

  // 点击「启动选中的配置」
  calls.length = 0;
  doc.getElementById('btnBatchStart')
     .dispatchEvent(new win.MouseEvent('click', { bubbles: true }));
  await sleep(200);

  const batch = calls.filter(c => c.path === '/api/run/start_batch');
  console.log('\n② 发出的 start_batch 次数:', batch.length);
  if (batch.length) {
    console.log('   profile_ids =', JSON.stringify(batch[0].body.profile_ids));
  }
  const ids = batch.length ? (batch[0].body.profile_ids || []) : [];
  const ok = ids.length === 2 && ids.includes('pa') && ids.includes('pb');
  console.log('\n' + (ok
    ? '✅ 只把两个能跑的配置发出去了，起不来的被挡住'
    : `❌ 发出的配置不对：${JSON.stringify(ids)}`));
  process.exit(ok ? 0 : 1);
})().catch(e => { console.error('异常:', e); process.exit(2); });
