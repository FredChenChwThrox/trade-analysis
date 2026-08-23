/* 运行状态页（task-09）：Pipeline/Report 双 tab、统计看板、筛选、详情、60s 自动刷新。 */
(function () {
  'use strict';
  const UI = window.UI;

  const state = { tab: 'pipeline', page: 1, page_size: 50,
                  run_id: null, stage: null, status: null, start: null, end: null };

  function loadStats() {
    UI.fetchJSON('/api/dashboard').then((d) => {
      const s = d.run_stats || {};
      const box = document.getElementById('runs-stats');
      const lastOk = d.latest_run && d.latest_run.status === 'success';
      box.innerHTML = `
        <div class="card"><div class="text-xs text-gray-500">今日运行</div><div class="text-lg font-semibold">${s.success + s.degraded + s.failed}</div></div>
        <div class="card"><div class="text-xs text-gray-500">成功</div><div class="text-lg font-semibold text-green-600">${s.success || 0}</div></div>
        <div class="card"><div class="text-xs text-gray-500">降级</div><div class="text-lg font-semibold text-yellow-600">${s.degraded || 0}</div></div>
        <div class="card"><div class="text-xs text-gray-500">失败</div><div class="text-lg font-semibold text-red-600">${s.failed || 0}</div></div>
        <div class="card"><div class="text-xs text-gray-500">最近运行</div><div class="text-xs font-mono mt-1 break-all">${d.latest_run ? UI.escapeHtml(d.latest_run.run_id) + ' · ' + UI.renderStatusBadge(d.latest_run.status) : '—'}</div></div>`;
    }).catch(() => {});
  }

  async function loadStages() {
    const data = await UI.fetchJSON('/api/runs?page_size=200');
    const stages = Array.from(new Set(data.items.map((r) => r.stage))).sort();
    const sel = document.getElementById('run-stage');
    sel.innerHTML = '<option value="">全部阶段</option>' + stages.map((s) =>
      `<option value="${UI.escapeHtml(s)}">${UI.escapeHtml(s)}</option>`).join('');
  }

  async function load() {
    const isPipe = state.tab === 'pipeline';
    const params = isPipe
      ? { run_id: state.run_id, stage: state.stage, status: state.status,
          start: state.start, end: state.end, page: state.page, page_size: state.page_size }
      : { report_type: state.stage, status: state.status, trade_date: state.end,
          page: state.page, page_size: state.page_size };
    const url = isPipe ? '/api/runs' : '/api/reports';
    const box = document.getElementById('runs-table');
    box.innerHTML = '<p class="text-gray-400 text-sm py-6 text-center">加载中…</p>';
    try {
      const data = await UI.fetchJSON(url + UI.buildQueryString(params));
      render(data, isPipe);
    } catch (e) {
      box.innerHTML = `<p class="text-red-500 text-sm py-6 text-center">${UI.escapeHtml(e.message)}</p>`;
    }
  }

  function render(data, isPipe) {
    const box = document.getElementById('runs-table');
    document.getElementById('runs-total').textContent = `共 ${data.total} 条`;
    document.getElementById('runs-prev').disabled = data.page <= 1;
    document.getElementById('runs-next').disabled = data.page * data.page_size >= data.total;
    if (!data.items.length) { box.innerHTML = '<p class="text-gray-400 text-sm py-6 text-center">无记录</p>'; return; }

    if (isPipe) {
      box.innerHTML = `<table class="data-table">
        <thead><tr><th>run_id</th><th>stage</th><th>data_cutoff</th><th>status</th><th>started_at</th><th>duration</th><th>rule_version</th><th>card</th><th></th></tr></thead>
        <tbody>${data.items.map(runRow).join('')}</tbody></table>`;
    } else {
      box.innerHTML = `<table class="data-table">
        <thead><tr><th>report_run_id</th><th>type</th><th>symbol</th><th>trade_date</th><th>rev</th><th>status</th><th>file_path</th><th>created_at</th><th></th></tr></thead>
        <tbody>${data.items.map(reportRow).join('')}</tbody></table>`;
    }
    box.querySelectorAll('.run-detail').forEach((btn) => {
      btn.addEventListener('click', () => showDetail(btn.dataset.run, btn.dataset.stage));
    });
  }

  function runRow(r) {
    const dur = r.duration_sec === null ? '—' : UI.formatNumber(r.duration_sec) + 's';
    return `<tr class="cursor-pointer hover:bg-gray-50">
      <td class="font-mono text-xs">${UI.escapeHtml(r.run_id)}</td>
      <td>${UI.escapeHtml(r.stage)}</td>
      <td class="font-mono text-xs">${UI.escapeHtml(r.data_cutoff || '—')}</td>
      <td>${UI.renderStatusBadge(r.status)}</td>
      <td class="font-mono text-xs">${UI.escapeHtml(String(r.started_at || '').slice(0, 19))}</td>
      <td class="font-mono text-xs">${dur}</td>
      <td class="font-mono text-xs">${UI.escapeHtml(r.rule_version || '—')}</td>
      <td class="font-mono text-xs">${UI.escapeHtml(r.card_version_id || '—')}</td>
      <td><button class="run-detail px-2 py-1 text-xs rounded border border-gray-300 hover:bg-gray-100" data-run="${UI.escapeHtml(r.run_id)}" data-stage="${UI.escapeHtml(r.stage)}">详情</button></td>
    </tr>`;
  }

  function reportRow(r) {
    const link = r.file_path ? `<a class="text-blue-600 hover:underline" target="_blank" href="/reports/${r.file_path.replace(/^reports\//, '')}">${UI.escapeHtml(r.file_path)}</a>` : '—';
    return `<tr>
      <td class="font-mono text-xs">${r.report_run_id}</td>
      <td>${UI.escapeHtml(r.report_type)}</td>
      <td>${UI.escapeHtml(r.symbol || '—')}</td>
      <td>${UI.formatDate(r.trade_date)}</td>
      <td>${r.revision}</td>
      <td>${UI.renderStatusBadge(r.status)}</td>
      <td class="text-xs">${link}</td>
      <td class="font-mono text-xs">${UI.escapeHtml(String(r.created_at || '').slice(0, 19))}</td>
      <td><button class="run-detail px-2 py-1 text-xs rounded border border-gray-300 hover:bg-gray-100" data-run="report:${r.report_run_id}" data-stage="">详情</button></td>
    </tr>`;
  }

  async function showDetail(runId, stage) {
    try {
      if (String(runId).startsWith('report:')) {
        // 报告行查 /api/reports（report_runs 表），不能拿 id 去撞 pipeline_runs
        const id = String(runId).replace(/^report:/, '');
        const data = await UI.fetchJSON('/api/reports?report_run_id=' + encodeURIComponent(id) + '&page_size=1');
        const r = data.items[0];
        if (!r) { UI.showToast('未找到记录', 'error'); return; }
        const json = UI.escapeHtml(JSON.stringify({
          report_type: r.report_type, symbol: r.symbol, trade_date: r.trade_date,
          revision: r.revision, status: r.status, card_version_id: r.card_version_id,
          rule_version: r.rule_version, config_hash: r.config_hash,
          file_path: r.file_path, run_id: r.run_id, created_at: r.created_at,
        }, null, 2));
        showModal('报告详情 ' + r.report_run_id, `
          <div class="text-sm">${UI.renderStatusBadge(r.status)}</div>
          <pre class="json-view mt-2">${json}</pre>`);
        return;
      }
      const data = await UI.fetchJSON('/api/runs?run_id=' + encodeURIComponent(runId) + '&page_size=1');
      const r = data.items.find((x) => x.stage === stage) || data.items[0];
      if (!r) { UI.showToast('未找到记录', 'error'); return; }
      const json = UI.escapeHtml(JSON.stringify({
        as_of: r.as_of, adapter_version: r.adapter_version, config_hash: r.config_hash,
        rule_version: r.rule_version, app_version: r.app_version, git_commit: r.git_commit,
        error: r.error, finished_at: r.finished_at,
      }, null, 2));
      showModal('运行详情', `
        <div class="text-sm">${UI.renderStatusBadge(r.status)}</div>
        <table class="data-table mt-2"><tbody>
          <tr><td class="font-mono text-xs">${UI.escapeHtml(r.run_id)}</td><td class="font-mono text-xs">${UI.escapeHtml(r.stage)}</td></tr>
        </tbody></table>
        <pre class="json-view mt-2">${json}</pre>`);
    } catch (e) {
      UI.showToast(e.message, 'error');
    }
  }

  function showModal(title, body) {
    let m = document.getElementById('runs-modal');
    if (!m) {
      m = document.createElement('div');
      m.id = 'runs-modal';
      m.className = 'fixed inset-0 bg-black bg-opacity-40 flex items-center justify-center z-40';
      m.innerHTML = `<div class="bg-white rounded-lg shadow-xl max-w-2xl w-full mx-4 max-h-[80vh] flex flex-col">
        <div class="flex items-center justify-between px-4 py-3 border-b border-gray-200"><h3 id="runs-modal-title" class="font-semibold text-sm"></h3><button id="runs-modal-close" class="text-gray-400 hover:text-gray-700 text-xl">&times;</button></div>
        <div id="runs-modal-body" class="p-4 overflow-auto"></div></div>`;
      document.body.appendChild(m);
      m.addEventListener('click', (e) => { if (e.target === m) m.classList.add('hidden'); });
      m.querySelector('#runs-modal-close').addEventListener('click', () => m.classList.add('hidden'));
    }
    m.querySelector('#runs-modal-title').textContent = title;
    m.querySelector('#runs-modal-body').innerHTML = body;
    m.classList.remove('hidden');
  }

  function switchTab(tab) {
    state.tab = tab;
    state.page = 1;
    document.getElementById('tab-pipeline').className =
      tab === 'pipeline' ? 'px-4 py-1.5 bg-blue-600 text-white' : 'px-4 py-1.5 hover:bg-gray-100';
    document.getElementById('tab-reports').className =
      tab === 'reports' ? 'px-4 py-1.5 bg-blue-600 text-white' : 'px-4 py-1.5 hover:bg-gray-100';
    load();
  }

  function init() {
    loadStats();
    loadStages().then(() => load()).catch(() => load());
    document.getElementById('tab-pipeline').addEventListener('click', () => switchTab('pipeline'));
    document.getElementById('tab-reports').addEventListener('click', () => switchTab('reports'));
    document.getElementById('run-apply').addEventListener('click', () => {
      state.run_id = document.getElementById('run-id').value || null;
      state.stage = document.getElementById('run-stage').value || null;
      state.status = document.getElementById('run-status').value || null;
      state.start = document.getElementById('run-start').value || null;
      state.end = document.getElementById('run-end').value || null;
      state.page = 1;
      load();
    });
    document.getElementById('runs-prev').addEventListener('click', () => { if (state.page > 1) { state.page--; load(); } });
    document.getElementById('runs-next').addEventListener('click', () => { state.page++; load(); });
    setInterval(() => { loadStats(); load(); }, 60000);
  }

  document.addEventListener('DOMContentLoaded', init);
})();
