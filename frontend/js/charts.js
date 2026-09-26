/**
 * frontend/js/charts.js - Chart.js lifecycle/plugin helpers shared across
 * the dashboard (PO list, PO modal, import PO list, material modal).
 * Split out of main.js (was a single 3,400+ line file) - see main.js's own
 * header comment for the module map.
 */

// pageCharts = charts owned by the page behind the modal (PO trend/status,
// materials drill-down) - only ever destroyed right before that same view
// re-renders and recreates its own charts (see renderPoList()/
// renderMaterialsChart()). modalCharts = charts owned by whatever's
// currently open in #modalBody (material stock/price trend) - destroyed on
// every modal close/reopen. Two separate arrays on purpose: closeModal()
// used to call the single destroyPageCharts(), which also silently wiped
// the dashboard's own PO trend/status charts (and the materials drill-down
// chart) every time a PO/material modal closed, since they shared one
// registry - the canvases went blank and nothing ever redrew them, because
// closing a modal doesn't re-run renderPoList()/renderMaterialsView(). Bug
// found and fixed 2026-09-04 - don't merge these back into one array.
let pageCharts = [];
let modalCharts = [];
function destroyPageCharts() { refreshChartTheme(); pageCharts.forEach(c => { try { c.destroy(); } catch (e) { console.warn('Chart.destroy() failed:', e); } }); pageCharts = []; }
function destroyModalCharts() { modalCharts.forEach(c => { try { c.destroy(); } catch (e) { console.warn('Chart.destroy() failed:', e); } }); modalCharts = []; }

// Bumped by every openPoModal()/openImportPoModal()/openMaterialModal()/
// openRodtepScriptDetail() call, and captured as each call's own
// `myModalRequestId`. Each function re-checks this after every await before
// touching #modalBody - without it, clicking row A then quickly row B
// before A's fetch resolves can let A's response land after B's and
// silently overwrite the modal (now showing B's title/backdrop) with A's
// stale data. Bug found and fixed for openImportPoModal()/openMaterialModal()
// 2026-09-04; openPoModal()/openRodtepScriptDetail() were missed at the time
// and got the same fix during a later full-codebase audit - if you add a
// fifth modal-opening function, apply this guard there too.
let modalRequestId = 0;


// ── Chart helpers (PO Value Trend / Status Breakdown) ──────────────────
// 'YYYY-MM' -> 'Mon YYYY' for chart axis labels - the raw ISO-ish month key
// used for grouping/sorting (monthTotals) is unreadable as an axis label.
const MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
function formatMonthLabel(m) {
  const parts = m.split('-');
  const idx = parseInt(parts[1], 10) - 1;
  return (MONTH_NAMES[idx] || parts[1]) + ' ' + parts[0];
}
// ── One look for every page chart ───────────────────────────────────────
// Set once on Chart.defaults, so the four page charts share one font, ink
// colour and tooltip style instead of each carrying its own copy. Legends
// are HTML (chartLegendHtml() below), never Chart.js's canvas legend: the
// canvas legend cannot wrap long labels, so on a narrow panel it clipped or
// overlapped them ("Delivery Date Unknow...").
let CHART_INK = '#475569';
let CHART_GRID = '#eef1f5';
let CHART_STRONG = '#1A2535';
let CHART_MUTED = '#94a3b8';
// Reads the chart colours from the stylesheet's tokens, so a chart drawn in
// dark mode uses dark-mode ink and grid lines. Called by destroyPageCharts(),
// which every page view runs right before drawing its charts - so a theme
// toggle is picked up on the next render.
function refreshChartTheme() {
  const css = getComputedStyle(document.documentElement);
  const read = (name, fallback) => (css.getPropertyValue(name) || '').trim() || fallback;
  CHART_INK = read('--slate-soft', '#475569');
  CHART_GRID = read('--border-soft', '#eef1f5');
  CHART_STRONG = read('--navy', '#1A2535');
  CHART_MUTED = read('--gray', '#94a3b8');
  if (typeof Chart !== 'undefined') Chart.defaults.color = CHART_INK;
}
if (typeof Chart !== 'undefined') {
  Chart.defaults.font.family = "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif";
  Chart.defaults.font.size = 11.5;
  Chart.defaults.color = CHART_INK;
  Chart.defaults.plugins.legend.display = false;
  Object.assign(Chart.defaults.plugins.tooltip, {
    backgroundColor: '#1A2535', padding: 10, cornerRadius: 8, boxPadding: 4, usePointStyle: true,
    titleFont: { size: 12, weight: '700' }, bodyFont: { size: 12 }, footerFont: { size: 11, weight: '400' },
    footerColor: '#cbd5e1',
    // HTML tooltip, not the canvas one - see htmlChartTooltip().
    enabled: false,
    external: context => htmlChartTooltip(context),
  });
}

// Every chart's tooltip, drawn as a DOM element over the canvas instead of
// painted into it. The canvas tooltip was painted into the chart's bitmap,
// so on a scaled display (125% here) its text came out soft, it was cut off
// at the canvas edge, and anything drawn after it (the doughnut's centre
// label) printed on top of it. A DOM tooltip is crisp at any zoom, can sit
// outside the chart area, and is always on top. Text goes in through
// textContent; the colour dot and position are set through el.style, which
// CSP allows (only style="" attributes in markup are blocked).
function htmlChartTooltip(context) {
  const { chart, tooltip } = context;
  const host = chart.canvas.parentNode;
  if (!host) return;
  if (getComputedStyle(host).position === 'static') host.style.position = 'relative';
  let el = host.querySelector(':scope > .chart-tooltip');
  if (!el) {
    el = document.createElement('div');
    el.className = 'chart-tooltip';
    el.setAttribute('aria-hidden', 'true');
    host.appendChild(el);
  }
  if (!tooltip || tooltip.opacity === 0 || !(tooltip.body || []).some(b => b.lines.length)) {
    el.classList.remove('show');
    return;
  }
  el.replaceChildren();
  const line = (cls, text) => { const d = document.createElement('div'); d.className = cls; d.textContent = text; return d; };
  (tooltip.title || []).filter(Boolean).forEach(t => el.appendChild(line('chart-tooltip-title', t)));
  const showDots = tooltip.options.displayColors !== false;
  (tooltip.body || []).forEach((b, i) => b.lines.forEach(text => {
    const row = line('chart-tooltip-row', '');
    if (showDots && tooltip.labelColors[i]) {
      const dot = document.createElement('span');
      dot.className = 'chart-tooltip-dot';
      dot.style.background = tooltip.labelColors[i].backgroundColor;
      row.appendChild(dot);
    }
    row.appendChild(document.createTextNode(String(text).trim()));
    el.appendChild(row);
  }));
  (tooltip.body || []).forEach(b => (b.after || []).filter(Boolean).forEach(t => el.appendChild(line('chart-tooltip-foot', t))));
  (tooltip.afterBody || []).concat(tooltip.footer || []).filter(Boolean).forEach(t => el.appendChild(line('chart-tooltip-foot', t)));
  el.classList.add('show');
  // Centred over the hovered point, above it when there is room, kept
  // inside the chart's own width so it never runs off the panel.
  const w = el.offsetWidth, h = el.offsetHeight;
  const x = Math.max(0, Math.min(tooltip.caretX - w / 2, host.clientWidth - w));
  const y = tooltip.caretY - h - 12 >= -8 ? tooltip.caretY - h - 12 : tooltip.caretY + 14;
  el.style.left = Math.round(x) + 'px';
  el.style.top = Math.round(y) + 'px';
}

// 'YYYY-MM' -> "Jan '26" - short enough that nine or twelve months fit on
// the axis without tilting.
function shortMonthLabel(m) {
  const parts = m.split('-');
  return (MONTH_NAMES[parseInt(parts[1], 10) - 1] || parts[1]) + " '" + parts[0].slice(2);
}

// A chart panel's header: title, a one-line plain-English subtitle saying
// what the chart measures, and an optional figure on the right.
function chartHeadHtml(title, sub, asideLabel, asideValue) {
  return '<div class="chart-head"><div class="chart-head-text"><h4>' + escapeHtml(title) + '</h4>' +
    (sub ? '<div class="chart-sub">' + escapeHtml(sub) + '</div>' : '') + '</div>' +
    (asideValue != null ? '<div class="chart-aside"><div class="chart-aside-val">' + escapeHtml(asideValue) + '</div>' +
      '<div class="chart-aside-label">' + escapeHtml(asideLabel || '') + '</div></div>' : '') +
    '</div>';
}

// HTML legend: groups of {title?, items: [{key, label, color, valText}]}.
// An item with a key is a button that filters like its KPI card
// (wireChartLegend()); one without is plain text. Items wrap, so a long
// label never clips. `rows` lays each item out as a table row (label left,
// count and share in aligned columns right) instead of a chip.
function chartLegendHtml(groups, selectedKey, rows) {
  return '<div class="chart-legend' + (rows ? ' chart-legend-rows' : '') + '">' + groups.map(g =>
    '<div class="chart-legend-group">' +
      (g.title ? '<div class="chart-legend-title">' + escapeHtml(g.title) + '</div>' : '') +
      '<div class="chart-legend-items">' + g.items.map(it => {
        const active = it.key && it.key === selectedKey;
        const inner = '<span class="legend-dot" data-dot-color="' + escapeHtml(it.color) + '"></span>' +
          '<span class="legend-label">' + escapeHtml(it.label) + '</span>' +
          (it.valText != null ? '<span class="legend-val">' + escapeHtml(it.valText) + '</span>' : '') +
          (it.pctText != null ? '<span class="legend-pct">' + escapeHtml(it.pctText) + '</span>' : '');
        return it.key
          ? '<button type="button" class="legend-chip legend-chip-btn' + (active ? ' active' : '') + '" data-legend-key="' + escapeHtml(it.key) + '" aria-pressed="' + !!active + '">' + inner + '</button>'
          : '<span class="legend-chip">' + inner + '</span>';
      }).join('') + '</div>' +
    '</div>').join('') + '</div>';
}
function wireChartLegend(root, onPick) {
  if (!root) return;
  applyDynamicStyles(root);
  root.querySelectorAll('[data-legend-key]').forEach(b => { b.onclick = () => onPick(b.dataset.legendKey); });
}

// '12%', or '<1%' for a small non-zero share, which '0%' would misstate.
function sharePct(n, total) {
  const pct = n / total * 100;
  return (n > 0 && pct < 1) ? '<1%' : Math.round(pct) + '%';
}

// The legend for renderTwoRingDoughnut(): one group per ring, each slice's
// count and share of all POs.
function twoRingLegendHtml(rings, selectedKey) {
  const group = (title, ring) => {
    const total = ring.reduce((a, s) => a + s.val, 0);
    return { title, items: ring.filter(s => s.val > 0).map(s => ({
      key: s.key, label: s.label, color: s.color,
      valText: String(s.val), pctText: total ? sharePct(s.val, total) : null,
    })) };
  };
  return chartLegendHtml([group('What has arrived (inner ring)', rings.inner), group('Delivery date (outer ring)', rings.outer)], selectedKey, true);
}

// Every 'YYYY-MM' from the earliest to the latest key given, in order, so a
// month with no orders shows as an empty slot on the axis instead of being
// skipped (which made two bars a quarter apart look consecutive).
function fillMonthRange(keys) {
  if (!keys.length) return [];
  const sorted = keys.slice().sort();
  const out = [];
  let [y, m] = sorted[0].split('-').map(Number);
  const [ey, em] = sorted[sorted.length - 1].split('-').map(Number);
  while (y < ey || (y === ey && m <= em)) {
    out.push(y + '-' + String(m).padStart(2, '0'));
    if (++m > 12) { m = 1; y++; }
  }
  return out;
}
// A two-ring status doughnut: `inner` is what has ARRIVED (received /
// partial / nothing), `outer` is the DELIVERY DATE (overdue / on order /
// date unknown / the rest). Each ring is a partition of the same POs, so
// each status KPI card has exactly one slice carrying its own number - a
// single ring could not, because "Overdue" and "Date Unknown" cut across
// received/partial (a card read 142 while its slice read 7).
//
// Slices are {key, label, val, color}; a null key is not clickable. Chart.js
// shares one labels array across datasets, so every slice gets an index in
// the combined list and each ring carries zeros for the other ring's slices.
function renderTwoRingDoughnut(canvas, { outer, inner, selectedKey, onPick, centerPlugin }) {
  const slices = outer.concat(inner);
  const ringData = (ring, offset) => slices.map((s, i) => (i >= offset && i < offset + ring.length ? s.val : 0));
  const ringTotal = ring => ring.reduce((a, s) => a + s.val, 0);
  const colors = slices.map(s => s.color);
  // Segment separators in the card colour, so they read as gaps in dark mode too.
  const ringGap = (getComputedStyle(document.documentElement).getPropertyValue('--card') || '').trim() || '#ffffff';
  return new Chart(canvas, {
    type: 'doughnut',
    data: {
      labels: slices.map(s => s.label),
      datasets: [
        { data: ringData(outer, 0), backgroundColor: colors, borderWidth: 2, borderColor: ringGap, borderRadius: 2, weight: 1,
          offset: slices.map(s => (s.key && s.key === selectedKey ? 10 : 0)) },
        { data: ringData(inner, outer.length), backgroundColor: colors, borderWidth: 2, borderColor: ringGap, borderRadius: 2, weight: 1,
          offset: slices.map(s => (s.key && s.key === selectedKey ? 10 : 0)) },
      ],
    },
    options: {
      maintainAspectRatio: false,
      cutout: '56%',
      onClick: (evt, elements) => {
        if (!elements.length) return;
        const s = slices[elements[0].index];
        if (s.key) onPick(s.key);
      },
      onHover: (evt, elements) => {
        evt.native.target.style.cursor = elements.length && slices[elements[0].index].key ? 'pointer' : 'default';
      },
      // The legend is HTML beside the canvas - twoRingLegendHtml().
      plugins: {
        tooltip: {
          filter: c => c.parsed > 0,
          callbacks: {
            title: items => (items.length && items[0].datasetIndex === 0 ? 'Delivery date' : 'What has arrived'),
            label: c => {
              const t = ringTotal(c.datasetIndex === 0 ? outer : inner);
              return ' ' + c.label + ': ' + c.parsed + (t ? ' (' + sharePct(c.parsed, t) + ')' : '');
            },
            afterLabel: c => (slices[c.dataIndex].key ? 'Click to filter the list below' : ''),
          },
        },
      },
    },
    plugins: centerPlugin ? [centerPlugin] : [],
  });
}
// Chart.js plugin (scoped per-chart via options.plugins array, not globally
// registered) that draws the slice total in the doughnut's own cutout hole -
// a "how many POs total" readout the legend/slices alone don't give at a
// glance. Self-contained: reads whatever dataset the chart it's attached to
// actually has, no dependency on the module-level `state`. Drawn in
// afterDatasetsDraw, not afterDraw: afterDraw runs after the tooltip, so the
// centre text used to print on top of it and garble it.
const centerTextPlugin = {
  id: 'poStatusCenterText',
  afterDatasetsDraw(chart) {
    const area = chart.chartArea;
    if (!area) return;
    const data = chart.data.datasets[0].data;
    const total = data.reduce((a, b) => a + b, 0);
    const ctx = chart.ctx;
    const cx = (area.left + area.right) / 2;
    const cy = (area.top + area.bottom) / 2;
    ctx.save();
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.font = "700 22px -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif";
    ctx.fillStyle = CHART_STRONG;
    ctx.fillText(String(total), cx, cy - 9);
    ctx.font = "700 9.5px -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif";
    ctx.fillStyle = CHART_MUTED;
    ctx.fillText('TOTAL POs', cx, cy + 11);
    ctx.restore();
  },
};

function closeModal() {
  // A half-written correction is real work - closing the modal used to drop
  // it (value AND reason) with nothing asked. cancelFieldCorrection() only
  // prompts when something was actually typed, so the common case of closing
  // a modal you were merely reading is unaffected. Guarded by typeof because
  // charts.js also loads on pages that never render a correction box.
  if (typeof cancelFieldCorrection === 'function' && typeof SELECTED_FIELD !== 'undefined' && SELECTED_FIELD) {
    if (!cancelFieldCorrection(false)) return;
  }
  document.getElementById('modalBackdrop').classList.remove('open');
  destroyModalCharts();
  // Removes the Escape/Tab-trap listeners and returns focus to whatever
  // opened the modal - see shared.js's openModalA11y(). Guarded because
  // charts.js also loads on pages that never call openModalA11y().
  if (typeof closeModalA11y === 'function') closeModalA11y();
}

// Every modal's own "×" close button used to carry a literal
// onclick="closeModal()" HTML attribute (po-modal.js/import-po.js/
// material-modal.js) - an inline event-handler attribute, which CSP's
// script-src treats the same as an inline <script> block. Replaced with
// this one delegated listener (added 2026-09-05, hardening pass) so
// script-src can drop 'unsafe-inline' - see config/security_headers.py.
document.addEventListener('click', (e) => {
  if (e.target.closest('.close-btn')) closeModal();
});


// Same idea as centerTextPlugin (PO status donut) - "TOTAL POs" -> "IMPORT
// POs" center label, everything else identical.
const centerImportTextPlugin = {
  id: 'importStageCenterText',
  afterDatasetsDraw(chart) {
    const area = chart.chartArea;
    if (!area) return;
    const data = chart.data.datasets[0].data;
    const totalV = data.reduce((a, b) => a + b, 0);
    const ctx = chart.ctx;
    const cx = (area.left + area.right) / 2;
    const cy = (area.top + area.bottom) / 2;
    ctx.save();
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.font = "700 22px -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif";
    ctx.fillStyle = CHART_STRONG;
    ctx.fillText(String(totalV), cx, cy - 9);
    ctx.font = "700 9.5px -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif";
    ctx.fillStyle = CHART_MUTED;
    ctx.fillText('IMPORT POs', cx, cy + 11);
    ctx.restore();
  },
};

// Trailing calendar-day moving average of `points` (each {date:'YYYY-MM-DD',
// price}) as of points[i]'s own date - averages every point whose date falls
// in [date - days, date], not a fixed-size window over the sorted array,
// since POs happen irregularly rather than daily (mirrors the reference
// design's own caption for this - see renderMaterialPriceTrendTab()).
function trailingPriceAvg(points, i, days) {
  const d = new Date(points[i].date + 'T00:00:00');
  const start = new Date(d);
  start.setDate(start.getDate() - days);
  const inWindow = points.filter(p => {
    const pd = new Date(p.date + 'T00:00:00');
    return pd >= start && pd <= d;
  });
  return inWindow.length ? inWindow.reduce((s, p) => s + p.price, 0) / inWindow.length : null;
}

