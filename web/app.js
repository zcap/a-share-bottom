'use strict';
/* A股 · 距历史最低 —— 读取 data/latest.json，在浏览器里按所选口径计算比值；
   可选从腾讯行情读取实时价（请求由你的浏览器直接发出）。 */

const $ = (s) => document.querySelector(s);
const PAGE = 100;
const S = {
  data: null, rows: [], view: [], shown: PAGE,
  sortKey: 'ratio', sortDir: 1,
  board: '', q: '', basis: 'low', useIssue: true, noST: false, noSusp: false, maxRatio: null,
  rt: {}, rtTime: '', rtBusy: false, rtStale: false, rtTimer: null,
};

// ---------- 工具 ----------
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const fmtP = (x) => (x > 0 && isFinite(x) ? x.toFixed(x < 1 ? 3 : 2) : '—');
function bjNow() {
  const d = new Date(Date.now() + 8 * 3600 * 1000); // 用 UTC 字段读出北京时间
  return { day: d.getUTCDay(), hm: d.toISOString().slice(11, 16) };
}
function inTradingHours() {
  const { day, hm } = bjNow();
  return day >= 1 && day <= 5 && hm >= '09:25' && hm <= '15:05';
}
function store(k, v) { try { localStorage.setItem('bottom.' + k, JSON.stringify(v)); } catch (e) { /* 忽略 */ } }
function recall(k, dflt) {
  try { const v = localStorage.getItem('bottom.' + k); return v === null ? dflt : JSON.parse(v); } catch (e) { return dflt; }
}

// ---------- 计算 ----------
function calc(r) {
  const q = S.rtStale ? null : S.rt[r.sym];
  let adj = r.adj, cur = r.hfq, price = r.close, live = false, susp = r.last_trade < S.data.market_date;
  let baseL = r.min_low, dateL = r.min_low_date, baseC = r.min_close, dateC = r.min_close_date;
  if (q && q.date > r.last_trade) {
    if (q.vol > 0 && q.price > 0) {
      // 今天是除权除息日：前收盘价（参考价）和昨收换算后对不上，按后复权价连续重算因子
      const expected = r.hfq / r.adj;
      if (q.pc > 0 && Math.abs(q.pc - expected) >= 0.005) adj = r.hfq / q.pc;
      cur = q.price * adj; price = q.price; live = true; susp = false;
      const todayLow = q.low > 0 ? q.low * adj : cur;
      if (todayLow < baseL) { baseL = todayLow; dateL = '今天'; }
      if (cur < baseC) { baseC = cur; dateC = '今天'; }
    } else {
      susp = true;
    }
  }
  let base = S.basis === 'low' ? baseL : baseC;
  let date = S.basis === 'low' ? dateL : dateC;
  if (S.useIssue && r.issue_price > 0 && r.issue_price < base) { base = r.issue_price; date = '发行价'; }
  r.price = price; r.live = live; r.susp = susp;
  r.ratio = cur / base; r.pct = (r.ratio - 1) * 100;
  r.lowNow = base / adj; r.lowDate = date; r.ip = r.issue_price;
}

function cmp(a, b) {
  const k = S.sortKey, x = a[k], y = b[k];
  const nx = x === null || x === undefined || x === '', ny = y === null || y === undefined || y === '';
  if (nx || ny) return nx === ny ? 0 : nx ? 1 : -1; // 空值排最后
  if (typeof x === 'number') return (x - y) * S.sortDir;
  return String(x).localeCompare(String(y), 'zh-CN') * S.sortDir;
}

function refresh() {
  for (const r of S.rows) calc(r);
  const q = S.q.trim().toLowerCase();
  const pre = S.rows.filter((r) =>
    (!S.board || r.board === S.board) &&
    (!q || r.code.startsWith(q) || r.name.toLowerCase().includes(q)) &&
    (!S.noST || !r.st) &&
    (!S.noSusp || !r.susp));
  renderBuckets(pre);
  S.view = (S.maxRatio ? pre.filter((r) => r.ratio <= S.maxRatio) : pre).sort(cmp);
  S.shown = PAGE;
  renderTable();
}

// ---------- 渲染 ----------
function renderBuckets(rows) {
  const th = [1.05, 1.1, 1.2, 1.5, 2];
  const parts = th.map((t) => {
    const n = rows.filter((r) => r.ratio <= t).length;
    const on = S.maxRatio === t ? ' class="on"' : '';
    return `<button data-t="${t}"${on}>≤${t} <b>${n}</b></button>`;
  });
  $('#buckets').innerHTML = `<span>比值分布（共 <b>${rows.length}</b> 只）：</span>` + parts.join('');
}

function renderTable() {
  const rows = S.view.slice(0, S.shown);
  const html = rows.map((r) => {
    const w = Math.max(2, Math.min(60, (60 * Math.log(r.ratio)) / Math.log(5)));
    return `<tr>
<td><a href="https://gu.qq.com/${r.sym}" target="_blank" rel="noopener">${r.code}</a></td>
<td>${esc(r.name)}${r.susp ? '<span class="tag">停牌</span>' : ''}</td>
<td class="hide-s dim">${r.board}</td>
<td class="num${r.live ? ' live' : ''}">${fmtP(r.price)}</td>
<td class="num hide-s">${fmtP(r.lowNow)}</td>
<td class="hide-s dim">${esc(r.lowDate)}</td>
<td class="num ratio${r.ratio <= 1.1 ? ' hot' : ''}">${r.ratio.toFixed(3)}</td>
<td class="num"><span class="pct"><span class="track"><i style="width:${w.toFixed(0)}px"></i></span><em>${r.pct.toFixed(1)}%</em></span></td>
<td class="num hide-m">${r.ip > 0 ? fmtP(r.ip) + (r.issue_src === 'infer' ? '<span class="hint">†</span>' : '') : '<span class="dim">—</span>'}</td>
<td class="hide-m dim">${r.list_date || ''}</td>
</tr>`;
  }).join('');
  $('#tb').innerHTML = html || '<tr><td colspan="10" class="dim">没有符合条件的股票</td></tr>';
  const btn = $('#moreBtn');
  btn.hidden = S.view.length <= S.shown;
  btn.textContent = `显示更多（还有 ${S.view.length - S.shown} 只）`;
  document.querySelectorAll('th[data-k]').forEach((th) => {
    th.classList.toggle('sorted', th.dataset.k === S.sortKey);
    th.classList.toggle('asc', th.dataset.k === S.sortKey && S.sortDir > 0);
    th.classList.toggle('desc', th.dataset.k === S.sortKey && S.sortDir < 0);
  });
}

// ---------- 实时行情（腾讯） ----------
function loadScript(syms) {
  return new Promise((resolve) => {
    const s = document.createElement('script');
    s.charset = 'gbk';
    s.src = 'https://qt.gtimg.cn/q=' + syms.join(',');
    let done = false;
    const fin = () => { if (!done) { done = true; s.remove(); resolve(); } };
    s.onload = fin; s.onerror = fin;
    setTimeout(fin, 15000);
    document.head.appendChild(s);
  });
}

function parseQuote(v) {
  const f = String(v).split('~');
  if (f.length < 35) return null;
  const ts = f[30] || '';
  return {
    price: +f[3], pc: +f[4], vol: +f[6], high: +f[33], low: +f[34],
    date: ts.length >= 8 ? `${ts.slice(0, 4)}-${ts.slice(4, 6)}-${ts.slice(6, 8)}` : '',
    time: ts.length >= 14 ? `${ts.slice(8, 10)}:${ts.slice(10, 12)}:${ts.slice(12, 14)}` : '',
  };
}

async function fetchRealtime() {
  if (S.rtBusy || !S.rows.length) return;
  S.rtBusy = true;
  const btn = $('#rtBtn'), st = $('#rtStatus');
  btn.disabled = true;
  const syms = S.rows.map((r) => r.sym);
  const batches = [];
  for (let i = 0; i < syms.length; i += 60) batches.push(syms.slice(i, i + 60));
  const res = {};
  let done = 0, latest = '';
  st.textContent = '正在获取实时价…';
  const worker = async () => {
    while (batches.length) {
      const b = batches.shift();
      await loadScript(b);
      for (const sym of b) {
        const key = 'v_' + sym;
        if (window[key] !== undefined) {
          const q = parseQuote(window[key]);
          if (q && q.date) { res[sym] = q; if (q.date + q.time > latest) latest = q.date + ' ' + q.time; }
          try { delete window[key]; } catch (e) { window[key] = undefined; }
        }
      }
      done += b.length;
      st.textContent = `正在获取实时价… ${Math.round((100 * done) / syms.length)}%`;
    }
  };
  await Promise.all(Array.from({ length: 6 }, worker));

  // 保护：收盘数据如果落后不止一天，昨收会和数据对不上，这时不使用实时价
  let n = 0, mism = 0;
  for (const r of S.rows) {
    const q = res[r.sym];
    if (q && q.date > r.last_trade && q.vol > 0 && q.pc > 0) {
      n++;
      if (Math.abs(q.pc - r.hfq / r.adj) >= 0.005) mism++;
    }
  }
  S.rtStale = n >= 50 && mism > 0.3 * n;
  S.rt = res;
  const cnt = Object.keys(res).length;
  if (!cnt) st.textContent = '实时价获取失败（腾讯行情无法访问）';
  else if (S.rtStale) st.textContent = '收盘数据落后一天以上，暂不使用实时价';
  else st.textContent = `实时价 ${latest.slice(5)}（腾讯行情，${cnt} 只）`;
  S.rtBusy = false;
  btn.disabled = false;
  refresh();
}

function setAuto(on) {
  clearInterval(S.rtTimer);
  S.rtTimer = null;
  if (on) {
    S.rtTimer = setInterval(() => {
      if (document.visibilityState === 'visible' && inTradingHours()) fetchRealtime();
    }, 60000);
  }
}

// ---------- 初始化 ----------
async function load() {
  let data;
  try {
    const resp = await fetch('data/latest.json?t=' + Date.now(), { cache: 'no-store' });
    data = await resp.json();
  } catch (e) {
    $('#status').textContent = '数据加载失败：可能还没有完成第一次数据构建，请稍后刷新。';
    return;
  }
  S.data = data;
  const F = data.fields;
  S.rows = data.rows.map((a) => {
    const r = {};
    F.forEach((k, i) => { r[k] = a[i]; });
    r.sym = r.mkt + r.code;
    r.st = !!r.st;
    return r;
  });
  $('#status').textContent =
    `收盘数据 ${data.market_date} · ${S.rows.length} 只 · 更新于 ${data.updated_at}（北京时间）`;
  if (data.build_pending) {
    const b = $('#banner');
    b.hidden = false;
    b.textContent = `数据首次构建尚未完成（已完成 ${S.rows.length} / ${data.total} 只），后续运行会自动补齐。`;
  }
  refresh();
  if (inTradingHours()) fetchRealtime();
}

function bind() {
  S.basis = recall('basis', 'low');
  S.useIssue = recall('useIssue', true);
  S.noST = recall('noST', false);
  S.noSusp = recall('noSusp', false);
  $('#basis').value = S.basis;
  $('#useIssue').checked = S.useIssue;
  $('#noST').checked = S.noST;
  $('#noSusp').checked = S.noSusp;
  $('#rtAuto').checked = recall('rtAuto', false);
  setAuto($('#rtAuto').checked);

  let t;
  $('#q').addEventListener('input', (e) => { clearTimeout(t); t = setTimeout(() => { S.q = e.target.value; refresh(); }, 150); });
  $('#boards').addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    S.board = b.dataset.board;
    document.querySelectorAll('#boards button').forEach((x) => x.classList.toggle('on', x === b));
    refresh();
  });
  for (const id of ['basis', 'useIssue', 'noST', 'noSusp']) {
    $('#' + id).addEventListener('change', (e) => {
      S[id] = e.target.type === 'checkbox' ? e.target.checked : e.target.value;
      store(id, S[id]);
      refresh();
    });
  }
  $('#maxRatio').addEventListener('input', (e) => {
    const v = parseFloat(e.target.value);
    S.maxRatio = v > 0 ? v : null;
    refresh();
  });
  $('#buckets').addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    const t = parseFloat(b.dataset.t);
    S.maxRatio = S.maxRatio === t ? null : t;
    $('#maxRatio').value = S.maxRatio ?? '';
    refresh();
  });
  $('#rtBtn').addEventListener('click', fetchRealtime);
  $('#rtAuto').addEventListener('change', (e) => { store('rtAuto', e.target.checked); setAuto(e.target.checked); });
  $('#moreBtn').addEventListener('click', () => { S.shown += PAGE * 2; renderTable(); });
  document.querySelector('thead').addEventListener('click', (e) => {
    const th = e.target.closest('th[data-k]');
    if (!th) return;
    const k = th.dataset.k;
    if (S.sortKey === k) S.sortDir = -S.sortDir;
    else { S.sortKey = k; S.sortDir = 1; }
    S.view.sort(cmp);
    renderTable();
  });
}

bind();
load();
