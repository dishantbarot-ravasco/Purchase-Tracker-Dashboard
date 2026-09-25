// ── Raw Material Analysis (list rendering) ──────────────────────────────
// One row per Stock lot (see file header). No purchase-type split here (see
// the module docstring at the top of this file for why) - only the
// plant/All-Plants axis applies.
function currentMaterials() {
  const keys = selectedPlantKeys();
  if (keys.length === 1) return MATERIALS_BY_PLANT[keys[0]] || [];
  const merged = [];
  keys.forEach(key => {
    (MATERIALS_BY_PLANT[key] || []).forEach(m => {
      merged.push(Object.assign({}, m, { _plantKey: key, _plantLabel: PLANTS[key].label }));
    });
  });
  return merged;
}

async function ensureMaterialsLoaded(plantKeys) {
  await Promise.all(plantKeys.map(async key => {
    if (!MATERIALS_BY_PLANT[key]) {
      const data = await apiForPlant(key, '/materials');
      MATERIALS_BY_PLANT[key] = data.materials;
    }
  }));
}

async function loadAndRenderMaterials() {
  const el = document.getElementById('viewContent');
  el.innerHTML = '<div class="load-banner"><div class="spinner"></div><div>Loading materials&hellip;</div></div>';
  try {
    // Also loads Purchase Orders for the same plant(s) - the KPI row and
    // drill-down chart below need PO line items to compute the "in transit"/
    // discrepancy/data-quality numbers (see materialLinksToItem()), not just
    // Stock data. Import orders too, since 2026-09-24 - see materialOrders().
    // Cheap: all three loaders cache, so this is a no-op re-fetch if the
    // Purchase Orders tab was already visited for the same plant(s).
    await Promise.all([ensureMaterialsLoaded(selectedPlantKeys()), ensurePOsLoaded(selectedPlantKeys()), ensureImportPOsLoaded()]);
    el.innerHTML = '<div id="materialsContent"></div>';
    renderMaterialsView();
    return true;
  } catch (e) {
    console.error('loadAndRenderMaterials failed:', e);
    el.innerHTML = '<div class="noaccess">Couldn\'t load material data right now. Please refresh, or contact IT if this keeps happening.</div>';
    // main.js's loadAndRender() reads this so a refresh never reports
    // success over this error panel.
    return false;
  }
}

// ── Material <-> PO line item linkage (best-effort, not ground truth) ──────
// There's no persisted DB join between a Stock lot and a PO line item (the
// only real chain is Stock<->MIR<->PO via *MirStockMatch/*POMirMatch, which
// by construction only ever covers already-delivered/matched goods - never
// an open, undelivered PO). So "which POs/vendors belong to this material"
// is computed the same best-effort way this app's own backend already
// computes PO<->MIR matches (apps/services/matching.py): normalized
// description equality (always linked), or token-overlap above
// MATERIAL_LINK_THRESHOLD gated on vendor when the material has one (Achhad
// Stock rows have no vendor column at all - description-only there, same
// precedent as matching_achhad.py's weaker material-only gate). NOT
// guaranteed-correct identity resolution - same "verify manually" caveat
// this app already gives PO<->MIR matches (see matchStatusHtml()).
const MATERIAL_LINK_THRESHOLD = 0.3;
// PER-RENDER MEMOIZATION (2026-09-21). materialLinksToItem() below is called
// once per (material x PO line item) pair - 703 stock lots against 1,112
// active line items on "All Plants" is 781,736 calls per render, on live
// data. It used to redo, on EVERY ONE of those calls, work that depends only
// on one string: two normalizeMaterial() regex passes, two tokenize+Set
// builds, a third Set for the union, and two normalizeVendor() passes (the
// vendor one runs a seven-alternative regex). There are only ~1,800 distinct
// strings behind those 781,736 calls.
//
// Measured in a browser at that exact scale: 3,926 ms -> 136 ms, a 29x
// speed-up, with byte-identical results (same 2,421 links, asserted pair by
// pair, not just counted). That is the single biggest cost in rendering Raw
// Material Analysis - the endpoint behind it answers in 33-51 ms.
//
// CLEARED AT THE START OF EVERY computeMaterialPoLinkage() PASS, deliberately:
// the whole win is WITHIN one render, and an "Edit Everywhere" save can change
// a description or a vendor name between renders. A cache that outlived the
// pass would serve the old string's tokens and silently link the wrong
// material - exactly the class of bug this file's own comments keep warning
// about. Clearing costs nothing measurable.
const _materialTokenCache = new Map();
const _vendorNormCache = new Map();

function materialTokenInfo(description) {
  let info = _materialTokenCache.get(description);
  if (info === undefined) {
    const norm = normalizeMaterial(description);
    info = { norm, tokens: new Set(norm ? norm.split(' ') : []) };
    _materialTokenCache.set(description, info);
  }
  return info;
}

function vendorNormCached(name) {
  let normalized = _vendorNormCache.get(name);
  if (normalized === undefined) {
    normalized = normalizeVendor(name);
    _vendorNormCache.set(name, normalized);
  }
  return normalized;
}

// How well `item` links to `material`: 2 for the same normalized name, the
// token Jaccard (>= MATERIAL_LINK_THRESHOLD) for a fuzzy link, -1 for none.
// The vendor gate applies to both, exactly as materialLinksToItem() applies it.
function materialLinkScore(material, po, item) {
  const matInfo = materialTokenInfo(material.description);
  const itemInfo = materialTokenInfo(item.description);
  if (!matInfo.norm || !itemInfo.norm) return -1;
  let score;
  if (matInfo.norm === itemInfo.norm) {
    score = 2;
  } else {
    const a = matInfo.tokens;
    const b = itemInfo.tokens;
    if (!a.size || !b.size) return -1;
    let overlapCount = 0;
    a.forEach(t => { if (b.has(t)) overlapCount++; });
    score = overlapCount / (a.size + b.size - overlapCount);
    if (score < MATERIAL_LINK_THRESHOLD) return -1;
    // A fabric of another width or grade is another material, however many
    // words the two descriptions share ("EE-200 fabric roll, width 73cm ...,
    // length 696m" tied with a dozen EE-200 rolls of other widths).
    if (fabricSpecContradicts(fabricSpec(material.description), fabricSpec(item.description))) return -1;
  }
  return vendorGatePasses(material, po) ? score : -1;
}

// Series+grade and width (whole cm) of a conveyor-belt fabric description -
// a port of matching_core._fabric_spec(), same patterns: "NN-200 fabric
// roll, width 67cm" -> ['NN200', 67], MIR's "EE250 142CM" -> ['EE250', 142].
// Memoized per string for the render, like materialTokenInfo().
const _fabricSpecCache = new Map();
function fabricSpec(text) {
  const key = text || '';
  let spec = _fabricSpecCache.get(key);
  if (spec === undefined) {
    const series = /\b(EEH|EE|NN|EP)\s*-?\s*(\d{2,4})/i.exec(key);
    const width = /(\d{2,3}(?:\.\d+)?)\s*CM\b/i.exec(key);
    spec = [series ? series[1].toUpperCase() + String(parseInt(series[2], 10)) : null, width ? Math.round(parseFloat(width[1])) : null];
    _fabricSpecCache.set(key, spec);
  }
  return spec;
}
// Both name a series+grade and they differ, or both a width more than 1 cm
// apart - matching_core._fabric_spec_contradicts().
function fabricSpecContradicts(a, b) {
  if (a[0] && b[0] && a[0] !== b[0]) return true;
  return !!(a[1] && b[1] && Math.abs(a[1] - b[1]) > 1);
}

// materialLinksToItem()'s vendor gate on its own: a material with vendors
// links only to a PO from one of them; one without (Achhad) links on
// description alone.
function vendorGatePasses(material, po) {
  const vendorCandidates = (material.vendors && material.vendors.length) ? material.vendors : (material.vendor ? [material.vendor] : []);
  if (!vendorCandidates.length) return true;
  const poVendor = vendorNormCached(po.vendorName);
  return vendorCandidates.some(v => vendorContains(vendorNormCached(v), poVendor));
}

function materialLinksToItem(material, po, item) {
  const matInfo = materialTokenInfo(material.description);
  const itemInfo = materialTokenInfo(item.description);
  if (!matInfo.norm || !itemInfo.norm) return false;
  if (matInfo.norm === itemInfo.norm) return true;
  const a = matInfo.tokens;
  const b = itemInfo.tokens;
  if (!a.size || !b.size) return false;
  let overlapCount = 0;
  a.forEach(t => { if (b.has(t)) overlapCount++; });
  // |A u B| = |A| + |B| - |A n B|, which is what the third Set this used to
  // allocate was measuring. Identical value, no allocation.
  const union = a.size + b.size - overlapCount;
  if (overlapCount / union < MATERIAL_LINK_THRESHOLD) return false;
  // `material` is either a single Stock lot (real `.vendor` string) or an
  // aggregateMaterialsByName() group (real `.vendors` array, one per
  // distinct contributing lot's vendor) - gate on whichever is present, any
  // one of a group's vendors clearing the gate is enough. Neither present
  // (Achhad has no vendor column at all) - description-only, same weaker-
  // gate precedent as matching_achhad.py.
  const vendorCandidates = (material.vendors && material.vendors.length) ? material.vendors : (material.vendor ? [material.vendor] : []);
  if (vendorCandidates.length) {
    const poVendor = vendorNormCached(po.vendorName);
    return vendorCandidates.some(v => vendorContains(vendorNormCached(v), poVendor));
  }
  return true;
}

// Days-left confidence bands, weakest to strongest (see
// apps/services/consumption_periods.py's own _BANDS) - used both to pick a
// group's overall confidence (the weakest contributing lot's band, never
// the best or a mean - see aggregateMaterialsByName() below) and to color
// the confidence dot (daysLeftCellHtml()).
//
// The backend's bands key on COVERAGE of the window since 2026-09-21, not
// on the span between the first and last snapshot. Expect more amber and
// fewer green dots than before until the snapshot job runs every day -
// that is the figure becoming honest about thin history, not a regression.
const CONF_RANK = { none: 0, low: 1, medium: 2, high: 3 };
const CONF_DOT_CLASS = { high: 'conf-dot-high', medium: 'conf-dot-medium', low: 'conf-dot-low', none: 'conf-dot-low' };

// One row per unique material NAME within the current plant scope (sums
// qty/value across every vendor lot contributing to it, and across all 3
// plants when "All Plants" is selected) - per the project owner's
// 2026-09-04 request, matching CLAUDE.md's long-planned "Materials
// consolidated cross-plant view" roadmap item. Scoped to selectedPlantKeys()
// like the rest of this view (not always cross-plant) - a single plant tab
// sums only that plant's own vendor lots, "All Plants" sums across all
// three. Never silently loses the underlying per-vendor/per-plant detail
// CLAUDE.md warned about losing (`lots` keeps every contributing row,
// `vendors` a deduped list) - the row-click modal and materialLinksToItem()
// above both use these, not just the summed totals.
function aggregateMaterialsByName(lots) {
  const groups = new Map();
  lots.forEach(m => {
    const key = normalizeMaterial(m.description);
    if (!key) return;
    if (!groups.has(key)) {
      groups.set(key, { description: m.description, materialCode: m.materialCode, category: '', subCategory: '', qty: 0, value: 0, rates: new Set(), vendorSeen: new Set(), vendors: [], lots: [] });
    }
    const g = groups.get(key);
    if (!g.category && m.category) g.category = m.category;
    if (!g.subCategory && m.subCategory) g.subCategory = m.subCategory;
    g.qty += (m.qty || 0);
    g.value += (m.value || 0);
    if (m.rate != null) g.rates.add(m.rate);
    if (m.vendor) {
      const nv = normalizeVendor(m.vendor);
      if (nv && !g.vendorSeen.has(nv)) { g.vendorSeen.add(nv); g.vendors.push(m.vendor); }
    }
    g.lots.push(m);
  });
  // Rate is only shown when every contributing lot agrees on it - once
  // summed across vendors/plants a single "rate" is otherwise misleading
  // (which lot's rate would it even be?), same reasoning as the reference
  // design's own "Not available" cells for multi-lot materials.
  return Array.from(groups.values()).map(g => {
    // Days-left is not summable (averaging/adding days across lots is
    // simply wrong) - sum the consumption *rates*, then divide the
    // already-summed quantity.
    //
    // **Count each rate ONCE PER PLANT, not once per lot (2026-09-21).**
    // The backend used to compute consumption per vendor lot, so summing
    // across g.lots was right. It now reads a MATERIAL-level ledger
    // (_domestic_base.py's _consumption_by_material()), so every sibling
    // lot of one material carries the identical figure and summing them
    // would multiply the rate by the lot count - a material HRS buys from
    // three vendors would read three times its real burn, and its days-left
    // a third of the truth. Deduping on _plantKey is what keeps "All
    // Plants" correct: the ledger is per plant, so three plants' rates for
    // one material genuinely do add up.
    //
    // Group confidence stays the weakest contributing band: one plant with
    // thin snapshot coverage makes the whole group's rate thin, and taking
    // the best or the mean would overstate it.
    //
    // **Days Left is each plant's own, and the row shows the tightest
    // (2026-09-25).** Pooling every plant's stock over every plant's burn let
    // one plant's large idle stock hide another plant about to run out - and
    // a plant with stock but no rate added its stock to the numerator while
    // adding nothing to the denominator. Stock is summed per plant, divided
    // by that plant's rate, and the smallest result is the row's.
    let avgDaily = 0;
    let weakest = null;
    const ratedPlants = new Map(); // plant key -> its avgDaily
    const plantQty = new Map();
    g.lots.forEach(l => {
      const c = l.consumption;
      const conf = (c && c.confidence) || 'none';
      const plantKey = l._plantKey || '';
      plantQty.set(plantKey, (plantQty.get(plantKey) || 0) + (l.qty || 0));
      if (c && c.avgDaily && !ratedPlants.has(plantKey)) {
        ratedPlants.set(plantKey, c.avgDaily);
        avgDaily += c.avgDaily;
      }
      if (!weakest || CONF_RANK[conf] < CONF_RANK[weakest.confidence]) {
        weakest = { confidence: conf, coverageDays: c ? c.coverageDays : 0, observedDays: c ? c.observedDays : 0, windowDays: c ? c.windowDays : 0 };
      }
    });
    const perPlantDays = [];
    ratedPlants.forEach((rate, plantKey) => {
      const q = plantQty.get(plantKey) || 0;
      if (q >= 0) perPlantDays.push(q / rate);
    });
    // Stock per unit: lots of one material can be booked in different units
    // (Rubber Process Oil 710 is KG at HRS, LTR at Vapi), and one bare sum
    // read 29,385. Blank units are left out of the check (Achhad's sheet
    // leaves most blank), so only two NAMED units that differ count.
    const qtyByUnit = new Map();
    g.lots.forEach(l => {
      const raw = String(l.uom || '').trim().toUpperCase().replace(/\.$/, '');
      if (!raw) return;
      const fam = MAT_UOM_FAMILIES[raw];
      const unit = fam ? fam[0] : raw;
      qtyByUnit.set(unit, (qtyByUnit.get(unit) || 0) + (l.qty || 0) * (fam ? fam[1] : 1));
    });
    const mixedUnits = qtyByUnit.size > 1;
    return {
      description: g.description, materialCode: g.materialCode,
      category: g.category, subCategory: g.subCategory,
      qty: g.qty, value: g.value,
      // Set only when the lots use more than one unit: the row then shows
      // this instead of the meaningless sum.
      qtyLabel: mixedUnits
        ? Array.from(qtyByUnit.entries()).map(([u, q]) => q.toLocaleString('en-IN', { maximumFractionDigits: 3 }) + ' ' + u).join(' + ')
        : null,
      rate: g.rates.size === 1 ? Array.from(g.rates)[0] : null,
      // True if ANY contributing lot has a real Stock<->MIR match (see the
      // backend's `mirMatched` field, apps/api/routers/*_views.py) - not a
      // frontend-computed proxy.
      mirMatched: g.lots.some(l => l.mirMatched),
      vendors: g.vendors, lots: g.lots, anchorLot: g.lots[0],
      consumption: {
        avgDaily: avgDaily > 0 ? avgDaily : null,
        // Negative stock is a sheet error, not an empty store: no Days Left
        // for it, so it can neither read as a real figure nor trip Low
        // Stock. Same rule as consumption_periods.days_of_cover().
        daysLeft: perPlantDays.length && !mixedUnits ? Math.min(...perPlantDays) : null,
        negativeStock: g.qty < 0,
        confidence: weakest ? weakest.confidence : 'none',
        coverageDays: weakest ? weakest.coverageDays : 0,
        observedDays: weakest ? weakest.observedDays : 0,
        windowDays: weakest ? weakest.windowDays : 0,
      },
    };
  });
}

// Below-15-days-of-cover, or already at/below Achhad's msl reorder point
// (daysToMsl === 0 - see _domestic_base.py's _lot_dict()) on any
// contributing lot - wired into the Status filter (matStatusFilter
// 'lowstock') alongside the existing qtydisc/ratedisc/flags states.
// The days-of-cover half counts only when daysLeftCellHtml() shows the
// number: under band 'none' the cell reads "-", and a Low Stock row whose
// Days Left cell is blank cannot be checked by the person reading it. The
// msl half needs no rate, so it is not gated.
function isMaterialLowStock(m) {
  const c = m.consumption;
  if (c && c.confidence !== 'none' && c.daysLeft != null && c.daysLeft < 15) return true;
  return (m.lots || []).some(l => l.daysToMsl === 0);
}

// Days Left cell: the number plus a confidence dot (green/amber/grey),
// tooltipped with the coverage it's based on - the visible half of the
// decision to show a days-left figure even on thin history (see
// apps/services/consumption_engine.py's module docstring). Band 'none'
// renders "-" instead of a number - a two-day estimate must never look
// identical to a month-long one. A watched material that did not move
// arrives with its plant's band and a null rate (consumption_periods.py's
// MaterialRates.no_movement) and reads "No movement".
function daysLeftCellHtml(m) {
  const c = m.consumption;
  const confidence = (c && c.confidence) || 'none';
  const dotClass = CONF_DOT_CLASS[confidence] || CONF_DOT_CLASS.none;
  // Checked before the band: a negative quantity is a sheet error worth
  // seeing whatever the coverage.
  if (c && c.negativeStock) {
    return '<span class="days-left-cell"><span class="days-left-value">Stock &lt; 0</span>' +
      '<span class="info-tooltip conf-dot ' + dotClass + '" data-tooltip="The stock sheet shows a negative quantity for this material - a data error to fix in the sheet. Days Left cannot be worked out from it." tabindex="0"></span></span>';
  }
  // An order-only row has no stock at all, so it has no history to lack.
  if (m.orderOnly) {
    return '<span class="days-left-cell"><span class="days-left-value">-</span>' +
      '<span class="info-tooltip conf-dot ' + dotClass + '" data-tooltip="On order only - nothing of this material is in stock yet" tabindex="0"></span></span>';
  }
  if (confidence === 'none' || !c) {
    return '<span class="days-left-cell"><span class="days-left-value">-</span>' +
      '<span class="info-tooltip conf-dot ' + dotClass + '" data-tooltip="Not enough snapshot history yet" tabindex="0"></span></span>';
  }
  const valueText = c.daysLeft != null ? Math.round(c.daysLeft).toLocaleString('en-IN') + ' d' : 'No movement';
  // Coverage, not span. The tooltip used to read "based on N days of
  // history", where N was the gap between the first and last snapshot -
  // it counted the calendar, so 7 real days inside a 17-day span read as
  // "17 days". It now says how much of the window actually carries data
  // and how much of that was observed on a dated day rather than
  // interpolated across a snapshot gap.
  const windowDays = c.windowDays || 0;
  const coverage = c.coverageDays || 0;
  const observed = c.observedDays || 0;
  const tip = coverage + ' of ' + windowDays + ' days covered, ' + observed +
    ' observed directly' + (coverage > observed ? ', the rest averaged across snapshot gaps' : '');
  return '<span class="days-left-cell"><span class="days-left-value">' + escapeHtml(valueText) + '</span>' +
    '<span class="info-tooltip conf-dot ' + dotClass + '" data-tooltip="' + escapeHtml(tip) + '" tabindex="0"></span></span>';
}

// ── Import orders count too (2026-09-24) ────────────────────────────────
// Project owner: "there's a vendor in imports, Kumho Petrochemical, and we
// ordered SBR, but in open POs in the raw material analysis I can't find it".
// This view read the domestic order book only, so every open import order was
// missing from In Transit, Quantity Ordered, the order-only rows and the
// material modal - HRS PO 3000001141 (201,600 KG of SBR from Kumho) among
// them. Import orders are now folded in, in the same shape as a domestic
// order, so every rule below (the link, "is this line still open", the flag
// categories) applies to both without a second code path.
//
// PRICES ARE CONVERTED TO INR FIRST. An import line is priced in its PO's own
// currency (USD for nearly all of them) while everything this view adds up is
// INR; left raw, a USD order would count at roughly 1/90th of its value.
// Converted exactly as the matcher converts it (matching_core's
// _import_rate_value_inr()): net price x exchange rate, or the bare net price
// when no rate is on file - the same fallback, so a figure here never
// disagrees with the one the PO<->MIR match was scored on.
//
// ORDERED quantity, not BOE quantity: "still to come" is measured against
// what was ordered, the same as a domestic line. Open-ness is the same MIR
// test too - a line cleared through customs but not yet received into the
// plant is genuinely still in transit.
function importRateInr(item) {
  if (item.netPrice == null) return null;
  return item.exchangeRate ? item.netPrice * item.exchangeRate : item.netPrice;
}

// Tooltip for an import line's INR figure: the price as ordered and the rate
// it was converted at, so a reader can check the conversion rather than take
// it on trust - and is told plainly when no rate was on file.
function importPriceTitle(item) {
  if (item.foreignPrice == null) return 'Import order - no price on file';
  if (!item.exchangeRate) return 'Import order - price ' + item.foreignPrice + ' per unit; no exchange rate on file, so it is counted as INR as written';
  return 'Import order - price ' + item.foreignPrice + ' per unit x exchange rate ' + item.exchangeRate + ' = Rs ' + (item.foreignPrice * item.exchangeRate).toFixed(2);
}

function importPoAsMaterialOrder(po) {
  return {
    poNumber: po.poNumber,
    vendorName: po.vendorName,
    createdDate: po.createdDate,
    remarks: '',
    isImport: true,
    items: (po.items || []).map(it => {
      const m = it.mirMatch || {};
      const rate = importRateInr(it);
      return {
        description: it.description,
        category: it.category || '',
        subCategory: it.subCategory || '',
        qty: it.qtyAsPerPo,
        uom: it.uom,
        netPrice: rate,
        netValue: rate != null && it.qtyAsPerPo != null ? rate * it.qtyAsPerPo : null,
        deliveryDate: it.deliveryDate,
        // The domestic line item's match fields, read off the import line's
        // nested mirMatch (null when nothing crossed MATCH_THRESHOLD).
        matched: !!it.mirMatch,
        dismissedByOverride: !!m.dismissedByOverride,
        qtyDiffPct: m.qtyDiffPct != null ? m.qtyDiffPct : null,
        qtyOverDelivered: m.qtyOverDelivered != null ? m.qtyOverDelivered : null,
        rateDiffPct: m.rateDiffPct != null ? m.rateDiffPct : null,
        valueDiffPct: m.valueDiffPct != null ? m.valueDiffPct : null,
        vendorMatched: it.mirMatch ? m.vendorMatched : undefined,
        taxTypeMismatch: !!m.taxTypeMismatch,
        netValueMismatched: !!m.netValueMismatched,
        taxableValueMismatched: !!m.taxableValueMismatched,
        finalValueMismatched: !!m.finalValueMismatched,
        uomMismatch: !!m.uomMismatch,
        stockMatched: !!m.stockMatched,
        // What has arrived, in this line's own unit - the same `received`
        // block a domestic line carries, so openQtyOfLine() treats both alike.
        received: m.received || null,
        foreignPrice: it.netPrice,
        exchangeRate: it.exchangeRate,
      };
    }),
  };
}

// Every order this view reads for one plant: its domestic orders, then its
// import orders in domestic shape. Memoized on the identity of the two source
// caches, which clearDataCaches() replaces on every refresh - so it rebuilds
// exactly when the data does, and between refreshes each order stays the SAME
// object. That identity matters: callers stamp _status/_categories onto an
// order and hand links back by reference.
let _materialOrdersMemo = { imports: undefined, byKey: {} };
function materialOrders(key) {
  if (_materialOrdersMemo.imports !== IMPORT_PO_CACHE) _materialOrdersMemo = { imports: IMPORT_PO_CACHE, byKey: {} };
  const domestic = PURCHASE_ORDERS_BY_PLANT[key] || [];
  let entry = _materialOrdersMemo.byKey[key];
  if (!entry || entry.domestic !== domestic) {
    const imports = (IMPORT_PO_CACHE || []).filter(po => po.plant === key).map(importPoAsMaterialOrder);
    entry = { domestic, list: domestic.concat(imports) };
    _materialOrdersMemo.byKey[key] = entry;
  }
  return entry.list;
}

// -- Each PO line links to its BEST material (2026-09-25) --
// The link is fuzzy, and it used to be applied to every (material, line)
// pair on its own: a line linked to EVERY material it cleared the 0.3 token
// threshold against. On Vapi's fabric orders one "EE-315 fabric roll, width
// ... GSM ..." line linked to 238 materials, 251 of 337 open lines linked
// to 50 or more, and the Quantity Mismatch / Data Quality Flags cards
// counted 291 and 337 materials off 337 open lines. It was also the whole
// cost of this view: 1.3 million pair checks per render, about 1 s.
//
// Now a line links to the material it resembles most - the same normalized
// name beats any fuzzy score, and only an exact tie links it to more than
// one - found through a token index instead of by trying every material.
// Built over the WHOLE plant scope, never the Category-filtered list, so a
// filter can never move a line to a different material; and memoized on the
// identity of the caches it reads, which clearDataCaches() replaces, so a
// KPI click or filter change reuses it.
let _lineLinkMemo = {};

function buildLineLinks(materials, plantKeys) {
  _materialTokenCache.clear();
  _vendorNormCache.clear();
  const normIdx = new Map();
  const tokenIdx = new Map();
  const push = (map, k, v) => { let arr = map.get(k); if (!arr) { arr = []; map.set(k, arr); } arr.push(v); };
  materials.forEach((m, i) => {
    const info = materialTokenInfo(m.description);
    if (!info.norm) return;
    push(normIdx, info.norm, i);
    info.tokens.forEach(t => push(tokenIdx, t, i));
  });
  const byNorm = new Map();
  const linkedItems = new Set();
  plantKeys.forEach(key => {
    materialOrders(key).forEach(po => {
      (po.items || []).forEach(item => {
        const info = materialTokenInfo(item.description);
        if (!info.norm) return;
        const cands = new Set(normIdx.get(info.norm) || []);
        info.tokens.forEach(t => (tokenIdx.get(t) || []).forEach(i => cands.add(i)));
        let best = -1;
        let winners = [];
        cands.forEach(i => {
          const score = materialLinkScore(materials[i], po, item);
          if (score < 0) return;
          if (score > best + 1e-9) { best = score; winners = [i]; } else if (Math.abs(score - best) <= 1e-9) winners.push(i);
        });
        if (!winners.length) return;
        linkedItems.add(item);
        const link = { po, item, plantKey: key, plantLabel: PLANTS[key].label };
        winners.forEach(i => push(byNorm, materialTokenInfo(materials[i].description).norm, link));
      });
    });
  });
  return { byNorm, linkedItems };
}

// The line links for `materials` over `plantKeys`, rebuilt only when a cache
// it reads was replaced. `slot` keeps the stock-only index that
// orderOnlyMaterials() needs apart from the full one.
function lineLinksFor(slot, materials, plantKeys) {
  const sig = [IMPORT_PO_CACHE].concat(
    plantKeys.map(k => PURCHASE_ORDERS_BY_PLANT[k]), plantKeys.map(k => MATERIALS_BY_PLANT[k]));
  const memo = _lineLinkMemo[slot];
  if (memo && memo.sig.length === sig.length && memo.sig.every((v, i) => v === sig[i])) return memo.value;
  const value = buildLineLinks(materials, plantKeys);
  _lineLinkMemo[slot] = { sig, value };
  return value;
}

// Every material row for `plantKeys`, stock rows plus order-only rows - the
// same list renderMaterialsView() shows before any filter.
function materialScope(plantKeys) {
  const lots = [];
  plantKeys.forEach(key => (MATERIALS_BY_PLANT[key] || []).forEach(m =>
    lots.push(plantKeys.length === 1 ? m : Object.assign({}, m, { _plantKey: key, _plantLabel: PLANTS[key].label }))));
  const stock = aggregateMaterialsByName(lots);
  return stock.concat(orderOnlyMaterials(stock, plantKeys));
}

// All (po, item) pairs across the given plant keys' orders (domestic and
// import - see materialOrders()) whose best material is `material`'s name,
// each tagged with its plant key/label. `material` may be a single lot (the
// material modal's anchor), so its own vendor gate applies on top. Callers
// must have loaded both order books first.
function linkedPoItemsForMaterial(material, plantKeys) {
  const index = lineLinksFor('full:' + plantKeys.join(','), materialScope(plantKeys), plantKeys);
  const links = index.byNorm.get(normalizeMaterial(material.description)) || [];
  return links.filter(l => vendorGatePasses(material, l.po));
}

// Is this (po, item) pair still to come? Judged on the LINE, not only on the
// PO (2026-09-24). A PO's status is 'partial' while any one of its lines is
// outstanding, so a line that had already arrived in full used to count as
// open on every multi-line PO - its whole value landing in "Inventory Value
// in Transit" and its material reading "On Order" for goods already in the
// warehouse. A short-delivered line stays open: lineItemFullyReceived() is
// false for it, and the rest of it genuinely is still coming.
function isOpenPoLine(po, item) {
  po._status = po._status || computeStatus(po);
  return po._status !== 'received' && !lineItemFullyReceived(item);
}

// How much of an open line is STILL TO COME, in the line's own unit
// (2026-09-25). The ordered quantity less what its matched MIR receipts add
// up to - `received` is the server's own figure (matching_core's
// received_against_line(), already converted to this line's unit). A line
// short-delivered 400 of 500 KG has 100 KG in transit, not 500. Falls back to
// the whole ordered quantity when nothing comparable has arrived: no match,
// a match a reviewer dismissed (not an arrival - see lineItemArrived()), or a
// unit clash the server could not convert.
function openQtyOfLine(item) {
  if (item.qty == null) return null;
  const r = item.received;
  if (!lineItemArrived(item) || !r || !r.comparable || r.qty == null) return item.qty;
  return Math.max(0, item.qty - r.qty);
}

function openValueOfLine(item) {
  const qty = openQtyOfLine(item);
  return item.netPrice != null && qty != null ? item.netPrice * qty : 0;
}

// Unit families for adding up open quantities - a mirror of
// parsers/common.py's _UOM_FAMILIES (same codes, same factors), so KG and MT
// add up and metres never get added to kilograms. An unrecognised unit stays
// out of every total and is only counted, same "never guess" rule as the
// backend's normalize_uom().
const MAT_UOM_FAMILIES = {
  KG: ['KG', 1], KGS: ['KG', 1], GM: ['KG', 0.001], MT: ['KG', 1000], MTS: ['KG', 1000], TO: ['KG', 1000], TON: ['KG', 1000], QTL: ['KG', 100],
  L: ['L', 1], LTR: ['L', 1], LTRS: ['L', 1], KL: ['L', 1000], ML: ['L', 0.001],
  NOS: ['NOS', 1], PCS: ['NOS', 1], PC: ['NOS', 1], EA: ['NOS', 1], UNIT: ['NOS', 1], SET: ['NOS', 1],
  ROLL: ['NOS', 1], ROLLS: ['NOS', 1],
  M: ['M', 1], CM: ['M', 0.01], MM: ['M', 0.001], MTR: ['M', 1], MTRS: ['M', 1],
};

// Open quantity across a set of distinct open lines, per base unit:
// { totals: { KG: n, M: n, ... }, unrecognised: <line count> }.
function summariseOpenQty(openLines) {
  const totals = {};
  let unrecognised = 0;
  openLines.forEach(x => {
    const qty = openQtyOfLine(x.item);
    if (qty == null) return;
    const fam = MAT_UOM_FAMILIES[String(x.item.uom || '').trim().toUpperCase().replace(/\.$/, '')];
    if (!fam) { unrecognised++; return; }
    totals[fam[0]] = (totals[fam[0]] || 0) + qty * fam[1];
  });
  return { totals, unrecognised };
}

// Rows for materials that are ON ORDER but have no RM Stock lot at all
// (2026-09-24, reported as "open POs, even if they are there, don't show up
// in Raw Material Analysis"). This view was built one row per Stock lot, so
// an open order could only appear by linking to a lot that already existed -
// measured on live data, 386 of 478 open line items linked to none and were
// simply invisible: Titanium Dioxide and ZDMC at Achhad, VNB-EPT and POE at
// HRS, nearly every Vapi fabric order. A first order for a new material, or
// one for something that has sold out and been dropped from the sheet, is
// exactly the purchase this view most needs to show.
//
// An open line gets one of these rows only when it links to NO stock
// material under the same materialLinksToItem() rule the linkage uses, so a
// line is never counted under a stock row and an order-only row at once.
// Lines are grouped by normalized description, the same key
// aggregateMaterialsByName() groups lots on. `vendors` carries every
// ordering vendor, so the linkage pass gates this row's own fuzzy links on
// vendor exactly as it does a lot's. Nothing here is a stock figure: qty and
// value are 0, which is the truth about the warehouse, and the open order
// shows through the same In Transit / Quantity Ordered path as any row.
function orderOnlyMaterials(stockMaterials, plantKeys) {
  const stockLinked = lineLinksFor('stock:' + plantKeys.join(','), stockMaterials, plantKeys).linkedItems;
  const groups = new Map();
  plantKeys.forEach(key => {
    materialOrders(key).forEach(po => {
      (po.items || []).forEach(item => {
        if (!isOpenPoLine(po, item)) return;
        const norm = materialTokenInfo(item.description).norm;
        if (!norm) return;
        if (stockLinked.has(item)) return;
        let g = groups.get(norm);
        if (!g) {
          g = { description: item.description, materialCode: '', category: '', subCategory: '', qty: 0, value: 0, rate: null,
            mirMatched: false, vendors: [], vendorSeen: new Set(), lots: [], anchorLot: null, consumption: null,
            orderOnly: true, orderKey: norm };
          groups.set(norm, g);
        }
        if ((!g.category || g.category === 'Uncategorized') && item.category) { g.category = item.category; g.subCategory = item.subCategory || ''; }
        const nv = normalizeVendor(po.vendorName);
        if (nv && !g.vendorSeen.has(nv)) { g.vendorSeen.add(nv); g.vendors.push(po.vendorName); }
      });
    });
  });
  return Array.from(groups.values()).map(g => { delete g.vendorSeen; return g; });
}

// The date the Materials list sorts on, newest first (ISO string, '' when
// unknown). For a stocked material: its newest lot's received date - a lot
// is one arrival, so this is "when did this material last come in". For an
// order-only row, which has no lot: its newest open order's created date, so
// a brand-new order for a new material surfaces near the top rather than
// sinking below every dated lot. The list labels which of the two it is.
function materialLatestDate(m, entry) {
  if (m.orderOnly) {
    return (entry ? entry.openLinks.map(l => l.po.createdDate).filter(Boolean).sort().pop() : '') || '';
  }
  return (m.lots || [m]).map(l => l.receivedDate).filter(Boolean).sort().pop() || '';
}

// What a row or chart bar hands openMaterialModal(): its anchor lot, or for
// an order-only row the "order::<normalized description>" form that modal
// resolves against the open orders themselves.
function materialModalKey(m) {
  if (m.orderOnly) return 'order::' + m.orderKey;
  return plantKeyFor(m.anchorLot) + '::' + m.anchorLot.lotId;
}

// Every material with >=1 linked open (non-received) PO line item, for the
// KPI row below - computed once per render and reused across cards 3/4/5/6/7
// rather than re-scanning PURCHASE_ORDERS_BY_PLANT per card.
function computeMaterialPoLinkage(materials, plantKeys, scope) {
  // `scope` is the unfiltered material list (renderMaterialsView()'s `all`),
  // so which material a line belongs to never depends on the filters.
  const index = lineLinksFor('full:' + plantKeys.join(','), scope || materialScope(plantKeys), plantKeys);
  return materials.map(m => {
    const links = index.byNorm.get(normalizeMaterial(m.description)) || [];
    links.forEach(l => { l.po._status = l.po._status || computeStatus(l.po); if (!l.po._categories) computePoFlags(l.po); });
    const openLinks = links.filter(l => isOpenPoLine(l.po, l.item));
    // Same categories a PO row's own rowFlags() shows (see renderPoList()),
    // but scoped to just this material's own linked line item(s) - qty/rate
    // discrepancy is checked against `l.item`'s own diff%, not the parent
    // PO's blanket _qtyFlag/_rateFlag, so a flag on a different line item in
    // a multi-item PO never gets misattributed to this material. Remarks-
    // based info categories stay PO-level (remarks are a whole-PO field, no
    // finer-grained source exists) - same as computePoFlags() itself.
    const catMap = new Map();
    links.forEach(l => {
      // A dismissed match (l.item.dismissedByOverride) is excluded from
      // every match-derived check below (2026-09-10 fix) - same reasoning
      // as flags.js's computePoFlags()'s own comment: the Plant Data
      // Correction email already excludes a dismissed match, and its badge
      // already renders struck-through/muted, so this KPI must agree.
      // `!l.item.matched` is unaffected - there's nothing to dismiss when
      // there's no match at all.
      const dismissed = l.item.dismissedByOverride;
      if (!dismissed && l.item.qtyDiffPct != null && l.item.qtyDiffPct > FLAG_PCT) catMap.set('Quantity Mismatch in MIR', { label: 'Quantity Mismatch in MIR', severity: 'critical' });
      // Rate only, not value - see flags.js's computePoFlags() for why
      // (value = qty x rate, so a qty mismatch alone would otherwise
      // double-count as a second, unrelated-looking rate/value problem).
      if (!dismissed && l.item.rateDiffPct != null && l.item.rateDiffPct > FLAG_PCT) catMap.set('Rate Mismatch in MIR', { label: 'Rate Mismatch in MIR', severity: 'critical' });
      // 2026-09-08 (Data Quality Flags clarity pass, extended to Raw
      // Material Analysis) - same previously-computed-but-discarded
      // match-quality signals Domestic's/Import's own category lists now
      // surface, checked against this material's own linked line item
      // (`l.item`), same per-item scoping as the qty/rate checks just
      // above - never misattributed from a different line item on the same
      // multi-item PO.
      // Not on an order that is simply not due yet (2026-09-25): nothing has
      // arrived and the delivery date has not passed, so there is nothing
      // for MIR to hold. Same rule as rowFlagsHtml()'s suppression of this
      // label on an On Order row - left in, every open order read as a Data
      // Quality Flag, and every order-only row carried one.
      if (!l.item.matched && l.po._status !== 'pending') catMap.set('PO Not Found in MIR', { label: 'PO Not Found in MIR', severity: 'critical' });
      if (!dismissed && l.item.taxTypeMismatch) catMap.set('Tax Type Mismatch in MIR', { label: 'Tax Type Mismatch in MIR', severity: 'info' });
      if (!dismissed && l.item.netValueMismatched) catMap.set('Net Value Mismatch in MIR', { label: 'Net Value Mismatch in MIR', severity: 'info' });
      if (!dismissed && l.item.taxableValueMismatched) catMap.set('Taxable Value Mismatch in MIR', { label: 'Taxable Value Mismatch in MIR', severity: 'info' });
      if (!dismissed && l.item.finalValueMismatched) catMap.set('Final Amount Mismatch in MIR', { label: 'Final Amount Mismatch in MIR', severity: 'info' });
      if (!dismissed && l.item.uomMismatch) catMap.set('UOM Mismatch in MIR', { label: 'UOM Mismatch in MIR', severity: 'info' });
      if (l.po.remarks) { const c = categorizeFlag(l.po.remarks); catMap.set(c.label, c); }
    });
    const categories = Array.from(catMap.values());
    // Same reasoning as computePoFlags()'s own _maxDiffPct (see flags.js) -
    // drives rowTintClass()'s severity-scaled row background.
    const allDiffs = links.flatMap(l => [l.item.qtyDiffPct, l.item.rateDiffPct, l.item.valueDiffPct]).filter(v => v != null);
    return {
      material: m,
      links,
      openLinks,
      openValue: openLinks.reduce((s, x) => s + openValueOfLine(x.item), 0),
      categories,
      qtyFlag: categories.some(c => c.label === 'Quantity Mismatch in MIR'),
      rateFlag: categories.some(c => c.label === 'Rate Mismatch in MIR'),
      hasInfoFlag: categories.some(c => c.severity === 'info'),
      maxDiffPct: allDiffs.length ? Math.max(...allDiffs) : 0,
    };
  });
}

// Materials' own status pill, same visual language as PO's Received/
// Partial Delivered/Pending/Overdue/Delivery Date Unknown pills (reuses the
// identical .status-pill.status-<key> CSS classes - no new styling needed).
// Not a delivery status like a PO has - a "business state" for the material
// itself, derived from its linked POs' own statuses + current stock:
//   overdue   - at least one linked, still-open PO is itself overdue
//   partial   - some qty already in stock AND a linked PO is still open
//               (mid-replenishment - part on hand, more incoming)
//   onorder   - a linked PO is open, nothing in stock yet
//   received  - every linked PO has been fully received, nothing open
//   instock   - no linked PO history at all, just sitting in stock (or,
//               rarer, no stock and no PO history either)
const MAT_STATUS_LABELS = { received: 'Received', partial: 'Partial', onorder: 'On Order', overdue: 'Overdue', instock: 'In Stock' };
const MAT_STATUS_PILL_CLASS = { received: 'status-received', partial: 'status-partial', onorder: 'status-pending', overdue: 'status-overdue', instock: 'status-unknown' };
function computeMaterialStatus(m, entry) {
  const links = entry ? entry.links : [];
  const openLinks = entry ? entry.openLinks : [];
  const stocked = (m.qty || 0) > 0;
  // _overdue, not _status === 'overdue' (2026-09-18): overdue is an overlay
  // now, so a partly-delivered late PO has status 'partial' and would be
  // missed by the old check. See flags.js's computeStatus().
  if (openLinks.some(l => l.po._overdue)) return 'overdue';
  if (openLinks.length && stocked) return 'partial';
  if (openLinks.length) return 'onorder';
  // "Received" once there's real evidence of an MIR receipt (m.mirMatched -
  // the actual Stock<->MIR match, see materialStepperHtml()'s comment) or,
  // failing that, a fuzzy-matched PO that's already fully received - never
  // inferred from stock qty alone, since qty>0 with neither signal just
  // means the material is sitting in stock with no traceable receipt found.
  if (m.mirMatched || links.length) return 'received';
  return 'instock';
}

// Table-only header filters (Material text search + Progress) - never touch
// the KPI row/chart, only which rows the table shows. Same role/shape as
// PO's applyColFilters()/state.colFilters. Category/Sub Category filter in
// the header row too (see matFilterCells below) but write into
// state.matCategoryFilter/matSubCategoryFilter directly, not here - they're
// "global" filters that narrow `filtered` before this function ever runs
// (KPI row + chart + table all reflect them), same as PO's Status. Status
// isn't filtered here either - it's applied separately in
// renderMaterialsView() itself since it needs the `linkage` lookup, not
// just a plain field on `m`. No more Stock/Inventory Value/Latest Rate
// range filters (project owner, 2026-09-04, removed to make room for
// Category/Sub Category/Progress).
function applyMatColFilters(recs) {
  const f = state.matColFilters;
  return recs.filter(m => {
    if (f.material && !(m.description || '').toLowerCase().includes(f.material.toLowerCase())) return false;
    if (f.progress === 'mirmatched' && !m.mirMatched) return false;
    if (f.progress === 'notmirmatched' && m.mirMatched) return false;
    return true;
  });
}

function renderMaterialsView() {
  const el = document.getElementById('materialsContent');
  destroyPageCharts();
  // One row per unique material NAME within the current plant scope, not
  // per Stock lot (see aggregateMaterialsByName()'s own header comment for
  // why/scope) - "All Plants" sums across all 3 plants, a single plant tab
  // sums only that plant's own vendor lots.
  const allLots = currentMaterials();
  const stockMaterials = aggregateMaterialsByName(allLots);
  // Plus a row per material on open order with no stock lot - see
  // orderOnlyMaterials(). Built before the category filters so those rows
  // sit in the Category/Sub Category options like any other.
  const all = stockMaterials.concat(orderOnlyMaterials(stockMaterials, selectedPlantKeys()));

  const catCounts = {};
  all.forEach(m => { catCounts[m.category || 'Uncategorized'] = (catCounts[m.category || 'Uncategorized'] || 0) + 1; });
  const catOptions = Object.entries(catCounts).sort((a, b) => b[1] - a[1]);

  // Sub-category options are scoped to the currently selected category (if
  // any) - a cascading dropdown, same UX convention as most category/
  // sub-category filter pairs, so it never offers a sub-category that can't
  // possibly match anything under the chosen category.
  const inSelectedCategory = state.matCategoryFilter ? all.filter(m => (m.category || 'Uncategorized') === state.matCategoryFilter) : all;
  const subCatCounts = {};
  inSelectedCategory.forEach(m => { subCatCounts[m.subCategory || 'Uncategorized'] = (subCatCounts[m.subCategory || 'Uncategorized'] || 0) + 1; });
  const subCatOptions = Object.entries(subCatCounts).sort((a, b) => b[1] - a[1]);

  // "Global" filters - narrow `filtered` itself, so the KPI row and
  // drill-down chart reflect only the selected category/sub-category, not
  // just the table (see state.matCategoryFilter's own comment).
  let filtered = all;
  if (state.matCategoryFilter) filtered = filtered.filter(m => (m.category || 'Uncategorized') === state.matCategoryFilter);
  if (state.matSubCategoryFilter) filtered = filtered.filter(m => (m.subCategory || 'Uncategorized') === state.matSubCategoryFilter);

  if (!all.length) {
    const msg = isAllPlants()
      ? 'No Raw Material Stock synced yet for any plant.'
      : 'No RM Stock synced yet - run <code>' + PLANTS[state.plant].syncCmdStock + '</code> to load it.';
    el.innerHTML = '<div class="empty-state">' + emptyStateHtml(msg) + '</div>';
    return;
  }

  const plantKeys = selectedPlantKeys();
  const linkage = computeMaterialPoLinkage(filtered, plantKeys, all);
  // O(1) lookup from a (category/sub-category-filtered) material back to
  // its linkage entry (flags/categories/open-PO links) while rendering the
  // table below - built from the exact same `filtered` array `linkage` was
  // computed from, so every material in `filtered` has an entry.
  const linkageByKey = new Map(linkage.map(l => [normalizeMaterial(l.material.description), l]));

  const totalMaterials = filtered.length;
  const orderOnlyCount = filtered.filter(m => m.orderOnly).length;
  const lotCount = filtered.reduce((s, m) => s + (m.lots ? m.lots.length : 0), 0);
  const totalValue = filtered.reduce((s, m) => s + (m.value || 0), 0);
  // EACH OPEN LINE COUNTED ONCE (2026-09-25). The link is fuzzy, so one PO
  // line can link to several materials - "SBR 1502" clears the 0.3 token
  // threshold against both SBR 1502 and SBR 1712 - and summing per material
  // added that line's value once per material it touched. The KPI totals are
  // taken over the distinct lines instead; the line objects are stable
  // within a render (see materialOrders()), so identity is the key.
  const openLineMap = new Map();
  linkage.forEach(l => l.openLinks.forEach(x => { if (!openLineMap.has(x.item)) openLineMap.set(x.item, x); }));
  const openLines = Array.from(openLineMap.values());
  const inTransitValue = openLines.reduce((s, x) => s + openValueOfLine(x.item), 0);
  const openQty = summariseOpenQty(openLines);
  // The headline is weight (nearly every raw material is bought by weight),
  // in MT once it reaches 10 tonnes so the figure fits its card - "4,889 MT"
  // rather than a clipped "48,89,0...". Other units are listed in the card's
  // tooltip rather than added to it.
  const qtyUnits = Object.keys(openQty.totals).sort((a, b) => (b === 'KG') - (a === 'KG') || openQty.totals[b] - openQty.totals[a]);
  const qtyHeadUnit = qtyUnits[0] || 'KG';
  const qtyHeadInMt = qtyHeadUnit === 'KG' && (openQty.totals.KG || 0) >= 10000;
  const qtyHeadValue = (openQty.totals[qtyHeadUnit] || 0) / (qtyHeadInMt ? 1000 : 1);
  const qtyOtherParts = qtyUnits.slice(1).map(u => Math.round(openQty.totals[u]).toLocaleString('en-IN') + ' ' + u);
  if (openQty.unrecognised) qtyOtherParts.push(openQty.unrecognised + (openQty.unrecognised === 1 ? ' line' : ' lines') + ' in a unit the app does not recognise (counted, not added)');
  // The material set behind BOTH "Inventory Value in Transit" and "Quantity
  // Ordered" - the two cards are different aggregates over the same rows, so
  // clicking either one filters the table to exactly this set (2026-09-21).
  // Until then both cards were rendered as buttons (role="button",
  // tabindex="0", pointer cursor, aria-pressed) and clicking them did
  // nothing at all - see the click handler below.
  const openPoMats = linkage.filter(l => l.openLinks.length > 0);
  const qtyDiscMats = linkage.filter(l => l.qtyFlag);
  const rateDiscMats = linkage.filter(l => l.rateFlag);
  // Redefined 2026-09-08 (Data Quality Flags clarity pass, same day as
  // Domestic's/Import's own redefinition) from "info severity only" to "any
  // category at all" - see computeMaterialPoLinkage()'s own comment for the
  // full category list this now covers.
  const flaggedMats = linkage.filter(l => l.categories.length > 0);
  const lowStockMats = filtered.filter(isMaterialLowStock);
  // Per-category breakdown for "Filter by Flags" (added 2026-09-08, same
  // pattern as Domestic's/Import's own flagCategoryCounts) - excludes the 2
  // labels that already have their own dedicated dropdown option
  // (Quantity/Rate Mismatch in MIR) so they don't appear twice.
  const DEDICATED_FLAG_LABELS = ['Quantity Mismatch in MIR', 'Rate Mismatch in MIR'];
  const flagCategoryCounts = {};
  linkage.forEach(l => l.categories.forEach(c => {
    if (DEDICATED_FLAG_LABELS.includes(c.label)) return;
    flagCategoryCounts[c.label] = (flagCategoryCounts[c.label] || 0) + 1;
  }));

  // Quantity mismatches split by direction, for that card's tooltip - the
  // same over/short split Purchase Orders shows as two cards of its own.
  const qtyDirOf = (l, over) => l.links.some(x => !x.item.dismissedByOverride && x.item.qtyDiffPct != null && x.item.qtyDiffPct > FLAG_PCT && x.item.qtyOverDelivered === over);
  const qtyOverCount = qtyDiscMats.filter(l => qtyDirOf(l, true)).length;
  const qtyShortCount = qtyDiscMats.filter(l => qtyDirOf(l, false)).length;
  const n = v => v.toLocaleString('en-IN');

  // Same card as Domestic Purchase Orders' KPI row (po-list.js's cardDef,
  // project owner 2026-09-25: "take inspiration from the Purchase Order tab,
  // preferably the domestic one") - big figure, small uppercase label with
  // its info icon, flag icon top-right, coloured left border. Order: plain
  // counts -> the open-order pair -> the two critical mismatch cards -> Low
  // Stock -> Data Quality Flags last. What each figure is made of (the
  // breakdowns) lives in its tooltip. The only difference from the PO row
  // is `.mat-kpi-grid`, which lets the eight cards share the row's width so
  // the currency and quantity figures always fit (see style.css).
  //
  // `fmt` picks the count-up formatter (wireKpiCountUps()): 'inr' for money,
  // 'locale' for a comma-grouped quantity, 'int' for counts. `unit` sits
  // beside the number, outside the element the count-up rewrites.
  const cardDef = [
    { key: 'total', cls: '', label: 'Materials Tracked', raw: totalMaterials, fmt: 'int',
      tip: 'Distinct material names: ' + n(totalMaterials - orderOnlyCount) + ' on the stock sheet (every vendor lot of one material is one row, including lots now at zero)' + (orderOnlyCount ? ', plus ' + n(orderOnlyCount) + ' on an open order with no stock lot yet' : '') + '.' },
    { key: 'value', cls: '', label: 'Inventory Value', raw: totalValue, fmt: 'inr',
      tip: 'The stock sheet\'s own Value column, summed across ' + n(lotCount) + ' stock lots' + (isAllPlants() ? ' at all three plants' : '') + '. A lot the sheet shows with negative stock subtracts here - fix it in the sheet.' },
    { key: 'transit', filterKey: 'openpo', cls: 'partial', label: 'Value in Transit', raw: inTransitValue, fmt: 'inr',
      tip: 'Still to arrive on ' + n(openLines.length) + ' open purchase order lines (domestic and import) across ' + n(openPoMats.length) + ' materials: ordered quantity less what MIR has already received, at the PO rate in INR. Each PO line is counted once, even when it links to more than one material.' },
    { key: 'qtyordered', filterKey: 'openpo', cls: 'partial', label: 'Quantity to Come', raw: qtyHeadValue, fmt: 'locale', unit: qtyHeadInMt ? 'MT' : qtyHeadUnit,
      tip: 'Quantity still to arrive on the same open lines, converted within one kind of unit (KG, grams and MT all to weight) and never added across kinds.' + (qtyOtherParts.length ? ' Also still to come: ' + qtyOtherParts.join('; ') + '.' : '') },
    { key: 'qtydisc', cls: 'critical', label: 'Quantity Mismatches', raw: qtyDiscMats.length, fmt: 'int', flag: KPI_FLAG_COLORS.critical,
      tip: 'Materials with a linked PO line whose quantity differs from what MIR received - zero tolerance, dismissed matches excluded. ' + n(qtyOverCount) + ' over-delivered, ' + n(qtyShortCount) + ' short (short includes a part-delivery still in progress).' },
    { key: 'ratedisc', cls: 'critical', label: 'Rate Mismatches', raw: rateDiscMats.length, fmt: 'int', flag: KPI_FLAG_COLORS.critical,
      tip: 'Materials with a linked PO line whose rate differs from its matched MIR entry - zero tolerance, dismissed matches excluded. Value is not compared, since a quantity difference would move it too.' },
    { key: 'lowstock', cls: 'critical', label: 'Low Stock (Reorder Soon)', raw: lowStockMats.length, fmt: 'int', flag: KPI_FLAG_COLORS.critical,
      tip: 'Under 15 days of cover at the recent consumption rate (only where that rate has enough snapshot history to show a Days Left figure), or already at or below Achhad\'s minimum stock level.' },
    { key: 'flags', cls: 'flags', label: 'Data Quality Flags', raw: flaggedMats.length, fmt: 'int', flag: KPI_FLAG_COLORS.quality,
      tip: 'Materials with any flag on a linked PO line: quantity or rate mismatch, PO not found in MIR (not counted on an order that is not due yet), tax type, net, taxable or final value mismatch, UOM mismatch, or a paperwork note from the remarks. Use "Filter by Flags" below for one issue.' },
  ];
  const kpiHtml = cardDef.map(c => {
    // `filterKey` is what a click sets, `key` is the card's own identity.
    // They differ only for the two open-PO cards, which are two aggregates
    // over ONE row set - so selecting either lights up both, which is honest
    // about what the table is now showing.
    const on = state.matStatusFilter === (c.filterKey || c.key);
    return '<div class="kpi-card ' + c.cls + (on ? ' active' : '') + '" data-matkpi="' + c.key + '" tabindex="0" role="button" aria-pressed="' + on + '">' +
      (c.flag ? flagIconHtml(c.flag) : '') +
      '<div class="mat-kpi-valrow"><span class="val" data-count-target="' + c.raw + '" data-count-fmt="' + c.fmt + '">0</span>' +
        (c.unit ? '<span class="mat-kpi-unit">' + escapeHtml(c.unit) + '</span>' : '') + '</div>' +
      '<div class="label">' + escapeHtml(c.label) + infoTooltipHtml(c.tip) + '</div></div>';
  }).join('');

  // Everything below the chart is rendered by materialsListRegionHtml()
  // from this context, so a Material-search keystroke can rebuild the list
  // alone instead of this whole view - see MAT_LIST_CTX's own comment.
  // catOptions/subCatOptions rather than a ready-made header-filter row:
  // each cell carries its filter's CURRENT value, so a snapshot taken here
  // would rewrite the Material search box back to what it held before the
  // keystroke that triggered the re-render - the list would narrow correctly
  // while the box under the cursor went blank. The option LISTS are safe to
  // carry: they depend on the category/sub-category filters, which take the
  // full render anyway.
  MAT_LIST_CTX = { linkage: linkage, linkageByKey: linkageByKey, filtered: filtered, openPoMats: openPoMats, qtyDiscMats: qtyDiscMats, rateDiscMats: rateDiscMats, lowStockMats: lowStockMats, flaggedMats: flaggedMats, catOptions: catOptions, subCatOptions: subCatOptions };

  el.innerHTML =
    '<div class="section-title">Raw Material and Inventory Analysis: ' + escapeHtml(plantDisplayLabel()) + '</div>' +
    '<div class="section-sub">One row per unique material' + (isAllPlants() ? ', summed across every vendor lot and all 3 plants' : ', summed across every vendor lot at this plant') + ', plus anything on an open order that has no stock lot yet. Click a row for its full cross-plant analysis.</div>' +
    matchingDisclaimerHtml(
      'Ordered qty, in-transit value and the flag columns are matched automatically. Days Left is an estimate.',
      '<p><strong>Matched columns.</strong> "Value in Transit", "Quantity to Come" and the mismatch/flag columns link each material to PO line items by description, and by vendor where it is known - the same best-effort approach used for PO&harr;MIR matching. It is not guaranteed-correct identity resolution, so verify before relying on it.</p>' +
      '<p><strong>Days Left.</strong> Estimated from recent stock-snapshot history, not reported by the sheet. The confidence dot beside it shows how much history it is based on.</p>'
    ) +
    '<div class="kpi-grid mat-kpi-grid">' + kpiHtml + '</div>' +
    // "Filter by Category" / "Filter by Sub Category" / "Filter by Flags"
    // bar - moved above the chart (project owner, 2026-09-04) so the chart
    // itself already reflects the selection. All three are "global"
    // filters, same as PO's own category filter bar: Category/Sub Category
    // narrow `filtered` (KPI row + chart + table all reflect them); Flags
    // is just this same bar's control over matStatusFilter (also settable
    // from a KPI-card click or the table's own Status header select - one
    // shared field, three ways to set it, same pattern as PO's Status).
    '<div class="filter-row">' +
      '<div class="filter-group">' +
        '<label>Filter by Category</label>' +
        '<select id="matCatSelect" class="select-w220">' +
          '<option value="">All categories (' + all.length + ')</option>' +
          catOptions.map(([c, n]) => '<option value="' + escapeHtml(c) + '"' + (state.matCategoryFilter === c ? ' selected' : '') + '>' + escapeHtml(categoryLabel(c)) + ' (' + n + ')</option>').join('') +
        '</select>' +
      '</div>' +
      '<div class="filter-group">' +
        '<label>Filter by Sub Category</label>' +
        '<select id="matSubCatSelect" class="select-w220">' +
          '<option value="">All sub-categories (' + inSelectedCategory.length + ')</option>' +
          subCatOptions.map(([c, n]) => '<option value="' + escapeHtml(c) + '"' + (state.matSubCategoryFilter === c ? ' selected' : '') + '>' + escapeHtml(categoryLabel(c)) + ' (' + n + ')</option>').join('') +
        '</select>' +
      '</div>' +
      '<div class="filter-group">' +
        '<label>Filter by Flags</label>' +
        '<select id="matFlagsSelect" class="select-w200">' +
          '<option value="">All flags</option>' +
          '<option value="qtydisc"' + (state.matStatusFilter === 'qtydisc' ? ' selected' : '') + '>Quantity Mismatch (' + qtyDiscMats.length + ')</option>' +
          '<option value="ratedisc"' + (state.matStatusFilter === 'ratedisc' ? ' selected' : '') + '>Rate Mismatch (' + rateDiscMats.length + ')</option>' +
          '<option value="lowstock"' + (state.matStatusFilter === 'lowstock' ? ' selected' : '') + '>Low Stock (' + lowStockMats.length + ')</option>' +
          '<option value="flags"' + (state.matStatusFilter === 'flags' ? ' selected' : '') + '>Data Quality Flag (' + flaggedMats.length + ')</option>' +
          Object.keys(flagCategoryCounts).sort().map(function (label) {
            const value = 'cat:' + label;
            const selected = state.matStatusFilter === value ? ' selected' : '';
            return '<option value="' + value + '"' + selected + '>' + label + ' (' + flagCategoryCounts[label] + ')</option>';
          }).join('') +
        '</select>' +
      '</div>' +
      ((state.matCategoryFilter || state.matSubCategoryFilter || state.matStatusFilter) ? '<button id="matClearCategoryFilter">Clear</button>' : '') +
    '</div>' +
    // Inventory Value chart: stock rows only. An order-only row holds no
    // inventory, and would only add zero-length bars at the material level.
    renderMaterialsChart(filtered.filter(m => !m.orderOnly)) +
    // Its own container so a Material-search keystroke can replace just this
    // (see renderMaterialsListRegion()), leaving the KPI row's count-up and
    // the drill-down chart above it untouched.
    '<div id="matListRegion">' + materialsListRegionHtml() + '</div>';

  applyDynamicStyles(el); // chart-box height, legend dots - see shared.js's own comment; must run before wireMaterialsChart() reads the container's height
  wireKpiCountUps();
  wireMaterialsListRegion();

  // KPI-CARD CLICK (fixed 2026-09-21). Every card here renders as a button -
  // role="button", tabindex="0", aria-pressed, pointer cursor - but FOUR of
  // the eight did nothing when clicked, because this line forced the filter
  // to null for them. Reported as "the KPIs clicking is not working".
  //
  //   total, value    -> correctly a no-op-ish CLEAR: both count the whole
  //                      filtered set, so there is no narrower set to show.
  //                      Same as Purchase Orders' own "Total PO's Created".
  //   transit,        -> NOT a clear. Both count materials with at least one
  //   qtyordered         open PO line item, which is a real subset, and
  //                      clicking them now shows exactly those rows.
  //
  // A card with a `filterKey` toggles that shared key; a card without one
  // toggles its own, or clears when it has no narrower set to offer.
  const KPI_FILTER_KEYS = Object.fromEntries(cardDef.map(c => [c.key, c.filterKey || null]));
  const KPI_CLEARS = new Set(['total', 'value']);
  document.querySelectorAll('[data-matkpi]').forEach(c => c.onclick = () => {
    const key = c.dataset.matkpi;
    const target = KPI_FILTER_KEYS[key] || key;
    state.matStatusFilter = (KPI_CLEARS.has(key) || state.matStatusFilter === target) ? null : target;
    state.matTablePage = 1;
    renderMaterialsView();
    // Take the reader to what they just filtered - the list sits below the
    // chart panel and is normally off-screen from the KPI row, so without
    // this the click looks like it did nothing. Only on NARROWING: when the
    // click cleared the filter, the full list is the thing they were already
    // looking at, and scrolling away from the cards would be the surprise.
    if (state.matStatusFilter) revealFilteredList('matListRegion');
  });
  const matCatSelect = document.getElementById('matCatSelect');
  if (matCatSelect) matCatSelect.onchange = () => { state.matCategoryFilter = matCatSelect.value || null; state.matSubCategoryFilter = null; state.matTablePage = 1; renderMaterialsView(); };
  const matSubCatSelect = document.getElementById('matSubCatSelect');
  if (matSubCatSelect) matSubCatSelect.onchange = () => { state.matSubCategoryFilter = matSubCatSelect.value || null; state.matTablePage = 1; renderMaterialsView(); };
  const matFlagsSelect = document.getElementById('matFlagsSelect');
  if (matFlagsSelect) matFlagsSelect.onchange = () => { state.matStatusFilter = matFlagsSelect.value || null; state.matTablePage = 1; renderMaterialsView(); };
  const matClearCategoryBtn = document.getElementById('matClearCategoryFilter');
  if (matClearCategoryBtn) matClearCategoryBtn.onclick = () => { state.matCategoryFilter = null; state.matSubCategoryFilter = null; state.matStatusFilter = null; state.matTablePage = 1; renderMaterialsView(); };

  wireMaterialsChart(filtered.filter(m => !m.orderOnly));
}

// ── The list region (heading + header filters + rows + pagination) ───────
// Split out of renderMaterialsView() on 2026-09-19. The Material text
// search narrows `tableRecs` ONLY (see applyMatColFilters()'s own comment -
// it is a table-only filter, it never touches the KPI row, the
// category/flag bar or the drill-down chart), so rebuilding the whole view
// on every keystroke was rebuilding six things that could not have changed
// - including restarting all 8 KPI count-up animations from 0 and
// destroying/recreating the Chart.js canvas, which is what the "it reloads
// every time I type" report was actually describing.
//
// MAT_LIST_CTX carries what the last full render already computed (the
// PO-linkage pass in particular is the expensive part and depends only on
// the category/sub-category filters, never on the text search). A keystroke
// re-runs the cheap tail: status/text narrowing, sort, page slice, markup.
// It is null until renderMaterialsView() has run once, and
// renderMaterialsListRegion() falls back to a full render in that case
// rather than assuming it is there.
let MAT_LIST_CTX = null;

function materialsListRegionHtml() {
  const ctx = MAT_LIST_CTX;
  // matStatusFilter is table-only-in-effect here (like PO's statusFilter on
  // its own table) even though it's also a KPI-card click target - narrows
  // `tableRecs`, not `filtered`, so the KPI counts always show the full
  // category/sub-category-filtered picture regardless of which status chip
  // is selected, exactly like PO's Quantity/Rate Discrepancy cards.
  let tableRecs = ctx.filtered;
  if (state.matStatusFilter === 'openpo') tableRecs = ctx.openPoMats.map(l => l.material);
  else if (state.matStatusFilter === 'qtydisc') tableRecs = ctx.qtyDiscMats.map(l => l.material);
  else if (state.matStatusFilter === 'ratedisc') tableRecs = ctx.rateDiscMats.map(l => l.material);
  else if (state.matStatusFilter === 'lowstock') tableRecs = ctx.lowStockMats;
  else if (state.matStatusFilter === 'flags') tableRecs = ctx.flaggedMats.map(l => l.material);
  // Per-category filter (added 2026-09-08) - 'cat:<label>' namespacing, same
  // convention/reasoning as Domestic's/Import's own list views.
  else if (typeof state.matStatusFilter === 'string' && state.matStatusFilter.startsWith('cat:')) {
    const wantedLabel = state.matStatusFilter.slice(4);
    tableRecs = ctx.linkage.filter(l => l.categories.some(c => c.label === wantedLabel)).map(l => l.material);
  }
  tableRecs = applyMatColFilters(tableRecs);
  // LATEST FIRST (2026-09-24, project owner: "stock lot added first or
  // latest, just like it's for PO latest first"). Was stock qty descending,
  // which kept the same big-quantity materials pinned to the top whatever
  // arrived. Now the material whose newest lot was received most recently
  // leads - the same reading order as Purchase Orders (Latest first). See
  // materialLatestDate() for what "latest" means on an order-only row.
  // Rows with no date at all go last; ties fall back to the old order (stock
  // qty, then value still on order).
  const entryOf = m => ctx.linkageByKey.get(normalizeMaterial(m.description));
  const openValueOf = m => { const e = entryOf(m); return e ? e.openValue : 0; };
  const latestOf = new Map(tableRecs.map(m => [m, materialLatestDate(m, entryOf(m))]));
  const sorted = tableRecs.slice().sort((a, b) =>
    latestOf.get(b).localeCompare(latestOf.get(a))
    || ((b.qty || 0) - (a.qty || 0))
    || (openValueOf(b) - openValueOf(a)));

  const showingAll = state.showAllMaterials;
  const PAGE_SIZE = 10;
  const totalPages = Math.max(1, Math.ceil(sorted.length / PAGE_SIZE));
  const matTablePage = Math.min(Math.max(1, state.matTablePage), totalPages);
  const listRecs = showingAll ? sorted.slice((matTablePage - 1) * PAGE_SIZE, matTablePage * PAGE_SIZE) : sorted.slice(0, 5);
  // Read back by wireMaterialsListRegion(), which has to clamp the same way
  // rather than re-deriving a second page count that could disagree.
  ctx.totalPages = totalPages;

  const linkageByKey = ctx.linkageByKey;
  // Built HERE, not handed over in MAT_LIST_CTX - see that object's own
  // comment for the blank-search-box bug a snapshot causes.
  // Header filter row inside the table, same pattern as PO's filterCells -
  // text search for Material, a <select> each for Category, Sub Category,
  // Status, and Progress (project owner, 2026-09-04: added Category/Sub
  // Category/Progress here, removed the old Stock/Inventory Value/Latest
  // Rate min/max ranges). One entry per <th> in the table header (Material,
  // Category, Sub Category, Stock, Inventory Value, Latest Rate, Status,
  // Progress, Details) - Stock/Inventory Value/Latest Rate/Details have no
  // header-row control of their own, so they still need an empty
  // placeholder entry, or every later cell silently shifts one column left
  // under the wrong header.
  // Category/Sub Category header-row selects reuse catOptions/subCatOptions
  // (already built above for the "Filter by" bar) and write into
  // state.matCategoryFilter/matSubCategoryFilter directly - same field, two
  // controls, same single-source-of-truth reasoning as Status. No more
  // Stock/Inventory Value/Latest Rate range filters (project owner,
  // 2026-09-04, removed to make room for these plus Progress).
  const matCatColOptionsHtml = ctx.catOptions.map(([c, n]) => '<option value="' + escapeHtml(c) + '"' + (state.matCategoryFilter === c ? ' selected' : '') + '>' + escapeHtml(categoryLabel(c)) + ' (' + n + ')</option>').join('');
  const matSubCatColOptionsHtml = ctx.subCatOptions.map(([c, n]) => '<option value="' + escapeHtml(c) + '"' + (state.matSubCategoryFilter === c ? ' selected' : '') + '>' + escapeHtml(categoryLabel(c)) + ' (' + n + ')</option>').join('');
  const matFilterCells = [
    '<input type="text" class="col-filter-input" data-mcf="material" placeholder="Search..." value="' + escapeHtml(state.matColFilters.material) + '">',
    '<select class="col-filter-input" data-mcf="category"><option value="">All</option>' + matCatColOptionsHtml + '</select>',
    '<select class="col-filter-input" data-mcf="subCategory"><option value="">All</option>' + matSubCatColOptionsHtml + '</select>',
    '',
    '',
    '',
    '', // Days Left - no header-row control of its own, same reasoning as Stock/Inventory Value/Latest Rate above.
    '<select class="col-filter-input" data-mcf="status"><option value="">All</option>' +
      // The two filters a KPI card or flag chip sets that the select used to
      // lack, so after such a click it read "All" while the list was narrowed.
      '<option value="openpo"' + (state.matStatusFilter === 'openpo' ? ' selected' : '') + '>On an open order</option>' +
      (typeof state.matStatusFilter === 'string' && state.matStatusFilter.startsWith('cat:')
        ? '<option value="' + escapeHtml(state.matStatusFilter) + '" selected>' + escapeHtml(state.matStatusFilter.slice(4)) + '</option>' : '') +
      '<option value="qtydisc"' + (state.matStatusFilter === 'qtydisc' ? ' selected' : '') + '>Quantity Mismatch</option>' +
      '<option value="ratedisc"' + (state.matStatusFilter === 'ratedisc' ? ' selected' : '') + '>Rate Mismatch</option>' +
      '<option value="lowstock"' + (state.matStatusFilter === 'lowstock' ? ' selected' : '') + '>Low Stock</option>' +
      '<option value="flags"' + (state.matStatusFilter === 'flags' ? ' selected' : '') + '>Data Quality Flag</option>' +
    '</select>',
    '<select class="col-filter-input" data-mcf="progress"><option value="">All</option>' +
      '<option value="mirmatched"' + (state.matColFilters.progress === 'mirmatched' ? ' selected' : '') + '>MIR Matched</option>' +
      '<option value="notmirmatched"' + (state.matColFilters.progress === 'notmirmatched' ? ' selected' : '') + '>Not MIR Matched</option>' +
    '</select>',
    '',
  ];
  const colFilterRow = '<tr class="col-filter-row">' + matFilterCells.map(c => '<th>' + c + '</th>').join('') + '</tr>';
  const stockAllPlantsSuffix = isAllPlants() ? ' (All Plants)' : '';

  // Colored flag-icon cluster per material row, identical pattern to PO's
  // own rowFlags() in renderPoList() - the same four buckets, see flags.js's
  // rowFlagsHtml(). Categories come from computeMaterialPoLinkage()'s
  // `categories` list; the delivery state comes from computeMaterialStatus(),
  // which is computed per row beside the pill, so rowFlags() takes it as an
  // argument rather than recomputing that whole linkage a second time.
  const rowFlags = (entry, status) => rowFlagsHtml({
    partial: status === 'partial',
    onOrder: status === 'onorder',
    categories: entry ? entry.categories : [],
  });

  const pageButtons = totalPages <= 10
    ? Array.from({ length: totalPages }, (_, i) => i + 1)
        .map(p => '<button class="page-btn page-num' + (p === matTablePage ? ' active' : '') + '" data-matpage="' + p + '">' + p + '</button>')
        .join('')
    : '<span class="page-info">Page ' + matTablePage + ' of ' + totalPages + '</span>';
  const paginationHtml = showingAll && totalPages > 1
    ? '<div class="pagination-row">' +
        '<button id="matPrevPageBtn" class="page-btn"' + (matTablePage <= 1 ? ' disabled' : '') + '>&larr; Prev</button>' +
        pageButtons +
        '<button id="matNextPageBtn" class="page-btn"' + (matTablePage >= totalPages ? ' disabled' : '') + '>Next &rarr;</button>' +
        jumpToPageHtml('mat', totalPages) +
      '</div>'
    : '';

  // The date under each material name - why that row sits where it does.
  const latestNote = m => {
    const d = latestOf.get(m);
    if (!d) return '';
    return '<div class="fs-11 text-slate-soft">' + (m.orderOnly ? 'Ordered ' : 'Last received ') + escapeHtml(formatDateIN(d)) + '</div>';
  };

  return '<div class="list-toggle-row"><div class="section-title m-0">Materials (Latest first) - showing ' + listRecs.length + ' of ' + sorted.length + '</div>' +
      (listRecs.some(m => { const e = linkageByKey.get(normalizeMaterial(m.description)); return e && (e.qtyFlag || e.rateFlag); }) ? rowTintLegendHtml() : '') +
      (sorted.length > 5 ? '<button class="view-all-btn" id="toggleMatBtn">' + (showingAll ? 'Show top 5' : 'View all ' + sorted.length + ' materials') + '</button>' : '') +
    '</div>' +
    (() => {
      // Compact top-5 preview renders as CSS-grid card rows, same
      // .list-header-row/.top5-list/.top5-row structure as Purchase
      // Orders'/Import Purchases' own top-5 preview (project owner,
      // 2026-09-04: Materials should match PO/Import's card structure, not
      // the other way around - see renderPoList()'s equivalent branch for
      // the fuller reasoning). "View all" still renders as a plain <table>
      // for all three views.
      if (showingAll) {
        return '<div class="table-wrap"><table><thead><tr><th>Material</th><th>Category</th><th>Sub Category</th><th>Stock' + stockAllPlantsSuffix + '</th><th>Inventory Value' + stockAllPlantsSuffix + '</th><th>Latest Rate</th><th>Days Left</th><th>Status</th><th>Progress</th><th>Details</th></tr>' +
          colFilterRow +
        '</thead><tbody>' +
        listRecs.map(m => {
          const key = escapeHtml(materialModalKey(m));
          const entry = linkageByKey.get(normalizeMaterial(m.description));
          const orderOnlyNote = m.orderOnly ? '<div class="fs-11 text-slate-soft">On order, no stock lot yet</div>' : '';
          return '<tr class="' + (entry ? rowTintClass(entry).trim() : '') + '"><td><span class="row-link" data-lot="' + key + '">' + escapeHtml(m.description || m.materialCode) + '</span>' + orderOnlyNote + latestNote(m) + '</td>' +
          '<td>' + escapeHtml(m.category || '-') + '</td>' +
          '<td>' + escapeHtml(m.subCategory || '-') + '</td>' +
          '<td>' + (m.qtyLabel ? escapeHtml(m.qtyLabel) : (m.qty ? m.qty.toLocaleString('en-IN') : '0')) + '</td>' +
          '<td>' + formatInr(m.value || 0) + '</td>' +
          '<td>' + (m.rate != null ? formatInr(m.rate) : 'Not available') + '</td>' +
          '<td>' + daysLeftCellHtml(m) + '</td>' +
          (() => { const st = computeMaterialStatus(m, entry); return '<td><span class="status-pill ' + MAT_STATUS_PILL_CLASS[st] + '">' + escapeHtml(MAT_STATUS_LABELS[st]) + '</span>' + rowFlags(entry, st) + '</td>'; })() +
          '<td>' + materialStepperHtml(m) + '</td>' +
          '<td><span class="row-link" data-lot="' + key + '">View analysis</span></td></tr>';
        }).join('') +
        '</tbody></table></div>' + paginationHtml;
      }
      return '<div class="list-header-row grid-cols"><div>Material</div><div>Category</div><div>Sub Category</div><div>Stock' + stockAllPlantsSuffix + '</div><div>Inventory Value' + stockAllPlantsSuffix + '</div><div>Latest Rate</div><div>Days Left</div><div>Status</div><div>Progress</div><div>Details</div></div>' +
        '<div class="list-header-row grid-cols col-filter-row-grid">' + matFilterCells.map(c => '<div>' + c + '</div>').join('') + '</div>' +
        '<div class="top5-list" id="matTop5List">' + listRecs.map(m => {
          const key = escapeHtml(materialModalKey(m));
          const entry = linkageByKey.get(normalizeMaterial(m.description));
          const orderOnlyNote = m.orderOnly ? '<div class="fs-11 text-slate-soft">On order, no stock lot yet</div>' : '';
          const st = computeMaterialStatus(m, entry);
          return '<div class="top5-row' + (entry ? rowTintClass(entry) : '') + '">' +
            '<div><span class="row-link" data-lot="' + key + '">' + escapeHtml(m.description || m.materialCode) + '</span>' + orderOnlyNote + latestNote(m) + '</div>' +
            '<div>' + escapeHtml(m.category || 'Not available') + '</div>' +
            '<div>' + escapeHtml(m.subCategory || 'Not available') + '</div>' +
            '<div>' + (m.qtyLabel ? escapeHtml(m.qtyLabel) : (m.qty ? m.qty.toLocaleString('en-IN') : '0')) + '</div>' +
            '<div>' + formatInr(m.value || 0) + '</div>' +
            '<div>' + (m.rate != null ? formatInr(m.rate) : 'Not available') + '</div>' +
            '<div>' + daysLeftCellHtml(m) + '</div>' +
            '<div><span class="status-pill ' + MAT_STATUS_PILL_CLASS[st] + '">' + escapeHtml(MAT_STATUS_LABELS[st]) + '</span>' + rowFlags(entry, st) + '</div>' +
            '<div>' + materialStepperHtml(m) + '</div>' +
            '<div><span class="row-link" data-lot="' + key + '">View analysis</span></div></div>';
        }).join('') + '</div>';
    })();
}

/** Re-renders the list region alone, in place. Falls back to a full
 * renderMaterialsView() if the region (or the context it needs) isn't there
 * - e.g. called before the first full render, or after something else
 * replaced #viewContent. */
function renderMaterialsListRegion() {
  const region = document.getElementById('matListRegion');
  if (!region || !MAT_LIST_CTX) { renderMaterialsView(); return; }
  preserveFocus(region, () => { region.innerHTML = materialsListRegionHtml(); });
  applyDynamicStyles(region); // the row-tint legend's dots - see shared.js's own comment
  wireMaterialsListRegion();
}

function wireMaterialsListRegion() {
  const totalPages = MAT_LIST_CTX ? MAT_LIST_CTX.totalPages : 1;
  const region = document.getElementById('matListRegion');
  if (!region) return;

  const toggleBtn = document.getElementById('toggleMatBtn');
  if (toggleBtn) toggleBtn.onclick = () => { state.showAllMaterials = !state.showAllMaterials; state.matTablePage = 1; renderMaterialsListRegion(); };
  region.querySelectorAll('[data-lot]').forEach(el2 => el2.onclick = () => openMaterialModal(el2.dataset.lot));

  const matPrevPageBtn = document.getElementById('matPrevPageBtn');
  if (matPrevPageBtn) matPrevPageBtn.onclick = () => { state.matTablePage = Math.max(1, state.matTablePage - 1); renderMaterialsListRegion(); };
  const matNextPageBtn = document.getElementById('matNextPageBtn');
  if (matNextPageBtn) matNextPageBtn.onclick = () => { state.matTablePage = state.matTablePage + 1; renderMaterialsListRegion(); };
  region.querySelectorAll('[data-matpage]').forEach(btn => btn.onclick = () => { state.matTablePage = Number(btn.dataset.matpage); renderMaterialsListRegion(); });
  wireJumpToPage('mat', totalPages, (n) => { state.matTablePage = n; renderMaterialsListRegion(); });

  // Header filter row (only present when showingAll).
  //
  // The Material text box re-renders THIS REGION ONLY, debounced (see
  // shared.js's debounceRender()) - it is a table-only filter, so the KPI
  // row, the category/flag bar and the chart above cannot have changed.
  // Category/Sub Category/Status write into the "global" filter fields the
  // KPI row and chart are built from, so those do need the full re-render -
  // and they fire on 'change' (a committed selection), not per keystroke,
  // so one full pass each is all they ever cost.
  region.querySelectorAll('[data-mcf]').forEach(inp => {
    const key = inp.dataset.mcf;
    const eventName = (inp.tagName === 'SELECT' || inp.type === 'number') ? 'change' : 'input';
    const isGlobal = key === 'status' || key === 'category' || key === 'subCategory';
    const rerender = isGlobal ? () => renderMaterialsView() : debounceRender(() => renderMaterialsListRegion());
    inp.addEventListener(eventName, () => {
      const raw = inp.value;
      if (key === 'status') { state.matStatusFilter = raw || null; }
      else if (key === 'material') { state.matColFilters.material = raw; }
      else if (key === 'category') { state.matCategoryFilter = raw || null; state.matSubCategoryFilter = null; }
      else if (key === 'subCategory') { state.matSubCategoryFilter = raw || null; }
      else { state.matColFilters[key] = raw; }
      state.matTablePage = 1;
      rerender();
    });
  });
}

// ── Drill-down horizontal bar chart: Inventory Value by Category ->
// Subcategory -> Material. Bars/breadcrumb markup built here; the actual
// Chart.js instance + click handling is wired in wireMaterialsChart() right
// after this HTML lands in the DOM (mirrors renderPoList()'s own
// render-HTML-then-wire-charts split).
const MAT_CHART_TOP_N = 5;
function materialsChartLevelData(materials) {
  if (state.matChartLevel === 'category') {
    const totals = {};
    materials.forEach(m => { const k = m.category || 'Uncategorized'; totals[k] = (totals[k] || 0) + (m.value || 0); });
    return Object.entries(totals).map(([label, value]) => ({ label, value }));
  }
  const inCategory = materials.filter(m => (m.category || 'Uncategorized') === state.matChartCategory);
  if (state.matChartLevel === 'subcategory') {
    const totals = {};
    inCategory.forEach(m => { const k = m.subCategory || 'Uncategorized'; totals[k] = (totals[k] || 0) + (m.value || 0); });
    return Object.entries(totals).map(([label, value]) => ({ label, value }));
  }
  // 'material' level
  const inSub = inCategory.filter(m => (m.subCategory || 'Uncategorized') === state.matChartSubcategory);
  return inSub.map(m => ({ label: m.description || m.materialCode, value: m.value || 0, material: m }));
}
// The top MAT_CHART_TOP_N bars by value, plus what was left off - shared by
// renderMaterialsChart() (markup, and the note naming the rest) and
// wireMaterialsChart() (the Chart.js data), so the two never disagree on
// which bars are shown. Top 5 with no "Other" bar (project owner,
// 2026-09-25): a sixth bar summing everything else was usually the longest
// one and said nothing about which materials it held.
function materialsChartBars(materials) {
  const all = materialsChartLevelData(materials).sort((a, b) => (b.value || 0) - (a.value || 0));
  const rest = all.slice(MAT_CHART_TOP_N);
  return { bars: all.slice(0, MAT_CHART_TOP_N), restCount: rest.length, restValue: rest.reduce((s, b) => s + (b.value || 0), 0) };
}
function renderMaterialsChart(materials) {
  const { bars, restCount, restValue } = materialsChartBars(materials);
  const levelNoun = state.matChartLevel === 'category' ? 'categories' : state.matChartLevel === 'subcategory' ? 'sub categories' : 'materials';
  const restNote = restCount ? 'Top ' + MAT_CHART_TOP_N + ' by value. ' + restCount + ' more ' + levelNoun + ' hold ' + formatInr(restValue) + '.' : '';
  const crumbs = [{ label: 'All Categories', level: 'category' }];
  if (state.matChartCategory) crumbs.push({ label: state.matChartCategory, level: 'subcategory' });
  if (state.matChartSubcategory) crumbs.push({ label: state.matChartSubcategory, level: 'material' });
  const crumbHtml = crumbs.map((c, i) => {
    const isLast = i === crumbs.length - 1;
    return '<span class="crumb ' + (isLast ? 'active' : '') + '"' + (isLast ? '' : ' data-crumb-level="' + c.level + '"') + '>' + escapeHtml(c.label) + '</span>';
  }).join('<span class="crumb-sep">&rsaquo;</span>');

  if (!bars.length) {
    return '<div class="chart-panel mb-20"><h4>Inventory Value by Category</h4>' +
      '<div class="chart-breadcrumb">' + crumbHtml + '</div>' +
      '<div class="no-data-note">No materials in this ' + (state.matChartLevel === 'category' ? 'view' : state.matChartLevel) + ' to chart.</div></div>';
  }
  // At most MAT_CHART_TOP_N bars, so this is ~220px - close to the 250px
  // .chart-box every other page's charts use. Chart.js's maxBarThickness
  // (below) caps how thick a bar gets when there is room to spare.
  const chartHeight = Math.min(300, Math.max(220, bars.length * 22));
  return '<div class="chart-panel mb-20"><h4>Inventory Value by Category' + (state.matChartLevel !== 'category' ? ' &rsaquo; Subcategory' : '') + (state.matChartLevel === 'material' ? ' &rsaquo; Material' : '') + '</h4>' +
    '<div class="chart-breadcrumb">' + crumbHtml + '</div>' +
    '<div class="chart-box" data-height-px="' + chartHeight + '"><canvas id="matDrillChart"></canvas></div>' +
    '<div class="no-data-note mt-8">' + escapeHtml([restNote, state.matChartLevel !== 'material' ? 'Click a bar to drill down.' : ''].filter(Boolean).join(' ')) + '</div>' +
  '</div>';
}
function wireMaterialsChart(materials) {
  document.querySelectorAll('[data-crumb-level]').forEach(c => c.onclick = () => {
    const level = c.dataset.crumbLevel;
    state.matChartLevel = level;
    if (level === 'category') { state.matChartCategory = null; state.matChartSubcategory = null; }
    if (level === 'subcategory') state.matChartSubcategory = null;
    renderMaterialsView();
  });
  const canvas = document.getElementById('matDrillChart');
  if (!canvas) return;
  const { bars } = materialsChartBars(materials);
  try {
    const chart = new Chart(canvas, {
      type: 'bar',
      data: {
        labels: bars.map(b => b.label),
        datasets: [{
          data: bars.map(b => b.value),
          backgroundColor: '#2563eb',
          borderRadius: 5,
          borderSkipped: false,
          maxBarThickness: 26,
        }],
      },
      options: {
        indexAxis: 'y',
        maintainAspectRatio: false,
        plugins: {
          legend: { display: false },
          tooltip: { backgroundColor: '#0f1b2d', padding: 10, cornerRadius: 8, displayColors: false, callbacks: { label: c => formatInr(c.parsed.x) } },
        },
        scales: {
          x: { grid: { color: '#eef1f5' }, border: { display: false }, ticks: { font: { size: 11 }, color: '#475569', callback: v => formatInr(v) } },
          y: { grid: { display: false }, ticks: { font: { size: 11 }, color: '#475569' } },
        },
        onClick: (evt, elements) => {
          if (!elements.length) return;
          const bar = bars[elements[0].index];
          if (state.matChartLevel === 'category') { state.matChartCategory = bar.label; state.matChartLevel = 'subcategory'; renderMaterialsView(); }
          else if (state.matChartLevel === 'subcategory') { state.matChartSubcategory = bar.label; state.matChartLevel = 'material'; renderMaterialsView(); }
          // `bar.material` is an aggregateMaterialsByName() group now (no
          // top-level lotId of its own) - open on its anchor lot, same as
          // the table's own row click (openMaterialModal re-derives the
          // full cross-plant picture from the description regardless of
          // which specific contributing lot it's handed).
          else if (bar.material) openMaterialModal(materialModalKey(bar.material));
        },
      },
    });
    pageCharts.push(chart);
  } catch (e) {
    console.error('Materials drill-down chart failed to render:', e);
    if (canvas.parentNode) canvas.parentNode.innerHTML = '<div class="no-data-note">Chart unavailable right now - the rest of the page is unaffected.</div>';
  }
}

