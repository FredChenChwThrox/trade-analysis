/* 纯指标分析页（task-05）：1~6 股 × 1~6 指标，逐指标子图，CSV 导出，URL 同步。 */
(function () {
  'use strict';
  const UI = window.UI;
  const GROUP = 'indicators-group';
  const MAX_SYMBOLS = 6;
  const MAX_FIELDS = 6;

  const state = {
    symbols: [], fields: [], granularity: 'daily', price: 'unadjusted',
    start: null, end: null,
  };

  function readURL() {
    const p = new URLSearchParams(location.search);
    const syms = p.getAll('symbols');
    // 兼容逗号拼接（列表页跳转）与重复参数两种形式
    if (syms.length) state.symbols = syms
      .flatMap((v) => v.split(',')).filter(Boolean).slice(0, MAX_SYMBOLS);
    const fs = p.get('fields');
    if (fs) state.fields = fs.split(',').slice(0, MAX_FIELDS);
    if (p.get('granularity')) state.granularity = p.get('granularity');
    if (p.get('price')) state.price = p.get('price');
    state.start = p.get('start');
    state.end = p.get('end');
  }

  function syncURL() {
    const params = {
      symbols: state.symbols, fields: state.fields.length ? state.fields.join(',') : null,
      granularity: state.granularity, price: state.price, start: state.start, end: state.end,
    };
    history.replaceState(null, '', '/indicators' + UI.buildQueryString(params));
  }

  function renderTags() {
    const box = document.getElementById('ind-symbol-tags');
    box.innerHTML = state.symbols.map((s) => {
      const name = symbolNames[s] || s;
      return `<span class="inline-flex items-center gap-1 bg-blue-50 border border-blue-200 rounded px-2 py-0.5 text-xs">
        ${UI.escapeHtml(s)} ${UI.escapeHtml(name)}
        <button class="text-gray-400 hover:text-red-500 ind-remove" data-s="${s}">&times;</button></span>`;
    }).join('');
    box.querySelectorAll('.ind-remove').forEach((btn) => {
      btn.addEventListener('click', () => {
        state.symbols = state.symbols.filter((s) => s !== btn.dataset.s);
        renderTags();
        syncURL();
      });
    });
  }

  let symbolNames = {};

  async function load() {
    if (!state.symbols.length || !state.fields.length) {
      document.getElementById('ind-charts').innerHTML = '';
      document.getElementById('ind-empty').classList.remove('hidden');
      return;
    }
    syncURL();
    const box = document.getElementById('ind-charts');
    box.innerHTML = '<p class="text-gray-400 text-sm">加载中…</p>';
    document.getElementById('ind-empty').classList.add('hidden');
    try {
      const params = {
        symbols: state.symbols.join(','), fields: state.fields.join(','),
        granularity: state.granularity, price: state.price,
        start: state.start, end: state.end,
      };
      const data = await UI.fetchJSON('/api/indicators' + UI.buildQueryString(params));
      symbolNames = {};
      (await UI.fetchJSON('/api/stocks/search?limit=50')).items.forEach((it) => {
        symbolNames[it.symbol] = it.name;
      });
      renderCharts(data);
      return data;
    } catch (e) {
      box.innerHTML = `<p class="text-red-500 text-sm">${UI.escapeHtml(e.message)}</p>`;
    }
  }

  function renderCharts(data) {
    const box = document.getElementById('ind-charts');
    const colors = ['#5470c6', '#91cc75', '#fac858', '#ee6666', '#73c0de', '#3ba272'];
    const charts = [];
    box.innerHTML = data.fields.map((f, idx) => `
      <div class="card">
        <h3 class="card-title">${UI.escapeHtml(f)}</h3>
        <div class="chart-box-sm" id="ind-chart-${idx}"></div>
      </div>`).join('');

    data.fields.forEach((f, idx) => {
      const el = document.getElementById('ind-chart-' + idx);
      const chart = echarts.init(el);
      chart.group = GROUP;
      const series = data.symbols.map((s, i) => ({
        name: (symbolNames[s] || s) + '·' + s, type: 'line',
        data: data.series[f] ? Object.entries(data.series[f])
          .sort((a, b) => a[0] < b[0] ? -1 : 1)
          .map(([d, m]) => ({ value: [d, m[s]] })) : [],
        showSymbol: false, connectNulls: true,
        lineStyle: { width: 1.5 }, itemStyle: { color: colors[i % colors.length] },
      }));
      chart.setOption({
        tooltip: { trigger: 'axis' },
        legend: { data: series.map((s) => s.name), top: 0 },
        grid: { left: 60, right: 16, top: 30, bottom: 45 },
        xAxis: { type: 'time', axisPointer: { label: { formatter: (p) => UI.formatDate(p.value) } } },
        yAxis: { scale: true, splitLine: { lineStyle: { type: 'dashed' } } },
        dataZoom: [{ type: 'inside', start: 0, end: 100 }],
        series,
      });
      charts.push(chart);
    });
    echarts.connect(GROUP, 'dataZoom');
  }

  function exportCSV() {
    if (!state.symbols.length || !state.fields.length) return;
    load().then((data) => {
      if (!data) return;
      const dates = new Set();
      data.fields.forEach((f) => Object.keys(data.series[f] || {}).forEach((d) => dates.add(d)));
      const sorted = Array.from(dates).sort();
      const header = ['date'].concat(data.fields.map((f) => f));
      const rows = sorted.map((d) => {
        const row = [d];
        data.fields.forEach((f) => {
          const bySym = data.series[f][d] || {};
          row.push(state.symbols.map((s) => bySym[s] === undefined ? '' : bySym[s]).join('|'));
        });
        return row;
      });
      const csv = [header, ...rows].map((r) => r.join(',')).join('\n');
      const blob = new Blob(['\ufeff' + csv], { type: 'text/csv;charset=utf-8' });
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = `indicators_${state.symbols.join('_')}_${state.granularity}.csv`;
      a.click();
      URL.revokeObjectURL(a.href);
    });
  }

  function applyControls() {
    document.getElementById('ind-granularity').value = state.granularity;
    document.getElementById('ind-price').value = state.price;
    document.getElementById('ind-start').value = state.start || '';
    document.getElementById('ind-end').value = state.end || '';
    document.querySelectorAll('.ind-field-cb').forEach((cb) => { cb.checked = state.fields.includes(cb.value); });
  }

  function init() {
    readURL();
    applyControls();
    renderTags();

    const searchEl = document.getElementById('ind-symbol-search');
    searchEl.innerHTML = '<input id="ind-symbol-input" type="text" placeholder="搜索并添加股票…" class="w-full px-2 py-1 rounded border border-gray-300">';
    UI.initStockSearch('ind-symbol-input', (it) => {
      if (!state.symbols.includes(it.symbol) && state.symbols.length < MAX_SYMBOLS) {
        state.symbols.push(it.symbol);
        renderTags();
        syncURL();
      }
    });

    document.getElementById('ind-granularity').addEventListener('change', (e) => { state.granularity = e.target.value; syncURL(); });
    document.getElementById('ind-price').addEventListener('change', (e) => { state.price = e.target.value; syncURL(); });
    document.getElementById('ind-start').addEventListener('change', (e) => { state.start = e.target.value || null; syncURL(); });
    document.getElementById('ind-end').addEventListener('change', (e) => { state.end = e.target.value || null; syncURL(); });
    document.querySelectorAll('.ind-field-cb').forEach((cb) => {
      cb.addEventListener('change', () => {
        state.fields = Array.from(document.querySelectorAll('.ind-field-cb:checked'))
          .map((c) => c.value).slice(0, MAX_FIELDS);
        syncURL();
      });
    });
    document.getElementById('ind-apply').addEventListener('click', () => load());
    document.getElementById('ind-clear').addEventListener('click', () => {
      state.symbols = []; state.fields = [];
      renderTags();
      document.getElementById('ind-charts').innerHTML = '';
      document.getElementById('ind-empty').classList.remove('hidden');
      syncURL();
    });
    document.getElementById('ind-csv').addEventListener('click', exportCSV);
    window.addEventListener('resize', () => {
      document.querySelectorAll('[id^=ind-chart-]').forEach((el) => {
        const c = echarts.getInstanceByDom(el);
        if (c) c.resize();
      });
    });

    if (state.symbols.length || state.fields.length) load();
  }

  document.addEventListener('DOMContentLoaded', init);
})();
