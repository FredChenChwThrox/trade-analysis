/* 信号时间轴页（task-06）：多条件筛选、分页、详情展开、分组/时间轴视图、URL 同步。 */
(function () {
  'use strict';
  const UI = window.UI;

  const state = {
    symbols: [], signals: [], states: [], triggered: null,
    start: null, end: null, anchor: null,
    page: 1, page_size: 100, sort: 'observed_on', order: 'desc', view: 'list',
  };

  function readURL() {
    const p = new URLSearchParams(location.search);
    state.symbols = p.getAll('symbols');
    state.signals = p.getAll('signals');
    state.states = p.getAll('states');
    const t = p.get('triggered');
    state.triggered = (t === '1' || t === '0') ? (t === '1') : null;
    state.start = p.get('start');
    state.end = p.get('end');
    state.anchor = p.get('anchor_id');
    state.page = parseInt(p.get('page') || '1', 10);
    state.page_size = parseInt(p.get('page_size') || '100', 10);
    state.sort = p.get('sort') || 'observed_on';
    state.order = p.get('order') || 'desc';
    state.view = p.get('view') || 'list';
  }

  function syncURL() {
    const params = {
      symbols: state.symbols, signals: state.signals, states: state.states,
      triggered: state.triggered === null ? null : (state.triggered ? '1' : '0'),
      start: state.start, end: state.end, anchor_id: state.anchor,
      page: state.page, page_size: state.page_size, sort: state.sort, order: state.order,
      view: state.view,
    };
    history.replaceState(null, '', '/signals' + UI.buildQueryString(params));
  }

  async function loadOptions() {
    const data = await UI.fetchJSON('/api/signals?page_size=1');
    const types = (await UI.fetchJSON('/api/signals?page_size=1')).items;
    const signalTypes = await fetchDistinct('signal');
    const states = await fetchDistinct('state');
    renderTypeChecks(signalTypes);
    renderStateChecks(states);
  }

  async function fetchDistinct(col) {
    // 通过大数据量分页 + 服务端返回排序近似获取 distinct；简化：调用 200 条取去重
    const data = await UI.fetchJSON('/api/signals?page_size=200');
    return Array.from(new Set(data.items.map((it) => it[col]))).sort();
  }

  function renderTypeChecks(types) {
    const box = document.getElementById('sig-type-checks');
    box.innerHTML = types.map((t) =>
      `<label class="inline-flex items-center gap-1"><input type="checkbox" class="sig-type-cb" value="${UI.escapeHtml(t)}" ${state.signals.includes(t) ? 'checked' : ''}> ${UI.escapeHtml(t)}</label>`
    ).join('');
  }

  function renderStateChecks(states) {
    const box = document.getElementById('sig-state-checks');
    box.innerHTML = states.map((t) =>
      `<label class="inline-flex items-center gap-1"><input type="checkbox" class="sig-state-cb" value="${UI.escapeHtml(t)}" ${state.states.includes(t) ? 'checked' : ''}> ${UI.escapeHtml(t)}</label>`
    ).join('');
  }

  function renderSymbolTags() {
    const box = document.getElementById('sig-symbol-tags');
    box.innerHTML = state.symbols.map((s) =>
      `<span class="inline-flex items-center gap-1 bg-blue-50 border border-blue-200 rounded px-2 py-0.5 text-xs">${UI.escapeHtml(s)}
        <button class="text-gray-400 hover:text-red-500 sig-remove" data-s="${UI.escapeHtml(s)}">&times;</button></span>`).join('');
    box.querySelectorAll('.sig-remove').forEach((btn) => {
      btn.addEventListener('click', () => {
        state.symbols = state.symbols.filter((s) => s !== btn.dataset.s);
        renderSymbolTags();
        syncURL();
      });
    });
  }

  function applyControls() {
    document.getElementById('sig-triggered').value =
      state.triggered === null ? '' : (state.triggered ? '1' : '0');
    document.getElementById('sig-start').value = state.start || '';
    document.getElementById('sig-end').value = state.end || '';
    document.getElementById('sig-anchor').value = state.anchor || '';
    document.getElementById('sig-view').value = state.view;
    document.getElementById('sig-page-size').value = String(state.page_size);
  }

  async function load() {
    syncURL();
    const params = {
      symbols: state.symbols.join(','), signals: state.signals.join(','),
      states: state.states.join(','),
      triggered: state.triggered === null ? null : (state.triggered ? '1' : '0'),
      start: state.start, end: state.end, anchor_id: state.anchor,
      page: state.page, page_size: state.page_size, sort: state.sort, order: state.order,
    };
    const tbody = document.getElementById('sig-tbody');
    tbody.innerHTML = '<tr><td colspan="8" class="text-center text-gray-400 py-6">加载中…</td></tr>';
    try {
      const data = await UI.fetchJSON('/api/signals' + UI.buildQueryString(params));
      render(data);
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="8" class="text-center text-red-500 py-6">${UI.escapeHtml(e.message)}</td></tr>`;
    }
  }

  function rowHtml(s) {
    const priceLink = `/stock/${s.symbol}?start=${encodeURIComponent((s.observed_on || '') )}`;
    return `<tr>
      <td>${UI.formatDate(s.observed_on)}</td>
      <td><a class="text-blue-600 hover:underline" href="${priceLink}">${s.symbol}</a></td>
      <td>${UI.escapeHtml(s.signal)}</td>
      <td>${UI.renderStatusBadge(s.state)}</td>
      <td>${s.triggered ? '<span class="text-green-600">✓</span>' : '<span class="text-gray-400">—</span>'}</td>
      <td>${UI.formatDate(s.active_until)}</td>
      <td>${s.anchor_id || '—'}</td>
      <td><button class="sig-detail px-2 py-1 text-xs rounded border border-gray-300 hover:bg-gray-100" data-id="${s.fact_id}">详情</button></td>
    </tr>`;
  }

  function render(data) {
    const tbody = document.getElementById('sig-tbody');
    document.getElementById('sig-total').textContent = `共 ${data.total} 条`;
    document.getElementById('sig-page-info').textContent =
      data.total ? `第 ${data.page}/${Math.max(1, Math.ceil(data.total / data.page_size))} 页` : '';
    document.getElementById('sig-prev').disabled = data.page <= 1;
    document.getElementById('sig-next').disabled = data.page * data.page_size >= data.total;
    document.getElementById('sig-empty').classList.toggle('hidden', data.items.length > 0);

    if (!data.items.length) { tbody.innerHTML = ''; return; }

    if (state.view === 'by_date') {
      const groups = {};
      data.items.forEach((s) => { (groups[s.observed_on] = groups[s.observed_on] || []).push(s); });
      tbody.innerHTML = Object.keys(groups).sort().reverse().map((d) =>
        `<tr class="bg-gray-50"><td colspan="8" class="font-semibold">${d}（${groups[d].length}）</td></tr>`
        + groups[d].map(rowHtml).join('')).join('');
    } else if (state.view === 'by_symbol') {
      const groups = {};
      data.items.forEach((s) => { (groups[s.symbol] = groups[s.symbol] || []).push(s); });
      tbody.innerHTML = Object.keys(groups).sort().map((sym) =>
        `<tr class="bg-gray-50"><td colspan="8" class="font-semibold">${UI.escapeHtml(sym)}（${groups[sym].length}）</td></tr>`
        + groups[sym].map(rowHtml).join('')).join('');
    } else {
      tbody.innerHTML = data.items.map(rowHtml).join('');
    }
    tbody.querySelectorAll('.sig-detail').forEach((btn) => {
      btn.addEventListener('click', () => showDetail(btn.dataset.id));
    });
  }

  async function showDetail(factId) {
    try {
      const d = await UI.fetchJSON('/api/signals/' + factId);
      showModal(d.signal + ' · ' + d.observed_on + ' · ' + d.symbol, `
        ${UI.renderStatusBadge(d.state)} ${d.triggered ? '<span class="text-green-600">已触发</span>' : '<span class="text-gray-400">未触发</span>'}
        <pre class="json-view mt-2">${UI.escapeHtml(JSON.stringify(d.details, null, 2))}</pre>
        ${d.anchor ? `<div class="text-xs mt-1">anchor #${d.anchor.anchor_id}：${d.anchor.anchor_type} @ ${UI.formatDate(d.anchor.trade_date)}（复权 ${UI.formatNumber(d.anchor.adjusted_price)} / 不复权 ${UI.formatNumber(d.anchor.raw_price)}）</div>` : ''}
        <div class="text-xs text-gray-400 mt-1">run_id=${UI.escapeHtml(d.run_id || '')} · rule=${UI.escapeHtml(d.rule_version || '')} · config=${UI.escapeHtml((d.config_hash || '').slice(0, 8))}</div>
        <a class="mt-2 inline-block text-xs text-blue-600" href="/stock/${d.symbol}?start=${d.observed_on}">查看单股图 →</a>`);
    } catch (e) {
      UI.showToast(e.message, 'error');
    }
  }

  function showModal(title, body) {
    let m = document.getElementById('sig-modal');
    if (!m) {
      m = document.createElement('div');
      m.id = 'sig-modal';
      m.className = 'fixed inset-0 bg-black bg-opacity-40 flex items-center justify-center z-40';
      m.innerHTML = `<div class="bg-white rounded-lg shadow-xl max-w-2xl w-full mx-4 max-h-[80vh] flex flex-col">
        <div class="flex items-center justify-between px-4 py-3 border-b border-gray-200"><h3 id="sig-modal-title" class="font-semibold text-sm"></h3><button id="sig-modal-close" class="text-gray-400 hover:text-gray-700 text-xl">&times;</button></div>
        <div id="sig-modal-body" class="p-4 overflow-auto"></div></div>`;
      document.body.appendChild(m);
      m.addEventListener('click', (e) => { if (e.target === m) m.classList.add('hidden'); });
      m.querySelector('#sig-modal-close').addEventListener('click', () => m.classList.add('hidden'));
    }
    m.querySelector('#sig-modal-title').textContent = title;
    m.querySelector('#sig-modal-body').innerHTML = body;
    m.classList.remove('hidden');
  }

  function renderTimeline() {
    const wrap = document.getElementById('sig-timeline');
    wrap.innerHTML = '<div id="sig-timeline-chart" class="chart-box"></div>';
    wrap.classList.remove('hidden');
    const chart = echarts.init(document.getElementById('sig-timeline-chart'));
    UI.fetchJSON('/api/signals?page_size=200').then((data) => {
      const types = Array.from(new Set(data.items.map((it) => it.signal)));
      const typeIndex = {};
      types.forEach((t, i) => { typeIndex[t] = i; });
      const colorByState = { active: '#3b82f6', triggered: '#22c55e', inactive: '#9ca3af',
                             incomplete: '#f59e0b', watching: '#06b6d4', confirmed: '#22c55e',
                             idle: '#9ca3af', failed: '#ef4444' };
      chart.setOption({
        tooltip: { trigger: 'item' },
        grid: { left: 80, right: 20, top: 20, bottom: 60 },
        xAxis: { type: 'time' },
        yAxis: { type: 'category', data: types },
        series: [{
          type: 'scatter',
          data: data.items.map((s) => ({
            value: [s.observed_on, typeIndex[s.signal]],
            symbolSize: s.triggered ? 14 : 8,
            itemStyle: { color: colorByState[s.state] || '#64748b' },
            name: s.signal + ' · ' + s.symbol,
          })),
          label: { show: true, formatter: (p) => p.data.name, position: 'right', fontSize: 9 },
        }],
      });
    });
  }

  function init() {
    readURL();
    applyControls();
    renderSymbolTags();
    loadOptions().catch((e) => UI.showToast(e.message, 'error'));

    const searchBox = document.getElementById('sig-search-box');
    searchBox.innerHTML = '<input id="sig-symbol-input" type="text" placeholder="搜索并添加股票…" class="w-full px-2 py-1 rounded border border-gray-300">';
    UI.initStockSearch('sig-symbol-input', (it) => {
      if (!state.symbols.includes(it.symbol)) { state.symbols.push(it.symbol); renderSymbolTags(); syncURL(); }
    });

    document.getElementById('sig-type-checks').addEventListener('change', (e) => {
      if (e.target.classList.contains('sig-type-cb')) {
        state.signals = Array.from(document.querySelectorAll('.sig-type-cb:checked')).map((c) => c.value);
      }
    });
    document.getElementById('sig-state-checks').addEventListener('change', (e) => {
      if (e.target.classList.contains('sig-state-cb')) {
        state.states = Array.from(document.querySelectorAll('.sig-state-cb:checked')).map((c) => c.value);
      }
    });
    document.getElementById('sig-triggered').addEventListener('change', (e) => {
      state.triggered = e.target.value === '' ? null : (e.target.value === '1');
    });
    document.getElementById('sig-start').addEventListener('change', (e) => { state.start = e.target.value || null; });
    document.getElementById('sig-end').addEventListener('change', (e) => { state.end = e.target.value || null; });
    document.getElementById('sig-anchor').addEventListener('change', (e) => { state.anchor = e.target.value || null; });
    document.getElementById('sig-apply').addEventListener('click', () => { state.page = 1; load(); });
    document.getElementById('sig-reset').addEventListener('click', () => {
      state.symbols = []; state.signals = []; state.states = []; state.triggered = null;
      state.start = state.end = state.anchor = null; state.page = 1;
      applyControls(); renderSymbolTags();
      document.querySelectorAll('.sig-type-cb, .sig-state-cb').forEach((cb) => { cb.checked = false; });
      load();
    });
    document.getElementById('sig-prev').addEventListener('click', () => { if (state.page > 1) { state.page--; load(); } });
    document.getElementById('sig-next').addEventListener('click', () => { state.page++; load(); });
    document.getElementById('sig-page-size').addEventListener('change', (e) => {
      state.page_size = parseInt(e.target.value, 10); state.page = 1; load();
    });
    document.getElementById('sig-view').addEventListener('change', (e) => { state.view = e.target.value; load(); });
    document.getElementById('sig-timeline-btn').addEventListener('click', () => {
      const wrap = document.getElementById('sig-timeline');
      if (wrap.classList.contains('hidden')) renderTimeline();
      else wrap.classList.add('hidden');
    });
    document.querySelectorAll('th.sortable').forEach((th) => {
      th.addEventListener('click', () => {
        const s = th.dataset.sort;
        if (state.sort === s) state.order = state.order === 'desc' ? 'asc' : 'desc';
        else { state.sort = s; state.order = 'desc'; }
        state.page = 1;
        load();
      });
    });
    load();
  }

  document.addEventListener('DOMContentLoaded', init);
})();
