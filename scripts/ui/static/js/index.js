/* 首页仪表板（task-10）：状态卡、运行趋势、异常清单、最近运行、60s 自动刷新。 */
(function () {
  'use strict';
  const UI = window.UI;
  let trendChart = null;

  const STAT_LINKS = {
    total_stocks: '/stocks',
    stocks_with_data_today: '/stocks',
    stocks_with_active_card: '/cards?status=active',
    stocks_with_signal_today: '/signals?triggered=1',
  };

  async function load() {
    try {
      const d = await UI.fetchJSON('/api/dashboard');
      renderStats(d);
      renderTrend(d.run_trend || []);
      renderTradeDates(d.trade_dates || {});
      renderAlerts(d.alerts || []);
      renderRecentRuns(d);
    } catch (e) {
      UI.showToast(e.message, 'error');
    }
  }

  function renderStats(d) {
    const box = document.getElementById('home-stats');
    const cards = [
      ['股票总数', d.total_stocks, '/stocks', 'text-blue-600'],
      ['今日有数据', d.stocks_with_data_today, '/stocks', 'text-green-600'],
      ['active 卡片', d.stocks_with_active_card, '/cards?status=active', 'text-emerald-600'],
      ['今日触发信号', d.stocks_with_signal_today, '/signals?triggered=1', 'text-orange-500'],
    ];
    box.innerHTML = cards.map(([label, val, href, color]) => `
      <a href="${href}" class="card block hover:shadow-md transition-shadow">
        <div class="text-xs text-gray-500">${label}</div>
        <div class="text-2xl font-bold ${color}">${val}</div>
      </a>`).join('');
  }

  function renderTrend(trend) {
    const el = document.getElementById('run-trend');
    if (!trendChart) trendChart = echarts.init(el);
    trendChart.setOption({
      tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
      legend: { data: ['success', 'degraded', 'failed'], top: 0 },
      grid: { left: 40, right: 16, top: 30, bottom: 40 },
      xAxis: { type: 'category', data: trend.map((t) => t.date.slice(5)) },
      yAxis: { type: 'value', minInterval: 1 },
      series: [
        { name: 'success', type: 'bar', stack: 'r', data: trend.map((t) => t.success), itemStyle: { color: '#22c55e' } },
        { name: 'degraded', type: 'bar', stack: 'r', data: trend.map((t) => t.degraded), itemStyle: { color: '#f59e0b' } },
        { name: 'failed', type: 'bar', stack: 'r', data: trend.map((t) => t.failed), itemStyle: { color: '#ef4444' } },
      ],
    });
  }

  function renderTradeDates(td) {
    const box = document.getElementById('trade-dates');
    box.innerHTML = Object.entries(td).map(([m, v]) => `
      <div class="flex justify-between">
        <span class="font-mono">${UI.escapeHtml(m)}</span>
        <span class="text-gray-500">${UI.formatDate(v.latest)}</span>
      </div>`).join('') || '<p class="text-gray-400 text-xs">暂无</p>';
  }

  function alertLink(a) {
    if (a.type === 'run_failed' || a.type === 'run_degraded') return `<a class="text-blue-600 text-xs" href="/runs?run_id=${encodeURIComponent(a.run_id || '')}">查看</a>`;
    if (a.symbol) return `<a class="text-blue-600 text-xs" href="/stock/${a.symbol}">${UI.escapeHtml(a.symbol)}</a>`;
    return '—';
  }

  function renderAlerts(alerts) {
    const tbody = document.getElementById('alerts-tbody');
    document.getElementById('alerts-empty').classList.toggle('hidden', alerts.length > 0);
    tbody.innerHTML = alerts.map((a) => `
      <tr>
        <td>${a.symbol ? UI.escapeHtml(a.symbol) : '—'}</td>
        <td><span class="status-badge status-${a.type === 'run_failed' ? 'failed' : 'incomplete'}">${UI.escapeHtml(a.type)}</span></td>
        <td class="text-xs">${UI.escapeHtml(a.message)}</td>
        <td>${alertLink(a)}</td>
      </tr>`).join('') || '';
  }

  function renderRecentRuns(d) {
    const tbody = document.getElementById('recent-runs');
    const runs = (d.latest_run ? [d.latest_run] : []);
    UI.fetchJSON('/api/runs?page_size=10').then((data) => {
      tbody.innerHTML = data.items.map((r) => `
        <tr>
          <td class="font-mono text-xs">${UI.escapeHtml(r.run_id)}</td>
          <td class="text-xs">${UI.escapeHtml(r.stage)}</td>
          <td>${UI.renderStatusBadge(r.status)}</td>
          <td class="font-mono text-xs">${UI.escapeHtml(String(r.started_at || '').slice(0, 19))}</td>
        </tr>`).join('');
    }).catch(() => { tbody.innerHTML = ''; });
  }

  function init() {
    load();
    setInterval(load, 60000);
    window.addEventListener('resize', () => { if (trendChart) trendChart.resize(); });
  }

  document.addEventListener('DOMContentLoaded', init);
})();
