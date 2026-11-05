/* 买卖记录本。持仓和流水只放在这一台浏览器的 localStorage，不进数据刷新，也不上传。
 * 键：nazhietf.trades、nazhietf.holdings。别人打开同一网址看不到、也改不了。
 * 页面不带任何示例持仓。持仓快照是数量和成本的依据；流水是日记。卖出金额用持仓数量 × 现价。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.NazhiLedger = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  const TRADES_KEY = 'nazhietf.trades';
  const HOLDINGS_KEY = 'nazhietf.holdings';
  const PURGE_KEY = 'nazhietf.publishedSeedPurgeV1';
  const TACTICAL_MID = 0.06;
  const ANNUAL_CAP = 0.20;
  const CORE_FLOOR = 0.75;
  const PEAK_JUMP = 1.25;

  const NASDAQ_CODES = ['159501', '159513', '159632', '159659', '159660', '159696', '159941', '513100', '513110', '513300', '513390', '513870'];
  const SP_CODES = ['159655', '513500', '513650'];
  const NAMES = {
    '159501': '嘉实纳斯达克100',
    '159513': '大成纳斯达克100',
    '159632': '华安纳斯达克100',
    '159659': '招商纳斯达克100',
    '159660': '汇添富纳斯达克100',
    '159696': '易方达纳斯达克100',
    '159941': '广发纳指100',
    '513100': '国泰纳斯达克100',
    '513110': '华泰柏瑞纳斯达克100',
    '513300': '华夏纳斯达克100',
    '513390': '博时纳斯达克100',
    '513870': '富国纳斯达克100',
    '513500': '博时标普500',
    '159655': '华夏标普500',
    '513650': '南方标普500',
  };

  function num(v) {
    if (v == null || v === '') return null;
    const n = typeof v === 'number' ? v : parseFloat(String(v).replace(/,/g, ''));
    return Number.isFinite(n) ? n : null;
  }
  function round2(n) { return Math.round(n * 100) / 100; }
  function round4(n) { return Math.round(n * 1e4) / 1e4; }
  function round6(n) { return Math.round(n * 1e6) / 1e6; }
  function uid(prefix) {
    return prefix + '-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 8);
  }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }
  function shanghaiToday() {
    try {
      return new Intl.DateTimeFormat('en-CA', {
        timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit',
      }).format(new Date());
    } catch (e) {
      return new Date().toISOString().slice(0, 10);
    }
  }
  function familyOfCode(code, data) {
    if (SP_CODES.indexOf(code) >= 0) return 'sp500';
    if (NASDAQ_CODES.indexOf(code) >= 0) return 'nasdaq';
    const groups = (data && data.picks && data.picks.groups) || [];
    for (let i = 0; i < groups.length; i++) {
      const items = groups[i].items || [];
      if (items.some((it) => it.code === code)) return groups[i].id === 'sp-500' ? 'sp500' : 'nasdaq';
    }
    return '';
  }
  function familyLabel(f) { return f === 'sp500' ? '标普500' : '纳指100'; }
  function sumMonthBuys(trades, family, yyyymm) {
    const month = String(yyyymm || '');
    const fam = family === 'sp500' ? 'sp500' : 'nasdaq';
    return (trades || []).reduce((sum, t) => {
      if (!t || t.side !== 'buy') return sum;
      const tf = t.family === 'sp500' ? 'sp500' : 'nasdaq';
      if (tf !== fam) return sum;
      if (String(t.date || '').slice(0, 7) !== month) return sum;
      const amount = num(t.amount);
      return sum + (amount > 0 ? amount : 0);
    }, 0);
  }
  function monthBought(family, yyyymm) {
    const month = yyyymm || shanghaiToday().slice(0, 7);
    return sumMonthBuys(state && state.trades, family, month);
  }
  function ytdSold(trades, year) {
    const y = String(year || '');
    return (trades || []).reduce((sum, t) => {
      if (!t || t.side !== 'sell') return sum;
      if (String(t.date || '').slice(0, 4) !== y) return sum;
      const amount = num(t.amount);
      return sum + (amount > 0 ? amount : 0);
    }, 0);
  }
  function clampSell(totalMV, peakMV, soldYTD) {
    const total = Math.max(0, num(totalMV) || 0);
    const peak = Math.max(total, num(peakMV) || 0);
    const sold = Math.max(0, num(soldYTD) || 0);
    const tactical = total * TACTICAL_MID;
    const annualRoom = Math.max(0, ANNUAL_CAP * peak - sold);
    const coreRoom = Math.max(0, total - CORE_FLOOR * peak);
    const raw = Math.min(tactical, annualRoom, coreRoom);
    const amount = Math.floor(raw + 1e-9);
    const eps = 0.01;
    let binding = 'tactical';
    if (coreRoom <= annualRoom + eps && coreRoom <= tactical - eps) binding = 'core';
    else if (annualRoom <= tactical - eps) binding = 'annual';
    return {
      totalMV: total,
      peakMV: peak,
      soldYTD: sold,
      tactical,
      annualRoom,
      coreRoom,
      amountRaw: raw,
      amount,
      binding,
      bandLo: total * 0.05,
      bandHi: total * 0.08,
      capLine: ANNUAL_CAP * peak,
      coreLine: CORE_FLOOR * peak,
      soldPct: peak > 0 ? (sold / peak) * 100 : 0,
      mvPctOfPeak: peak > 0 ? (total / peak) * 100 : 0,
      afterMV: Math.max(0, total - amount),
      afterPct: peak > 0 ? ((total - amount) / peak) * 100 : 0,
    };
  }
  function valueRows(rows, quote) {
    return (rows || []).map((row) => {
      const shares = num(row.shares) || 0;
      const cost = num(row.cost) || 0;
      const q = (quote && quote(row)) || {};
      const price = num(q.price);
      const costAmount = shares * cost;
      const mv = shares > 0 && price != null ? shares * price : (shares > 0 ? null : 0);
      const pnl = mv == null ? null : mv - costAmount;
      return Object.assign({}, row, {
        shares,
        cost,
        price,
        premium: q.premium == null ? null : num(q.premium),
        source: q.source || '',
        costAmount,
        mv,
        pnl,
        pnlPct: costAmount > 0 && pnl != null ? (pnl / costAmount) * 100 : null,
      });
    });
  }
  function sameInstrument(a, b) {
    if ((a.family || '') !== (b.family || '')) return false;
    const ac = (a.code || '').trim();
    const bc = (b.code || '').trim();
    if (ac || bc) return ac === bc;
    return (a.name || '').trim() === (b.name || '').trim();
  }
  function pickTarget(valued, families) {
    const allow = new Set(families || []);
    const pool = valued.filter((h) => h.shares > 0 && h.mv != null && allow.has(h.family));
    pool.sort((a, b) => {
      const pa = a.premium == null ? -Infinity : a.premium;
      const pb = b.premium == null ? -Infinity : b.premium;
      if (pb !== pa) return pb - pa;
      return (b.mv || 0) - (a.mv || 0);
    });
    return pool[0] || null;
  }
  function allocate(valued, top, amount) {
    if (!top || !(amount > 0)) return [];
    const same = valued.filter((h) => h.shares > 0 && h.mv > 0 && sameInstrument(h, top))
      .sort((a, b) => b.mv - a.mv);
    let left = amount;
    const out = [];
    same.forEach((h) => {
      if (left <= 0) return;
      const sellMV = Math.min(left, h.mv);
      out.push({
        id: h.id,
        account: brokerOf(h),
        broker: brokerOf(h),
        code: h.code || '',
        name: h.name || '',
        price: h.price,
        sellMV,
        sellShares: h.price ? sellMV / h.price : null,
      });
      left -= sellMV;
    });
    return out;
  }
  function familyRollup(list) {
    const mvKnown = list.every((h) => h.mv != null);
    const mv = list.reduce((s, h) => s + (h.mv || 0), 0);
    const cost = list.reduce((s, h) => s + h.costAmount, 0);
    const pnl = mvKnown ? mv - cost : null;
    return {
      mv,
      cost,
      pnl,
      pnlPct: cost > 0 && pnl != null ? (pnl / cost) * 100 : null,
      count: list.length,
    };
  }
  function computePlan(opts) {
    const quote = opts.quote || function () { return {}; };
    const year = String(opts.year || '');
    const valued = valueRows(opts.rows || [], quote);
    const active = valued.filter((h) => h.shares > 0);
    const missing = active.filter((h) => h.mv == null).length;
    const priced = active.filter((h) => h.mv != null);
    const totalMV = priced.reduce((s, h) => s + h.mv, 0);
    const storedPeak = opts.peaks && opts.peaks[year] ? (num(opts.peaks[year].mv) || 0) : 0;
    const peakBase = missing === 0 ? Math.max(storedPeak, totalMV) : Math.max(storedPeak, 0);
    const clamp = clampSell(totalMV, peakBase || totalMV, ytdSold(opts.trades, year));
    const top = pickTarget(priced, opts.families || []);
    const allocation = allocate(priced, top, clamp.amount);
    const nasdaq = familyRollup(active.filter((h) => h.family === 'nasdaq'));
    const sp = familyRollup(active.filter((h) => h.family === 'sp500'));
    const all = familyRollup(active);
    return Object.assign({}, clamp, {
      valued,
      hasHoldings: active.length > 0,
      missingPrices: missing,
      storedPeak,
      year,
      peakAsOf: (opts.peaks && opts.peaks[year] && opts.peaks[year].asOf) || '',
      code: top ? (top.code || '') : '',
      name: top ? (top.name || '') : '',
      family: top ? top.family : '',
      premium: top ? top.premium : null,
      account: allocation[0] ? allocation[0].account : '',
      allocation,
      byFamily: { nasdaq, sp500: sp, all },
      triggered: !!opts.triggered,
    });
  }
  const KNOWN_BROKERS = ['平安证券', '银河证券', '招商证券'];
  function brokerOf(row) {
    return String((row && (row.broker || row.account)) || '').trim().slice(0, 40);
  }
  function holdingKey(h) {
    const code = (h.code || '').trim();
    const name = (h.name || '').trim();
    return [brokerOf(h), h.family || '', code || ('name:' + name)].join('|');
  }
  function tradeShares(trade) {
    const shares = num(trade.shares);
    const amount = num(trade.amount);
    const price = num(trade.price);
    if (shares != null && shares > 0) return shares;
    if (amount != null && price != null && price > 0) return amount / price;
    return null;
  }
  function applyTradeToHoldings(rows, trade, update) {
    const copy = (rows || []).map((r) => Object.assign({}, r));
    if (!update) return { ok: true, rows: copy, message: '' };
    const shares = tradeShares(trade);
    if (!(shares > 0)) return { ok: false, rows: copy, message: '缺少数量或价格，没有改持仓。' };
    const amount = num(trade.amount);
    const price = num(trade.price);
    const spent = amount != null && amount > 0 ? amount : (price != null ? shares * price : null);
    if (trade.side === 'buy' && !(spent > 0)) return { ok: false, rows: copy, message: '买入要有金额，才能更新成本。' };
    const idx = copy.findIndex((r) => holdingKey(r) === holdingKey(trade));
    if (trade.side === 'sell') {
      if (idx < 0) return { ok: false, rows: copy, message: '没有对应持仓，未扣减。' };
      if ((num(copy[idx].shares) || 0) + 1e-6 < shares) return { ok: false, rows: copy, message: '持仓数量不够，未扣减。' };
      copy[idx].shares = round4((num(copy[idx].shares) || 0) - shares);
      if (price > 0) copy[idx].snapshotPrice = price;
      copy[idx].seed = false;
      return { ok: true, rows: copy, message: '已从持仓扣减数量，成本价未改。' };
    }
    if (idx < 0) {
      copy.push({
        id: uid('h'),
        account: brokerOf(trade),
        broker: brokerOf(trade),
        family: trade.family === 'sp500' ? 'sp500' : 'nasdaq',
        code: (trade.code || '').trim(),
        name: trade.name || '',
        shares: round4(shares),
        cost: round6(price > 0 ? price : spent / shares),
        snapshotPrice: price > 0 ? price : null,
        note: '',
        seed: false,
      });
      return { ok: true, rows: copy, message: '已新增一行持仓。' };
    }
    const oldShares = num(copy[idx].shares) || 0;
    const oldCost = num(copy[idx].cost) || 0;
    const newShares = oldShares + shares;
    copy[idx].shares = round4(newShares);
    copy[idx].cost = newShares > 0 ? round6((oldShares * oldCost + spent) / newShares) : oldCost;
    if (trade.name) copy[idx].name = trade.name;
    if (price > 0) copy[idx].snapshotPrice = price;
    copy[idx].seed = false;
    return { ok: true, rows: copy, message: '已按金额加权更新持仓成本。' };
  }
  function deriveFields(fields, edited) {
    const out = {
      amount: num(fields.amount),
      price: num(fields.price),
      shares: num(fields.shares),
    };
    if (edited === 'amount') {
      if (out.amount > 0 && out.price > 0) out.shares = out.amount / out.price;
      else if (out.amount > 0 && out.shares > 0) out.price = out.amount / out.shares;
    } else if (edited === 'price') {
      if (out.price > 0 && out.amount > 0) out.shares = out.amount / out.price;
      else if (out.price > 0 && out.shares > 0) out.amount = out.shares * out.price;
    } else if (edited === 'shares') {
      if (out.shares > 0 && out.price > 0) out.amount = out.shares * out.price;
      else if (out.shares > 0 && out.amount > 0) out.price = out.amount / out.shares;
    }
    return out;
  }
  function normalizeTrade(t) {
    if (!t || typeof t !== 'object') return null;
    const date = String(t.date || '').slice(0, 10);
    const side = t.side === 'sell' ? 'sell' : (t.side === 'buy' ? 'buy' : '');
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || !side) return null;
    const amount = num(t.amount);
    const price = num(t.price);
    const shares = num(t.shares);
    if (!(amount > 0) && !(shares > 0 && price > 0)) return null;
    const resolvedAmount = amount > 0 ? amount : round2(shares * price);
    const resolvedPrice = price > 0 ? price : (shares > 0 ? resolvedAmount / shares : null);
    const resolvedShares = shares > 0 ? shares : (resolvedPrice > 0 ? resolvedAmount / resolvedPrice : null);
    return {
      id: String(t.id || uid('t')),
      date,
      side,
      account: brokerOf(t),
      broker: brokerOf(t),
      family: t.family === 'sp500' ? 'sp500' : 'nasdaq',
      code: String(t.code || '').trim(),
      name: String(t.name || ''),
      amount: resolvedAmount,
      price: resolvedPrice,
      premium: num(t.premium),
      shares: resolvedShares,
      note: String(t.note || '').slice(0, 200),
      seed: !!t.seed,
      journal: !!t.journal,
      createdAt: String(t.createdAt || ''),
    };
  }
  function normalizeHolding(h) {
    if (!h || typeof h !== 'object') return null;
    const shares = num(h.shares);
    if (shares == null || shares < 0) return null;
    if (!(shares > 0) && !String(h.code || '').trim() && !String(h.name || '').trim()) return null;
    const cost = num(h.cost);
    return {
      id: String(h.id || uid('h')),
      account: brokerOf(h),
      broker: brokerOf(h),
      family: h.family === 'sp500' ? 'sp500' : 'nasdaq',
      code: String(h.code || '').trim(),
      name: String(h.name || ''),
      shares,
      cost: cost != null && cost >= 0 ? cost : 0,
      snapshotPrice: num(h.snapshotPrice),
      note: String(h.note || '').slice(0, 200),
      seed: !!h.seed,
    };
  }
  function normalizeBundle(raw) {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new Error('不是 JSON 对象');
    const tradesIn = Array.isArray(raw.trades) ? raw.trades : [];
    let rowsIn = [];
    let peaks = {};
    let peaksByFamily = {};
    let seeded = true;
    if (Array.isArray(raw.holdings)) rowsIn = raw.holdings;
    else if (raw.holdings && typeof raw.holdings === 'object') {
      rowsIn = Array.isArray(raw.holdings.rows) ? raw.holdings.rows : [];
      if (raw.holdings.peaks && typeof raw.holdings.peaks === 'object') peaks = raw.holdings.peaks;
      if (raw.holdings.peaksByFamily && typeof raw.holdings.peaksByFamily === 'object') peaksByFamily = raw.holdings.peaksByFamily;
      if (raw.holdings.seeded != null) seeded = !!raw.holdings.seeded;
    }
    if (raw.peaks && typeof raw.peaks === 'object') peaks = raw.peaks;
    if (raw.peaksByFamily && typeof raw.peaksByFamily === 'object') peaksByFamily = raw.peaksByFamily;
    if (raw.seeded != null) seeded = !!raw.seeded;
    return {
      trades: tradesIn.map(normalizeTrade).filter(Boolean),
      rows: rowsIn.map(normalizeHolding).filter(Boolean),
      peaks,
      peaksByFamily,
      seeded,
    };
  }
  function emptyLedger() {
    return { trades: [], rows: [], peaks: {}, peaksByFamily: {}, seeded: true };
  }
  // 旧页面写进 localStorage 的示例行用 seed- 开头的 id，并且 seed 仍为 true。
  // 用户后来保存过的行会把 seed 清掉，那些行留下。这里不保存示例里的数量或成本。
  function isPublishedSeedRow(row) {
    if (!row || row.seed !== true) return false;
    return String(row.id || '').indexOf('seed-') === 0;
  }
  function stripPublishedSeed(bundle) {
    const prevRows = bundle.rows || [];
    const prevTrades = bundle.trades || [];
    const rows = prevRows.filter((row) => !isPublishedSeedRow(row));
    const trades = prevTrades.filter((row) => !isPublishedSeedRow(row));
    const removed = rows.length !== prevRows.length || trades.length !== prevTrades.length;
    bundle.rows = rows;
    bundle.trades = trades;
    bundle.seeded = true;
    if (removed && rows.length === 0 && trades.length === 0) {
      bundle.peaks = {};
      bundle.peaksByFamily = {};
    }
    return removed;
  }
  function adoptUserLedger(bundle) {
    (bundle.rows || []).forEach((row) => { row.seed = false; });
    (bundle.trades || []).forEach((row) => { row.seed = false; });
    bundle.seeded = true;
    return bundle;
  }
  function createSeed() {
    return emptyLedger();
  }
  function applyBrokerSwap() {
    return false;
  }
  function insertCmsSeed() {
    return false;
  }

  let ctx = null;
  let state = null;
  let memoryOnly = false;
  let memory = null;
  let editingTradeId = '';
  let editingHoldId = '';
  let lastPlan = null;
  let bound = false;

  function storageGet(key) {
    if (memoryOnly) return memory && Object.prototype.hasOwnProperty.call(memory, key) ? memory[key] : null;
    try { return localStorage.getItem(key); }
    catch (e) { memoryOnly = true; memory = memory || {}; return null; }
  }
  function storageSet(key, value) {
    if (memoryOnly) { memory = memory || {}; memory[key] = value; return; }
    try { localStorage.setItem(key, value); }
    catch (e) { memoryOnly = true; memory = memory || {}; memory[key] = value; }
  }
  function persist() {
    storageSet(TRADES_KEY, JSON.stringify(state.trades || []));
    storageSet(HOLDINGS_KEY, JSON.stringify({
      rows: state.rows || [],
      peaks: state.peaks || {},
      peaksByFamily: state.peaksByFamily || {},
      seeded: state.seeded !== false,
    }));
  }
  function loadState() {
    const tRaw = storageGet(TRADES_KEY);
    const hRaw = storageGet(HOLDINGS_KEY);
    if (tRaw == null && hRaw == null) {
      state = emptyLedger();
      storageSet(PURGE_KEY, '1');
      persist();
      return;
    }
    try {
      const trades = tRaw ? JSON.parse(tRaw) : [];
      const hold = hRaw ? JSON.parse(hRaw) : {};
      state = normalizeBundle({
        trades: Array.isArray(trades) ? trades : [],
        holdings: Array.isArray(hold) ? hold : hold,
        peaks: hold && hold.peaks,
        peaksByFamily: hold && hold.peaksByFamily,
        seeded: true,
      });
      const purged = stripPublishedSeed(state);
      if (purged || storageGet(PURGE_KEY) !== '1') {
        storageSet(PURGE_KEY, '1');
        persist();
      }
    } catch (e) {
      state = emptyLedger();
      state.loadError = true;
    }
  }
  function catalog() {
    const data = (ctx && ctx.data) || {};
    const map = new Map();
    function add(code, name, family) {
      if (!code) return;
      const prev = map.get(code);
      map.set(code, {
        code,
        name: name || (prev && prev.name) || NAMES[code] || code,
        family: family || (prev && prev.family) || familyOfCode(code, data) || 'nasdaq',
      });
    }
    NASDAQ_CODES.forEach((c) => add(c, NAMES[c], 'nasdaq'));
    SP_CODES.forEach((c) => add(c, NAMES[c], 'sp500'));
    (data.peers || []).forEach((p) => add(p.code, p.name, 'nasdaq'));
    ((data.picks && data.picks.groups) || []).forEach((g) => {
      const family = g.id === 'sp-500' ? 'sp500' : 'nasdaq';
      (g.items || []).forEach((it) => add(it.code, it.name, family));
    });
    ((data.calculator && data.calculator.etfs) || []).forEach((e) => {
      add(e.code, e.name, familyOfCode(e.code, data) || 'nasdaq');
    });
    return Array.from(map.values()).sort((a, b) => (a.family < b.family ? -1 : a.family > b.family ? 1 : 0) || a.code.localeCompare(b.code));
  }
  function quoteOf(code, snapshotPrice) {
    const data = (ctx && ctx.data) || {};
    const live = ctx && ctx.liveMap && ctx.liveMap[code];
    if (live && num(live.price) > 0) return { price: num(live.price), premium: num(live.premium), source: '行情' };
    const peer = (data.peers || []).find((p) => p.code === code);
    if (peer) {
      const price = peer.live_price != null ? num(peer.live_price) : num(peer.close);
      if (price > 0) return { price, premium: num(peer.ref_premium_pct), source: '快照' };
    }
    let pickPrem = null;
    ((data.picks && data.picks.groups) || []).forEach((g) => {
      (g.items || []).forEach((it) => {
        if (it.code === code && it.ref_premium_pct != null) pickPrem = num(it.ref_premium_pct);
      });
    });
    if (num(snapshotPrice) > 0) return { price: num(snapshotPrice), premium: pickPrem, source: '持仓快照' };
    return { price: null, premium: pickPrem, source: '—' };
  }
  function currentPlan(view) {
    const year = shanghaiToday().slice(0, 4);
    return computePlan({
      rows: state.rows,
      trades: state.trades,
      peaks: state.peaks,
      year,
      families: (view && view.families) || [],
      triggered: !!(view && view.triggered),
      quote: (row) => quoteOf(row.code, row.snapshotPrice),
    });
  }
  function maybeRaisePeak(plan) {
    if (!plan || plan.missingPrices > 0 || !(plan.totalMV > 0)) return plan;
    const prev = plan.storedPeak || 0;
    if (prev > 0 && plan.totalMV > prev * PEAK_JUMP) {
      plan.peakRejected = true;
      return plan;
    }
    if (plan.totalMV <= prev + 0.01) return plan;
    state.peaks = state.peaks || {};
    state.peaks[plan.year] = { mv: round2(plan.totalMV), asOf: shanghaiToday() };
    persist();
    const again = currentPlan({ families: plan._families, triggered: plan.triggered });
    again.peakRaised = true;
    return again;
  }
  function fmtMoney(n) {
    if (n == null || !Number.isFinite(Number(n))) return '—';
    return Number(n).toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function fmtPct(n) {
    if (n == null || !Number.isFinite(Number(n))) return '—';
    const v = Number(n);
    return (v > 0 ? '+' : '') + v.toFixed(2) + '%';
  }
  function fmtPrem(n) {
    if (n == null || !Number.isFinite(Number(n))) return '—';
    return Number(n).toFixed(2) + '%';
  }
  function fmtShares(n) {
    if (n == null || !Number.isFinite(Number(n))) return '—';
    const r = round4(Number(n));
    if (Math.abs(r - Math.round(r)) < 1e-4) return Math.round(r).toLocaleString('zh-CN');
    return r.toLocaleString('zh-CN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function fmtPx(n) {
    if (n == null || !Number.isFinite(Number(n))) return '—';
    const s = Number(n).toFixed(4).replace(/0+$/, '').replace(/\.$/, '');
    return (s.split('.')[1] || '').length < 3 ? Number(n).toFixed(3) : s;
  }
  function fmtInput(n, d) {
    if (n == null || !Number.isFinite(Number(n))) return '';
    const p = Math.pow(10, d);
    return String(Math.round(Number(n) * p) / p);
  }
  function pnlHtml(pnl, pct) {
    if (pnl == null) return '—';
    const cls = pnl > 0.005 ? 'up' : (pnl < -0.005 ? 'down' : 'flat');
    return `<span class="${cls}">${pnl > 0 ? '+' : ''}${fmtMoney(pnl)}</span>` + (pct == null ? '' : `<span class="sub ${cls}">${fmtPct(pct)}</span>`);
  }
  function bindingText(binding) {
    if (binding === 'annual') return '本年 20% 额度';
    if (binding === 'core') return '底仓 75%';
    return '单次 6%';
  }
  function quotaBlock(title, plan) {
    if (!plan) return '';
    const peakWhen = plan.peakAsOf ? `（记于 ${esc(plan.peakAsOf)}）` : '';
    let head = '<p>现在不卖。要同时满足这一边的净值近 60 日新高和溢价明显贵。</p>';
    if (plan.triggered && !plan.hasHoldings) {
      head = '<p>条件已经满足，但还没有这一边的持仓，所以只提示卖该品种市值的 5–8%，不算人民币。</p>';
    } else if (plan.triggered && plan.missingPrices > 0 && !(plan.totalMV > 0)) {
      head = '<p>条件已经满足，但这一边的持仓缺少现价，算不出市值。</p>';
    } else if (plan.triggered && !(plan.amount > 0)) {
      head = `<p>已触发，但再卖就会低于这一边峰值的 75%，或本年 20% 已经用完。本次被「${bindingText(plan.binding)}」卡住。</p>`;
    } else if (plan.triggered && !plan.code && !plan.name) {
      head = `<div class="sell-amt">¥${fmtMoney(plan.amount)}</div><p>金额只按这一边的市值估算，账上没有可指定的代码。</p>`;
    } else if (plan.triggered) {
      const bits = (plan.allocation || []).map((a) => `${esc(a.broker || a.account || '未填券商')}约 ¥${fmtMoney(a.sellMV)}（约 ${fmtShares(a.sellShares)} 份，现价 ${fmtPx(a.price)}）`).join('；');
      const one = plan.allocation && plan.allocation.length === 1;
      const fit = one ? '这一笔没有超出该券商这一只的市值。' : '同一代码按市值从大到小分到各券商。';
      head = `<div class="sell-amt">¥${fmtMoney(plan.amount)}</div>
        <p>优先 <strong>${esc(plan.code || '（代码空）')} ${esc(plan.name || '')}</strong>${plan.premium == null ? '' : `，溢价 ${fmtPrem(plan.premium)}`}。${bits}。${fit}</p>
        <p>取数：这一边市值 × 6% = ¥${fmtMoney(plan.tactical)}；本年还可卖 ¥${fmtMoney(plan.annualRoom)}；为留下 75% 底仓最多卖 ¥${fmtMoney(plan.coreRoom)}。本次被「${bindingText(plan.binding)}」卡住。卖后剩余约 ¥${fmtMoney(plan.afterMV)}，为这一边峰值的 ${plan.afterPct.toFixed(2)}%。</p>`;
    }
    const jump = plan.peakRejected ? '<p>这次市值比已记录峰值高出 25% 以上，没有自动抬峰值。</p>' : '';
    return `<div class="sell-family">
      <h3>${title}</h3>
      ${head}
      ${jump}
      <div class="grid">
        <div class="kpi"><div class="k">这一边市值</div><div class="v">¥${fmtMoney(plan.totalMV)}</div><div class="s">单次约 6%，¥${fmtMoney(plan.bandLo)}–¥${fmtMoney(plan.bandHi)}</div></div>
        <div class="kpi"><div class="k">本年已卖</div><div class="v">¥${fmtMoney(plan.soldYTD)}</div><div class="s">占这一边峰值 ${plan.soldPct.toFixed(2)}% · 上限 20%</div></div>
        <div class="kpi"><div class="k">剩余可卖</div><div class="v">¥${fmtMoney(plan.annualRoom)}</div><div class="s">峰值 × 20% − 本年已卖</div></div>
        <div class="kpi"><div class="k">底仓线</div><div class="v">¥${fmtMoney(plan.coreLine)}</div><div class="s">峰值 ¥${fmtMoney(plan.peakMV)} 的 75%${peakWhen} · 当前 ${plan.mvPctOfPeak.toFixed(2)}%</div></div>
      </div>
    </div>`;
  }
  function sellSplitHtml(nasdaq, sp500) {
    return `<h3>卖出额度</h3>
      <p>纳指底仓和标普底仓分开算，不把两边合成一个底仓。每一边都是：卖后至少留下该边峰值市值的 75%，本年卖出不超过该边峰值的 20%，单次大约是该边市值的 6%。仅供个人记录辅助，非投资建议。</p>
      <div class="sell-split">
        ${quotaBlock('纳指底仓', nasdaq)}
        ${quotaBlock('标普底仓', sp500)}
      </div>`;
  }
  function brokerCards(list) {
    const map = new Map();
    (list || []).filter((h) => h.shares > 0).forEach((h) => {
      const name = brokerOf(h) || '未填券商';
      const cur = map.get(name) || { mv: 0, cost: 0, missing: false };
      if (h.mv == null) cur.missing = true;
      else cur.mv += h.mv;
      cur.cost += h.costAmount || 0;
      map.set(name, cur);
    });
    const order = ['平安证券', '银河证券', '招商证券'];
    return Array.from(map.keys()).sort((a, b) => {
      const ia = order.indexOf(a);
      const ib = order.indexOf(b);
      return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib) || a.localeCompare(b, 'zh');
    }).map((name) => {
      const row = map.get(name);
      const pnl = row.missing ? null : row.mv - row.cost;
      return card(name, {
        mv: row.mv,
        cost: row.cost,
        pnl,
        pnlPct: row.cost > 0 && pnl != null ? (pnl / row.cost) * 100 : null,
      });
    }).join('');
  }
  function card(title, roll) {
    const pnl = roll.pnl == null ? '现价不全' : `浮盈亏 ${roll.pnl > 0 ? '+' : ''}${fmtMoney(roll.pnl)}（${fmtPct(roll.pnlPct)}）`;
    return `<div class="kpi"><div class="k">${title}</div><div class="v">¥${fmtMoney(roll.mv)}</div><div class="s">成本 ¥${fmtMoney(roll.cost)} · ${pnl}</div></div>`;
  }
  function sortHoldings(list) {
    const fam = { nasdaq: 0, sp500: 1 };
    const acc = { '平安证券': 0, '银河证券': 1, '招商证券': 2 };
    return list.slice().sort((a, b) => {
      const aBroker = brokerOf(a);
      const bBroker = brokerOf(b);
      return (fam[a.family] == null ? 9 : fam[a.family]) - (fam[b.family] == null ? 9 : fam[b.family])
        || String(a.code || '').localeCompare(String(b.code || ''))
        || (acc[aBroker] == null ? 9 : acc[aBroker]) - (acc[bBroker] == null ? 9 : acc[bBroker])
        || aBroker.localeCompare(bBroker, 'zh');
    });
  }
  function paint(plan) {
    const cards = document.getElementById('holdingCards');
    if (cards) {
      cards.innerHTML = card('纳指100', plan.byFamily.nasdaq) + card('标普500', plan.byFamily.sp500) + card('全部', plan.byFamily.all);
    }
    const brokers = document.getElementById('holdingBrokers');
    if (brokers) brokers.innerHTML = brokerCards(plan.valued);
    const body = document.getElementById('holdingBody');
    if (body) {
      const rows = sortHoldings(plan.valued.filter((h) => h.shares > 0 || h.note || h.code));
      body.innerHTML = rows.length ? rows.map((h) => `<tr>
        <td>${esc(brokerOf(h) || '未填')}</td>
        <td>${familyLabel(h.family)}</td>
        <td><strong>${esc(h.code || '—')}</strong></td>
        <td>${esc(h.name || '')}${h.note ? `<span class="sub cell-wrap">${esc(h.note)}</span>` : ''}</td>
        <td class="num">${fmtShares(h.shares)}</td>
        <td class="num">${fmtPx(h.cost)}</td>
        <td class="num">${fmtMoney(h.costAmount)}</td>
        <td class="num">${fmtPx(h.price)}<span class="sub">${esc(h.source || '')}</span></td>
        <td class="num">${fmtPrem(h.premium)}</td>
        <td class="num">${h.mv == null ? '—' : fmtMoney(h.mv)}</td>
        <td class="num">${pnlHtml(h.pnl, h.pnlPct)}</td>
        <td><button type="button" class="btn tiny" data-act="edit-hold" data-id="${esc(h.id)}">修改</button>
          <button type="button" class="btn tiny danger" data-act="del-hold" data-id="${esc(h.id)}">删除</button></td>
      </tr>`).join('') : '<tr><td colspan="12">还没有持仓。用下面的表新增一行。</td></tr>';
    }
    const foot = document.getElementById('holdingFoot');
    if (foot) {
      const all = plan.byFamily.all;
      foot.innerHTML = `<tr>
        <td colspan="6">合计</td>
        <td class="num">${fmtMoney(all.cost)}</td>
        <td></td><td></td>
        <td class="num">${fmtMoney(all.mv)}</td>
        <td class="num">${pnlHtml(all.pnl, all.pnlPct)}</td>
        <td></td>
      </tr>`;
    }
    const tbody = document.getElementById('tradeBody');
    if (tbody) {
      const trades = (state.trades || []).slice().sort((a, b) => String(b.date).localeCompare(String(a.date)) || String(b.createdAt || '').localeCompare(String(a.createdAt || '')));
      tbody.innerHTML = trades.length ? trades.map((t) => `<tr>
        <td>${esc(t.date)}</td>
        <td><span class="badge ${t.side === 'sell' ? 'sell' : 'ok'}">${t.side === 'sell' ? '卖出' : '买入'}</span></td>
        <td>${esc(brokerOf(t) || '未填')}</td>
        <td>${familyLabel(t.family)}</td>
        <td><strong>${esc(t.code || '—')}</strong></td>
        <td>${esc(t.name || '')}</td>
        <td class="num">${fmtMoney(t.amount)}</td>
        <td class="num">${fmtPx(t.price)}</td>
        <td class="num">${t.premium == null ? '—' : fmtPrem(t.premium)}</td>
        <td class="num">${fmtShares(t.shares)}</td>
        <td>${t.note ? `<div class="cell-wrap">${esc(t.note)}</div>` : ''}</td>
        <td><button type="button" class="btn tiny" data-act="edit-trade" data-id="${esc(t.id)}">修改</button>
          <button type="button" class="btn tiny danger" data-act="del-trade" data-id="${esc(t.id)}">删除</button></td>
      </tr>`).join('') : '<tr><td colspan="12">还没有流水。</td></tr>';
    }
    const note = document.getElementById('ledgerSeedNote');
    if (note) {
      if (state.loadError) {
        note.hidden = false;
        note.textContent = '这台浏览器里的记录读不出来。为避免覆盖，先不要记新流水；可以清空或导入一份 JSON。';
      } else if (memoryOnly) {
        note.hidden = false;
        note.textContent = '当前浏览器不让网页保存本地记录，刷新后会丢失。请先导出 JSON。';
      } else {
        note.hidden = true;
        note.textContent = '';
      }
    }
  }
  function render(view) {
    if (!state) return null;
    const families = (view && view.families) || [];
    const full = currentPlan({ families: [], triggered: false });
    const nasdaq = scopedSell('nasdaq', families.indexOf('nasdaq') >= 0);
    const sp500 = scopedSell('sp500', families.indexOf('sp500') >= 0);
    lastPlan = nasdaq;
    const panel = document.getElementById('sellPanel');
    if (panel) panel.innerHTML = sellSplitHtml(nasdaq, sp500);
    paint(full);
    return nasdaq;
  }
  function val(id) {
    const el = document.getElementById(id);
    return el ? el.value : '';
  }
  function setTradeMsg(text) {
    const el = document.getElementById('tradeMsg');
    if (el) el.textContent = text || '';
  }
  function setHoldMsg(text) {
    const el = document.getElementById('holdMsg');
    if (el) el.textContent = text || '';
  }
  function selectedCode(selectId, customId) {
    const sel = val(selectId);
    if (sel === '__custom') return val(customId).trim();
    return sel.trim();
  }
  function readBroker(selectId, customId) {
    const sel = val(selectId);
    if (sel === '__other') return val(customId).trim().slice(0, 40);
    return String(sel || '').trim();
  }
  function setBroker(selectId, customId, name) {
    const select = document.getElementById(selectId);
    const custom = document.getElementById(customId);
    if (!select || !custom) return;
    const broker = String(name || '').trim().slice(0, 40);
    if (!broker) {
      select.value = '';
      custom.value = '';
      custom.hidden = true;
      return;
    }
    if (KNOWN_BROKERS.indexOf(broker) >= 0) {
      select.value = broker;
      custom.value = '';
      custom.hidden = true;
      return;
    }
    select.value = '__other';
    custom.value = broker;
    custom.hidden = false;
  }
  function wireBroker(selectId, customId) {
    const select = document.getElementById(selectId);
    const custom = document.getElementById(customId);
    if (!select || !custom) return;
    const sync = () => { custom.hidden = select.value !== '__other'; };
    select.addEventListener('change', sync);
    sync();
  }
  function setCode(selectId, customId, code) {
    const select = document.getElementById(selectId);
    const custom = document.getElementById(customId);
    if (!select || !custom) return;
    const has = Array.prototype.some.call(select.options, (o) => o.value === code);
    if (code && has) { select.value = code; custom.value = ''; }
    else if (code) { select.value = '__custom'; custom.value = code; }
    else { select.value = ''; custom.value = ''; }
    custom.hidden = select.value !== '__custom';
  }
  function fillCodeSelect(select) {
    if (!select) return;
    const current = select.value;
    const items = catalog();
    const group = (family, label) => {
      const opts = items.filter((i) => i.family === family).map((i) => `<option value="${esc(i.code)}">${esc(i.code)} ${esc(i.name)}</option>`).join('');
      return `<optgroup label="${label}">${opts}</optgroup>`;
    };
    select.innerHTML = '<option value="">选择代码</option>' + group('nasdaq', '纳指100') + group('sp500', '标普500') + '<option value="__custom">其他（手填代码）</option>';
    if (Array.prototype.some.call(select.options, (o) => o.value === current)) select.value = current;
  }
  function syncCustom(selectId, customId) {
    const custom = document.getElementById(customId);
    if (custom) custom.hidden = val(selectId) !== '__custom';
  }
  function onCodePick(selectId, customId, familyId, nameId, priceId, premId) {
    syncCustom(selectId, customId);
    const code = selectedCode(selectId, customId);
    const item = catalog().find((i) => i.code === code);
    const nameEl = document.getElementById(nameId);
    if (item && nameEl && (!nameEl.value || nameEl.dataset.auto === '1')) {
      nameEl.value = item.name;
      nameEl.dataset.auto = '1';
    }
    if (item) {
      const fam = document.getElementById(familyId);
      if (fam) fam.value = item.family;
    }
    if (priceId) {
      const priceEl = document.getElementById(priceId);
      const q = quoteOf(code, null);
      if (priceEl && !priceEl.value && q.price) priceEl.value = fmtInput(q.price, 4);
      if (premId) {
        const premEl = document.getElementById(premId);
        if (premEl && !premEl.value && q.premium != null) premEl.value = fmtInput(q.premium, 2);
      }
      const amountEl = document.getElementById('tradeAmount');
      const sharesEl = document.getElementById('tradeShares');
      if (priceEl && amountEl && sharesEl && num(amountEl.value) > 0 && num(priceEl.value) > 0 && !num(sharesEl.value)) {
        sharesEl.value = fmtInput(num(amountEl.value) / num(priceEl.value), 4);
      }
    }
  }
  function syncSide() {
    const sell = val('tradeSide') === 'sell';
    const label = document.getElementById('tradeUpdateLabel');
    if (label) label.textContent = sell ? '卖出后从持仓扣减数量（成本价不变）' : '买入后更新持仓（数量相加，成本按金额加权）';
    const box = document.getElementById('tradeUpdate');
    if (box && !box.dataset.touched && !editingTradeId) box.checked = !sell;
  }
  function resetTradeForm() {
    editingTradeId = '';
    const date = document.getElementById('tradeDate');
    if (date && !date.value) date.value = shanghaiToday();
    if (date) date.value = shanghaiToday();
    const side = document.getElementById('tradeSide');
    if (side) side.value = 'buy';
    setBroker('tradeAccount', 'tradeBrokerCustom', '');
    setCode('tradeCode', 'tradeCodeCustom', '');
    const name = document.getElementById('tradeName');
    if (name) { name.value = ''; name.dataset.auto = ''; }
    ['tradeAmount', 'tradePrice', 'tradePremium', 'tradeShares', 'tradeNote'].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.value = '';
    });
    const box = document.getElementById('tradeUpdate');
    if (box) { box.checked = true; box.disabled = false; box.dataset.touched = ''; }
    const submit = document.getElementById('tradeSubmit');
    if (submit) submit.textContent = '记入流水';
    const cancel = document.getElementById('tradeCancel');
    if (cancel) cancel.hidden = true;
    syncSide();
  }
  function readTrade() {
    let amount = num(val('tradeAmount'));
    let price = num(val('tradePrice'));
    let shares = num(val('tradeShares'));
    if (!(amount > 0) && shares > 0 && price > 0) amount = round2(shares * price);
    if (!(shares > 0) && amount > 0 && price > 0) shares = amount / price;
    if (!(price > 0) && amount > 0 && shares > 0) price = amount / shares;
    return {
      date: val('tradeDate'),
      side: val('tradeSide') === 'sell' ? 'sell' : 'buy',
      account: readBroker('tradeAccount', 'tradeBrokerCustom'),
      broker: readBroker('tradeAccount', 'tradeBrokerCustom'),
      family: val('tradeFamily') === 'sp500' ? 'sp500' : 'nasdaq',
      code: selectedCode('tradeCode', 'tradeCodeCustom'),
      name: val('tradeName').trim(),
      amount: amount > 0 ? round2(amount) : null,
      price: price > 0 ? price : null,
      premium: num(val('tradePremium')),
      shares: shares > 0 ? shares : null,
      note: val('tradeNote').trim().slice(0, 200),
    };
  }
  function guardWrite() {
    if (state.loadError) {
      setTradeMsg('本地记录读失败。先清空或导入，避免覆盖原来的数据。');
      return false;
    }
    return true;
  }
  function refresh() {
    if (ctx && typeof ctx.requestRefresh === 'function') ctx.requestRefresh();
    else render({ families: [], triggered: false, reason: '' });
  }
  function onTradeSubmit(ev) {
    ev.preventDefault();
    if (!guardWrite()) return;
    const trade = readTrade();
    if (!/^\d{4}-\d{2}-\d{2}$/.test(trade.date || '')) return setTradeMsg('请填写日期。');
    if (!(trade.amount > 0)) return setTradeMsg('请填写金额，或同时填写价格和数量。');
    if (!(trade.price > 0)) return setTradeMsg('请填写价格。');
    const update = document.getElementById('tradeUpdate').checked && !editingTradeId;
    if (update && !trade.broker) return setTradeMsg('勾了更新持仓时请选择券商。');
    if (update && !trade.code && !trade.name) return setTradeMsg('勾了更新持仓时请填写代码或名称。');
    if (editingTradeId) {
      const idx = state.trades.findIndex((t) => t.id === editingTradeId);
      if (idx < 0) return setTradeMsg('找不到这条流水。');
      const prev = state.trades[idx];
      state.trades[idx] = Object.assign({}, prev, trade, {
        id: prev.id, seed: false, journal: prev.journal, createdAt: prev.createdAt,
      });
      persist();
      resetTradeForm();
      setTradeMsg('已保存修改。改流水不会自动改持仓。');
      refresh();
      return;
    }
    const row = Object.assign({ id: uid('t'), createdAt: new Date().toISOString(), seed: false, journal: false }, trade);
    if (update) {
      const applied = applyTradeToHoldings(state.rows, row, true);
      if (!applied.ok) return setTradeMsg(applied.message);
      state.rows = applied.rows;
      state.trades.push(row);
      persist();
      resetTradeForm();
      setTradeMsg('已记下这笔。' + applied.message);
      refresh();
      return;
    }
    state.trades.push(row);
    persist();
    resetTradeForm();
    setTradeMsg('已记下这笔流水，持仓数量没有改。');
    refresh();
  }
  function onHoldSubmit(ev) {
    ev.preventDefault();
    if (!guardWrite()) { setHoldMsg('本地记录读失败。先清空或导入。'); return; }
    const broker = readBroker('holdAccount', 'holdBrokerCustom');
    const row = {
      account: broker,
      broker,
      family: val('holdFamily') === 'sp500' ? 'sp500' : 'nasdaq',
      code: selectedCode('holdCode', 'holdCodeCustom'),
      name: val('holdName').trim(),
      shares: num(val('holdShares')),
      cost: num(val('holdCost')),
      snapshotPrice: num(val('holdSnap')),
      note: val('holdNote').trim().slice(0, 200),
      seed: false,
    };
    if (!(row.shares > 0)) return setHoldMsg('请填写持仓数量。');
    if (row.cost == null || row.cost < 0) return setHoldMsg('请填写成本价。');
    if (!row.code && !row.name) return setHoldMsg('请填写代码或名称。');
    const key = holdingKey(row);
    if (editingHoldId) {
      const idx = state.rows.findIndex((r) => r.id === editingHoldId);
      if (idx < 0) return setHoldMsg('找不到这条持仓。');
      if (state.rows.some((r) => r.id !== editingHoldId && holdingKey(r) === key)) return setHoldMsg('已经有同一券商和代码的持仓。');
      state.rows[idx] = Object.assign({}, state.rows[idx], row, { id: editingHoldId });
    } else {
      const idx = state.rows.findIndex((r) => holdingKey(r) === key);
      if (idx >= 0) {
        state.rows[idx] = Object.assign({}, state.rows[idx], row, { id: state.rows[idx].id });
      } else {
        row.id = uid('h');
        state.rows.push(row);
      }
    }
    persist();
    resetHoldForm();
    setHoldMsg('持仓已保存。');
    refresh();
  }
  function resetHoldForm() {
    editingHoldId = '';
    setBroker('holdAccount', 'holdBrokerCustom', '');
    setCode('holdCode', 'holdCodeCustom', '');
    ['holdName', 'holdShares', 'holdCost', 'holdSnap', 'holdNote'].forEach((id) => {
      const el = document.getElementById(id);
      if (el) el.value = '';
    });
    const name = document.getElementById('holdName');
    if (name) name.dataset.auto = '';
    const submit = document.getElementById('holdSubmit');
    if (submit) submit.textContent = '保存持仓';
    const cancel = document.getElementById('holdCancel');
    if (cancel) cancel.hidden = true;
  }
  function fillTrade(t) {
    editingTradeId = t.id;
    document.getElementById('tradeDate').value = t.date;
    document.getElementById('tradeSide').value = t.side;
    setBroker('tradeAccount', 'tradeBrokerCustom', brokerOf(t));
    document.getElementById('tradeFamily').value = t.family === 'sp500' ? 'sp500' : 'nasdaq';
    setCode('tradeCode', 'tradeCodeCustom', t.code || '');
    const name = document.getElementById('tradeName');
    name.value = t.name || '';
    name.dataset.auto = '';
    document.getElementById('tradeAmount').value = t.amount != null ? fmtInput(t.amount, 2) : '';
    document.getElementById('tradePrice').value = t.price != null ? fmtInput(t.price, 4) : '';
    document.getElementById('tradePremium').value = t.premium != null ? fmtInput(t.premium, 2) : '';
    document.getElementById('tradeShares').value = t.shares != null ? fmtInput(t.shares, 4) : '';
    document.getElementById('tradeNote').value = t.note || '';
    const box = document.getElementById('tradeUpdate');
    box.checked = false;
    box.disabled = true;
    document.getElementById('tradeSubmit').textContent = '保存修改';
    document.getElementById('tradeCancel').hidden = false;
    syncSide();
    document.getElementById('tradeForm').scrollIntoView({ behavior: 'smooth', block: 'center' });
    setTradeMsg('正在修改流水。保存不会回头改持仓。');
  }
  function fillHold(h) {
    editingHoldId = h.id;
    setBroker('holdAccount', 'holdBrokerCustom', brokerOf(h));
    document.getElementById('holdFamily').value = h.family === 'sp500' ? 'sp500' : 'nasdaq';
    setCode('holdCode', 'holdCodeCustom', h.code || '');
    const name = document.getElementById('holdName');
    name.value = h.name || '';
    name.dataset.auto = '';
    document.getElementById('holdShares').value = fmtInput(h.shares, 4);
    document.getElementById('holdCost').value = fmtInput(h.cost, 4);
    document.getElementById('holdSnap').value = h.snapshotPrice != null ? fmtInput(h.snapshotPrice, 4) : '';
    document.getElementById('holdNote').value = h.note || '';
    document.getElementById('holdSubmit').textContent = '保存修改';
    document.getElementById('holdCancel').hidden = false;
    const editor = document.getElementById('holdEditor');
    if (editor) {
      editor.open = true;
      editor.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
    setHoldMsg('正在修改持仓快照。');
  }
  function prefillBuy() {
    const s = ctx && ctx.buySuggestion && ctx.buySuggestion();
    if (!s || !(num(s.amount) > 0)) return setTradeMsg((s && s.emptyNote) || '计算器里还没有建议金额。');
    const keepBroker = readBroker('tradeAccount', 'tradeBrokerCustom');
    resetTradeForm();
    setBroker('tradeAccount', 'tradeBrokerCustom', keepBroker);
    document.getElementById('tradeDate').value = shanghaiToday();
    document.getElementById('tradeSide').value = 'buy';
    const known = familyOfCode(s.code, ctx.data);
    if (known) document.getElementById('tradeFamily').value = known;
    setCode('tradeCode', 'tradeCodeCustom', s.code || '');
    const name = document.getElementById('tradeName');
    name.value = s.name || (NAMES[s.code] || '');
    name.dataset.auto = '1';
    document.getElementById('tradeAmount').value = fmtInput(s.amount, 2);
    const q = quoteOf(s.code, null);
    if (q.price) document.getElementById('tradePrice').value = fmtInput(q.price, 4);
    const prem = q.source === '行情' && q.premium != null ? q.premium : (s.premium != null ? s.premium : q.premium);
    if (prem != null) document.getElementById('tradePremium').value = fmtInput(prem, 2);
    const amount = num(val('tradeAmount'));
    const price = num(val('tradePrice'));
    if (amount > 0 && price > 0) document.getElementById('tradeShares').value = fmtInput(amount / price, 4);
    document.getElementById('tradeUpdate').checked = true;
    document.getElementById('tradeUpdate').disabled = false;
    syncSide();
    document.getElementById('tradeForm').scrollIntoView({ behavior: 'smooth', block: 'center' });
    setTradeMsg('已按今天可买的金额填入，还没有保存。请选券商，核对价格和溢价后再记一笔。');
  }
  function exportJson() {
    const payload = {
      version: 1,
      exportedAt: new Date().toISOString(),
      trades: state.trades || [],
      holdings: state.rows || [],
      peaks: state.peaks || {},
      peaksByFamily: state.peaksByFamily || {},
      seeded: true,
    };
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'nazhietf-ledger-' + shanghaiToday() + '.json';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }
  function onImportFile(file) {
    if (!file) return;
    if (file.size > 2 * 1024 * 1024) return setTradeMsg('文件超过 2MB，没有导入。');
    const reader = new FileReader();
    reader.onload = () => {
      try {
        const raw = JSON.parse(String(reader.result || ''));
        const bundle = adoptUserLedger(normalizeBundle(raw));
        if (!window.confirm('导入会覆盖这台浏览器里的流水、持仓和峰值。文件只写进这一台，不会上传。确定？')) return;
        state = bundle;
        state.loadError = false;
        persist();
        resetTradeForm();
        resetHoldForm();
        setTradeMsg(`已导入 ${bundle.trades.length} 笔流水、${bundle.rows.length} 行持仓。`);
        refresh();
      } catch (e) {
        setTradeMsg('这份 JSON 读不出来，没有覆盖现有记录。');
      }
    };
    reader.readAsText(file);
  }
  function bind() {
    if (bound) return;
    bound = true;
    fillCodeSelect(document.getElementById('tradeCode'));
    fillCodeSelect(document.getElementById('holdCode'));
    wireBroker('tradeAccount', 'tradeBrokerCustom');
    wireBroker('holdAccount', 'holdBrokerCustom');
    const tradeForm = document.getElementById('tradeForm');
    const holdForm = document.getElementById('holdForm');
    if (tradeForm) tradeForm.addEventListener('submit', onTradeSubmit);
    if (holdForm) holdForm.addEventListener('submit', onHoldSubmit);
    const date = document.getElementById('tradeDate');
    if (date) date.value = shanghaiToday();
    document.getElementById('tradeSide').addEventListener('change', syncSide);
    document.getElementById('tradeUpdate').addEventListener('change', (ev) => { ev.target.dataset.touched = '1'; });
    document.getElementById('tradeName').addEventListener('input', (ev) => { ev.target.dataset.auto = ''; });
    document.getElementById('holdName').addEventListener('input', (ev) => { ev.target.dataset.auto = ''; });
    ['tradeAmount', 'tradePrice', 'tradeShares'].forEach((id) => {
      document.getElementById(id).addEventListener('input', () => {
        const edited = id === 'tradeAmount' ? 'amount' : (id === 'tradePrice' ? 'price' : 'shares');
        const next = deriveFields({
          amount: val('tradeAmount'), price: val('tradePrice'), shares: val('tradeShares'),
        }, edited);
        if (edited !== 'amount' && next.amount != null) document.getElementById('tradeAmount').value = fmtInput(next.amount, 2);
        if (edited !== 'price' && next.price != null) document.getElementById('tradePrice').value = fmtInput(next.price, 4);
        if (edited !== 'shares' && next.shares != null) document.getElementById('tradeShares').value = fmtInput(next.shares, 4);
      });
    });
    document.getElementById('tradeCode').addEventListener('change', () => {
      onCodePick('tradeCode', 'tradeCodeCustom', 'tradeFamily', 'tradeName', 'tradePrice', 'tradePremium');
    });
    document.getElementById('tradeCodeCustom').addEventListener('change', () => {
      onCodePick('tradeCode', 'tradeCodeCustom', 'tradeFamily', 'tradeName', 'tradePrice', 'tradePremium');
    });
    document.getElementById('holdCode').addEventListener('change', () => {
      onCodePick('holdCode', 'holdCodeCustom', 'holdFamily', 'holdName');
    });
    document.getElementById('tradePrefill').addEventListener('click', prefillBuy);
    document.getElementById('tradeCancel').addEventListener('click', () => { resetTradeForm(); setTradeMsg(''); });
    document.getElementById('holdCancel').addEventListener('click', () => { resetHoldForm(); setHoldMsg(''); });
    document.getElementById('ledgerExport').addEventListener('click', exportJson);
    document.getElementById('ledgerImportBtn').addEventListener('click', () => document.getElementById('ledgerFile').click());
    document.getElementById('ledgerFile').addEventListener('change', (ev) => {
      const file = ev.target.files && ev.target.files[0];
      ev.target.value = '';
      onImportFile(file);
    });
    document.getElementById('ledgerClear').addEventListener('click', () => {
      if (!window.confirm('清空这台浏览器里的流水、持仓和峰值？只清这一台，不会写到别人的浏览器。')) return;
      state = { trades: [], rows: [], peaks: {}, peaksByFamily: {}, seeded: true, loadError: false };
      persist();
      resetTradeForm();
      resetHoldForm();
      setTradeMsg('已清空本机记录。');
      refresh();
    });
    document.getElementById('ledgerSection').addEventListener('click', (ev) => {
      const btn = ev.target.closest('[data-act]');
      if (!btn || !document.getElementById('ledgerSection').contains(btn)) return;
      const id = btn.getAttribute('data-id');
      const act = btn.getAttribute('data-act');
      if (act === 'edit-trade') {
        const t = (state.trades || []).find((r) => r.id === id);
        if (t) fillTrade(t);
      } else if (act === 'del-trade') {
        if (!window.confirm('删除这条流水？不会改持仓。')) return;
        state.trades = state.trades.filter((r) => r.id !== id);
        if (editingTradeId === id) resetTradeForm();
        persist();
        setTradeMsg('已删除流水。');
        refresh();
      } else if (act === 'edit-hold') {
        const h = (state.rows || []).find((r) => r.id === id);
        if (h) fillHold(h);
      } else if (act === 'del-hold') {
        if (!window.confirm('删除这条持仓快照？不会删除流水。')) return;
        state.rows = state.rows.filter((r) => r.id !== id);
        if (editingHoldId === id) resetHoldForm();
        persist();
        setHoldMsg('已删除持仓。');
        refresh();
      }
    });
    syncSide();
  }
  function scopedSell(family, triggered) {
    if (!state || !family) return null;
    const year = shanghaiToday().slice(0, 4);
    const rows = (state.rows || []).filter((r) => r.family === family);
    const trades = (state.trades || []).filter((t) => (t.family || '') === family);
    const quote = (row) => quoteOf(row.code, row.snapshotPrice);
    const run = (peaks) => computePlan({
      rows,
      trades,
      peaks: peaks || {},
      year,
      families: [family],
      triggered: !!triggered,
      quote,
    });
    let plan = run((state.peaksByFamily && state.peaksByFamily[family]) || {});
    plan.triggered = !!triggered;
    if (plan.missingPrices === 0 && plan.totalMV > 0) {
      const prev = plan.storedPeak || 0;
      if (prev > 0 && plan.totalMV > prev * PEAK_JUMP) {
        plan.peakRejected = true;
      } else if (plan.totalMV > prev + 0.01) {
        state.peaksByFamily = state.peaksByFamily || {};
        state.peaksByFamily[family] = state.peaksByFamily[family] || {};
        state.peaksByFamily[family][year] = { mv: round2(plan.totalMV), asOf: shanghaiToday() };
        persist();
        plan = run(state.peaksByFamily[family]);
        plan.triggered = !!triggered;
        plan.peakRaised = true;
      }
    }
    return plan;
  }
  function mount(nextCtx) {
    ctx = nextCtx || {};
    loadState();
    bind();
    return state;
  }
  function codes() {
    const set = [];
    const seen = new Set();
    function add(code) {
      if (code && !seen.has(code)) { seen.add(code); set.push(code); }
    }
    if (state) {
      (state.rows || []).forEach((r) => add(r.code));
      (state.trades || []).forEach((t) => add(t.code));
    }
    return set;
  }

  function selfCheckMonthBuys() {
    const trades = [
      { date: '2026-11-02', side: 'buy', family: 'nasdaq', amount: 1000 },
      { date: '2026-11-03', side: 'buy', family: 'nasdaq', amount: 500.5 },
      { date: '2026-11-03', side: 'sell', family: 'nasdaq', amount: 800 },
      { date: '2026-10-31', side: 'buy', family: 'nasdaq', amount: 9000 },
      { date: '2026-11-01', side: 'buy', family: 'sp500', amount: 2000 },
      { date: '2026-11-04', side: 'buy', family: 'nasdaq', amount: 0 },
      { date: '2026-11-04', side: 'buy', amount: 200 },
      { date: '2026-11-05', side: 'buy', family: 'sp500', amount: 100 },
    ];
    const nasdaq = sumMonthBuys(trades, 'nasdaq', '2026-11');
    const sp = sumMonthBuys(trades, 'sp500', '2026-11');
    if (Math.abs(nasdaq - 1700.5) > 1e-6) throw new Error('纳指当月买入自检失败 ' + nasdaq);
    if (Math.abs(sp - 2100) > 1e-6) throw new Error('标普当月买入自检失败 ' + sp);
    if (sumMonthBuys(trades, 'nasdaq', '2026-10') !== 9000) throw new Error('上月买入不应算进本月');
    if (sumMonthBuys(trades, 'sp500', '2026-11') === sumMonthBuys(trades, 'nasdaq', '2026-11')) {
      throw new Error('纳指和标普不应并成一笔');
    }
  }
  if (typeof module !== 'undefined' && module.exports) selfCheckMonthBuys();

  return {
    TRADES_KEY,
    HOLDINGS_KEY,
    TACTICAL_MID,
    ANNUAL_CAP,
    CORE_FLOOR,
    createSeed,
    applyBrokerSwap,
    insertCmsSeed,
    clampSell,
    ytdSold,
    sumMonthBuys,
    monthBought,
    computePlan,
    applyTradeToHoldings,
    deriveFields,
    normalizeBundle,
    valueRows,
    mount,
    render,
    scopedSell,
    codes,
    quoteForTest: quoteOf,
  };
});
