/* 卡片列表页（task-08）：筛选、分页、详情弹窗（JSON + 价区表 + 版本链）。 */
(function () {
  'use strict';
  const UI = window.UI;

  const state = { symbol: null, status: [], ef: null, et: null, page: 1, page_size: 50 };

  function readURL() {
    const p = new URLSearchParams(location.search);
    state.symbol = p.get('symbol');
    state.status = p.getAll('status');
    state.ef = p.get('effective_from');
    state.et = p.get('effective_to');
    state.page = parseInt(p.get('page') || '1', 10);
    if (p.get('q')) state.symbol = p.get('q');  // 从单股页卡片链接进入
  }

  function syncURL() {
    const params = { symbol: state.symbol, status: state.status,
                     effective_from: state.ef, effective_to: state.et, page: state.page };
    history.replaceState(null, '', '/cards' + UI.buildQueryString(params));
  }

  function applyControls() {
    document.getElementById('card-symbol').value = state.symbol || '';
    document.getElementById('card-ef').value = state.ef || '';
    document.getElementById('card-et').value = state.et || '';
    document.querySelectorAll('.card-status-cb').forEach((cb) => { cb.checked = state.status.includes(cb.value); });
  }

  function initStatusChecks() {
    const box = document.getElementById('card-status-checks');
    [['active', 'active'], ['draft', 'draft'], ['superseded', 'superseded'], ['rejected', 'rejected']]
      .forEach(([v]) => {
        const l = document.createElement('label');
        l.className = 'inline-flex items-center gap-1';
        l.innerHTML = `<input type="checkbox" class="card-status-cb" value="${v}"> ${v}`;
        box.appendChild(l);
      });
  }

  async function load() {
    syncURL();
    const params = { symbol: state.symbol, status: state.status.join(','),
                     effective_from: state.ef, effective_to: state.et,
                     page: state.page, page_size: state.page_size };
    const tbody = document.getElementById('card-tbody');
    tbody.innerHTML = '<tr><td colspan="9" class="text-center text-gray-400 py-6">加载中…</td></tr>';
    try {
      const data = await UI.fetchJSON('/api/cards' + UI.buildQueryString(params));
      render(data);
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="9" class="text-center text-red-500 py-6">${UI.escapeHtml(e.message)}</td></tr>`;
    }
  }

  function render(data) {
    const tbody = document.getElementById('card-tbody');
    document.getElementById('card-total').textContent = `共 ${data.total} 张卡片`;
    document.getElementById('card-prev').disabled = data.page <= 1;
    document.getElementById('card-next').disabled = data.page * data.page_size >= data.total;
    document.getElementById('card-empty').classList.toggle('hidden', data.items.length > 0);
    if (!data.items.length) { tbody.innerHTML = ''; return; }
    tbody.innerHTML = data.items.map((c) => {
      const tiers = (c.tier_summary || []).map((t) => `T${t[0]} ${t[1]}–${t[2]}`).join('；');
      return `<tr>
        <td class="font-mono text-xs">${UI.escapeHtml(c.card_version_id)}</td>
        <td><a class="text-blue-600 hover:underline" href="/stock/${c.symbol}">${c.symbol}</a></td>
        <td>${UI.renderStatusBadge(c.status)}</td>
        <td>${UI.formatDate(c.effective_from)}</td>
        <td>${UI.formatDate(c.effective_to)}</td>
        <td class="font-mono text-xs">${UI.escapeHtml(c.supersedes_id || '—')}</td>
        <td class="text-xs">${UI.escapeHtml(tiers || '—')}</td>
        <td>${UI.formatDate(c.next_review_at)}</td>
        <td><button class="card-detail px-2 py-1 text-xs rounded border border-gray-300 hover:bg-gray-100" data-id="${UI.escapeHtml(c.card_version_id)}">详情</button></td>
      </tr>`;
    }).join('');
    tbody.querySelectorAll('.card-detail').forEach((btn) => {
      btn.addEventListener('click', () => showDetail(btn.dataset.id));
    });
  }

  async function showDetail(cardId) {
    try {
      const d = await UI.fetchJSON('/api/cards/' + cardId);
      const tierRows = (d.tier_summary || []).map((t) => `<tr><td>T${t[0]}</td><td class="font-mono">${t[1]}</td><td class="font-mono">${t[2]}</td></tr>`).join('');
      const chain = (d.version_chain || []).map((v, i) => `<div class="text-xs ${i === 0 ? 'font-semibold text-blue-600' : ''}">${i === 0 ? '当前' : '被替代'}：${UI.escapeHtml(v)}</div>`).join('');
      const json = JSON.stringify({
        earnings_scenarios_json: d.earnings_scenarios_json,
        valuation_scenarios_json: d.valuation_scenarios_json,
        invalidation_json: d.invalidation_json,
        swing_box_json: d.swing_box_json,
        right_side_trigger_json: d.right_side_trigger_json,
        input_snapshot_json: d.input_snapshot_json,
      }, null, 2);
      showModal(`卡片 ${cardId}`, `
        <div class="text-sm mb-2">${d.symbol} · ${UI.renderStatusBadge(d.status)}
          生效 ${UI.formatDate(d.effective_from)} → ${UI.formatDate(d.effective_to)}
          · next_review ${UI.formatDate(d.next_review_at)} · run_id=${UI.escapeHtml(d.run_id || '')}</div>
        <div class="mb-3"><div class="font-semibold text-xs text-gray-500 mb-1">版本链</div>${chain}</div>
        <div class="mb-3"><div class="font-semibold text-xs text-gray-500 mb-1">价区</div>
          <table class="data-table"><thead><tr><th>档</th><th>低</th><th>高</th></tr></thead><tbody>${tierRows}</tbody></table></div>
        <pre class="json-view">${UI.escapeHtml(json)}</pre>`);
    } catch (e) {
      UI.showToast(e.message, 'error');
    }
  }

  function showModal(title, body) {
    let m = document.getElementById('card-modal');
    if (!m) {
      m = document.createElement('div');
      m.id = 'card-modal';
      m.className = 'fixed inset-0 bg-black bg-opacity-40 flex items-center justify-center z-40';
      m.innerHTML = `<div class="bg-white rounded-lg shadow-xl max-w-3xl w-full mx-4 max-h-[85vh] flex flex-col">
        <div class="flex items-center justify-between px-4 py-3 border-b border-gray-200"><h3 id="card-modal-title" class="font-semibold text-sm"></h3><button id="card-modal-close" class="text-gray-400 hover:text-gray-700 text-xl">&times;</button></div>
        <div id="card-modal-body" class="p-4 overflow-auto"></div></div>`;
      document.body.appendChild(m);
      m.addEventListener('click', (e) => { if (e.target === m) m.classList.add('hidden'); });
      m.querySelector('#card-modal-close').addEventListener('click', () => m.classList.add('hidden'));
    }
    m.querySelector('#card-modal-title').textContent = title;
    m.querySelector('#card-modal-body').innerHTML = body;
    m.classList.remove('hidden');
  }

  function init() {
    readURL();
    initStatusChecks();
    applyControls();
    document.getElementById('card-status-checks').addEventListener('change', (e) => {
      if (e.target.classList.contains('card-status-cb')) {
        state.status = Array.from(document.querySelectorAll('.card-status-cb:checked')).map((c) => c.value);
      }
    });
    document.getElementById('card-symbol').addEventListener('change', (e) => { state.symbol = e.target.value || null; state.page = 1; });
    document.getElementById('card-ef').addEventListener('change', (e) => { state.ef = e.target.value || null; state.page = 1; });
    document.getElementById('card-et').addEventListener('change', (e) => { state.et = e.target.value || null; state.page = 1; });
    document.getElementById('card-apply').addEventListener('click', () => { state.page = 1; load(); });
    document.getElementById('card-reset').addEventListener('click', () => {
      state.symbol = null; state.status = []; state.ef = state.et = null; state.page = 1;
      applyControls();
      load();
    });
    document.getElementById('card-prev').addEventListener('click', () => { if (state.page > 1) { state.page--; load(); } });
    document.getElementById('card-next').addEventListener('click', () => { state.page++; load(); });
    load();
  }

  document.addEventListener('DOMContentLoaded', init);
})();
