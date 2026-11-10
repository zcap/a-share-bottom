'use strict';
/* 股票 · 距历史最低 —— 读取 data/*.json，在浏览器里按所选口径计算比值；
   可选从腾讯行情读取实时价（请求由你的浏览器直接发出）。 */

const $ = (s) => document.querySelector(s);
const PAGE = 100;
const MK = {
  a: {
    label: 'A股', file: 'data/latest.json', csv: 'data/latest.csv', cur: '元', tz: 'Asia/Shanghai', open: '09:25', close: '15:05',
    boards: ['沪主板', '深主板', '创业板', '科创板', '北交所'],
    link: (r) => `https://gu.qq.com/${r.sym}`,
  },
  hk: {
    label: '港股', file: 'data/hk.json', csv: 'data/hk.csv', cur: '港元', tz: 'Asia/Hong_Kong', open: '09:20', close: '16:15',
    boards: ['主板', '创业板'],
    link: (r) => `https://xueqiu.com/S/${r.code}`,
  },
  us: {
    label: '美股', file: 'data/us.json', csv: 'data/us.csv', cur: '美元', tz: 'America/New_York', open: '09:25', close: '16:05',
    boards: ['纳斯达克', '纽交所', 'NYSE American', '其他'],
    link: (r) => `https://xueqiu.com/S/${r.code.replace('.', '-')}`,
  },
};
const S = {
  m: 'a', cache: {}, data: null, rows: [], view: [], shown: PAGE,
  sortKey: 'ratio', sortDir: 1,
  board: '', q: '', basis: 'low', useIssue: true, noST: false, noSusp: false, noDelist: false, maxRatio: null, minScore: null, scoreKey: 'tscore', minCap: 0,
  rt: {}, rtTime: '', rtBusy: false, rtStale: false, rtTimer: null,
  narrow: false, // 窄屏（手机）：代码并到名称下面，列的顺序把最常看的放前面
};
const intl = () => S.m !== 'a';

// ---------- 工具 ----------
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const fmtP = (x) => (x > 0 && isFinite(x) ? x.toFixed(x >= 1 ? 2 : x >= 0.1 ? 3 : 4) : '—');
// 现价：港股有 0.005 的价位（如 1.325），需要时显示 3 位小数
const fmtPx = (x) => (x >= 1 && isFinite(x) && Math.abs(x * 100 - Math.round(x * 100)) > 1e-6 ? x.toFixed(3) : fmtP(x));
const isNum = (x) => typeof x === 'number' && isFinite(x);
const fmtPE = (x) => (!isNum(x) ? '<span class="dim">—</span>' : x < 0 ? '<span class="dim">亏损</span>' : x.toFixed(1));
const fmtPB = (x) => (!isNum(x) ? '<span class="dim">—</span>' : x < 0 ? '<span class="dim">资不抵债</span>' : x.toFixed(2));
function fmtCap(x) { // 总市值：亿 / 万亿
  if (!isNum(x) || x <= 0) return '<span class="dim">—</span>';
  if (x >= 1e12) return (x / 1e12).toFixed(2) + '万亿';
  if (x >= 1e8) return (x / 1e8).toFixed(x >= 1e10 ? 0 : 1) + '亿';
  return (x / 1e4).toFixed(0) + '万';
}
function scoreCell(r, k, tip) {
  const v = r[k];
  return `<td class="num score${isNum(v) && v >= 80 ? ' good' : ''}" title="${esc(tip)}">${isNum(v) ? Math.round(v) : '<span class="dim">—</span>'}</td>`;
}
const DESC_FIRST = new Set(['tscore', 'vscore', 'qscore', 'gscore']); // 第一次点击就从高到低排
function localNow(tz) {
  // 用 Intl 读出某个时区的星期和时刻
  const p = {};
  for (const x of new Intl.DateTimeFormat('en-US', { timeZone: tz, weekday: 'short', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).formatToParts(new Date())) p[x.type] = x.value;
  return { day: ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'].indexOf(p.weekday), hm: `${p.hour}:${p.minute}` };
}
function inTradingHours() {
  const c = MK[S.m];
  const { day, hm } = localNow(c.tz);
  return day >= 1 && day <= 5 && hm >= c.open && hm <= c.close;
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
    if (q.price > 0 && (q.vol > 0 || r.intl)) {
      const expected = r.hfq / r.adj;
      if (q.pc > 0) {
        if (!r.intl && Math.abs(q.pc - expected) >= 0.005) {
          // A 股除权除息日：前收盘价（参考价）和昨收换算后对不上，按复权价连续重算因子
          adj = r.hfq / q.pc;
        } else if (r.intl && Math.abs(q.pc / expected - 1) > 0.25) {
          // 港美股交易所不调整分红的昨收，只有拆股、合股时昨收才会大幅变化
          adj = r.hfq / q.pc;
        }
      }
      cur = q.price * adj; price = q.price; live = true; susp = false;
      const todayLow = q.low > 0 ? Math.min(q.low, q.price) * adj : cur;
      if (todayLow < baseL) { baseL = todayLow; dateL = '今天'; }
      if (cur < baseC) { baseC = cur; dateC = '今天'; }
    } else {
      susp = true;
    }
  }
  let base = S.basis === 'low' ? baseL : baseC;
  let date = S.basis === 'low' ? dateL : dateC;
  const ic = r.intl ? r.issue_cmp : r.issue_price; // 港美股的发行价要先换算到今天口径
  if (S.useIssue && ic > 0 && ic < base) { base = ic; date = '发行价'; }
  r.price = price; r.live = live; r.susp = susp;
  // 港美股：两周以上没有成交的标“停牌”，否则标“无成交”（港股每天有几百只小盘股没人买卖）
  r.suspLabel = !r.intl || (Date.parse(S.data.market_date) - Date.parse(r.last_trade)) / 864e5 > 14 ? '停牌' : '无成交';
  r.ratio = Math.max(1, cur / base); r.pct = (r.ratio - 1) * 100; // 当前价本身计入了最低，比值不会小于 1
  r.lowNow = base / adj; r.lowDate = date; r.ip = r.issue_price;
}

function cmp(a, b) {
  const k = S.sortKey, x = a[k], y = b[k];
  const nx = x === null || x === undefined || x === '', ny = y === null || y === undefined || y === '';
  if (nx || ny) return nx === ny ? 0 : nx ? 1 : -1; // 空值排最后
  if (typeof x === 'number') return (x - y) * S.sortDir;
  return String(x).localeCompare(String(y), 'zh-CN') * S.sortDir;
}

function boardOk(r) {
  if (!S.board) return true;
  if (S.board === '其他') return !MK[S.m].boards.includes(r.board);
  return r.board === S.board;
}

function refresh() {
  if (!S.data) return;
  for (const r of S.rows) calc(r);
  const q = S.q.trim().toLowerCase();
  const a = !intl();
  const pre = S.rows.filter((r) =>
    boardOk(r) &&
    (!q || r.code.toLowerCase().startsWith(q) || r.name.toLowerCase().includes(q)) &&
    (!a || !S.noST || !r.st) &&
    (!S.noSusp || !r.susp) &&
    (!a || !S.noDelist || !r.delisting));
  renderBuckets(pre);
  S.view = pre.filter((r) =>
    (!S.maxRatio || r.ratio <= S.maxRatio) &&
    (S.minScore === null || (isNum(r[S.scoreKey]) && r[S.scoreKey] >= S.minScore)) &&
    (a || !S.minCap || (isNum(r.mcap) && r.mcap >= S.minCap))).sort(cmp);
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

const CUT_TIP = {
  hk: '历史数据没有覆盖到上市日（Yahoo 的港股历史最早到 2000 年），只计算 2000 年以来的最低价',
  us: '上市很早的老股票，Yahoo 的历史数据可能不是从上市日开始，只计算有数据以来的最低价',
};
const WARN_TIP = '历史中有单日涨跌超过 4 倍的情况，可能是拆股、合股没有复权，比值仅供参考';
const ISSUE_SRC = { em: '东方财富', f10: '东方财富发行资料', infer: '首日前收盘价推断', nasdaq: '纳斯达克 IPO 日历' };

function issueCell(r) {
  if (r.issue_src === 'none') return '<span class="dim" title="换股吸收合并等方式上市，没有发行价">—</span>';
  if (!(r.ip > 0)) return '<span class="dim">—</span>';
  if (!r.intl) return fmtP(r.ip) + (r.issue_src === 'infer' ? '<span class="hint">†</span>' : '');
  if (r.issue_cmp > 0) {
    return `<span title="${esc(ISSUE_SRC[r.issue_src] || '')}；按今天价格口径 ${fmtP(r.issue_cmp)}">${fmtP(r.ip)}</span>`;
  }
  return `<span class="dim" title="没有计入：发行价和历史数据对不上（详见 CSV）">${fmtP(r.ip)}</span>`;
}

// ---------- 表格的列 ----------
// only：只在 A 股（a）或港美股（intl）显示。
const COLS = [
  { k: 'code', label: '代码' },
  { k: 'name', label: '名称' },
  { k: 'board', label: '板块' },
  { k: 'price', label: '现价', num: 1 },
  { k: 'lowNow', label: '历史最低<span class="hint">*</span>', num: 1 },
  { k: 'lowDate', label: '最低日期' },
  { k: 'ratio', label: '比值', num: 1 },
  { k: 'pct', label: '距最低', num: 1 },
  { k: 'tscore', label: '综合', num: 1, tip: '价值 50% + 质量 30% + 成长 20%，0–100' },
  { k: 'vscore', label: '价值', num: 1, tip: '同行业内比较，越高越便宜' },
  { k: 'qscore', label: '质量', num: 1, tip: '同行业内比较：净资产收益率、毛利率、经营现金流/净资产' },
  { k: 'gscore', label: '成长', num: 1, tip: '同行业内比较：营收同比、盈利同比' },
  { k: 'peSort', label: '市盈率', num: 1 },
  { k: 'pbSort', label: '市净率', num: 1 },
  { k: 'mcap', label: '市值', num: 1, only: 'intl', tip: '按最新收盘价换算，单位为当地货币' },
  { k: 'ip', label: '发行价', num: 1 },
  { k: 'list_date', label: '上市日期' },
];
// 手机上的顺序：名称（冻结，代码在名称下面）后面先放最常看的现价、比值、分数，其余往右滑
const NARROW_ORDER = ['name', 'price', 'ratio', 'tscore', 'vscore', 'qscore', 'gscore', 'pct', 'lowNow', 'lowDate',
  'peSort', 'pbSort', 'mcap', 'board', 'ip', 'list_date'];

function activeCols() {
  const ok = (c) => !c.only || (c.only === 'intl') === intl();
  if (!S.narrow) return COLS.filter(ok);
  return NARROW_ORDER.map((k) => COLS.find((c) => c.k === k)).filter(ok);
}

function tagsHtml(r) {
  return (r.susp ? `<span class="tag" title="最后成交日 ${esc(r.last_trade)}">${r.suspLabel}</span>` : '') +
    (r.warn ? `<span class="tag warn" title="${WARN_TIP}">⚠</span>` : '');
}

const CELL = {
  code: (r) => `<td class="c-code stick1"><a href="${MK[S.m].link(r)}" target="_blank" rel="noopener">${esc(r.code)}</a></td>`,
  name: (r) => {
    const tip = esc(r.name + (r.industry ? '\n' + r.industry : ''));
    if (!S.narrow) return `<td class="c-name stick2 edge" title="${tip}"><span class="nm">${esc(r.name)}</span>${tagsHtml(r)}</td>`;
    return `<td class="c-name stick1 edge" title="${tip}"><span class="nm">${esc(r.name)}</span>` +
      `<span class="sub"><a href="${MK[S.m].link(r)}" target="_blank" rel="noopener">${esc(r.code)}</a>${tagsHtml(r)}</span></td>`;
  },
  board: (r) => `<td class="dim">${esc(r.board)}</td>`,
  price: (r) => `<td class="num${r.live ? ' live' : ''}">${fmtPx(r.price)}</td>`,
  lowNow: (r) => `<td class="num">${fmtP(r.lowNow)}</td>`,
  lowDate: (r) => `<td class="dim">${esc(r.lowDate)}${r.hist_cut ? `<span class="hint" title="${CUT_TIP[S.m] || ''}"> ※</span>` : ''}</td>`,
  ratio: (r) => `<td class="num ratio${r.ratio <= 1.1 ? ' hot' : ''}">${r.ratio.toFixed(3)}</td>`,
  pct: (r) => {
    const w = Math.max(2, Math.min(60, (60 * Math.log(r.ratio)) / Math.log(5)));
    return `<td class="num"><span class="pct"><span class="track"><i style="width:${w.toFixed(0)}px"></i></span><em>${r.pct.toFixed(1)}%</em></span></td>`;
  },
  tscore: (r) => scoreCell(r, 'tscore', r.totTip),
  vscore: (r) => scoreCell(r, 'vscore', r.valTip),
  qscore: (r) => scoreCell(r, 'qscore', r.qualTip),
  gscore: (r) => scoreCell(r, 'gscore', r.growTip),
  peSort: (r) => `<td class="num">${fmtPE(r.pe)}</td>`,
  pbSort: (r) => `<td class="num">${fmtPB(r.pb)}</td>`,
  mcap: (r) => `<td class="num">${fmtCap(r.mcap)}</td>`,
  ip: (r) => `<td class="num">${issueCell(r)}</td>`,
  list_date: (r) => `<td class="dim">${r.list_date || '—'}</td>`,
};

function renderHead() {
  const stick = S.narrow ? { name: 'stick1 edge' } : { code: 'stick1', name: 'stick2 edge' };
  $('#hd').innerHTML = activeCols().map((c) => {
    const cls = [c.num ? 'num' : '', c.k === 'code' ? 'c-code' : '', c.k === 'name' ? 'c-name' : '', stick[c.k] || ''].filter(Boolean).join(' ');
    const label = S.narrow && c.k === 'name' ? '名称 / 代码' : c.label;
    return `<th data-k="${c.k}"${cls ? ` class="${cls}"` : ''}${c.tip ? ` title="${c.tip}"` : ''}>${label}</th>`;
  }).join('');
}

function renderTable() {
  const cols = activeCols();
  if ($('#hd').children.length !== cols.length || ($('#hd').firstElementChild || {}).dataset?.k !== cols[0].k) renderHead();
  const rows = S.view.slice(0, S.shown);
  const html = rows.map((r) => '<tr>' + cols.map((c) => CELL[c.k](r)).join('') + '</tr>').join('');
  $('#tb').innerHTML = html || `<tr><td colspan="${cols.length}" class="dim">${S.rows.length ? '没有符合条件的股票' : '暂无数据'}</td></tr>`;
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
  const ts = (f[30] || '').replace(/\D/g, ''); // A 股 20261109150003；港股 2026/11/09 16:08:09；美股 2026-11-06 16:03:41
  return {
    price: +f[3], pc: +f[4], vol: +f[6], high: +f[33], low: +f[34],
    date: ts.length >= 8 ? `${ts.slice(0, 4)}-${ts.slice(4, 6)}-${ts.slice(6, 8)}` : '',
    time: ts.length >= 14 ? `${ts.slice(8, 10)}:${ts.slice(10, 12)}:${ts.slice(12, 14)}` : '',
  };
}

function takeQuote(sym) {
  // 带点的美股代码（如 usBRK.B），返回的变量名里点可能是下划线
  for (const key of ['v_' + sym, 'v_' + sym.replace(/\./g, '_')]) {
    if (window[key] !== undefined) {
      const q = parseQuote(window[key]);
      try { delete window[key]; } catch (e) { window[key] = undefined; }
      return q;
    }
  }
  return null;
}

async function fetchRealtime() {
  if (S.rtBusy || !S.rows.length) return;
  const m = S.m;
  S.rtBusy = true;
  const btn = $('#rtBtn'), st = $('#rtStatus');
  btn.disabled = true;
  const syms = S.rows.map((r) => r.sym);
  // 带点的代码单独请求：万一返回的变量名不合法，不会连累同一批的其他股票
  const plain = syms.filter((x) => !x.includes('.')), dotted = syms.filter((x) => x.includes('.'));
  const batches = [];
  for (let i = 0; i < plain.length; i += 60) batches.push(plain.slice(i, i + 60));
  for (const x of dotted) batches.push([x]);
  const res = {};
  let done = 0, latest = '';
  st.textContent = '正在获取实时价…';
  const worker = async () => {
    while (batches.length) {
      const b = batches.shift();
      await loadScript(b);
      for (const sym of b) {
        const q = takeQuote(sym);
        if (q && q.date) { res[sym] = q; if (q.date + q.time > latest) latest = q.date + ' ' + q.time; }
      }
      done += b.length;
      st.textContent = `正在获取实时价… ${Math.round((100 * done) / syms.length)}%`;
    }
  };
  await Promise.all(Array.from({ length: 6 }, worker));
  S.rtBusy = false;
  btn.disabled = false;
  if (m !== S.m) return; // 获取期间切换了市场

  // 保护：收盘数据如果落后不止一天，昨收会和数据对不上，这时不使用实时价
  let n = 0, mism = 0;
  for (const r of S.rows) {
    const q = res[r.sym];
    if (q && q.date > r.last_trade && q.price > 0 && q.pc > 0) {
      n++;
      const exp = r.hfq / r.adj;
      if (r.intl ? Math.abs(q.pc / exp - 1) >= 0.01 : Math.abs(q.pc - exp) >= 0.005) mism++;
    }
  }
  S.rtStale = n >= 50 && mism > 0.3 * n;
  S.rt = res;
  const cnt = Object.keys(res).length;
  if (!cnt) st.textContent = '实时价获取失败（腾讯行情无法访问）';
  else if (S.rtStale) st.textContent = '收盘数据落后一天以上，暂不使用实时价';
  else st.textContent = `实时价 ${latest.slice(5)}（腾讯行情，${cnt} 只${intl() ? '，可能有延迟' : ''}）`;
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

// ---------- 数据 ----------
function prepare(data, m) {
  const F = data.fields;
  return data.rows.map((a) => {
    const r = {};
    F.forEach((k, i) => { r[k] = a[i]; });
    r.sym = r.mkt + r.code;
    r.intl = m !== 'a';
    r.st = !!r.st;
    r.delisting = !r.intl && /退$|^退市/.test(r.name); // 退市整理期的股票名称以“退”结尾
    // 市盈率、市净率排序：亏损/资不抵债排在最后
    r.peSort = isNum(r.pe) ? (r.pe > 0 ? r.pe : 1e12) : null;
    r.pbSort = isNum(r.pb) ? (r.pb > 0 ? r.pb : 1e12) : null;
    const part = (label, v, d, u = '') => `${label} ${isNum(v) ? v.toFixed(d) + u : '—'}`;
    if (r.intl) {
      const ind = r.industry ? `行业：${r.sector ? r.sector + ' / ' : ''}${r.industry}（同行业不足 8 只时与同大类比较，再不够与全市场比较）`
        : '无行业信息，与全市场比较';
      const noFin = r.val_date ? null : '估值与财务数据还没取到（每次运行取一部分，一周内取完）';
      r.valTip = isNum(r.vscore)
        ? `${part('市盈率TTM', r.pe, 1)} · ${part('市净率', r.pb, 2)} · ${part('市销率TTM', r.ps, 2)} · ${part('股息率', r.dy, 2, '%')}（${r.val_date}）\n${ind}`
        : (noFin || '估值数据不足，暂无价值分');
      r.qualTip = isNum(r.qscore)
        ? `${part('净资产收益率', r.roe, 1, '%')} · ${part('毛利率', r.gpm, 1, '%')} · ${part('经营现金流/净资产', r.cfroe, 1, '%')}（最近 12 个月）\n${ind}`
        : (noFin || '没有可比的财务数据，暂无质量分');
      r.growTip = isNum(r.gscore)
        ? `${part('营收同比', r.rev_yoy, 1, '%')} · ${part('盈利同比', r.np_yoy, 1, '%')}（最近一个季度和去年同期比）\n${ind}`
        : (noFin || '没有可比的财务数据，暂无成长分');
      r.totTip = isNum(r.tscore)
        ? `价值 ${Math.round(r.vscore)} × 50% + 质量 ${Math.round(r.qscore)} × 30% + 成长 ${Math.round(r.gscore)} × 20%\n${ind}`
        : (noFin || '价值、质量、成长三项分数不全，暂无综合分');
      r.industry = r.industry ? `${r.sector ? r.sector + ' / ' : ''}${r.industry}` : '';
      return r;
    }
    const ind = r.industry ? `与「${r.industry}」行业内比较` : '无行业信息，与全市场比较';
    const fp = r.fin_period ? `${r.fin_period} 财报` : '';
    const noScore = r.delisting ? '退市整理期，不参与评分' : null;
    r.valTip = isNum(r.vscore)
      ? `${part('市盈率TTM', r.pe, 1)} · ${part('市净率', r.pb, 2)} · ${part('市销率TTM', r.ps, 2)}（${r.val_date}）\n${ind}`
      : (noScore || '估值数据不足，暂无价值分');
    r.qualTip = isNum(r.qscore)
      ? `${part('净资产收益率', r.roe, 1, '%')} · ${part('毛利率', r.gpm, 1, '%')} · ${part('经营现金流/净资产', r.cfroe, 1, '%')}（${fp}）\n${ind}`
      : (noScore || '没有可比的财务数据，暂无质量分');
    r.growTip = isNum(r.gscore)
      ? `${part('营收同比', r.rev_yoy, 1, '%')} · ${part('净利润同比', r.np_yoy, 1, '%')}（${fp}）\n${ind}`
      : (noScore || '没有可比的财务数据，暂无成长分');
    r.totTip = isNum(r.tscore)
      ? `价值 ${Math.round(r.vscore)} × 50% + 质量 ${Math.round(r.qscore)} × 30% + 成长 ${Math.round(r.gscore)} × 20%\n${ind}`
      : (noScore || '价值、质量、成长三项分数不全，暂无综合分');
    return r;
  });
}

async function loadMarket(m) {
  if (S.cache[m]) return S.cache[m];
  const resp = await fetch(MK[m].file + '?t=' + Date.now(), { cache: 'no-store' });
  if (!resp.ok) throw new Error('HTTP ' + resp.status);
  const data = await resp.json();
  S.cache[m] = { data, rows: prepare(data, m) };
  return S.cache[m];
}

function renderBoards() {
  const c = MK[S.m];
  $('#boards').innerHTML = ['', ...c.boards].map((b) =>
    `<button data-board="${esc(b)}"${b === S.board ? ' class="on"' : ''}>${b || '全部'}</button>`).join('');
}

async function switchMarket(m, first = false) {
  if (!MK[m]) m = 'a';
  S.m = m;
  store('market', m);
  if (!first) history.replaceState(null, '', m === 'a' ? location.pathname + location.search : '#' + m);
  document.body.classList.toggle('intl', m !== 'a');
  document.querySelectorAll('#mkts button').forEach((b) => b.classList.toggle('on', b.dataset.m === m));
  const c = MK[m];
  $('#title').textContent = `${c.label} · 距历史最低`;
  document.title = `${c.label} · 距历史最低`;
  $('#csv').href = c.csv;
  $('#noSuspLabel').textContent = m === 'a' ? '排除停牌' : '排除停牌、无成交';
  $('#note').textContent = m === 'a'
    ? '* 历史最低价已换算成今天的价格口径（相当于等比前复权），可以直接和现价比较。† 发行价由上市首日前收盘价推断。'
    : `* 历史最低价已换算成今天的价格口径，可以直接和现价比较。价格单位：${c.cur}。※ 历史数据没有覆盖到上市日。⚠ 数据可能有拆合股未复权。`;
  S.board = '';
  S.rt = {}; S.rtStale = false; $('#rtStatus').textContent = '';
  S.sortKey = 'ratio'; S.sortDir = 1;
  renderBoards();
  renderHead();
  $('#banner').hidden = true;
  $('#status').textContent = '加载中…';
  S.data = null; S.rows = [];
  let got;
  try {
    got = await loadMarket(m);
  } catch (e) {
    if (S.m !== m) return;
    $('#status').textContent = `${c.label}数据还没有生成（第一次运行完成后出现），请稍后刷新。`;
    $('#buckets').innerHTML = '';
    renderHead();
    $('#tb').innerHTML = `<tr><td colspan="${activeCols().length}" class="dim">暂无数据</td></tr>`;
    return;
  }
  if (S.m !== m) return;
  S.data = got.data; S.rows = got.rows;
  const d = got.data;
  $('#status').textContent = d.rows.length
    ? `收盘数据 ${d.market_date} · ${d.rows.length} 只 · 更新于 ${d.updated_at}（北京时间）${m === 'a' ? '' : ' · 单位：' + c.cur}`
    : `${c.label}数据还没有生成（第一次运行完成后出现），请稍后刷新。`;
  if (d.build_pending && S.rows.length < d.total) { // 每周校正期间数据完整，不显示
    const b = $('#banner');
    b.hidden = false;
    b.textContent = `数据首次构建尚未完成（已完成 ${S.rows.length} / ${d.total} 只），后续运行会自动补齐。`;
  }
  refresh();
  if (inTradingHours()) fetchRealtime();
}

function bind() {
  const mq = window.matchMedia('(max-width: 760px)');
  S.narrow = mq.matches;
  const onMq = (e) => { S.narrow = e.matches; renderHead(); if (S.data) renderTable(); };
  if (mq.addEventListener) mq.addEventListener('change', onMq); else mq.addListener(onMq);
  S.basis = recall('basis', 'low');
  S.useIssue = recall('useIssue', true);
  S.noST = recall('noST', false);
  S.noSusp = recall('noSusp', false);
  S.noDelist = recall('noDelist', false);
  $('#basis').value = S.basis;
  $('#useIssue').checked = S.useIssue;
  $('#noST').checked = S.noST;
  $('#noSusp').checked = S.noSusp;
  $('#noDelist').checked = S.noDelist;
  $('#rtAuto').checked = recall('rtAuto', false);
  setAuto($('#rtAuto').checked);

  $('#mkts').addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (b && b.dataset.m !== S.m) switchMarket(b.dataset.m);
  });
  window.addEventListener('hashchange', () => {
    const m = location.hash.slice(1) || 'a';
    if (MK[m] && m !== S.m) switchMarket(m, true);
  });
  let t;
  $('#q').addEventListener('input', (e) => { clearTimeout(t); t = setTimeout(() => { S.q = e.target.value; refresh(); }, 150); });
  $('#boards').addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (!b) return;
    S.board = b.dataset.board;
    document.querySelectorAll('#boards button').forEach((x) => x.classList.toggle('on', x === b));
    refresh();
  });
  for (const id of ['basis', 'useIssue', 'noST', 'noSusp', 'noDelist']) {
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
  $('#scoreKey').addEventListener('change', (e) => { S.scoreKey = e.target.value; refresh(); });
  $('#minCap').addEventListener('change', (e) => { S.minCap = +e.target.value || 0; refresh(); });
  $('#minScore').addEventListener('input', (e) => {
    const v = parseFloat(e.target.value);
    S.minScore = v >= 0 ? v : null;
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
  $('#tb').addEventListener('click', (e) => { // 点名称：展开 / 收起被截断的全名
    const td = e.target.closest('td.c-name');
    if (td && !e.target.closest('a')) td.classList.toggle('full');
  });
  document.querySelector('thead').addEventListener('click', (e) => {
    const th = e.target.closest('th[data-k]');
    if (!th) return;
    const k = th.dataset.k;
    if (S.sortKey === k) S.sortDir = -S.sortDir;
    else { S.sortKey = k; S.sortDir = DESC_FIRST.has(k) ? -1 : 1; }
    S.view.sort(cmp);
    renderTable();
  });
}

bind();
switchMarket(MK[location.hash.slice(1)] ? location.hash.slice(1) : recall('market', 'a'), true);
