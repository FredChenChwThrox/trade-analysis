/* 多股对比页（task-07）：2~6 股单指标折线、标准化、区间统计、URL 同步。 */
(function () {
  'use strict';
  const UI = window.UI;

  const state = { symbols: [], metric: 'pe_ttm', granularity: 'daily', price: 'unadjusted',
                  start: null, end: null, normalize: false };
  let symbolNames = {};
  let chart = null;
  let lastData = null;

  function readURL() {
    const p = new URLSearchParams(location.search);
    // 兼容逗号拼接（列表页"对比选中"跳转）与重复参数两种形式
    state.symbols = p.getAll('symbols')
      .flatMap((v) => v.split(',')).filter(Boolean).slice(0, 6);
    if (p.get('metric')) state.metric = p.get('metric');
    if (p.get('granularity')) state.granularity = p.get('granularity');
    if (p.get('price')) state.price = p.get('price');
    state.start = p.get('start');
    state.end = p.get('end');
    state.normalize = p.get('normalize') === '1';
  }

  function syncURL() {
    const params = { symbols: state.symbols, metric: state.metric,
                     granularity: state.granularity, price: state.price,
                     start: state.start, end: state.end, normalize: state.normalize ? '1' : null };
    history.replaceState(null, '', '/compare' + UI.buildQueryString(params));
  }

  function renderTags() {
    const box = document.getElementById('cmp-tags');
    box.innerHTML = state.symbols.map((s) =>
      `<span class="inline-flex items-center gap-1 bg-blue-50 border border-blue-200 rounded px-2 py-0.5 text-xs">${UI.escapeHtml(s)} ${UI.escapeHtml(symbolNames[s] || '')}
        <button class="text-gray-400 hover:text-red-500 cmp-remove" data-s="${UI.escapeHtml(s)}">&times;</button></span>`).join('');
    box.querySelectorAll('.cmp-remove').forEach((btn) => {
      btn.addEventListener('click', () => {
        state.symbols = state.symbols.filter((s) => s !== btn.dataset.s);
        renderTags(); syncURL(); if (state.symbols.length >= 2) load(); 
      });
    });
  }

  function applyControls() {
    document.getElementById('cmp-metric').value = state.metric;
    document.getElementById('cmp-granularity').value = state.granularity;
    document.getElementById('cmp-price').value = state.price;
    document.getElementById('cmp-start').value = state.start || '';
    document.getElementById('cmp-end').value = state.end || '';
    document.getElementById('cmp-normalize').checked = state.normalize;
  }

  async function load() {
    if (state.symbols.length < 2) {
      document.getElementById('cmp-chart').style.display = 'none';
      document.getElementById('cmp-empty').classList.remove('hidden');
      return;
    }
    document.getElementById('cmp-empty').classList.add('hidden');
    document.getElementById('cmp-chart').style.display = 'block';
    syncURL();
    try {
      const params = { symbols: state.symbols.join(','), metric: state.metric,
                       granularity: state.granularity, price: state.price,
                       start: state.start, end: state.end };
      const data = await UI.fetchJSON('/api/compare' + UI.buildQueryString(params));
      symbolNames = {};
      Object.values(data.metadata).forEach((m) => { symbolNames[data.symbols.find((s) => data.metadata[s] === m)] = m.name; });
      data.symbols.forEach((s) => { symbolNames[s] = data.metadata[s].name; });
      lastData = data;
      renderChart(data);
      renderStats(data);
      renderTags();
    } catch (e) {
      UI.showToast(e.message, 'error');
    }
  }

  function renderChart(data) {
    const el = document.getElementById('cmp-chart');
    if (!chart) chart = echarts.init(el);
    const colors = ['#5470c6', '#91cc75', '#fac858', '#ee6666', '#73c0de', '#3ba272'];
    const series = data.symbols.map((s, i) => {
      let values = data.series[s];
      if (state.normalize) {
        const base = values.find((v) => v != null);
        if (base != null && base !== 0) values = values.map((v) => (v == null ? null : v / base * 100));
      }
      return { name: symbolNames[s] ? symbolNames[s] + '·' + s : s, type: 'line',
               data: values.map((v, idx) => (v == null ? null : [data.dates[idx], v])),
               showSymbol: false, connectNulls: true,
               lineStyle: { width: 1.5 }, itemStyle: { color: colors[i % colors.length] } };
    });
    chart.setOption({
      tooltip: { trigger: 'axis' },
      legend: { data: series.map((s) => s.name), top: 0 },
      toolbox: { feature: { dataZoom: {}, saveAsImage: {} }, right: 10, top: 0 },
      grid: { left: 60, right: 20, top: 40, bottom: 60 },
      xAxis: { type: 'time', axisPointer: { label: { formatter: (p) => UI.formatDate(p.value) } } },
      yAxis: { scale: true, splitLine: { lineStyle: { type: 'dashed' } } },
      dataZoom: [{ type: 'inside', start: 0, end: 100 },
                 { type: 'slider', bottom: 8, height: 22 }],
      series,
    }, true);  // notMerge：移除股票后清除残留 series
    chart.resize();
  }

  function renderStats(data) {
    const tbody = document.getElementById('cmp-stats-tbody');
    tbody.innerHTML = data.symbols.map((s, i) => {
      const vals = data.series[s].filter((v) => v != null);
      const first = vals[0], last = vals[vals.length - 1];
      const chg = (first != null && first !== 0 && last != null) ? ((last - first) / Math.abs(first)) * 100 : null;
      const max = vals.length ? Math.max(...vals) : null;
      const min = vals.length ? Math.min(...vals) : null;
      const mean = vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : null;
      const std = vals.length > 1
        ? Math.sqrt(vals.reduce((a, b) => a + (b - mean) * (b - mean), 0) / (vals.length - 1)) : null;
      const color = i % 6;
      return `<tr>
        <td><span class="inline-block w-2 h-2 rounded-full mr-1" style="background:${['#5470c6','#91cc75','#fac858','#ee6666','#73c0de','#3ba272'][color]}"></span>${UI.escapeHtml(symbolNames[s] || s)}</td>
        <td class="font-mono">${UI.formatNumber(last)}</td>
        <td class="font-mono">${UI.formatNumber(first)}</td>
        <td class="${chg === null ? '' : (chg >= 0 ? 'text-red-600' : 'text-green-600')}">${chg === null ? '—' : UI.formatNumber(chg) + '%'}</td>
        <td class="font-mono">${UI.formatNumber(max)}</td>
        <td class="font-mono">${UI.formatNumber(min)}</td>
        <td class="font-mono">${UI.formatNumber(mean)}</td>
        <td class="font-mono">${UI.formatNumber(std)}</td>
      </tr>`;
    }).join('');
  }

  function init() {
    readURL();
    applyControls();
    renderTags();
    const box = document.getElementById('cmp-search-box');
    box.innerHTML = '<input id="cmp-symbol-input" type="text" placeholder="搜索并添加股票…" class="w-full px-2 py-1 rounded border border-gray-300">';
    UI.initStockSearch('cmp-symbol-input', (it) => {
      if (!state.symbols.includes(it.symbol) && state.symbols.length < 6) {
        state.symbols.push(it.symbol);
        symbolNames[it.symbol] = it.name;
        renderTags();
        syncURL();
      }
    });
    document.getElementById('cmp-metric').addEventListener('change', (e) => { state.metric = e.target.value; if (state.symbols.length >= 2) load(); });
    document.getElementById('cmp-granularity').addEventListener('change', (e) => { state.granularity = e.target.value; if (state.symbols.length >= 2) load(); });
    document.getElementById('cmp-price').addEventListener('change', (e) => { state.price = e.target.value; if (state.symbols.length >= 2) load(); });
    document.getElementById('cmp-start').addEventListener('change', (e) => { state.start = e.target.value || null; if (state.symbols.length >= 2) load(); });
    document.getElementById('cmp-end').addEventListener('change', (e) => { state.end = e.target.value || null; if (state.symbols.length >= 2) load(); });
    document.getElementById('cmp-normalize').addEventListener('change', (e) => {
      state.normalize = e.target.checked;
      if (lastData) renderChart(lastData);
      syncURL();
    });
    document.getElementById('cmp-load').addEventListener('click', () => load());
    window.addEventListener('resize', () => { if (chart) chart.resize(); });
    if (state.symbols.length) load();
  }

  document.addEventListener('DOMContentLoaded', init);
})();
