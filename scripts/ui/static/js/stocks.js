/* 股票列表页（task-03）：筛选、排序、分页、多选批量操作、URL 状态同步。 */
(function () {
  'use strict';
  const UI = window.UI;

  const state = {
    page: 1,
    page_size: 50,
    sort: 'latest_trade_date',
    order: 'desc',
    filters: {},
  };

  function readURL() {
    const p = new URLSearchParams(location.search);
    const f = {};
    ['q', 'pe_min', 'pe_max', 'pct_chg_min', 'pct_chg_max', 'volume_min', 'volume_max',
     'recent_signal_days'].forEach((k) => {
      const v = p.get(k);
      if (v) f[k] = v;
    });
    const markets = p.getAll('market');
    if (markets.length) f.market = markets;
    const peStatus = p.getAll('pe_status');
    if (peStatus.length) f.pe_status = peStatus;
    const hc = p.get('has_active_card');
    if (hc === '1' || hc === '0') f.has_active_card = hc === '1';
    state.filters = f;
    state.page = parseInt(p.get('page') || '1', 10);
    state.page_size = parseInt(p.get('page_size') || '50', 10);
    state.sort = p.get('sort') || 'latest_trade_date';
    state.order = p.get('order') || 'desc';
    if (!['latest_trade_date', 'latest_close', 'pe_ttm', 'pct_chg', 'signal_count_5d']
        .includes(state.sort)) state.sort = 'latest_trade_date';
  }

  function syncURL() {
    const params = Object.assign({}, state.filters);
    params.page = state.page;
    params.page_size = state.page_size;
    params.sort = state.sort;
    params.order = state.order;
    history.replaceState(null, '', '/stocks' + UI.buildQueryString(params));
  }

  function applyFormToFilters() {
    const f = {};
    const form = document.getElementById('filter-form');
    const q = form.querySelector('[name=q]').value.trim();
    if (q) f.q = q;
    const hc = form.querySelector('[name=has_active_card]').value;
    if (hc === '1' || hc === '0') f.has_active_card = hc === '1';
    const rsd = form.querySelector('[name=recent_signal_days]').value;
    if (rsd) f.recent_signal_days = rsd;
    ['pe_min', 'pe_max', 'pct_chg_min', 'pct_chg_max', 'volume_min', 'volume_max']
      .forEach((k) => {
        const v = form.querySelector(`[name=${k}]`).value;
        if (v !== '') f[k] = v;
      });
    const markets = Array.from(form.querySelectorAll('.market-cb:checked'))
      .map((cb) => cb.value);
    if (markets.length) f.market = markets;
    const peStatus = Array.from(form.querySelectorAll('[name=pe_status]:checked'))
      .map((cb) => cb.value);
    if (peStatus.length) f.pe_status = peStatus;
    state.filters = f;
    state.page = 1;
  }

  function fillForm() {
    const form = document.getElementById('filter-form');
    const f = state.filters;
    form.querySelector('[name=q]').value = f.q || '';
    form.querySelector('[name=has_active_card]').value =
      f.has_active_card === undefined ? '' : (f.has_active_card ? '1' : '0');
    form.querySelector('[name=recent_signal_days]').value = f.recent_signal_days || '';
    ['pe_min', 'pe_max', 'pct_chg_min', 'pct_chg_max', 'volume_min', 'volume_max']
      .forEach((k) => { form.querySelector(`[name=${k}]`).value = f[k] || ''; });
    form.querySelectorAll('.market-cb').forEach((cb) => { cb.checked = (f.market || []).includes(cb.value); });
    form.querySelectorAll('[name=pe_status]').forEach((cb) => { cb.checked = (f.pe_status || []).includes(cb.value); });
  }

  async function load() {
    const params = Object.assign({}, state.filters);
    params.page = state.page;
    params.page_size = state.page_size;
    params.sort = state.sort;
    params.order = state.order;
    syncURL();
    const tbody = document.getElementById('stocks-tbody');
    tbody.innerHTML = '<tr><td colspan="11" class="text-center text-gray-400 py-6">加载中…</td></tr>';
    try {
      const data = await UI.fetchJSON('/api/stocks' + UI.buildQueryString(params));
      render(data);
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="11" class="text-center text-red-500 py-6">${UI.escapeHtml(e.message)}</td></tr>`;
    }
  }

  function render(data) {
    const tbody = document.getElementById('stocks-tbody');
    document.getElementById('stock-total').textContent = `共 ${data.total} 只`;
    document.getElementById('page-info').textContent =
      data.total === 0 ? '' : `第 ${data.page}/${Math.max(1, Math.ceil(data.total / data.page_size))} 页`;
    document.getElementById('page-prev').disabled = data.page <= 1;
    document.getElementById('page-next').disabled = data.page * data.page_size >= data.total;

    if (!data.items.length) {
      tbody.innerHTML = '';
      document.getElementById('stocks-empty').classList.remove('hidden');
      return;
    }
    document.getElementById('stocks-empty').classList.add('hidden');

    tbody.innerHTML = data.items.map((it) => {
      const suspended = it.trading_status === 'suspended';
      const cls = suspended ? 'row-suspended' : '';
      const peBadge = it.pe_status && it.pe_status !== ''
        ? `<span class="status-badge status-incomplete ml-1" title="${UI.escapeHtml(it.pe_status)}">${UI.escapeHtml(it.pe_status)}</span>`
        : '';
      const tierText = it.tier_state && it.tier_state.tier
        ? `T${it.tier_state.tier}（距边界 ${UI.formatNumber(it.tier_state.dist_pct)}%）`
        : '—';
      const pctCls = it.pct_chg === null ? 'text-gray-400' : (it.pct_chg >= 0 ? 'text-red-600' : 'text-green-600');
      return `<tr class="${cls}">
        <td><input type="checkbox" class="row-check" value="${it.symbol}" ${suspended ? 'disabled' : ''}></td>
        <td><a class="text-blue-600 hover:underline" href="/stock/${it.symbol}">${it.symbol}</a></td>
        <td>${UI.escapeHtml(it.name)}<div class="text-xs text-gray-400">${it.market}${it.currency ? ' · ' + it.currency : ''}</div></td>
        <td>${UI.formatDate(it.latest_trade_date)}</td>
        <td class="font-mono">${UI.formatNumber(it.latest_close)}</td>
        <td class="font-mono">${UI.formatNumber(it.price_adj_factor, 4)}</td>
        <td><span class="font-mono">${UI.formatNumber(it.pe_ttm)}</span>${peBadge}</td>
        <td class="${pctCls}">${it.pct_chg === null ? '—' : UI.formatNumber(it.pct_chg) + '%'}</td>
        <td class="text-center">${it.signal_count_5d || 0}</td>
        <td class="text-xs">${tierText}</td>
        <td>${UI.renderStatusBadge(it.last_run_status)}</td>
      </tr>`;
    }).join('');
    updateBatchButtons();
  }

  function updateBatchButtons() {
    const n = document.querySelectorAll('.row-check:checked').length;
    document.getElementById('btn-compare').disabled = n < 2;
    document.getElementById('btn-indicators').disabled = n < 1;
  }

  function selectedSymbols() {
    return Array.from(document.querySelectorAll('.row-check:checked')).map((cb) => cb.value);
  }

  function init() {
    readURL();
    fillForm();
    const form = document.getElementById('filter-form');
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      applyFormToFilters();
      load();
    });
    document.getElementById('filter-reset-stocks').addEventListener('click', () => {
      form.reset();
      state.filters = {};
      state.page = 1;
      load();
    });
    document.getElementById('page-prev').addEventListener('click', () => { if (state.page > 1) { state.page--; load(); } });
    document.getElementById('page-next').addEventListener('click', () => { state.page++; load(); });
    document.getElementById('page-size').addEventListener('change', (e) => {
      state.page_size = parseInt(e.target.value, 10);
      state.page = 1;
      load();
    });
    document.getElementById('check-all').addEventListener('change', (e) => {
      document.querySelectorAll('.row-check:not(:disabled)').forEach((cb) => { cb.checked = e.target.checked; });
      updateBatchButtons();
    });
    document.getElementById('stocks-tbody').addEventListener('change', updateBatchButtons);
    document.querySelectorAll('th.sortable').forEach((th) => {
      th.addEventListener('click', () => {
        const s = th.getAttribute('data-sort');
        if (state.sort === s) state.order = state.order === 'desc' ? 'asc' : 'desc';
        else { state.sort = s; state.order = 'desc'; }
        state.page = 1;
        load();
      });
    });
    document.getElementById('btn-compare').addEventListener('click', () => {
      const syms = selectedSymbols();
      if (syms.length >= 2) location.href = '/compare?symbols=' + syms.join(',');
    });
    document.getElementById('btn-indicators').addEventListener('click', () => {
      const syms = selectedSymbols();
      if (syms.length) location.href = '/indicators?symbols=' + syms.join(',');
    });
    // 数据质量选择
    const peBox = document.getElementById('pe-status-checks');
    [['ok', 'OK（正常）'], ['degraded', '降级'], ['missing', '缺失']].forEach(([v, label]) => {
      const l = document.createElement('label');
      l.className = 'inline-flex items-center gap-1 mr-3';
      l.innerHTML = `<input type="checkbox" name="pe_status" value="${v}"> ${label}`;
      peBox.appendChild(l);
    });
    initFilterBar();
    load();
  }

  function initFilterBar() {
    const body = document.getElementById('filter-body');
    if (!body) return;
    const toggle = document.getElementById('filter-toggle');
    toggle.addEventListener('click', () => body.classList.toggle('hidden'));
    body.classList.remove('hidden', 'md:flex');
    document.getElementById('filter-apply').addEventListener('click', () => {
      applyFormToFilters();
      load();
    });
    document.getElementById('filter-reset').addEventListener('click', () => {
      formResetAndReload();
    });
  }

  function formResetAndReload() {
    document.getElementById('filter-form').reset();
    state.filters = {};
    state.page = 1;
    load();
  }

  document.addEventListener('DOMContentLoaded', init);
})();
