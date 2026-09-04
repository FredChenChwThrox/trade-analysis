/* 单股页（2026-08-10 原型定稿落地）：关键数字卡 + 三联图（单实例三 grid）+ 信号现状 + 排期卡 + 执行。
   口径纪律（§5.1）：卡片价区/证伪线/箱体/执行价为不复权口径，完全复权模式下不叠加标记。 */
(function () {
  'use strict';
  const UI = window.UI;

  const state = {
    symbol: document.getElementById('stock-page').dataset.symbol,
    meta: JSON.parse(document.getElementById('stock-page').dataset.meta || '{}'),
    granularity: 'daily',
    price: 'unadjusted',
  };

  let bars = [];
  let indicators = [];
  let signals = [];
  let executions = [];

  const RED = '#dc2626', GREEN = '#16a34a';
  const ZOOM_BARS = 120;  // dataZoom 初始窗口：最近约 120 根

  const SIGNAL_NAMES = {
    panic: '恐慌型', dry_up: '干涸型', no_new_low_3w: '三周不创新低',
    divergence: '周线底背离', duration: '持续时间',
    tier_proximity: '档位临近', tier_triggered: '档位触发',
    falsification_breach: '证伪线', box_position: '箱体位置',
    right_side: '右侧确认', accumulation: '吸筹形态',
    daily_watch: '日度观察', ma_comparison: '均线对比', card_conversion: '卡片换算',
  };

  const PRICE_NOTE = {
    unadjusted: '不复权口径 · 价区底色为卡片三档 · 标线为证伪/箱体/右侧',
    fully_adjusted: '完全复权口径 · 卡片/执行/信号标记为不复权口径，本模式不叠加（§5.1 口径纪律）',
  };

  function readURL() {
    const p = new URLSearchParams(location.search);
    if (p.get('granularity')) state.granularity = p.get('granularity');
    if (p.get('price')) state.price = p.get('price');
  }

  function syncURL() {
    history.replaceState(null, '', '/stock/' + state.symbol + UI.buildQueryString({
      granularity: state.granularity, price: state.price,
    }));
  }

  function applyControls() {
    document.getElementById('ctl-granularity').value = state.granularity;
    document.getElementById('ctl-price').value = state.price;
  }

  async function loadAll() {
    const q = UI.buildQueryString({
      granularity: state.granularity, price: state.price, start: '2000-01-01',
    });
    const sigQ = UI.buildQueryString({ start: '2000-01-01', limit: 2000 });
    const [barsData, indData, sigData, execData, ovData] = await Promise.all([
      UI.fetchJSON('/api/stocks/' + state.symbol + '/bars' + q),
      UI.fetchJSON('/api/stocks/' + state.symbol + '/indicators' + q),
      UI.fetchJSON('/api/stocks/' + state.symbol + '/signals' + sigQ),
      UI.fetchJSON('/api/stocks/' + state.symbol + '/executions'),
      UI.fetchJSON('/api/stocks/' + state.symbol + '/overview'),
    ]);
    bars = barsData.bars;
    indicators = indData.indicators;
    signals = sigData.items;
    executions = execData.items;
    document.getElementById('chart-note').textContent = PRICE_NOTE[state.price] || '';
    renderNumCards(ovData);
    renderFactors(ovData.factor_snapshot);
    renderFundamentals(ovData);
    renderSummary(ovData);
    renderCardPanel(ovData).catch((e) => UI.showToast(e.message, 'error'));
    renderExec();
    renderChart();
    syncURL();
  }

  // ---------------------------------------------------------------- 数字卡片
  function g(v) {  // Python :g 风格：'54.70' -> '54.7'
    const n = parseFloat(v);
    return Number.isNaN(n) ? String(v) : String(n);
  }

  function peStatusText(s) {
    /* pe_status 原因码 → 人读中文（与 docs/prototype/generate.py 一致） */
    if (!s) return '';
    const str = String(s);
    let parts;
    if (str.indexOf('ok') === 0) parts = ['正常'];
    else if (str.indexOf('degraded') >= 0) parts = ['降级'];
    else return '数据缺失';
    if (str.indexOf('degraded_available_at') >= 0) parts.push('披露日降级');
    return parts.join('·');
  }

  function setCard(id, value, sub, cls) {
    const v = document.getElementById('nc-' + id);
    v.textContent = value;
    v.className = 'value' + (cls ? ' ' + cls : '');
    document.getElementById('nc-' + id + '-sub').textContent = sub || '';
  }

  function renderNumCards(ov) {
    setCard('close', ov.close_raw == null ? '—' : Number(ov.close_raw).toFixed(2),
            ov.latest_trade_date || '');
    const pct = ov.pct_chg;
    setCard('pct', pct == null ? '—' : (pct > 0 ? '+' : '') + Number(pct).toFixed(2) + '%',
            '', (pct || 0) >= 0 ? 'up' : 'down');
    setCard('pe', ov.pe_ttm == null ? '—' : Number(ov.pe_ttm).toFixed(1),
            peStatusText(ov.pe_status));

    const t = ov.tier;
    if (t && t.tier) setCard('tier', 'T' + t.tier + ' 内', g(t.zone_low) + '–' + g(t.zone_high));
    else if (t) setCard('tier', '档外',
      '距 T' + t.nearest_tier + ' ' + (t.nearest_side === 'high' ? '上沿' : '下沿') +
      ' ' + Number(t.dist_pct).toFixed(1) + '%');
    else setCard('tier', '—', '');

    const card = state.meta.active_card || {};
    const box = card.swing_box_json || {};
    // 波段箱体已废弃的卡（swing_box 为空）显示"不适用"，不再渲染空价区
    const boxNA = ov.has_card && box.box_low == null;
    setCard('box', boxNA ? '不适用' : ((ov.box && ov.box.text) || '—'),
            box.box_low != null ? g(box.box_low) + '–' + g(box.box_high) : '');
    const rst = card.right_side_trigger_json || {};
    setCard('right', (ov.right_side && ov.right_side.text) || '—',
            rst.trigger_level != null ? '触发 ' + g(rst.trigger_level) : '');
    setCard('accum', (ov.accumulation && ov.accumulation.text) || '—', '');
    const ex = ov.exhaustion;
    setCard('exhaust', ex ? ex.active + '/' + ex.total + ' 活跃' : '—',
            ex ? '完成周 ' + ex.week_end : '');
  }

  // ---------------------------------------------------------------- 基本面
  function _yoyTxt(r) {
    return r == null ? '' : (r > 0 ? '+' : '') + (Number(r) * 100).toFixed(1) + '%';
  }
  function _setFund(id, value, sub) {
    document.getElementById(id).textContent = value;
    if (sub != null) document.getElementById(id + '-sub').textContent = sub;
  }
  function renderFundamentals(ov) {
    const f = ov.fundamentals || {};
    const a = f.annual, it = f.interim, vs = f.valuation_snapshot, fc = f.forecast_np_yi;
    _setFund('fd-rev', a && a.revenue_yi != null ? a.revenue_yi + ' 亿' : '—',
             a ? `FY${a.fiscal_year} ${_yoyTxt(a.revenue_yoy)}` : '');
    _setFund('fd-np', a && a.net_profit_yi != null ? a.net_profit_yi + ' 亿' : '—',
             a ? `FY${a.fiscal_year} ${_yoyTxt(a.net_profit_yoy)}` : '');
    _setFund('fd-interim', it && it.net_profit_yi != null ? it.net_profit_yi + ' 亿' : '—',
             it ? `${it.period_end} ${_yoyTxt(it.net_profit_yoy)}` : '');
    if (it) document.getElementById('fd-interim-label').textContent = '最新季报净利';
    _setFund('fd-pb', vs && vs.pb_mrq != null ? vs.pb_mrq : '—',
             vs ? '同花顺快照 ' + UI.formatDate(vs.snapshot_at) : '');
    _setFund('fd-ps', vs && vs.ps_lyr != null ? vs.ps_lyr : '—',
             vs ? '同花顺快照 ' + UI.formatDate(vs.snapshot_at) : '');
    _setFund('fd-fc', fc ? `${fc.fy1 ?? '—'} / ${fc.fy2 ?? '—'} / ${fc.fy3 ?? '—'} 亿` : '—',
             fc ? 'FY1/FY2/FY3 净利 · ' + UI.formatDate(fc.snapshot_at) : '');
    const notes = [];
    if (vs) notes.push('PB/PS 为快照单点值（非历史序列）');
    if (!a && !it) notes.push('财报数据缺失（§2.5）');
    document.getElementById('fund-note').textContent = notes.join('；');
  }

  // ---------------------------------------------------------------- 信号现状
  function renderSummary(ov) {
    const box = document.getElementById('signal-summary');
    if (!ov.summaries || !ov.summaries.length) {
      box.innerHTML = '<p class="py-4 text-center text-gray-400">暂无信号数据</p>';
      return;
    }
    box.innerHTML = ov.summaries.map((s) => `
      <div class="flex items-center gap-3 py-2">
        <span class="w-28 shrink-0 font-medium">${UI.escapeHtml(s.name)}</span>
        <span class="w-20 shrink-0">${UI.renderStatusBadge(s.state, s.state_text)}</span>
        <span class="text-gray-600">${UI.escapeHtml(s.detail || '—')}</span>
        ${s.fact_id ? `<button class="sig-detail ml-auto shrink-0 text-xs px-2 py-1 rounded border border-gray-300 hover:bg-gray-100" data-id="${s.fact_id}">详情</button>` : ''}
      </div>`).join('');
    box.querySelectorAll('.sig-detail').forEach((btn) => {
      btn.addEventListener('click', () => showSignalDetail(btn.dataset.id));
    });
  }

  async function showSignalDetail(factId) {
    try {
      const d = await UI.fetchJSON('/api/signals/' + factId);
      const html = `<pre class="json-view mt-2">${UI.escapeHtml(JSON.stringify(d.details, null, 2))}</pre>
        ${d.anchor ? `<div class="text-xs mt-1">锚：${d.anchor.anchor_type} @ ${UI.formatDate(d.anchor.trade_date)}（复权 ${UI.formatNumber(d.anchor.adjusted_price)} / 不复权 ${UI.formatNumber(d.anchor.raw_price)}）</div>` : ''}
        <div class="text-xs text-gray-400 mt-1">run_id=${UI.escapeHtml(d.run_id || '')} · rule_version=${UI.escapeHtml(d.rule_version || '')} · config_hash=${UI.escapeHtml(d.config_hash ? d.config_hash.slice(0, 8) : '')}</div>`;
      showModal('信号详情 ' + (d.signal_name || d.signal) + '（' + d.observed_on + '）', html);
    } catch (e) {
      UI.showToast(e.message, 'error');
    }
  }

  // ---------------------------------------------------------------- 关联因子（顶部区块）
  function renderFactors(fs) {
    /* 关联因子现状（overview.factor_snapshot；factor_watch 口径：缺失/陈旧如实标注），顶部独立区块 */
    const body = document.getElementById('factor-body');
    document.getElementById('factor-asof').textContent =
      fs && fs.as_of ? '截至 ' + fs.as_of : '';
    let html;
    if (!fs) {
      html = '<p class="text-gray-400 text-xs">无行情日期，因子快照不可用</p>';
    } else if (!fs.factors || !fs.factors.length) {
      html = `<p class="text-gray-400 text-xs">${UI.escapeHtml(fs.note || '无因子映射')}</p>`;
    } else {
      html = fs.factors.map((f) => {
        const dirBadge = f.direction === 'positive'
          ? '<span class="text-red-700">上行利好</span>'
          : (f.direction === 'negative' ? '<span class="text-green-700">上行利空</span>' : '');
        const fmtChg = (v) => v == null ? '—' : (v > 0 ? '+' : '') + (v * 100).toFixed(1) + '%';
        const val = f.status === 'missing'
          ? '<span class="text-gray-400">缺数据</span>'
          : `<span class="font-medium">${UI.escapeHtml(f.close)} ${UI.escapeHtml(f.unit || '')}</span>
             <span class="text-gray-400">${UI.formatDate(f.trade_date)}${f.status === 'stale' ? '（陈旧）' : ''}</span>`;
        return `<div class="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 py-1 text-xs">
          <span class="font-medium">${UI.escapeHtml(f.name || f.code)}</span>
          <span class="text-gray-400">${UI.escapeHtml(f.code)}</span>
          ${dirBadge} ${val}
          <span class="text-gray-500">近20读数 ${fmtChg(f.change_20d)} · 近60读数 ${fmtChg(f.change_60d)}</span>
          ${f.note ? `<span class="text-gray-400">${UI.escapeHtml(f.note)}</span>` : ''}
        </div>`;
      }).join('') + (fs.note ? `<p class="text-gray-400 text-xs mt-1">${UI.escapeHtml(fs.note)}</p>` : '');
    }
    body.innerHTML = html;
  }

  // ---------------------------------------------------------------- 排期卡
  /* 面板渲染接共享渲染器 card_detail.js（与 /cards 详情同口径），此处只负责取数 */
  async function renderCardPanel(ov) {
    const box = document.getElementById('card-detail');
    let html;
    if (!ov.has_card) {
      html = '<p class="text-gray-400 text-sm">无生效中的排期卡</p>';
    } else {
      const d = await UI.fetchJSON('/api/cards/' + ov.card_id);
      html = CardDetail.render(d);
    }
    box.innerHTML = html;
  }

  // ---------------------------------------------------------------- 执行记录
  function renderExec() {
    const tbody = document.getElementById('exec-tbody');
    if (!executions.length) {
      tbody.innerHTML = '';
      document.getElementById('exec-empty').classList.remove('hidden');
      return;
    }
    document.getElementById('exec-empty').classList.add('hidden');
    tbody.innerHTML = executions.map((x) => {
      const isSell = x.action_type === 'sell';
      return `
      <tr>
        <td>${UI.escapeHtml(String(x.executed_at).slice(0, 19))}</td>
        <td class="${isSell ? 'down' : 'up'}">${isSell ? '卖出' : '买入'}</td>
        <td>${x.tier || '—'}</td>
        <td class="font-mono">${x.price}</td>
        <td class="font-mono">${x.quantity}</td>
      </tr>`;
    }).join('');
  }

  // ---------------------------------------------------------------- 三联图
  function renderChart() {
    const el = document.getElementById('chart-all');
    const chart = echarts.getInstanceByDom(el) || echarts.init(el);
    const dates = bars.map((b) => b.trade_date);
    const closeByDate = {};
    bars.forEach((b) => { closeByDate[b.trade_date] = b.close; });
    const indByDate = {};
    indicators.forEach((r) => { indByDate[r.date] = r; });
    const ind = (key) => dates.map((d) => { const r = indByDate[d]; return r ? r[key] : null; });

    const xBase = { type: 'category', data: dates, boundaryGap: true,
                    axisLine: { lineStyle: { color: '#e5e7eb' } }, axisTick: { show: false } };
    const yBase = { scale: true, position: 'right',
                    splitLine: { lineStyle: { color: '#f3f4f6' } },
                    axisLabel: { fontSize: 10, color: '#9ca3af' } };

    // ---- grid 0：蜡烛图（红涨绿跌）+ MA5/20/60
    const series = [{
      name: 'K线', type: 'candlestick',
      data: bars.map((b) => [b.open, b.close, b.low, b.high]),
      itemStyle: { color: RED, color0: GREEN, borderColor: RED, borderColor0: GREEN },
      z: 5,
    }];
    const maColors = { ma5: '#f59e0b', ma20: '#a855f7', ma60: '#64748b' };
    ['ma5', 'ma20', 'ma60'].forEach((m) => series.push({
      name: m.toUpperCase(), type: 'line', data: ind(m),
      showSymbol: false, connectNulls: true, lineStyle: { width: 1, color: maColors[m] }, z: 4,
    }));

    // 卡片/执行/信号标记为不复权口径，完全复权模式下不叠加（§5.1 口径纪律）
    const rawCaliber = state.price !== 'fully_adjusted';
    const areas = [], markers = [];
    const card = state.meta.active_card;
    if (rawCaliber && card) {
      const tierColors = { 1: 'rgba(22,163,74,0.07)', 2: 'rgba(37,99,235,0.07)', 3: 'rgba(220,38,38,0.07)' };
      (card.price_tiers_json ? card.price_tiers_json.tiers || [] : []).forEach((t) => {
        areas.push([{ name: 'T' + t.tier, yAxis: parseFloat(t.zone_low),
                      itemStyle: { color: tierColors[t.tier] || 'rgba(0,0,0,0.05)' },
                      label: { show: true, position: 'insideLeft', fontSize: 9, color: '#9ca3af',
                               formatter: 'T' + t.tier + '  ' + t.zone_low + '–' + t.zone_high } },
                    { yAxis: parseFloat(t.zone_high) }]);
      });
      const line = (y, text, color, type, width) => {
        if (y == null) return;
        markers.push({ yAxis: parseFloat(y),
                       label: { show: true, position: 'insideStartTop', fontSize: 9, color, formatter: text },
                       lineStyle: { color, type, width } });
      };
      const inv = card.invalidation_json || {};
      line(inv.line, '证伪 ' + inv.line, RED, 'solid', 1.5);
      const bx = card.swing_box_json || {};
      line(bx.box_low, '箱体 ' + bx.box_low, GREEN, 'dotted', 1.2);
      line(bx.box_high, '箱体 ' + bx.box_high, GREEN, 'dotted', 1.2);
      const rst = card.right_side_trigger_json || {};
      line(rst.trigger_level, '右侧触发 ' + rst.trigger_level, '#2563eb', 'dashed', 1);
    }

    // 执行标记：买红卖绿，带"买/卖+价格"小字
    if (rawCaliber && executions.length) {
      const pts = executions
        .filter((x) => closeByDate[x.executed_at.slice(0, 10)] != null)
        .map((x) => ({
          coord: [x.executed_at.slice(0, 10), closeByDate[x.executed_at.slice(0, 10)]],
          value: (x.action_type === 'sell' ? '卖' : '买') + x.price,
          itemStyle: { color: x.action_type === 'sell' ? GREEN : RED },
        }));
      if (pts.length) {
        series.push({ name: '执行', type: 'scatter', data: pts, symbolSize: 9, z: 8,
          label: { show: true, formatter: (p) => p.data.value, position: 'top', fontSize: 9, color: '#6b7280' } });
      }
    }

    // 触发信号：pin 不带文字，悬停见名
    if (rawCaliber && signals.length) {
      const seen = {};
      const sigPts = [];
      signals.forEach((s) => {
        if (!s.triggered || closeByDate[s.observed_on] == null) return;
        const key = s.observed_on + '|' + s.signal;
        if (seen[key]) return;
        seen[key] = true;
        sigPts.push({ coord: [s.observed_on, closeByDate[s.observed_on]],
                      value: SIGNAL_NAMES[s.signal] || s.signal,
                      itemStyle: { color: '#f59e0b' } });
      });
      if (sigPts.length) {
        series.push({ name: '信号', type: 'scatter', data: sigPts,
                      symbol: 'pin', symbolSize: 14, z: 7, label: { show: false } });
      }
    }

    // ---- grid 1：成交量（涨红跌绿，万单位）+ 均量20
    const volData = bars.map((b, i) => ({
      value: (b.volume || 0) / 10000,
      itemStyle: { color: (i > 0 && b.close < bars[i - 1].close) ? 'rgba(22,163,74,0.65)' : 'rgba(220,38,38,0.65)' },
    }));
    series.push({ name: '成交量', type: 'bar', data: volData, xAxisIndex: 1, yAxisIndex: 1, barWidth: '60%' });
    series.push({ name: '均量20', type: 'line',
      data: ind('vol_mean20').map((v) => (v == null ? null : v / 10000)),
      xAxisIndex: 1, yAxisIndex: 1, showSymbol: false, lineStyle: { width: 1, color: '#f59e0b' } });

    // ---- grid 2：MACD（柱正红负绿）
    const histData = ind('macd_hist').map((v) => ({
      value: v,
      itemStyle: { color: (v || 0) >= 0 ? 'rgba(220,38,38,0.6)' : 'rgba(22,163,74,0.6)' },
    }));
    series.push({ name: 'DIF', type: 'line', data: ind('dif'),
      xAxisIndex: 2, yAxisIndex: 2, showSymbol: false, lineStyle: { width: 1, color: '#2563eb' } });
    series.push({ name: 'DEA', type: 'line', data: ind('dea'),
      xAxisIndex: 2, yAxisIndex: 2, showSymbol: false, lineStyle: { width: 1, color: '#f59e0b' } });
    series.push({ name: 'MACD柱', type: 'bar', data: histData, xAxisIndex: 2, yAxisIndex: 2, barWidth: '60%' });

    if (areas.length) series[0].markArea = { silent: true, data: areas };
    if (markers.length) series[0].markLine = { silent: true, symbol: 'none', data: markers };

    const n = dates.length;
    const zoomStart = n > ZOOM_BARS ? Math.max(0, (1 - ZOOM_BARS / n) * 100) : 0;

    chart.setOption({
      animation: false,
      axisPointer: { link: [{ xAxisIndex: 'all' }] },   // 三格十字线联动
      tooltip: { trigger: 'axis', axisPointer: { type: 'cross', label: { fontSize: 10 } },
                 textStyle: { fontSize: 11 },
                 formatter: (ps) => {
                   if (!ps.length) return '';
                   const lines = [ps[0].axisValue];
                   ps.forEach((p) => {
                     if (p.seriesType === 'candlestick') {
                       const d = p.data;   // [idx, open, close, low, high]
                       lines.push(`开 ${d[1]}　收 ${d[2]}　低 ${d[3]}　高 ${d[4]}`);
                     } else if (p.seriesName === '执行' || p.seriesName === '信号') {
                       lines.push(`${p.marker}${p.seriesName} ${p.data.value}`);
                     } else if (p.seriesName === '成交量' || p.seriesName === '均量20') {
                       lines.push(`${p.marker}${p.seriesName} ${p.value == null ? '—' : Number(p.value).toFixed(0)}万`);
                     } else {
                       lines.push(`${p.marker}${p.seriesName} ${p.value == null ? '—' : Number(p.value).toFixed(2)}`);
                     }
                   });
                   return lines.join('<br>');
                 } },
      legend: { data: ['K线', 'MA5', 'MA20', 'MA60', '执行', '信号'],
                top: 0, textStyle: { fontSize: 11 }, itemWidth: 14, itemHeight: 8 },
      grid: [
        { left: 40, right: 52, top: 30, height: 290 },   // 价格
        { left: 40, right: 52, top: 342, height: 82 },   // 成交量
        { left: 40, right: 52, top: 442, height: 100 },  // MACD
      ],
      xAxis: [
        Object.assign({}, xBase, { gridIndex: 0, axisLabel: { show: false } }),
        Object.assign({}, xBase, { gridIndex: 1, axisLabel: { show: false } }),
        Object.assign({}, xBase, { gridIndex: 2, axisLabel: { fontSize: 10, color: '#9ca3af', formatter: (v) => String(v).slice(5) } }),
      ],
      yAxis: [
        Object.assign({}, yBase, { gridIndex: 0 }),
        Object.assign({}, yBase, { gridIndex: 1, name: '量·万', nameTextStyle: { fontSize: 9, color: '#9ca3af' }, splitNumber: 2 }),
        Object.assign({}, yBase, { gridIndex: 2, name: 'MACD', nameTextStyle: { fontSize: 9, color: '#9ca3af' }, splitNumber: 3 }),
      ],
      dataZoom: [
        { type: 'inside', xAxisIndex: [0, 1, 2], start: zoomStart, end: 100 },
        { type: 'slider', xAxisIndex: [0, 1, 2], bottom: 6, height: 16, start: zoomStart, end: 100,
          borderColor: '#e5e7eb', fillerColor: 'rgba(37,99,235,0.08)' },
      ],
      series: series,
    }, true);  // notMerge：口径/周期切换时清除残留 series 与 markLine/markArea
    chart.resize();
  }

  // ---------------------------------------------------------------- 弹窗
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
    window.addEventListener('resize', () => {
      const c = echarts.getInstanceByDom(document.getElementById('chart-all'));
      if (c) c.resize();
    });
  }

  document.addEventListener('DOMContentLoaded', init);
})();
