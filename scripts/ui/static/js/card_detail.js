/* 排期卡详情共享渲染器（2026-08-29 UX 重做）：/cards 详情弹窗与单股页排期卡面板共用。
   口径纪律：卡内全部为中文展示；长文研判（input_snapshot 各 note）整行段落展示；
   空结构（如 swing_box={}）不渲染对应区块，只给一句话说明；原始 JSON 折叠在末尾。 */
(function (global) {
  'use strict';
  const UI = global.UI;

  const PRICE_BASIS_CN = { raw: '不复权', forward: '前复权', backward: '后复权' };
  const EPS_SCENARIO_CN = { worst: '极悲', bear_mid: '悲观', base_neutral: '中性', bull_recovery: '乐观' };
  const ANCHOR_LABELS = { pe_scale: 'PE(TTM) 刻度', pe_static_scale: '静态折算 PE 刻度',
    pb: 'PB', ps: 'PS', price_band: '价格底带', mixed: '混合锚' };

  function esc(s) { return UI.escapeHtml(s === null || s === undefined ? '—' : s); }

  /* 标签-值短行（值短时用）；长文一律用 para 整行段落 */
  function kv(label, value) {
    return `<div class="flex gap-2 py-0.5"><span class="flex-none w-20 text-gray-400 text-xs leading-5">${esc(label)}</span><span class="text-xs leading-5">${esc(value)}</span></div>`;
  }
  function para(label, text) {
    if (!text) return '';
    return `<div class="mt-1"><span class="text-xs text-gray-400">${esc(label)}</span><div class="text-xs leading-5 mt-0.5">${esc(text)}</div></div>`;
  }
  function section(title, inner) {
    if (!inner) return '';
    return `<div class="mt-3"><div class="text-xs font-medium text-gray-600 mb-1">${esc(title)}</div>${inner}</div>`;
  }

  /* 每档价区反推隐含 EPS×PE 口径（纯算术展示） */
  function tierImplied(t, eps) {
    const lo = parseFloat(t.zone_low), hi = parseFloat(t.zone_high);
    const base = eps.base != null ? parseFloat(eps.base) : null;
    const bear = eps.bear != null ? parseFloat(eps.bear) : null;
    if (t.tier === 3 && bear) {
      return `≈ 悲观EPS ${bear} × PE ${(lo / bear).toFixed(1)}–${(hi / bear).toFixed(1)}`;
    }
    if (base) {
      return `≈ 中性EPS ${base} × PE ${(lo / base).toFixed(1)}–${(hi / base).toFixed(1)}`;
    }
    return '—';
  }

  function renderTiers(d, snap) {
    const tiers = (d.price_tiers_json || {}).tiers || [];
    if (!tiers.length) return '';
    const eps = (d.earnings_scenarios_json || {}).eps || {};
    const win = (snap.win_rate_estimate || {});
    const ranges = win.tier_ranges || {}, kelly = win.kelly_caps || {};
    const rows = tiers.map((t) => `
      <tr class="border-t border-gray-100">
        <td class="py-1">T${t.tier}</td>
        <td class="font-mono">${esc(t.zone_low)}–${esc(t.zone_high)}</td>
        <td class="text-xs text-gray-500">${esc(tierImplied(t, eps))}</td>
        <td class="text-xs">${esc(ranges[String(t.tier)])}</td>
        <td class="text-xs">${esc(kelly[String(t.tier)])}</td>
      </tr>`).join('');
    const matrix = snap.matrix_source ? `<div class="text-xs text-gray-400 mt-1">${esc(snap.matrix_source)}</div>` : '';
    return `<table class="text-sm w-full"><thead><tr class="text-left text-gray-500 text-xs">
      <th>档位</th><th>价区</th><th>反推口径</th><th>胜率区间</th><th>Kelly 上限</th>
      </tr></thead><tbody>${rows}</tbody></table>${matrix}`;
  }

  function renderScenarios(d, snap) {
    const val = d.valuation_scenarios_json || {};
    const eps = (d.earnings_scenarios_json || {}).eps || {};
    const pe = val.pe || {};
    const scales = val.panic_floor_scales || [];
    const win = val.sample_window || {};

    /* 锚定指标明示：优先 valuation.anchor（新卡结构化字段），回退 input_snapshot.anchor_type_note */
    const anchor = val.anchor || null;
    const snapNote = snap.anchor_type_note || '';
    let anchorTxt, anchorIsPe;
    if (anchor && anchor.metric) {
      anchorTxt = ANCHOR_LABELS[anchor.metric] || anchor.metric;
      anchorIsPe = anchor.metric === 'pe_scale' || anchor.metric === 'pe_static_scale';
    } else if (snapNote) {
      anchorTxt = snapNote;
      anchorIsPe = null;  // 老卡快照说明，不据此改标注
    } else {
      anchorTxt = 'PE(TTM) 刻度（默认，未显式声明）';
      anchorIsPe = true;
    }
    // 三态口径：true=纯 PE 锚；false=非锚仅分位参考；null=无结构化 anchor（旧卡），
    // 不能把 null 静默当 PE 锚渲染（否则旧卡被误标成"PE 三情景"）
    let peRowLabel;
    if (anchorIsPe === true) {
      peRowLabel = 'PE 三情景';
    } else if (anchorIsPe === false) {
      peRowLabel = 'PE 三情景（非锚，仅分位参考）';
    } else {
      peRowLabel = 'PE 三情景（旧卡未结构化锚，仅分位参考）';
    }

    let html = kv('锚定指标', anchorTxt);
    if (anchor && anchor.note) html += para('锚说明', anchor.note);
    html += kv('EPS 三情景', `悲观 ${eps.bear || '—'} / 中性 ${eps.base || '—'} / 乐观 ${eps.bull || '—'}`);
    html += kv(peRowLabel, `悲观 ${pe.pessimistic || '—'} / 中性 ${pe.neutral || '—'} / 乐观 ${pe.optimistic || '—'}`);

    /* EPS 情景推导假设（新卡有，老卡无则跳过） */
    const detail = snap.eps_scenario_detail || {};
    const order = ['worst', 'bear_mid', 'base_neutral', 'bull_recovery'];
    const keys = order.filter((k) => detail[k]).concat(
      Object.keys(detail).filter((k) => !order.includes(k)));
    if (keys.length) {
      html += keys.map((k) => {
        const it = detail[k] || {};
        const label = EPS_SCENARIO_CN[k] || k;
        return para(`${label}情景（EPS ${it.eps || '—'}）`, it.assumption);
      }).join('');
    }

    if (scales.length) {
      html += '<div class="mt-1"><span class="text-xs text-gray-400">恐慌底刻度</span><div class="text-xs leading-5 mt-0.5">' +
        scales.map((s) => `<div>· ${esc(s.date)} PE ${esc(s.pe_ttm_at)}${s.note ? '——' + esc(s.note) : ''}</div>`).join('') +
        '</div></div>';
    }
    if (win.from || win.to) html += kv('样本窗口', `${win.from || '—'} ~ ${win.to || '—'}`);
    if (win.note) html += para('样本说明', win.note);
    html += para('体系判断', val.regime);
    return html;
  }

  function renderTradeFrame(d, snap) {
    const inv = d.invalidation_json || {};
    const bx = d.swing_box_json || {};
    const rst = d.right_side_trigger_json || {};
    let html = '';
    if (inv.line) {
      html += kv('证伪线', inv.line);
      html += para('', inv.note);
    }
    /* 波段箱体：新卡为空对象（不适用）时不渲染字段行，只给说明 */
    if (bx.box_low != null) {
      html += kv('波段箱体', `${bx.box_low}–${bx.box_high}：买区 ${bx.buy_zone_low || '—'}–${bx.buy_zone_high || '—'}，卖区 ${bx.sell_zone_low || '—'}–${bx.sell_zone_high || '—'}，跌破 ${bx.box_invalidation || '—'} 箱体失效`);
    } else if (snap.swing_box_notes) {
      html += `<div class="text-xs text-gray-400 mt-1">波段箱体：${esc(snap.swing_box_notes)}</div>`;
    }
    if (rst.trigger_level) {
      html += kv('右侧预案', `收盘站上 ${rst.trigger_level} 触发，止损 ${rst.stop_level || '—'}`);
      html += para('', snap.right_side_notes);
    }
    return html;
  }

  function renderWinRate(snap) {
    const win = snap.win_rate_estimate || {};
    let html = para('胜率合成', win.note);
    const rt = win.recovery_target || {};
    if (rt.price) html += kv('修复目标价', `${rt.price}${rt.basis ? '——' + rt.basis : ''}`);
    return html;
  }

  function renderEarningsBasis(snap) {
    const eb = snap.earnings_basis || {};
    let html = para('最新财报', eb.latest_report);
    html += para('一致预期裂口', eb.forecast_gap);
    html += para('基数', eb.h2_base);
    return html;
  }

  function renderChain(d) {
    const chain = d.version_chain || [];
    if (!chain.length) return '';
    return chain.map((v, i) =>
      `<div class="text-xs ${i === 0 ? 'font-semibold text-blue-600' : 'text-gray-500'}">${i === 0 ? '当前版本' : '被替代'}：<span class="font-mono">${esc(v)}</span></div>`
    ).join('');
  }

  function renderRawJson(d) {
    const json = JSON.stringify({
      price_tiers_json: d.price_tiers_json, invalidation_json: d.invalidation_json,
      swing_box_json: d.swing_box_json, right_side_trigger_json: d.right_side_trigger_json,
      earnings_scenarios_json: d.earnings_scenarios_json,
      valuation_scenarios_json: d.valuation_scenarios_json,
      input_snapshot_json: d.input_snapshot_json,
    }, null, 2);
    return `<details class="mt-3"><summary class="text-xs text-gray-400 cursor-pointer">原始 JSON（排查用）</summary><pre class="json-view">${UI.escapeHtml(json)}</pre></details>`;
  }

  /* 主入口：d 为 /api/cards/<id> 返回的卡片详情（JSON 字段已解析为对象） */
  function render(d, opts) {
    opts = opts || {};
    const snap = d.input_snapshot_json || {};
    const basis = PRICE_BASIS_CN[d.price_basis] || d.price_basis || '不复权';
    const title = `${d.symbol}${d.name ? ' ' + d.name : ''}`;

    let html = `<div class="text-xs text-gray-500 mb-2">
      <span class="font-medium text-gray-700">${esc(title)}</span> ·
      ${UI.renderStatusBadge(d.status, d.status_cn)} ·
      <span class="font-mono">${esc(d.card_version_id)}</span><br>
      ${UI.formatDate(d.effective_from)} 生效 · 下次复核 ${UI.formatDate(d.next_review_at)} · 价格口径 ${esc(basis)}
    </div>`;

    if (opts.showChain) html += section('版本链', renderChain(d));
    html += section('三档价区（估值锚定）', renderTiers(d, snap));
    html += section('交易框架', renderTradeFrame(d, snap));
    html += section('胜率与赔率', renderWinRate(snap));
    html += section('情景假设', renderScenarios(d, snap));
    html += section('盈利底稿', renderEarningsBasis(snap));
    html += section('复核触发', snap.review_triggers ? para('', snap.review_triggers) : '');
    html += renderRawJson(d);
    return html;
  }

  global.CardDetail = { render };
})(window);
