/* Кимо Mini App: без сборки; весь текст данных попадает в DOM только через textContent. */
(function () {
  'use strict';
  const tg = window.Telegram && window.Telegram.WebApp;
  const S = { me: null, tab: 'home', stack: [], sheets: [], core: null, coreState: { activity: 0.25 }, mainFn: null };
  const app = document.getElementById('app');

  /* ---------- утилиты ---------- */
  function h(tag, props, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v == null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
      else if (k === 'value') el.value = v;
      else if (k === 'text') el.textContent = v;
      else if (v === true) el.setAttribute(k, '');
      else el.setAttribute(k, v);
    }
    const add = (c) => { if (c == null || c === false) return; if (Array.isArray(c)) c.forEach(add); else el.append(c.nodeType ? c : document.createTextNode(String(c))); };
    kids.forEach(add);
    return el;
  }
  const svgNS = 'http://www.w3.org/2000/svg';
  function svg(tag, attrs, ...kids) {
    const el = document.createElementNS(svgNS, tag);
    for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
    kids.forEach((c) => c && el.append(c.nodeType ? c : document.createTextNode(String(c))));
    return el;
  }
  const t = (key, vars) => window.I18N.t(key, vars);
  const lang = () => (S.me && S.me.settings.lang) || 'ru';
  const locale = () => (lang() === 'uz' ? 'uz-UZ' : 'ru-RU');
  const tz = () => (S.me && S.me.settings.tz) || 'Asia/Tashkent';

  function haptic(kind) {
    try {
      const hf = tg && tg.HapticFeedback; if (!hf) return;
      if (kind === 'ok' || kind === 'error' || kind === 'warning') hf.notificationOccurred(kind === 'ok' ? 'success' : kind);
      else if (kind === 'select') hf.selectionChanged(); else hf.impactOccurred('light');
    } catch (e) { /* нет haptic — не страшно */ }
  }
  let toastTimer = 0;
  function toast(msg, bad) {
    const el = document.getElementById('toast');
    el.textContent = msg; el.className = 'show' + (bad ? ' bad' : '');
    clearTimeout(toastTimer); toastTimer = setTimeout(() => { el.className = ''; }, 2600);
    haptic(bad ? 'error' : 'ok');
  }
  const fmtNum = (n, d = 0) => Number(n || 0).toLocaleString('ru-RU', { maximumFractionDigits: d, minimumFractionDigits: d }).replace(/ /g, ' ');
  const money = (n, cur) => (cur === 'USD' ? '$' + fmtNum(n, 2) : fmtNum(n) + ' ' + t('sum'));
  const compact = (n) => { const a = Math.abs(n); return a >= 1e9 ? (n / 1e9).toFixed(1) + 'B' : a >= 1e6 ? (n / 1e6).toFixed(1) + 'M' : a >= 1e3 ? Math.round(n / 1e3) + 'K' : String(Math.round(n)); };
  function fmtDT(iso, withTime = true) {
    if (!iso) return '—';
    const o = { timeZone: tz(), day: 'numeric', month: 'short' };
    if (withTime) { o.hour = '2-digit'; o.minute = '2-digit'; }
    try { return new Date(iso).toLocaleString(locale(), o); } catch (e) { return iso; }
  }
  function ago(iso) {
    if (!iso) return t('never');
    const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if (s < 90) return t('just_now');
    if (s < 3600) return t('min_ago', { n: Math.round(s / 60) });
    if (s < 86400) return t('h_ago', { n: Math.round(s / 3600) });
    return t('d_ago', { n: Math.round(s / 86400) });
  }
  const today = () => new Date().toLocaleDateString('sv-SE', { timeZone: tz() });
  const icons = {
    home: 'M3 11l9-8 9 8M5 10v10h5v-6h4v6h5V10', tasks: 'M9 11l3 3 8-8M20 12v7a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h9',
    stats: 'M4 20V10M10 20V4M16 20v-7M22 20H2', business: 'M4 5h16v11H8l-4 4zM8 9h8M8 12h5',
    more: 'M5 12h.01M12 12h.01M19 12h.01', plus: 'M12 5v14M5 12h14', check: 'M5 12l5 5 9-10', close: 'M6 6l12 12M18 6L6 18',
    trash: 'M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3', send: 'M4 12l16-8-6 16-3-7z',
  };
  const icon = (name, size) => svg('svg', { viewBox: '0 0 24 24', width: size || 22, height: size || 22, 'aria-hidden': 'true', style: 'fill:none;stroke:currentColor;stroke-width:1.9;stroke-linecap:round;stroke-linejoin:round' }, svg('path', { d: icons[name] }));

  /* ---------- API ---------- */
  async function api(path, opts = {}) {
    let res;
    try {
      res = await fetch(path, {
        method: opts.method || 'GET', cache: 'no-store',
        headers: { Authorization: 'tma ' + (tg ? tg.initData : ''), 'Content-Type': 'application/json' },
        body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
      });
    } catch (e) { const err = new Error(t('err_network')); err.code = 'network'; throw err; }
    let data = null; try { data = await res.json(); } catch (e) { /* пустой ответ */ }
    if (!res.ok) { const err = new Error((data && data.message) || res.statusText); err.status = res.status; err.code = data && data.error; throw err; }
    return data;
  }
  async function safe(fn) { try { return await fn(); } catch (e) { toast(e.message || t('err_generic'), true); return null; } }

  /* ---------- тема ---------- */
  function applyTheme() {
    const s = S.me ? S.me.settings : {};
    const r = document.documentElement.style;
    if (s.accent) {
      r.setProperty('--accent', s.accent);
      const m = /^#([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(s.accent);
      const lum = m ? (0.299 * parseInt(m[1], 16) + 0.587 * parseInt(m[2], 16) + 0.114 * parseInt(m[3], 16)) : 160;
      r.setProperty('--accent-fg', lum > 150 ? '#06201e' : '#ffffff');
    }
    document.documentElement.lang = lang();
    if (S.core) S.core.refreshColor();
    try { if (tg) { tg.setHeaderColor('#0d1013'); tg.setBackgroundColor('#0d1013'); if (tg.setBottomBarColor) tg.setBottomBarColor('#0d1013'); } } catch (e) { /* старый клиент */ }
  }

  /* ---------- Telegram: Back / Main ---------- */
  function syncBack() {
    if (!tg) return;
    const need = S.sheets.length > 0 || S.stack.length > 0;
    try { need ? tg.BackButton.show() : tg.BackButton.hide(); } catch (e) { /* */ }
  }
  function onBack() {
    haptic('select');
    if (S.sheets.length) S.sheets[S.sheets.length - 1].close();
    else if (S.stack.length) { S.stack.pop(); render(); }
  }
  function setMain(text, fn, disabled) {
    S.mainFn = fn;
    if (!tg) return;
    try { tg.MainButton.setParams({ text, is_visible: true, color: getComputedStyle(document.documentElement).getPropertyValue('--accent').trim(), text_color: '#06201e' }); disabled ? tg.MainButton.disable() : tg.MainButton.enable(); } catch (e) { /* */ }
  }
  function clearMain() { S.mainFn = null; try { tg && tg.MainButton.hide(); } catch (e) { /* */ } }
  function onMain() { if (S.mainFn) S.mainFn(); }

  /* ---------- шторка ---------- */
  function openSheet(title, build, mainText, onSubmit) {
    const scrim = h('div', { class: 'scrim' });
    const body = h('div', { class: 'stack' });
    const sheet = h('div', { class: 'sheet', role: 'dialog', 'aria-label': title }, h('div', { class: 'grab' }), h('h2', { text: title }), body);
    const ctl = {
      body,
      close() {
        scrim.remove(); sheet.remove();
        const i = S.sheets.indexOf(ctl); if (i >= 0) S.sheets.splice(i, 1);
        if (!S.sheets.length) clearMain(); else if (S.sheets[S.sheets.length - 1].restoreMain) S.sheets[S.sheets.length - 1].restoreMain();
        syncBack();
      },
    };
    if (mainText) {
      const submit = async () => { if (ctl.busy) return; ctl.busy = true; try { const ok = await onSubmit(ctl); if (ok !== false) ctl.close(); } finally { ctl.busy = false; } };
      ctl.restoreMain = () => setMain(mainText, submit);
      body.append(h('button', { class: 'btn primary', onclick: submit, text: mainText }));   // запасная кнопка вне Telegram
      ctl.restoreMain();
    }
    scrim.addEventListener('click', () => ctl.close());
    document.body.append(scrim, sheet);
    S.sheets.push(ctl); syncBack(); build(body, ctl);
    return ctl;
  }

  /* ---------- общие блоки ---------- */
  const head = (title, ...right) => h('div', { class: 'head' }, h('h1', { text: title }), h('div', { class: 'row' }, right));
  const plusBtn = (fn) => h('button', { class: 'icon-btn', 'aria-label': t('add'), onclick: () => { haptic(); fn(); } }, icon('plus'));
  const loading = () => h('div', { class: 'spinner', role: 'progressbar' });
  const empty = (text) => h('div', { class: 'empty', text });
  const sw = (on, fn) => { const el = h('button', { class: 'switch' + (on ? ' on' : ''), role: 'switch', 'aria-checked': String(!!on), onclick: () => { haptic('select'); fn(el); } }); return el; };
  const seg = (items, current, fn) => h('div', { class: 'seg', role: 'tablist' }, items.map(([k, label]) => h('button', { class: k === current ? 'on' : '', role: 'tab', 'aria-selected': String(k === current), onclick: () => { haptic('select'); fn(k); }, text: label })));
  const field = (label, input) => h('label', { class: 'field' }, h('span', { text: label }), input);
  const pill = (cls, text) => h('span', { class: 'pill ' + cls }, h('i'), text);
  const statusPill = (st) => pill(st, t('st_' + st));
  const DAYS = ['1', '2', '3', '4', '5', '6', '7'];
  function bstatus(s) {   // «manual_needed: причина» -> «Нужна ручная отправка: причина»
    if (!s) return '';
    const i = s.indexOf(':'), code = i < 0 ? s : s.slice(0, i), rest = i < 0 ? '' : s.slice(i + 1).trim();
    const label = window.I18N.has('bs_' + code) ? t('bs_' + code) : code;
    return rest ? label + ': ' + rest : label;
  }
  const bclass = (s) => (/^sent/.test(s) ? 'up' : /^(manual|failed|stopped|blocked|missed)/.test(s) ? 'down' : '');
  function daysText(days) {
    if (days === '1234567') return t('d_daily'); if (days === '12345') return t('d_weekdays'); if (days === '67') return t('d_weekend');
    return [...days].map((d) => t('wd' + d)).join(', ');
  }
  async function guarded(el, fn) {   // загрузка с обработкой ошибок внутри блока
    el.replaceChildren(loading());
    try { el.replaceChildren(await fn()); } catch (e) { el.replaceChildren(empty(e.code === 'forbidden' ? t('no_rights') : (e.message || t('err_generic')))); }
  }

  /* ---------- главная ---------- */
  async function screenHome(root) {
    const host = h('div', { class: 'stack' });
    root.append(head(t('hello', { name: S.me.name || '' }), h('button', { class: 'icon-btn', 'aria-label': t('assistant'), onclick: openAssistant }, icon('send'))), host);
    await guarded(host, async () => {
      const d = await api('/api/home');
      const total = (d.agents || []).length, busy = (d.agents || []).filter((a) => a.status === 'busy').length, online = d.pulse || 0;
      S.coreState.activity = S.me.role === 'admin' ? Math.min(1, 0.2 + busy / Math.max(total, 1) * 0.8 + (online ? 0.1 : 0)) : 0.3;
      const canvas = h('canvas', { id: 'core', 'aria-label': 'Kimo core' });
      const label = h('div', { class: 'core-label' }, h('b', { text: S.me.role === 'admin' ? (busy ? t('agents_busy', { n: busy }) : t('all_calm')) : t('ready') }),
        h('span', { text: S.me.role === 'admin' ? t('agents_online', { n: online, m: total }) : t('kimo_with_you') }));
      const out = [h('div', { class: 'core-wrap' }, canvas, label)];
      if (d.agents) out.push(h('div', { class: 'chips' }, d.agents.map((a) => h('span', { class: 'chip' + (a.status === 'online' || a.status === 'busy' ? ' on' : '') }, a.display_name))));
      const cards = [];
      if (d.tasks) cards.push(h('div', { class: 'card tap', onclick: () => switchTab('tasks') }, h('div', { class: 'kpi' }, h('small', { text: t('today_tasks') }), h('b', { class: 'num', text: `${d.tasks.done}/${d.tasks.total}` }),
        h('div', { class: 'bar' }, h('i', { style: `width:${d.tasks.total ? Math.round(d.tasks.done / d.tasks.total * 100) : 0}%` })))));
      if (d.finance) cards.push(h('div', { class: 'card tap', onclick: () => switchTab('stats') }, h('div', { class: 'kpi' }, h('small', { text: t('today_money') }),
        h('b', { class: 'num up', text: '+' + compact(d.finance.income_uzs) }), h('small', { class: 'num down', text: '−' + compact(d.finance.expense_uzs) + ' ' + t('sum') }))));
      out.push(h('div', { class: 'grid2' }, cards));
      if (d.business) out.push(h('div', { class: 'card tap row', onclick: () => switchTab('business') }, h('div', { class: 'grow' }, h('div', { text: t('business_chat') }), h('div', { class: 'sub', text: d.business.connected ? t('biz_connected') : t('biz_not_connected') })),
        statusPill(d.business.connected ? (d.business.ai_enabled ? 'ok' : 'warn') : 'offline')));
      out.push(h('div', { class: 'card tap row', onclick: () => push('advice') }, h('div', { class: 'grow' }, h('div', { text: t('ai_advice') }), h('div', { class: 'sub', text: t('ai_advice_hint') })), icon('send', 18)));
      const wrap = h('div', { class: 'stack' }, out);
      requestAnimationFrame(() => { if (S.core) S.core.destroy(); S.core = window.KimoCore.mount(canvas, () => S.coreState); });
      return wrap;
    });
  }

  /* ---------- задачи ---------- */
  async function screenTasks(root) {
    let day = S.tasksDay || 'today';
    const list = h('div', { class: 'stack' }), stats = h('div');
    const reload = () => guarded(list, async () => {
      const d = await api('/api/tasks?day=' + day);
      if (!d.tasks.length) return empty(t('no_tasks'));
      return h('div', { class: 'stack' }, d.tasks.map((tk) => h('div', { class: 'card task' + (tk.done ? ' done' : '') },
        h('button', { class: 'check' + (tk.done ? ' on' : ''), 'aria-label': t('done'), onclick: async () => { if (tk.done) return; haptic(); if (await safe(() => api(`/api/tasks/${tk.id}/done`, { method: 'POST' }))) { haptic('ok'); reload(); loadStats(); } } }, tk.done ? icon('check', 16) : null),
        h('div', { class: 'grow' }, h('div', { class: 't', text: tk.text }), tk.has_time ? h('div', { class: 'sub num', text: fmtDT(tk.due) }) : null),
        h('button', { class: 'icon-btn', 'aria-label': t('delete'), onclick: async () => { if (await safe(() => api(`/api/tasks/${tk.id}`, { method: 'DELETE' }))) { reload(); loadStats(); } } }, icon('trash', 18)))));
    });
    const loadStats = () => guarded(stats, async () => {
      const d = await api('/api/tasks/stats?days=14');
      const max = Math.max(1, ...d.days.map((x) => x.total));
      const chart = svg('svg', { viewBox: '0 0 280 56', class: 'chart', role: 'img', 'aria-label': t('stats_tasks') });
      d.days.forEach((x, i) => {
        const bw = 280 / d.days.length, hh = (x.total / max) * 50;
        chart.append(svg('rect', { x: i * bw + 2, y: 54 - hh, width: bw - 4, height: Math.max(hh, 2), rx: 3, fill: 'var(--line-2)' }),
          svg('rect', { x: i * bw + 2, y: 54 - (x.done / max) * 50, width: bw - 4, height: Math.max((x.done / max) * 50, x.done ? 2 : 0), rx: 3, fill: 'var(--accent)' }));
      });
      return h('div', { class: 'card stack' }, h('div', { class: 'row' }, h('div', { class: 'grow' }, h('div', { class: 'label', text: t('stats_tasks') }),
        h('b', { class: 'num', style: 'font-size:24px', text: d.percent == null ? '—' : d.percent + '%' })), h('div', { class: 'sub', style: 'text-align:right' }, h('div', { class: 'num up', text: t('done_n', { n: d.done }) }), h('div', { class: 'num down', text: t('undone_n', { n: d.total - d.done }) }), h('div', { text: t('streak', { n: d.streak }) }))), chart);
    });
    root.append(head(t('tasks'), plusBtn(() => taskSheet(false, () => { reload(); loadStats(); }))),
      seg([['today', t('today')], ['tomorrow', t('tomorrow')]], day, (k) => { S.tasksDay = k; render(); }), list, stats);
    reload(); loadStats();
  }
  function taskSheet(reminder, done) {
    openSheet(reminder ? t('new_reminder') : t('new_task'), (b) => {
      const text = h('input', { type: 'text', id: 'f-text', maxlength: 500, placeholder: t('what_to_do') });
      const date = h('input', { type: 'date', id: 'f-date', value: today() }), time = h('input', { type: 'time', id: 'f-time' });
      b.prepend(field(t('text'), text), h('div', { class: 'grid2' }, field(t('date'), date), field(t('time') + (reminder ? '' : ' (' + t('optional') + ')'), time)));
      b._get = () => ({ text: text.value, date: date.value || null, time: time.value || null });
      setTimeout(() => text.focus(), 250);
    }, t('add'), async (ctl) => {
      const v = ctl.body._get();
      if (!v.text.trim() || (reminder && !v.time)) { toast(reminder ? t('need_text_time') : t('need_text'), true); return false; }
      if (!await safe(() => api(reminder ? '/api/reminders' : '/api/tasks', { method: 'POST', body: v }))) return false;
      toast(t('added')); done && done(); return true;
    });
  }

  /* ---------- аналитика ---------- */
  function barChart(points, cur, group) {
    const W = 320, H = 150, L = 34, B = 18, T = 8, pw = (W - L) / points.length;
    const key = (k) => (cur === 'USD' ? k + '_usd' : k + '_uzs');
    const max = Math.max(1, ...points.map((p) => Math.max(p[key('income')], p[key('expense')])));
    const c = svg('svg', { viewBox: `0 0 ${W} ${H}`, class: 'chart', role: 'img', 'aria-label': t('dynamics') });
    [0, 0.5, 1].forEach((f) => { const y = T + (H - B - T) * (1 - f); c.append(svg('line', { x1: L, x2: W, y1: y, y2: y, stroke: 'var(--line)', 'stroke-width': 1 }), svg('text', { x: L - 5, y: y + 3, 'text-anchor': 'end' }, compact(max * f))); });
    points.forEach((p, i) => {
      const x = L + i * pw, bw = Math.max(2, pw * 0.34);
      [['income', 'var(--up)', 0], ['expense', 'var(--down)', 1]].forEach(([k, color, j]) => {
        const hh = (p[key(k)] / max) * (H - B - T);
        c.append(svg('rect', { x: x + pw * 0.12 + j * (bw + 1), y: H - B - hh, width: bw, height: Math.max(hh, p[key(k)] ? 1.5 : 0), rx: 2.5, fill: color }));
      });
      if (i === 0 || i === points.length - 1 || i === Math.floor(points.length / 2)) {
        const d = new Date(p.start + 'T00:00:00');
        const lbl = group === 'month' ? d.toLocaleDateString(locale(), { month: 'short' }) : d.toLocaleDateString(locale(), { day: 'numeric', month: 'short' });
        c.append(svg('text', { x: x + pw / 2, y: H - 4, 'text-anchor': i === 0 ? 'start' : i === points.length - 1 ? 'end' : 'middle' }, lbl));
      }
    });
    return c;
  }
  function donut(cats, cur, rate) {
    const R = 38, C = 2 * Math.PI * R, colors = ['var(--c1)', 'var(--c2)', 'var(--c3)', 'var(--c4)', 'var(--c5)', 'var(--c6)'];
    const c = svg('svg', { viewBox: '0 0 100 100', width: 120, height: 120, role: 'img', 'aria-label': t('categories') });
    c.append(svg('circle', { cx: 50, cy: 50, r: R, fill: 'none', stroke: 'var(--bg-2)', 'stroke-width': 14 }));
    let off = 0;
    cats.slice(0, 6).forEach((x, i) => {
      const len = x.share * C;
      c.append(svg('circle', { cx: 50, cy: 50, r: R, fill: 'none', stroke: colors[i], 'stroke-width': 14, 'stroke-dasharray': `${Math.max(len - 1.5, 0.5)} ${C}`, 'stroke-dashoffset': -off, transform: 'rotate(-90 50 50)' }));
      off += len;
    });
    return c;
  }
  async function screenStats(root) {
    const st = S.st = S.st || { group: 'day', cur: 'UZS', kind: 'expense' };
    const body = h('div', { class: 'stack' });
    root.append(head(t('analytics'), plusBtn(() => moneySheet(() => render()))),
      seg([['day', t('g_day')], ['week', t('g_week')], ['month', t('g_month')]], st.group, (k) => { st.group = k; render(); }),
      seg([['UZS', 'UZS'], ['USD', 'USD']], st.cur, (k) => { st.cur = k; render(); }), body);
    await guarded(body, async () => {
      const period = { day: 'day', week: 'week', month: 'month' }[st.group];
      const [sum, ser, cats, tk, rate, items] = await Promise.all([
        api('/api/finance/summary?period=' + period), api(`/api/finance/series?group=${st.group}`),
        api(`/api/finance/categories?kind=${st.kind}&period=${period === 'day' ? 'week' : period}`), api('/api/tasks/stats?days=30').catch(() => null),
        api('/api/rate'), api('/api/finance/items?limit=8')]);
      const k = st.cur === 'USD' ? '_usd' : '_uzs', C = st.cur;
      const out = [h('div', { class: 'grid2' },
        h('div', { class: 'card kpi' }, h('small', { text: t('income') }), h('b', { class: 'num up', text: money(sum['income' + k], C) })),
        h('div', { class: 'card kpi' }, h('small', { text: t('expense') }), h('b', { class: 'num down', text: money(sum['expense' + k], C) }))),
        h('div', { class: 'card kpi' }, h('small', { text: t('balance') }), h('b', { class: 'num ' + (sum['balance' + k] >= 0 ? 'up' : 'down'), text: money(sum['balance' + k], C) })),
        h('div', { class: 'card stack' }, h('div', { class: 'label', text: t('dynamics') }), barChart(ser.points, C, st.group),
          h('div', { class: 'legend' }, h('span', {}, h('i', { style: 'background:var(--up)' }), t('income')), h('span', {}, h('i', { style: 'background:var(--down)' }), t('expense')))),
        h('div', { class: 'card stack' }, h('div', { class: 'row' }, h('div', { class: 'label grow', text: t('categories') }), seg([['expense', t('expense')], ['income', t('income')]], st.kind, (kk) => { st.kind = kk; render(); })),
          cats.categories.length ? h('div', { class: 'row', style: 'align-items:center' }, donut(cats.categories, C), h('div', { class: 'stack grow' }, cats.categories.slice(0, 6).map((x, i) =>
            h('div', { class: 'cat' }, h('i', { style: `background:var(--c${i + 1})` }), h('span', { class: 'grow trunc', text: x.category }), h('span', { class: 'num sub', text: Math.round(x.share * 100) + '%' }))))) : empty(t('no_data')))];
      if (tk) out.push(h('div', { class: 'card stack' }, h('div', { class: 'label', text: t('task_progress') }), h('div', { class: 'row' },
        h('b', { class: 'num', style: 'font-size:24px', text: tk.percent == null ? '—' : tk.percent + '%' }), h('div', { class: 'sub grow', text: t('done_of', { a: tk.done, b: tk.total }) + ' · ' + t('streak', { n: tk.streak }) })),
        h('div', { class: 'bar' }, h('i', { style: `width:${tk.percent || 0}%` }))));
      out.push(h('div', { class: 'card row' }, h('div', { class: 'grow' }, h('div', { text: t('usd_rate') }), h('div', { class: 'sub', text: 'cbu.uz · ' + (rate.date || '') })), h('b', { class: 'num', text: rate.usd_uzs ? fmtNum(rate.usd_uzs, 2) : '—' })));
      out.push(h('div', { class: 'label', text: t('recent_ops') }));
      out.push(items.items.length ? h('div', { class: 'stack' }, items.items.map((it) => h('div', { class: 'card row' }, h('div', { class: 'grow' }, h('div', { class: 'trunc', text: it.category + (it.note ? ' · ' + it.note : '') }), h('div', { class: 'sub', text: fmtDT(it.created) })),
        h('b', { class: 'num ' + (it.kind === 'income' ? 'up' : 'down'), text: (it.kind === 'income' ? '+' : '−') + money(it.amount, it.currency) }),
        h('button', { class: 'icon-btn', 'aria-label': t('delete'), onclick: async () => { if (await safe(() => api('/api/finance/' + it.id, { method: 'DELETE' }))) render(); } }, icon('trash', 16))))) : empty(t('no_data')));
      return h('div', { class: 'stack' }, out);
    });
  }
  function moneySheet(done) {
    const CATS = ['Еда', 'Транспорт', 'Покупки', 'Жильё', 'Связь', 'Здоровье', 'Развлечения', 'Другое'];
    openSheet(t('new_operation'), (b) => {
      let kind = 'expense', cur = 'UZS';
      const amount = h('input', { type: 'number', id: 'f-amount', inputmode: 'decimal', min: '0', placeholder: '0' });
      const cat = h('input', { type: 'text', id: 'f-cat', maxlength: 50, value: 'Другое' }), note = h('input', { type: 'text', id: 'f-note', maxlength: 200 });
      const kindHost = h('div'), curHost = h('div');
      const drawSegs = () => { kindHost.replaceChildren(seg([['expense', t('expense')], ['income', t('income')]], kind, (k) => { kind = k; drawSegs(); })); curHost.replaceChildren(seg([['UZS', 'UZS'], ['USD', 'USD']], cur, (k) => { cur = k; drawSegs(); })); };
      drawSegs();
      b.prepend(kindHost, field(t('amount'), amount), curHost, field(t('category'), cat), h('div', { class: 'chips' }, CATS.map((c) => h('button', { class: 'chip', onclick: () => { cat.value = c; haptic('select'); }, text: c }))), field(t('note'), note));
      b._get = () => ({ kind, currency: cur, amount: amount.value, category: cat.value, note: note.value || null });
    }, t('save'), async (ctl) => {
      const v = ctl.body._get(); v.amount = Number(v.amount);
      if (!(v.amount > 0)) { toast(t('need_amount'), true); return false; }
      if (!await safe(() => api('/api/finance', { method: 'POST', body: v }))) return false;
      toast(t('added')); done && done(); return true;
    });
  }

  /* ---------- напоминания, советы ---------- */
  async function screenReminders(root) {
    const host = h('div', { class: 'stack' });
    root.append(head(t('reminders'), plusBtn(() => taskSheet(true, render))), host);
    await guarded(host, async () => {
      const d = await api('/api/reminders');
      if (!d.reminders.length) return empty(t('no_reminders'));
      return h('div', { class: 'stack' }, d.reminders.map((r) => h('div', { class: 'card row' }, h('div', { class: 'grow' }, h('div', { text: r.text }), h('div', { class: 'sub num', text: fmtDT(r.due) })),
        h('button', { class: 'icon-btn', 'aria-label': t('delete'), onclick: async () => { if (await safe(() => api('/api/tasks/' + r.id, { method: 'DELETE' }))) render(); } }, icon('trash', 18)))));
    });
  }
  async function screenAdvice(root) {
    const host = h('div', { class: 'stack' });
    root.append(head(t('ai_advice')), host);
    await guarded(host, async () => {
      const d = await api('/api/advice');
      if (d.empty) return empty(t('advice_empty'));
      if (d.unavailable) return empty(t('advice_unavailable'));
      const areas = { expenses: t('expense'), income: t('income'), tasks: t('tasks') };
      return h('div', { class: 'stack' }, d.stale ? h('div', { class: 'sub', text: t('advice_stale', { d: d.day }) }) : h('div', { class: 'sub', text: t('advice_daily') }),
        d.tips.map((tip) => h('div', { class: 'card glow stack' }, h('div', { class: 'label', text: areas[tip.area] || tip.area }), h('div', { text: tip.text }))));
    });
  }

  /* ---------- агенты (admin) ---------- */
  async function screenAgents(root) {
    const host = h('div', { class: 'stack' });
    root.append(head(t('agents'), plusBtn(() => newAgentSheet())), host);
    await guarded(host, async () => {
      const d = await api('/api/agents');
      return h('div', { class: 'stack' }, h('div', { class: 'sub', text: t('agents_hint') }), d.agents.map((a) => h('div', { class: 'card stack' },
        h('div', { class: 'row' }, h('div', { class: 'grow' }, h('div', { style: 'font-weight:650', text: a.display_name }), h('div', { class: 'sub mono', text: a.name + (a.bot_username ? ' · @' + a.bot_username : '') })), statusPill(a.draft ? 'warn' : a.status)),
        h('div', { class: 'sub', text: a.role }),
        h('div', { class: 'row' }, h('div', { class: 'sub grow', text: t('heartbeat') + ': ' + ago(a.last_seen) }),
          a.name === 'kimo' ? null : sw(a.enabled, async (el) => {
            const want = !a.enabled;
            const r = await safe(() => api(`/api/agents/${a.name}/toggle`, { method: 'POST', body: { enabled: want } }));
            if (r) { toast(t('confirm_in_tg')); haptic('warning'); }
          })),
        a.last_error ? h('div', { class: 'sub down', text: a.last_error }) : null)));
    });
  }
  function newAgentSheet() {
    openSheet(t('new_agent'), (b) => {
      const d = h('textarea', { id: 'f-agent', maxlength: 3000, placeholder: t('agent_placeholder') });
      b.prepend(h('div', { class: 'sub', text: t('agent_flow') }), field(t('agent_describe'), d));
      b._get = () => d.value;
    }, t('create_via_mimo'), async (ctl) => {
      const description = ctl.body._get().trim();
      if (description.length < 10) { toast(t('need_longer'), true); return false; }
      const r = await safe(() => api('/api/agents', { method: 'POST', body: { description } }));
      if (!r) return false;
      toast(t('confirm_in_tg')); return true;
    });
  }

  /* ---------- бизнес-чат ---------- */
  async function screenBusiness(root) {
    const host = h('div', { class: 'stack' });
    root.append(head(t('business_chat')), host);
    await guarded(host, async () => {
      const d = await api('/api/business');
      const conn = d.connections.some((c) => c.is_enabled && c.can_reply), ai = d.connections.length ? d.connections.every((c) => c.ai_enabled) : false;
      const rules = d.rules, inputs = {};
      const text = (key, label, rows) => { inputs[key] = h('textarea', { id: 'r-' + key, rows: rows || 3, maxlength: 3000, value: rules[key] || '' }); return field(label, inputs[key]); };
      const num = (key, label) => { inputs[key] = h('input', { type: 'number', id: 'r-' + key, min: 0, value: String(rules[key] ?? 6) }); return field(label, inputs[key]); };
      const out = [h('div', { class: 'card stack glow' }, h('div', { class: 'row' }, h('div', { class: 'grow' }, h('div', { text: t('connection') }), h('div', { class: 'sub', text: conn ? t('biz_connected') : t('biz_how') })), statusPill(conn ? 'ok' : 'offline')),
        h('div', { class: 'row' }, h('div', { class: 'grow' }, t('ai_answers'), h('div', { class: 'sub', text: t('ai_answers_hint') })), sw(ai, async (el) => { const on = !el.classList.contains('on'); if (await safe(() => api('/api/business/pause', { method: 'POST', body: { ai_enabled: on } }))) { el.classList.toggle('on', on); toast(on ? t('ai_on') : t('ai_off')); } })))];
      out.push(h('div', { class: 'card stack' }, h('div', { class: 'label', text: t('rules') }), text('prompt', t('r_prompt')), text('info', t('r_info')), text('ban', t('r_ban'), 2), text('tone', t('r_tone'), 2),
        field(t('r_name') + ' (' + (d.owner_name_default || '—') + ')', (inputs.name = h('input', { type: 'text', id: 'r-name', maxlength: 100, value: rules.name || '' }))),
        h('div', { class: 'grid2' }, num('pause', t('r_pause')), num('flood', t('r_flood'))),
        h('button', { class: 'btn primary', onclick: async () => {
          const body = {}; for (const [k, el] of Object.entries(inputs)) body[k] = el.value;
          if (await safe(() => api('/api/business/rules', { method: 'PUT', body }))) toast(t('saved'));
        }, text: t('save') })));
      out.push(h('div', { class: 'label', text: t('chats') }));
      out.push(d.chats.length ? h('div', { class: 'stack' }, d.chats.map((c) => h('div', { class: 'card row tap', onclick: () => chatLogSheet(c) },
        h('div', { class: 'grow' }, h('div', { class: 'trunc', text: c.name || String(c.chat_id) }), h('div', { class: 'sub', text: (c.username ? '@' + c.username + ' · ' : '') + ago(c.last_incoming) })),
        c.paused && !c.excluded ? pill('warn', t('paused')) : null,
        sw(!c.excluded, async (el) => { const on = !el.classList.contains('on'); if (await safe(() => api(`/api/business/chats/${c.chat_id}/pause`, { method: 'POST', body: { excluded: !on } }))) el.classList.toggle('on', on); }),
      ))) : empty(t('no_chats')));
      const wrap = h('div', { class: 'stack' }, out);
      wrap.addEventListener('click', (e) => { if (e.target.closest('.switch')) e.stopPropagation(); }, true);
      return wrap;
    });
  }
  function chatLogSheet(chat) {
    openSheet(chat.name || String(chat.chat_id), (b) => {
      const log = h('div', { class: 'stack' }); b.prepend(h('div', { class: 'label', text: t('dialog_log') }), log);
      guarded(log, async () => {
        const d = await api('/api/business/log?chat_id=' + chat.chat_id + '&limit=50');
        return d.messages.length ? h('div', { class: 'stack' }, d.messages.map((m) => h('div', { class: 'msg ' + (m.role === 'client' ? 'ai' : m.role === 'owner' ? 'owner' : 'me') }, h('div', { text: m.text }), h('div', { class: 'sub', text: t('role_' + m.role) + ' · ' + fmtDT(m.at) })))) : empty(t('no_data'));
      });
    });
  }

  /* ---------- рассылки ---------- */
  async function screenBroadcasts(root) {
    const host = h('div', { class: 'stack' });
    let cache = null;
    root.append(head(t('broadcasts'), plusBtn(() => cache && broadcastSheet(null, cache, render))), host);
    await guarded(host, async () => {
      const d = cache = await api('/api/broadcasts');
      const names = Object.fromEntries(d.chats.map((c) => [c.chat_id, c.name]));
      const out = [h('div', { class: 'card sub', text: t('bc_rules') })];
      out.push(d.schedules.length ? h('div', { class: 'stack' }, d.schedules.map((s) => h('div', { class: 'card stack tap', onclick: () => broadcastSheet(s, d, render) },
        h('div', { class: 'row' }, h('div', { class: 'grow' }, h('div', { class: 'trunc', text: names[s.chat_id] || String(s.chat_id) }), h('div', { class: 'sub num', text: `${s.time} · ${daysText(s.days)} · ${s.tz.split('/').pop()}` })),
          sw(s.active, async (el) => { const on = !el.classList.contains('on'); if (await safe(() => api('/api/broadcasts/' + s.id, { method: 'PUT', body: { active: on } }))) el.classList.toggle('on', on); })),
        h('div', { class: 'row' }, pill(s.channel === 'bot' ? 'ok' : 'warn', s.channel === 'bot' ? t('via_bot') : 'Business'), pill('', s.mode === 'ai' ? 'AI' : t('text'))),
        h('div', { class: 'sub trunc', text: s.mode === 'ai' ? s.topic : s.text }),
        s.last_status ? h('div', { class: 'sub ' + bclass(s.last_status), text: bstatus(s.last_status) }) : null))) : empty(t('no_broadcasts')));
      if (d.link) out.push(h('div', { class: 'card stack' }, h('div', { class: 'label', text: t('sub_link') }), h('div', { class: 'sub', text: t('sub_link_hint') }), h('div', { class: 'mono trunc', text: d.link }),
        h('button', { class: 'btn small', onclick: async () => { try { await navigator.clipboard.writeText(d.link); toast(t('copied')); } catch (e) { toast(d.link); } }, text: t('copy') })));
      out.push(h('div', { class: 'label', text: t('history') }));
      out.push(d.log.length ? h('div', { class: 'stack' }, d.log.map((l) => h('div', { class: 'card row' }, h('div', { class: 'grow' }, h('div', { class: 'trunc', text: names[l.chat_id] || String(l.chat_id) }), h('div', { class: 'sub', text: fmtDT(l.at) + (l.detail && l.status !== 'sent' ? ' · ' + l.detail : '') })), pill(l.status === 'sent' ? 'ok' : l.status === 'manual_needed' ? 'warn' : 'bad', bstatus(l.status))))) : empty(t('no_data')));
      return h('div', { class: 'stack' }, out);
    });
  }
  function broadcastSheet(sch, data, done) {
    const isNew = !sch;
    openSheet(isNew ? t('new_broadcast') : t('edit_broadcast'), (b) => {
      const st = { chat: sch ? String(sch.chat_id) : (data.chats[0] ? String(data.chats[0].chat_id) : ''), mode: sch ? sch.mode : 'text', days: new Set(sch ? [...sch.days] : DAYS) };
      const chatSel = h('select', { id: 'b-chat' }, data.chats.map((c) => h('option', { value: String(c.chat_id), text: (c.name || c.chat_id) + ' · ' + (c.channel === 'bot' ? t('via_bot') : 'Business') })));
      const manual = h('input', { type: 'number', id: 'b-manual', inputmode: 'numeric', placeholder: t('manual_id') });
      const time = h('input', { type: 'time', id: 'b-time', value: sch ? sch.time : '10:00' });
      const text = h('textarea', { id: 'b-text', maxlength: 1000, value: sch ? sch.text : '' }), topic = h('input', { type: 'text', id: 'b-topic', maxlength: 300, value: sch ? sch.topic : '', placeholder: t('topic_placeholder') });
      const modeHost = h('div'), daysHost = h('div'), textHost = h('div');
      if (st.chat) chatSel.value = st.chat;
      const draw = () => {
        modeHost.replaceChildren(seg([['text', t('mode_text')], ['ai', t('mode_ai')]], st.mode, (k) => { st.mode = k; draw(); }));
        textHost.replaceChildren(st.mode === 'text' ? field(t('message'), text) : field(t('topic'), topic));
        daysHost.replaceChildren(h('div', { class: 'chips' }, DAYS.map((d) => h('button', { class: 'chip' + (st.days.has(d) ? ' on' : ''), onclick: () => { haptic('select'); st.days.has(d) ? st.days.delete(d) : st.days.add(d); draw(); }, text: t('wd' + d) }))));
      };
      draw();
      b.prepend(...(isNew ? [field(t('chat'), chatSel), field(t('or_manual'), manual)] : []), field(t('time'), time), h('div', { class: 'stack' }, h('span', { class: 'sub', text: t('days') }), daysHost), modeHost, textHost, h('div', { class: 'sub', text: t('bc_window') }),
        isNew ? null : h('button', { class: 'btn danger', onclick: async () => { if (await safe(() => api('/api/broadcasts/' + sch.id, { method: 'DELETE' }))) { toast(t('deleted')); S.sheets[S.sheets.length - 1].close(); done && done(); } }, text: t('delete') }));
      b._get = () => ({ chat: manual.value || chatSel.value, time: time.value, days: [...st.days].sort().join(''), mode: st.mode, text: text.value, topic: topic.value });
    }, t('save'), async (ctl) => {
      const v = ctl.body._get();
      if (!v.days) { toast(t('need_days'), true); return false; }
      const payload = { time: v.time, days: v.days, mode: v.mode, text: v.text, topic: v.topic };
      const r = isNew ? await safe(() => api('/api/broadcasts', { method: 'POST', body: { ...payload, chat_id: Number(v.chat) } })) : await safe(() => api('/api/broadcasts/' + sch.id, { method: 'PUT', body: payload }));
      if (!r) return false;
      toast(t('saved')); done && done(); return true;
    });
  }

  /* ---------- доступы (admin) ---------- */
  async function screenAccess(root) {
    const host = h('div', { class: 'stack' });
    root.append(head(t('access'), plusBtn(() => grantSheet(render))), host);
    await guarded(host, async () => {
      const d = await api('/api/access');
      return d.grants.length ? h('div', { class: 'stack' }, d.grants.map((g) => h('div', { class: 'card row' }, h('div', { class: 'grow' }, h('div', { class: 'trunc', text: g.name || (g.username ? '@' + g.username : String(g.tg_id)) }), h('div', { class: 'sub num', text: `ID ${g.tg_id} · ${t('until')} ${fmtDT(g.expires_at, false)}` })),
        h('button', { class: 'btn small danger', onclick: async () => { if (await safe(() => api('/api/access/revoke', { method: 'POST', body: { tg_id: g.tg_id } }))) { toast(t('revoked')); render(); } }, text: t('revoke') })))) : empty(t('no_grants'));
    });
  }
  function grantSheet(done) {
    openSheet(t('grant_access'), (b) => {
      const id = h('input', { type: 'number', id: 'g-id', inputmode: 'numeric', placeholder: 'Telegram ID' }), days = h('input', { type: 'number', id: 'g-days', min: 1, max: 3650, value: '30' });
      b.prepend(field(t('user_id'), id), field(t('days_n'), days), h('div', { class: 'chips' }, [7, 30, 90, 365].map((n) => h('button', { class: 'chip', onclick: () => { days.value = n; haptic('select'); }, text: n + ' ' + t('d_short') }))));
      b._get = () => ({ tg_id: Number(id.value), days: Number(days.value) });
    }, t('grant'), async (ctl) => {
      const v = ctl.body._get();
      if (!(v.tg_id > 0) || !(v.days > 0)) { toast(t('need_id_days'), true); return false; }
      if (!await safe(() => api('/api/access/grant', { method: 'POST', body: v }))) return false;
      toast(t('granted')); done && done(); return true;
    });
  }

  /* ---------- настройки ---------- */
  const ACCENTS = ['#19d3c5', '#4da3ff', '#a98bff', '#ff7aa8', '#ffb347', '#7ee07a'];
  const ZONES = ['Asia/Tashkent', 'Asia/Samarkand', 'Europe/Moscow', 'Asia/Almaty', 'Europe/Istanbul', 'Asia/Dubai', 'UTC'];
  async function screenSettings(root) {
    const host = h('div', { class: 'stack' });
    root.append(head(t('settings')), host);
    await guarded(host, async () => {
      const d = await api('/api/settings');
      const st = d.settings;
      const save = async (patch) => { const r = await safe(() => api('/api/settings', { method: 'PUT', body: patch })); if (r) { S.me.settings = r.settings; window.I18N.setLang(lang()); applyTheme(); render(); } };
      const color = h('input', { type: 'color', id: 's-color', value: st.accent, 'aria-label': t('custom_color'), style: 'width:44px;height:36px;padding:0;border:0;background:none' });
      color.addEventListener('change', () => save({ accent: color.value }));
      const out = [
        h('div', { class: 'card stack' }, h('div', { class: 'label', text: t('accent') }), h('div', { class: 'swatches' }, ACCENTS.map((c) => h('button', { class: 'swatch' + (c === st.accent ? ' on' : ''), style: `background:${c}`, 'aria-label': c, onclick: () => { haptic('select'); save({ accent: c }); } })), color)),
        h('div', { class: 'card stack' }, h('div', { class: 'label', text: t('language') }), seg([['ru', 'Русский'], ['uz', "O'zbekcha"]], st.lang, (k) => save({ lang: k }))),
        h('div', { class: 'card stack' }, field(t('timezone'), (() => { const sel = h('select', { id: 's-tz' }, [...new Set([st.tz, ...ZONES])].map((z) => h('option', { value: z, text: z }))); sel.value = st.tz; sel.addEventListener('change', () => save({ tz: sel.value })); return sel; })()),
          field(t('max_per_day'), (() => { const sel = h('select', { id: 's-max' }, [1, 2, 3, 5].map((n) => h('option', { value: String(n), text: String(n) }))); sel.value = String(st.broadcast_max_per_day); sel.addEventListener('change', () => save({ broadcast_max_per_day: Number(sel.value) })); return sel; })())),
        h('div', { class: 'card stack' }, h('div', { class: 'label', text: t('your_access') }), h('div', { class: 'row' }, pill('ok', S.me.role === 'admin' ? t('role_admin') : t('role_user')), S.me.access_until ? h('span', { class: 'sub', text: t('until') + ' ' + fmtDT(S.me.access_until, false) }) : null),
          h('div', { class: 'chips' }, S.me.perms.map((p) => h('span', { class: 'chip on', text: p })))),
      ];
      if (d.payments) {
        out.push(h('div', { class: 'card stack' }, h('div', { class: 'label', text: t('payments') }), h('div', { class: 'row' }, h('div', { class: 'grow', text: 'Telegram Stars (' + d.payments.price_xtr + ' ⭐ / ' + d.payments.days + ' ' + t('d_short') + ')' }), statusPill(d.payments.subscription_enabled ? 'ok' : 'offline')),
          h('div', { class: 'row' }, h('div', { class: 'grow', text: 'Click (' + fmtNum(d.payments.price_uzs) + ' ' + t('sum') + ')' }), statusPill(d.payments.click_enabled ? 'ok' : 'offline')), h('div', { class: 'sub', text: t('flags_hint') })));
        out.push(h('div', { class: 'card stack' }, h('div', { class: 'label', text: t('tokens') }), d.tokens.map((x) => h('div', { class: 'row' }, h('div', { class: 'grow mono', text: x.agent }), h('span', { class: 'mono sub', text: x.set ? '••••••••••' : t('not_set') }))),
          h('div', { class: 'row' }, h('div', { class: 'grow mono', text: 'GROQ_API_KEY' }), h('span', { class: 'mono sub', text: d.groq_key_set ? '••••••••••' : t('not_set') })), h('div', { class: 'sub', text: t('tokens_hint') })));
      }
      return h('div', { class: 'stack' }, out);
    });
  }

  /* ---------- ассистент ---------- */
  function openAssistant() {
    openSheet(t('assistant'), (b) => {
      const log = h('div', { class: 'stack' }, h('div', { class: 'msg ai', text: t('assistant_hello') }));
      const input = h('input', { type: 'text', id: 'a-text', maxlength: 1000, placeholder: t('assistant_ph'), enterkeyhint: 'send' });
      const send = async () => {
        const text = input.value.trim(); if (!text) return; input.value = ''; haptic();
        log.append(h('div', { class: 'msg me', text }));
        const wait = h('div', { class: 'msg ai', text: '…' }); log.append(wait);
        try { const r = await api('/api/assistant', { method: 'POST', body: { text } }); wait.textContent = r.text || '✓'; haptic('ok'); }
        catch (e) { wait.textContent = e.message; wait.classList.add('down'); haptic('error'); }
        wait.scrollIntoView({ block: 'nearest' });
      };
      input.addEventListener('keydown', (e) => { if (e.key === 'Enter') send(); });
      b.append(log, h('div', { class: 'row' }, input, h('button', { class: 'icon-btn', 'aria-label': t('send'), onclick: send }, icon('send', 20))));
    });
  }

  /* ---------- «Ещё» ---------- */
  function screenMore(root) {
    const admin = S.me.role === 'admin';
    const items = [['reminders', t('reminders'), t('m_reminders')], ['advice', t('ai_advice'), t('m_advice')], ['broadcasts', t('broadcasts'), t('m_broadcasts')]];
    if (admin) items.push(['agents', t('agents'), t('m_agents')], ['access', t('access'), t('m_access')]);
    items.push(['settings', t('settings'), t('m_settings')]);
    root.append(head(t('more')), h('div', { class: 'stack' }, h('div', { class: 'card tap row', onclick: openAssistant }, h('div', { class: 'grow' }, h('div', { text: t('assistant') }), h('div', { class: 'sub', text: t('m_assistant') })), icon('send', 18)),
      items.map(([k, title, sub]) => h('div', { class: 'card tap row', onclick: () => { haptic(); push(k); } }, h('div', { class: 'grow' }, h('div', { text: title }), h('div', { class: 'sub', text: sub })), h('span', { class: 'sub', text: '›' })))));
  }

  /* ---------- маршрутизация и запуск ---------- */
  const SCREENS = { home: screenHome, tasks: screenTasks, stats: screenStats, business: screenBusiness, more: screenMore, reminders: screenReminders, advice: screenAdvice, agents: screenAgents, broadcasts: screenBroadcasts, access: screenAccess, settings: screenSettings };
  const TABS = [['home', 'home', 'nav_home'], ['tasks', 'tasks', 'nav_tasks'], ['stats', 'stats', 'nav_stats'], ['business', 'business', 'nav_business'], ['more', 'more', 'nav_more']];
  function switchTab(name) { S.stack = []; S.tab = name; render(); }
  function push(name) { S.stack.push(name); render(); }
  function render() {
    if (S.core) { S.core.destroy(); S.core = null; }
    S.sheets.slice().forEach((s) => s.close());
    const name = S.stack.length ? S.stack[S.stack.length - 1] : S.tab;
    const screen = h('main', { class: 'screen', 'data-screen': name });
    const nav = h('nav', { class: 'nav', 'aria-label': 'Navigation' }, TABS.map(([k, ic, label]) => h('button', { class: !S.stack.length && S.tab === k ? 'on' : '', 'aria-label': t(label), onclick: () => { if (S.tab !== k || S.stack.length) { haptic('select'); switchTab(k); } } }, icon(ic), t(label))));
    app.replaceChildren(screen, nav);
    window.scrollTo(0, 0); syncBack();
    SCREENS[name](screen);
  }
  function gate(title, text, extra) { app.replaceChildren(h('div', { class: 'gate' }, h('h1', { text: title }), h('p', { class: 'sub', text }), extra || null)); }

  async function boot() {
    if (!tg || !tg.initData) { gate(t('open_in_tg'), t('open_in_tg_hint')); return; }
    try { tg.ready(); tg.expand(); tg.disableVerticalSwipes && tg.disableVerticalSwipes(); } catch (e) { /* */ }
    try {
      tg.BackButton.onClick(onBack); tg.MainButton.onClick(onMain);
      const insets = () => { const c = tg.contentSafeAreaInset || {}; document.documentElement.style.setProperty('--tg-top', ((c.top || 0) + (tg.safeAreaInset ? tg.safeAreaInset.top || 0 : 0)) + 'px'); };
      insets(); tg.onEvent('contentSafeAreaChanged', insets); tg.onEvent('safeAreaChanged', insets);
    } catch (e) { /* старый клиент */ }
    app.replaceChildren(loading());
    try { S.me = await api('/api/me'); }
    catch (e) {
      if (e.code === 'no_access') { gate(t('no_access'), e.message, h('div', { class: 'sub', text: t('no_access_hint') })); return; }
      gate(t('err_generic'), e.message, h('button', { class: 'btn', onclick: () => location.reload(), text: t('retry') })); return;
    }
    window.I18N.setLang(lang()); applyTheme();
    const want = new URLSearchParams(location.search).get('s');   // «открой аналитику» из чата: /?s=stats
    if (want && ['tasks', 'stats', 'business'].includes(want)) S.tab = want;
    else if (want && SCREENS[want] && want !== 'home') S.stack = [want];
    render();
  }
  window.__kimo = { S, render, api };
  document.addEventListener('DOMContentLoaded', boot);
  if (document.readyState !== 'loading') boot();
})();
