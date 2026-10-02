// home.html's page-bootstrap script - extracted from an inline <script> so
// script-src can drop 'unsafe-inline' (see config/security_headers.py).
// Content unchanged from the inline version - a straight extraction, not a
// rewrite.
(async function () {
  const user = await requireAuth();
  if (!user) return; // requireAuth() already redirected to /login.html

  const firstName = (user.fullName || user.email || '').split(' ')[0].split('@')[0];
  document.getElementById('heroName').textContent = firstName || user.email;
  renderNavTabs(document.getElementById('navTabs'), 'home');
  renderUserBadge(document.getElementById('navUser'));
  initThemeToggle();
  if (user.role === 'admin') document.getElementById('adminCard').hidden = false;
  // Each quick-action card only for a page this account may open (auth.js).
  document.querySelectorAll('#actionCards a.action-card[data-page]').forEach(card => {
    if (!canOpenPage(card.dataset.page)) card.hidden = true;
  });
  if (!document.querySelector('#actionCards a.action-card:not([hidden])')) {
    ['actionsTitle', 'actionCards'].forEach(id => { document.getElementById(id).hidden = true; });
  }
  if (isLockedAccount(user)) document.getElementById('lockedNotice').hidden = false;

  // The KPI row counts purchase orders, which needs an order view.
  if (userHasPerm('view_dashboard', 'view_on_order', 'view_stock_orders')) await loadKpis();
  else ['kpiTitle', 'kpiRow'].forEach(id => { document.getElementById(id).hidden = true; });

  document.getElementById('loadingOverlay').style.display = 'none';
  document.getElementById('mainContent').hidden = false;
})();

// loadKpis() moved to shared.js (2026-09-07) so the Admin Panel's Overview
// tab can reuse the exact same Total PO's/Suppliers/This Month/This Week
// computation against the same #kpiTotalPos/#kpiSuppliers/#kpiThisMonth/
// #kpiThisWeek/#kpiRow element ids, instead of a second near-duplicate
// aggregation living in admin-page.js.
