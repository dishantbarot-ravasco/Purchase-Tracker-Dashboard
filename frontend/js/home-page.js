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
  if (user.role === 'admin') document.getElementById('adminCard').style.display = '';

  await loadKpis();

  document.getElementById('loadingOverlay').style.display = 'none';
  document.getElementById('mainContent').style.display = '';
})();

// KPIs are real, computed client-side from all 3 plants' live PO data -
// fetched in parallel here rather than reusing js/main.js's per-plant
// cache (this page never loads main.js - it's a lightweight landing
// page, not the full dashboard). A fetch failure for one plant must not
// blank the whole row - each plant's data is optional, the KPIs sum
// whatever loaded successfully and this is disclosed via console.error,
// never silently treated as "0 POs for that plant".
async function loadKpis() {
  const results = await Promise.all(PLANT_KEYS.map(async key => {
    try {
      const data = await apiForPlant(key, '/purchase-orders');
      return data.purchaseOrders || [];
    } catch (e) {
      console.error('loadKpis: failed to load purchase orders for ' + key + ':', e);
      return null; // distinct from [] - "failed to load", not "zero POs"
    }
  }));

  const loaded = results.filter(r => r !== null);
  const allPos = loaded.flat();

  animateCountUp(document.getElementById('kpiTotalPos'), allPos.length);

  const suppliers = new Set();
  allPos.forEach(po => { if (po.vendorName && po.vendorName.trim()) suppliers.add(po.vendorName.trim().toLowerCase()); });
  animateCountUp(document.getElementById('kpiSuppliers'), suppliers.size);

  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const todayISO = today.toISOString().slice(0, 10);
  const monthPrefix = todayISO.slice(0, 7);
  const weekCutoff = new Date(today);
  weekCutoff.setDate(weekCutoff.getDate() - 6);
  const weekCutoffISO = weekCutoff.toISOString().slice(0, 10);

  const thisMonthCount = allPos.filter(po => po.createdDate && po.createdDate.startsWith(monthPrefix)).length;
  const thisWeekCount = allPos.filter(po => po.createdDate && po.createdDate >= weekCutoffISO && po.createdDate <= todayISO).length;

  animateCountUp(document.getElementById('kpiThisMonth'), thisMonthCount);
  document.getElementById('kpiThisMonthSub').textContent = today.toLocaleString('en-IN', { month: 'long', year: 'numeric' });
  animateCountUp(document.getElementById('kpiThisWeek'), thisWeekCount);

  if (loaded.length < PLANT_KEYS.length) {
    const row = document.getElementById('kpiRow');
    const note = document.createElement('div');
    note.className = 'stat-sub';
    note.style.cssText = 'grid-column:1/-1;color:var(--red);margin-top:-8px;';
    note.textContent = 'Note: ' + (PLANT_KEYS.length - loaded.length) + ' plant(s) failed to load - these totals are undercounted. Refresh to retry.';
    row.parentNode.insertBefore(note, row.nextSibling);
  }
}
