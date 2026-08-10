/* 单股仪表板（task-04）：主图 + 副图 + 信号 + 卡片 + 执行，ECharts 联动缩放。 */
(function () {
  'use strict';
  const UI = window.UI;
  const GROUP = 'stock-dashboard';

  const state = {
    symbol: document.getElementById('stock-page').dataset.symbol,
    meta: JSON.parse(document.getElementById('stock-page').dataset.meta || '{}'),
    granularity: 'daily',
    price: 'unadjusted',
    chartType: 'line',
    start: null,
    end: null,
    ma: ['ma5', 'ma10', 'ma20', 'ma60'],
    panels: ['volume', 'macd', 'rsi', 'kdj'],
    cardMarkers: true,
    execMarkers: false,
  };

  let bars = [];
  let indicators = [];
  let signals = [];
  let executions = [];

  const PRICE_NOTE = {
    unadjusted: '当前显示：不复权价格；指标已按当日复权因子折回（可与卡片价区/证伪线对比）',
    fully_adjusted: '当前显示：完全复权价格；指标为原始复权值（技术面连续比较）；卡片/执行标记为不复权口径，本模式下不显示（§5.1 口径纪律）',
  };

  function readURL() {
    const p = new URLSearchParams(location.search);
    if (p.get('granularity')) state.granularity = p.get('granularity');
    if (p.get('price')) state.price = p.get('price');
    if (p.get('chart')) state.chartType = p.get('chart');
    state.start = p.get('start');
    state.end = p.get('end');
    if (p.get('ma')) state.ma = p.get('ma').split(',');
    if (p.get('panels')) state.panels = p.get('panels').split(',');
    state.cardMarkers = p.get('cards') !== '0';
    state.execMarkers = p.get('exec') === '1';
  }

  function syncURL() {
    const params = {
      start: state.start, end: state.end,
      granularity: state.granularity, price: state.price, chart: state.chartType,
      ma: state.ma.length ? state.ma.join(',') : null,
      panels: state.panels.join(','),
      cards: state.cardMarkers ? '1' : '0', exec: state.execMarkers ? '1' : '0',
    };
    history.replaceState(null, '', '/stock/' + state.symbol + UI.buildQueryString(params));
  }

  function applyControls() {
    document.getElementById('ctl-granularity').value = state.granularity;
    document.getElementById('ctl-price').value = state.price;
    document.getElementById('ctl-chart-type').value = state.chartType;
    document.getElementById('ctl-start').value = state.start || '';
    document.getElementById('ctl-end').value = state.end || '';
    document.querySelectorAll('.ma-cb').forEach((cb) => { cb.checked = state.ma.includes(cb.value); });
    document.querySelectorAll('.panel-select').forEach((sel) => {
      sel.value = state.panels[parseInt(sel.dataset.panel, 10)] || '';
    });
    document.getElementById('ctl-card-markers').checked = state.cardMarkers;
    document.getElementById('ctl-exec-markers').checked = state.execMarkers;
  }

  async function loadAll() {
    const q = UI.buildQueryString({
      granularity: state.granularity, price: state.price, start: state.start, end: state.end,
    });
    const [barsData, indData, sigData, cardsData, execData] = await Promise.all([
      UI.fetchJSON('/api/stocks/' + state.symbol + '/bars' + q),
      UI.fetchJSON('/api/stocks/' + state.symbol + '/indicators' + q),
      UI.fetchJSON('/api/stocks/' + state.symbol + '/signals'),
      UI.fetchJSON('/api/stocks/' + state.symbol + '/cards'),
      UI.fetchJSON('/api/stocks/' + state.symbol + '/executions'),
    ]);
    bars = barsData.bars;
    indicators = indData.indicators;
    signals = sigData.items;
    executions = execData.items;
    document.getElementById('price-note').textContent = PRICE_NOTE[state.price] || '';
    renderSignals();
    renderCardDetail(cardsData.items);
    renderExec();
    renderPriceChart();
    renderPanels();
    syncURL();
  }

  function chartBase() {
    const colors = ['#5470c6', '#91cc75', '#fac858', '#ee6666', '#73c0de', '#3ba272'];
    return { color: colors, textStyle: { fontSize: 11 } };
  }

  function xAxisConf() {
    return {
      type: 'category', data: bars.map((b) => b.trade_date),
      axisLabel: { formatter: (v) => String(v).slice(5) },
      axisPointer: { label: { formatter: (p) => p.value } },
    };
  }

  function renderPriceChart() {
    const el = document.getElementById('price-chart');
    const chart = echarts.getInstanceByDom(el) || echarts.init(el);
    chart.group = GROUP;
    const dates = bars.map((b) => b.trade_date);
    const series = [];

    if (state.chartType === 'candlestick') {
      series.push({
        name: 'K线', type: 'candlestick',
        data: bars.map((b) => [b.open, b.close, b.low, b.high]),
        itemStyle: { color: '#ef4444', color0: '#22c55e', borderColor: '#ef4444', borderColor0: '#22c55e' },
      });
    } else {
      series.push({
        name: '收盘', type: 'line', data: bars.map((b) => b.close),
        showSymbol: false, lineStyle: { width: 1.5 }, itemStyle: { color: '#5470c6' },
      });
    }

    // 均线（指标已按 price 模式折回/复权）
    const indByDate = {};
    indicators.forEach((r) => { indByDate[r.date] = r; });
    const maColors = { ma5: '#f59e0b', ma10: '#3b82f6', ma20: '#a855f7', ma60: '#10b981', ma120: '#ef4444', ma250: '#64748b' };
    state.ma.forEach((m) => {
      const vals = dates.map((d) => { const r = indByDate[d]; return r ? r[m] : null; });
      series.push({
        name: m.toUpperCase(), type: 'line', data: vals, showSymbol: false,
        connectNulls: true, lineStyle: { width: 1, color: maColors[m] || '#999' },
      });
    });

    // 卡片与执行标记为不复权口径（卡片 price_basis=raw、执行价为原始成交价），
    // 完全复权模式下不叠加，避免跨尺度错位（§5.1 口径纪律）
    const rawCaliber = state.price !== 'fully_adjusted';

    // 卡片标记
    if (state.cardMarkers && rawCaliber && state.meta.active_card) {
      const card = state.meta.active_card;
      const markers = [];
      const areas = [];
      const tierColors = { 1: 'rgba(16,185,129,0.12)', 2: 'rgba(59,130,246,0.10)', 3: 'rgba(239,68,68,0.10)' };
      (card.price_tiers_json ? card.price_tiers_json.tiers || [] : []).forEach((t) => {
        areas.push([{ name: 'T' + t.tier, xAxis: dates[0], itemStyle: { color: tierColors[t.tier] || 'rgba(0,0,0,0.05)' } },
                    { xAxis: dates[dates.length - 1] }]);
        markers.push({ name: 'T' + t.tier + 'L', xAxis: dates[0], yAxis: parseFloat(t.zone_low),
                       label: { formatter: 'T' + t.tier + ' ' + t.zone_low }, lineStyle: { color: '#0d9488', type: 'dashed', width: 1 } });
        markers.push({ name: 'T' + t.tier + 'H', xAxis: dates[0], yAxis: parseFloat(t.zone_high),
                       label: { formatter: 'T' + t.tier + ' ' + t.zone_high }, lineStyle: { color: '#0d9488', type: 'dashed', width: 1 } });
      });
      if (card.invalidation_json && card.invalidation_json.line) {
        markers.push({ name: '证伪线', yAxis: parseFloat(card.invalidation_json.line),
                       label: { formatter: '证伪 ' + card.invalidation_json.line, position: 'insideEndTop' },
                       lineStyle: { color: '#dc2626', type: 'solid', width: 2 } });
      }
      if (card.swing_box_json) {
        ['box_low', 'box_high'].forEach((k) => {
          if (card.swing_box_json[k] != null) {
            markers.push({ name: '箱体' + k, yAxis: parseFloat(card.swing_box_json[k]),
                           label: { formatter: '箱体 ' + card.swing_box_json[k] },
                           lineStyle: { color: '#16a34a', type: 'dotted', width: 1.5 } });
          }
        });
      }
      if (card.right_side_trigger_json) {
        const rs = card.right_side_trigger_json;
        [['trigger_level', '#2563eb'], ['stop_level', '#dc2626']].forEach(([k, col]) => {
          if (rs[k] != null) {
            markers.push({ name: k, yAxis: parseFloat(rs[k]),
                           label: { formatter: (k === 'trigger_level' ? '触发 ' : '止损 ') + rs[k] },
                           lineStyle: { color: col, type: 'solid', width: 1.5 } });
          }
        });
      }
      if (areas.length) series.push({ name: '价区', type: 'line', data: [], markArea: { silent: true, data: areas } });
      if (markers.length) series[0].markLine = { silent: true, symbol: 'none', data: markers };
    }

    // 执行记录标记（closeByDate 供执行与信号散点共用，须在块外声明）
    const closeByDate = {};
    bars.forEach((b) => { closeByDate[b.trade_date] = b.close; });
    if (state.execMarkers && rawCaliber && executions.length) {
      const pts = executions
        .filter((x) => closeByDate[x.executed_at.slice(0, 10)] != null)
        .map((x) => ({
          coord: [x.executed_at.slice(0, 10), closeByDate[x.executed_at.slice(0, 10)]],
          value: (x.action_type === 'sell' ? '卖 ' : '买 ') + x.price,
          itemStyle: { color: x.action_type === 'sell' ? '#ef4444' : '#22c55e' },
        }));
      if (pts.length) {
        series.push({
          name: '执行', type: 'scatter', data: pts,
          symbolSize: 10, label: { show: true, formatter: (p) => p.data.value, position: 'top', fontSize: 10 },
        });
      }
    }

    // 信号标记（triggered）
    const sigByDate = {};
    signals.forEach((s) => { if (s.triggered) sigByDate[s.observed_on] = s.signal; });
    const sigPts = Object.keys(sigByDate)
      .filter((d) => closeByDate[d] != null)
      .map((d) => ({ coord: [d, closeByDate[d]], value: sigByDate[d], itemStyle: { color: '#f59e0b' } }));
    if (sigPts.length) {
      series.push({ name: '信号', type: 'scatter', data: sigPts, symbolSize: 12,
                    label: { show: true, formatter: (p) => p.data.value, position: 'bottom', fontSize: 9 } });
    }

    chart.setOption(Object.assign(chartBase(), {
      tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } },
      legend: { data: series.map((s) => s.name), top: 0 },
      grid: { left: 60, right: 16, top: 30, bottom: 70 },
      xAxis: Object.assign(xAxisConf(), { gridIndex: 0 }),
      yAxis: { scale: true, splitLine: { lineStyle: { type: 'dashed' } } },
      dataZoom: [
        { type: 'inside', xAxisIndex: 0, start: 0, end: 100 },
        { type: 'slider', xAxisIndex: 0, bottom: 8, height: 22 },
      ],
      series: series,
    }), true);  // notMerge：取消勾选/隐藏标记时清除残留 series 与 markLine
    chart.resize();
  }

  const PANEL_DEFS = {
    volume: { name: '成交量', series: [{ name: '成交量', type: 'bar', key: 'volume' }] },
    macd: { name: 'MACD', series: [
      { name: 'DIF', type: 'line', key: 'dif' }, { name: 'DEA', type: 'line', key: 'dea' },
      { name: '柱', type: 'bar', key: 'macd_hist' },
    ] },
    rsi: { name: 'RSI', series: [
      { name: 'RSI6', type: 'line', key: 'rsi6' }, { name: 'RSI12', type: 'line', key: 'rsi12' },
      { name: 'RSI24', type: 'line', key: 'rsi24' },
    ] },
    kdj: { name: 'KDJ', series: [
      { name: 'K', type: 'line', key: 'kdj_k' }, { name: 'D', type: 'line', key: 'kdj_d' },
      { name: 'J', type: 'line', key: 'kdj_j' },
    ] },
    boll: { name: 'BOLL', series: [
      { name: '中轨', type: 'line', key: 'boll_mid' }, { name: '上轨', type: 'line', key: 'boll_upper' },
      { name: '下轨', type: 'line', key: 'boll_lower' },
    ] },
    pe: { name: 'PE(TTM)', series: [{ name: 'PE', type: 'line', key: 'pe_ttm' }] },
    pct_chg: { name: '涨跌幅', series: [{ name: 'pct_chg', type: 'bar', key: 'pct_chg' }] },
  };

  function renderPanels() {
    const dates = bars.map((b) => b.trade_date);
    const indByDate = {};
    indicators.forEach((r) => { indByDate[r.date] = r; });
    const barByDate = {};
    bars.forEach((b) => { barByDate[b.trade_date] = b; });

    state.panels.forEach((type, i) => {
      const el = document.getElementById('panel-' + i);
      const def = PANEL_DEFS[type];
      const chart = echarts.getInstanceByDom(el) || echarts.init(el);
      chart.group = GROUP;
      if (!def) {
        chart.clear();
        return;
      }
      const series = def.series.map((s) => {
        let data;
        if (s.key === 'volume') data = dates.map((d) => (barByDate[d] ? barByDate[d].volume : null));
        else data = dates.map((d) => { const r = indByDate[d]; return r ? r[s.key] : null; });
        return Object.assign({}, s, { data, connectNulls: true, showSymbol: false,
          itemStyle: { color: type === 'volume' ? '#93c5fd' : undefined } });
      });
      chart.setOption(Object.assign(chartBase(), {
        title: { text: def.name, textStyle: { fontSize: 12 } },
        tooltip: { trigger: 'axis' },
        legend: { data: series.map((s) => s.name), top: 0 },
        grid: { left: 60, right: 16, top: 30, bottom: 40 },
        xAxis: Object.assign(xAxisConf(), { gridIndex: 0 }),
        yAxis: { scale: true, splitLine: { lineStyle: { type: 'dashed' } } },
        dataZoom: [{ type: 'inside', xAxisIndex: 0, start: 0, end: 100 }],
        series: series,
      }), true);  // notMerge：切换副图类型时清除残留 series
      chart.resize();
    });
    echarts.connect(GROUP, 'dataZoom');
    echarts.connect(GROUP, 'restore');
  }

  function renderSignals() {
    const tbody = document.getElementById('signals-tbody');
    if (!signals.length) {
      tbody.innerHTML = '';
      document.getElementById('signals-empty').classList.remove('hidden');
      return;
    }
    document.getElementById('signals-empty').classList.add('hidden');
    tbody.innerHTML = signals.map((s) => `
      <tr>
        <td>${UI.formatDate(s.observed_on)}</td>
        <td>${UI.escapeHtml(s.signal)}</td>
        <td>${UI.renderStatusBadge(s.state)}</td>
        <td>${s.triggered ? '<span class="text-green-600">✓</span>' : '<span class="text-gray-400">—</span>'}</td>
        <td>${UI.formatDate(s.active_until)}</td>
        <td>${s.anchor_id || '—'}</td>
        <td><button class="sig-detail text-xs px-2 py-1 rounded border border-gray-300 hover:bg-gray-100" data-id="${s.fact_id}">详情</button></td>
      </tr>`).join('');
    tbody.querySelectorAll('.sig-detail').forEach((btn) => {
      btn.addEventListener('click', () => showSignalDetail(btn.dataset.id));
    });
  }

  async function showSignalDetail(factId) {
    try {
      const d = await UI.fetchJSON('/api/signals/' + factId);
      const html = `<pre class="json-view mt-2">${UI.escapeHtml(JSON.stringify(d.details, null, 2))}</pre>
        ${d.anchor ? `<div class="text-xs mt-1">anchor: ${d.anchor.anchor_type} @ ${UI.formatDate(d.anchor.trade_date)}（复权 ${UI.formatNumber(d.anchor.adjusted_price)} / 不复权 ${UI.formatNumber(d.anchor.raw_price)}）</div>` : ''}
        <div class="text-xs text-gray-400 mt-1">run_id=${UI.escapeHtml(d.run_id || '')} · rule_version=${UI.escapeHtml(d.rule_version || '')} · config_hash=${UI.escapeHtml(d.config_hash ? d.config_hash.slice(0, 8) : '')}</div>`;
      showModal('信号详情 ' + d.signal + '（' + d.observed_on + '）', html);
    } catch (e) {
      UI.showToast(e.message, 'error');
    }
  }

  function renderCardDetail(cards) {
    const box = document.getElementById('card-detail');
    const active = cards.find((c) => c.status === 'active');
    if (!active) { box.innerHTML = '<p class="text-gray-400 text-sm">无 active 卡片</p>'; return; }
    const tiers = (active.tier_summary || []).map((t) => {
      const [n, lo, hi] = t;
      return `<tr><td>T${n}</td><td class="font-mono">${lo}</td><td class="font-mono">${hi}</td></tr>`;
    }).join('');
    box.innerHTML = `
      <div class="text-xs text-gray-500 mb-1">${active.card_version_id} · ${active.status} · ${UI.formatDate(active.effective_from)} → ${UI.formatDate(active.effective_to)} · next_review ${UI.formatDate(active.next_review_at)}</div>
      <table class="data-table w-full"><thead><tr><th>档位</th><th>低</th><th>高</th></tr></thead><tbody>${tiers}</tbody></table>
      <button id="btn-card-json" class="mt-2 text-xs px-2 py-1 rounded border border-gray-300 hover:bg-gray-100">查看完整 JSON</button>`;
    document.getElementById('btn-card-json').addEventListener('click', async () => {
      const d = await UI.fetchJSON('/api/cards/' + active.card_version_id);
      const json = JSON.stringify({
        price_tiers_json: d.price_tiers_json, invalidation_json: d.invalidation_json,
        swing_box_json: d.swing_box_json, right_side_trigger_json: d.right_side_trigger_json,
        earnings_scenarios_json: d.earnings_scenarios_json,
        valuation_scenarios_json: d.valuation_scenarios_json,
      }, null, 2);
      showModal('卡片 ' + active.card_version_id, `<pre class="json-view">${UI.escapeHtml(json)}</pre>`);
    });
  }

  function renderExec() {
    const tbody = document.getElementById('exec-tbody');
    if (!executions.length) {
      tbody.innerHTML = '';
      document.getElementById('exec-empty').classList.remove('hidden');
      return;
    }
    document.getElementById('exec-empty').classList.add('hidden');
    tbody.innerHTML = executions.map((x) => `
      <tr>
        <td>${UI.escapeHtml(String(x.executed_at).slice(0, 19))}</td>
        <td class="${x.action_type === 'sell' ? 'text-green-600' : 'text-red-600'}">${x.action_type}</td>
        <td>${x.tier || '—'}</td>
        <td class="font-mono">${x.price}</td>
        <td class="font-mono">${x.quantity}</td>
      </tr>`).join('');
  }

  function showModal(title, bodyHtml) {
    let m = document.getElementById('modal');
    if (!m) {
      m = document.createElement('div');
      m.id = 'modal';
      m.className = 'fixed inset-0 bg-black bg-opacity-40 flex items-center justify-center z-40';
      m.innerHTML = `<div class="bg-white rounded-lg shadow-xl max-w-2xl w-full mx-4 max-h-[80vh] flex flex-col">
        <div class="flex items-center justify-between px-4 py-3 border-b border-gray-200">
          <h3 class="font-semibold text-sm" id="modal-title"></h3>
          <button id="modal-close" class="text-gray-400 hover:text-gray-700 text-xl">&times;</button>
        </div>
        <div id="modal-body" class="p-4 overflow-auto"></div>
      </div>`;
      document.body.appendChild(m);
      m.addEventListener('click', (e) => { if (e.target === m) m.classList.add('hidden'); });
      m.querySelector('#modal-close').addEventListener('click', () => m.classList.add('hidden'));
    }
    m.querySelector('#modal-title').textContent = title;
    m.querySelector('#modal-body').innerHTML = bodyHtml;
    m.classList.remove('hidden');
  }

  function init() {
    readURL();
    applyControls();
    loadAll().catch((e) => UI.showToast(e.message, 'error'));

    document.getElementById('ctl-granularity').addEventListener('change', (e) => {
      state.granularity = e.target.value;
      loadAll().catch((err) => UI.showToast(err.message, 'error'));
    });
    document.getElementById('ctl-price').addEventListener('change', (e) => {
      state.price = e.target.value;
      loadAll().catch((err) => UI.showToast(err.message, 'error'));
    });
    document.getElementById('ctl-chart-type').addEventListener('change', (e) => {
      state.chartType = e.target.value;
      renderPriceChart();
      syncURL();
    });
    document.getElementById('ctl-start').addEventListener('change', (e) => { state.start = e.target.value || null; reload(); });
    document.getElementById('ctl-end').addEventListener('change', (e) => { state.end = e.target.value || null; reload(); });
    document.querySelectorAll('.ma-cb').forEach((cb) => {
      cb.addEventListener('change', () => {
        state.ma = Array.from(document.querySelectorAll('.ma-cb:checked')).map((c) => c.value);
        renderPriceChart();
        syncURL();
      });
    });
    document.querySelectorAll('.panel-select').forEach((sel) => {
      sel.addEventListener('change', () => {
        const i = parseInt(sel.dataset.panel, 10);
        state.panels[i] = sel.value;
        renderPanels();
        syncURL();
      });
    });
    document.getElementById('ctl-card-markers').addEventListener('change', (e) => {
      state.cardMarkers = e.target.checked;
      renderPriceChart();
      syncURL();
    });
    document.getElementById('ctl-exec-markers').addEventListener('change', (e) => {
      state.execMarkers = e.target.checked;
      renderPriceChart();
      syncURL();
    });
    document.querySelectorAll('.quick-btn').forEach((btn) => {
      btn.addEventListener('click', () => {
        const n = parseInt(btn.dataset.quick, 10);
        const end = new Date();
        const start = new Date();
        start.setDate(start.getDate() - n);
        state.end = end.toISOString().slice(0, 10);
        state.start = start.toISOString().slice(0, 10);
        applyControls();
        reload();
      });
    });
    window.addEventListener('resize', () => {
      const pc = echarts.getInstanceByDom(document.getElementById('price-chart'));
      if (pc) pc.resize();
      state.panels.forEach((_, i) => {
        const c = echarts.getInstanceByDom(document.getElementById('panel-' + i));
        if (c) c.resize();
      });
    });
  }

  function reload() {
    loadAll().catch((e) => UI.showToast(e.message, 'error'));
  }

  document.addEventListener('DOMContentLoaded', init);
})();
