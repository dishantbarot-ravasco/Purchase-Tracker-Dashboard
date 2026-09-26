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
function destroyPageCharts() { pageCharts.forEach(c => { try { c.destroy(); } catch (e) { console.warn('Chart.destroy() failed:', e); } }); pageCharts = []; }
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
// Chart.js plugin (scoped per-chart via options.plugins array, not globally
// registered) that draws the slice total in the doughnut's own cutout hole -
// a "how many POs total" readout the legend/slices alone don't give at a
// glance. Self-contained: reads whatever dataset the chart it's attached to
// actually has, no dependency on the module-level `state`.
const centerTextPlugin = {
  id: 'poStatusCenterText',
  afterDraw(chart) {
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
    ctx.fillStyle = '#0f1b2d';
    ctx.fillText(String(total), cx, cy - 9);
    ctx.font = "700 9.5px -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif";
    ctx.fillStyle = '#94a3b8';
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
  afterDraw(chart) {
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
    ctx.fillStyle = '#0f1b2d';
    ctx.fillText(String(totalV), cx, cy - 9);
    ctx.font = "700 9.5px -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif";
    ctx.fillStyle = '#94a3b8';
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

