/* 通用工具函数（task-02），供所有页面 JS 复用。 */

(function (global) {
  'use strict';

  function pad(n) { return String(n).padStart(2, '0'); }

  /* 2026-08-07T09:00:00+08:00 -> 2026-08-07 */
  function formatDate(value) {
    if (!value) return '—';
    const s = String(value);
    return s.slice(0, 10);
  }

  function formatNumber(num, precision) {
    if (num === null || num === undefined || num === '') return '—';
    precision = precision === undefined ? 2 : precision;
    const n = Number(num);
    if (Number.isNaN(n)) return String(num);
    return n.toLocaleString('zh-CN', {
      minimumFractionDigits: precision,
      maximumFractionDigits: precision,
    });
  }

  function formatCompact(num) {
    const n = Number(num);
    if (Number.isNaN(n)) return '—';
    if (Math.abs(n) >= 1e8) return (n / 1e8).toFixed(2) + '亿';
    if (Math.abs(n) >= 1e4) return (n / 1e4).toFixed(2) + '万';
    return n.toFixed(0);
  }

  function buildQueryString(params) {
    const p = new URLSearchParams();
    Object.entries(params).forEach(([k, v]) => {
      if (v === null || v === undefined || v === '') return;
      if (Array.isArray(v)) { v.forEach((x) => p.append(k, x)); }
      else { p.append(k, v); }
    });
    const s = p.toString();
    return s ? '?' + s : '';
  }

  function debounce(fn, ms) {
    let t = null;
    return function () {
      const args = arguments;
      clearTimeout(t);
      t = setTimeout(() => fn.apply(null, args), ms);
    };
  }

  async function fetchJSON(url) {
    const rv = await fetch(url, { headers: { 'Accept': 'application/json' } });
    if (!rv.ok) {
      let msg = rv.statusText;
      try { const body = await rv.json(); msg = body.error || msg; } catch (_e) {}
      throw new Error(msg);
    }
    return rv.json();
  }

  function showToast(message, type) {
    type = type || 'info';
    let container = document.getElementById('toast-container');
    if (!container) {
      container = document.createElement('div');
      container.id = 'toast-container';
      document.body.appendChild(container);
    }
    const el = document.createElement('div');
    el.className = 'toast toast-' + type;
    el.textContent = message;
    container.appendChild(el);
    setTimeout(() => el.remove(), 3000);
  }

  const STATUS_COLORS = {
    success: 'success', complete: 'complete', ok: 'ok',
    degraded: 'degraded', incomplete: 'incomplete', draft: 'draft',
    failed: 'failed', rejected: 'rejected', suspended: 'suspended',
    active: 'active', running: 'running', triggered: 'triggered', watching: 'watching',
    superseded: 'superseded', inactive: 'inactive', idle: 'idle',
  };

  function renderStatusBadge(status, text) {
    // text 可选：中文等展示文本（配色仍由枚举驱动）；不传则原样显示 status
    if (!status && !text) return '<span class="status-badge status-idle">—</span>';
    const cls = STATUS_COLORS[String(status).toLowerCase()] || 'idle';
    const label = (text === undefined || text === null) ? String(status) : String(text);
    return '<span class="status-badge status-' + cls + '">' + escapeHtml(label) + '</span>';
  }

  function escapeHtml(s) {
    return String(s === null || s === undefined ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  /* 日期区间：快捷按钮 + 起止 input 联动 */
  function initDateRangePicker(startId, endId, onChange) {
    const start = document.getElementById(startId);
    const end = document.getElementById(endId);
    if (!start || !end) return;

    function daysAgo(n) {
      const d = new Date();
      d.setDate(d.getDate() - n);
      return d.toISOString().slice(0, 10);
    }

    document.querySelectorAll('[data-quick]').forEach((btn) => {
      btn.addEventListener('click', () => {
        const n = parseInt(btn.getAttribute('data-quick'), 10);
        const today = new Date().toISOString().slice(0, 10);
        start.value = daysAgo(n);
        end.value = today;
        if (onChange) onChange();
      });
    });
  }

  /* 股票搜索自动补全 */
  function initStockSearch(inputId, onSelect) {
    const input = document.getElementById(inputId);
    if (!input) return;
    const listEl = document.createElement('div');
    listEl.className = 'absolute z-20 bg-white border border-gray-300 rounded shadow w-full hidden';
    listEl.style.marginTop = '2px';
    input.parentElement.style.position = 'relative';
    input.parentElement.appendChild(listEl);

    async function search(q) {
      try {
        const data = await fetchJSON('/api/stocks/search?q=' + encodeURIComponent(q) + '&limit=10');
        render(data.items);
      } catch (_e) { render([]); }
    }

    function render(items) {
      listEl.innerHTML = '';
      if (!items.length) { listEl.classList.add('hidden'); return; }
      items.forEach((it) => {
        const opt = document.createElement('div');
        opt.className = 'px-3 py-1 text-sm cursor-pointer hover:bg-blue-50';
        opt.textContent = it.symbol + ' · ' + it.name;
        opt.addEventListener('mousedown', (e) => {
          e.preventDefault();
          input.value = it.symbol;
          listEl.classList.add('hidden');
          if (onSelect) onSelect(it);
        });
        listEl.appendChild(opt);
      });
      listEl.classList.remove('hidden');
    }

    input.addEventListener('input', debounce(() => {
      const q = input.value.trim();
      if (q.length >= 1) search(q);
      else listEl.classList.add('hidden');
    }, 300));
    input.addEventListener('blur', () => setTimeout(() => listEl.classList.add('hidden'), 150));
  }

  global.UI = {
    formatDate, formatNumber, formatCompact, buildQueryString, debounce,
    fetchJSON, showToast, renderStatusBadge, escapeHtml, initDateRangePicker, initStockSearch,
  };
})(window);
