# Frontend (static HTML + vanilla JS)

Six pages live in `frontend/`: `index.html` (the dashboard at `/`), `home.html`, `search-po.html`,
`review.html`, `admin.html` and the unauthenticated `login.html`. **There is no bundler, no build
step and no ES modules**: every `.js` file is a plain `<script>` tag and all of a page's scripts
share one global scope, so load order is the dependency graph. What each page loads, in order, is in
[HTML pages](#frontendhtml-pages). Feature rules (what a KPI means, why a flag fires) belong to
[api-and-features.md](api-and-features.md); this doc covers how the JS implements them. Auth and
session behaviour is in [auth-security-email.md](auth-security-email.md).

---

### Frontend

#### Serving, load order and the one global scope

`/` is rendered by Django (`config/urls.py`'s `TemplateView.as_view(template_name="index.html")`,
with `TEMPLATES["DIRS"]` pointing at `frontend/`), which is why `index.html` is the one page using
`{% load static %}` / `{% static %}` tags. Every other page and every CSS/JS/image file is served by
WhiteNoise directly from `frontend/` (`WHITENOISE_ROOT`), with plain relative paths and no
templating. See [architecture.md](architecture.md) for the request flow.

- `theme-init.js` runs first, synchronously in `<head>`, on every page (theme before first paint).
- On every protected page `auth.js` is the first body script, then `shared.js`, then the page's own
  scripts. `auth.js` has no dependencies at load time; its page gate is `requireAuth()`, which each
  page bootstrap awaits before rendering anything.
- On `index.html`, every per-concern file must load before `main.js`: `main.js` calls `init()` on
  its last line and reaches into all of them through shared globals. `main.js`'s header comment has
  a module map, though it does not list `export-panel.js`, `rodtep-panel.js` or
  `advance-license-panel.js`.

`main.js` was once a single ~3,460-line file. It is now one file per concern (see the
[File reference](#file-reference)). **If you are looking for a function that used to be in
`main.js`, grep `frontend/js/` rather than assuming it was deleted.**

Each page's own bootstrap was likewise extracted from inline `<script>` blocks (`theme-init.js`,
`login-theme-toggle.js`, `home-page.js`, `admin-page.js`, `search-po-page.js`, `review-page.js`), and
every inline `onclick`/`onerror` attribute became a real listener. That split was done so CSP's
`script-src` could drop `'unsafe-inline'` entirely, not for tidiness. The same is true of styles:
`style-src` has no `'unsafe-inline'` either, so **never write a literal `style="..."` into rendered
markup**. A genuinely dynamic value goes into a `data-*` attribute and is applied from JS after
render (`shared.js`'s `applyDynamicStyles()`, `admin-page.js`'s `renderBarList()`); setting
`el.style.x` from JS is not blocked by CSP. See [auth-security-email.md](auth-security-email.md) for
the CSP itself.

Cross-file references are ordinary globals, and some go "backwards" at call time (for example
`shared.js`'s `dismissMatch()` calls `closeModal()`, which lives in
`charts.js`). That works because nothing is called until the page has finished loading. It is also
why ESLint's `no-undef` is off (see [`.eslintrc.json`](#eslintrcjson)).

#### The dashboard keeps itself fresh - `main.js`'s freshness watcher

`fetchSyncStatus(key)` shares one sync-status request per plant among every caller within
`SYNC_STATUS_SHARE_MS` (3 s) - on load the badges, the view tabs and `resumeSyncIfRunning()` each
fetched it, three per plant. The sync-progress poll and the pre-sync baseline still read it directly;
`clearDataCaches()` empties the share. `loadSyncStatus()` carries its own request id
(`syncStatusRequestId`), so a fast plant-tab switch cannot land the previous plant's badges last.

`currentDataStamp()` folds each plant's `sync-status` `dataChangedAt` (apps/services/data_stamp.py) in
with its SyncRun times, so a colleague's correction, pin or dismissal reloads other open dashboards
on the next tick instead of waiting for the hourly sync.

`ensurePOsLoaded()` fills `PURCHASE_ORDERS_BY_PLANT` once and only refetches when the cache is
cleared, and at one time the only two places that cleared it were the viewer's own "Refresh Data"
click and the end of an admin-triggered sync's polling loop. **Every other way the database moves
left an open page showing its open-time numbers indefinitely, with nothing on screen saying so**:
the hourly scheduled sync, a colleague's sync, a sync from `admin.html` or another tab, an admin sync
whose poll hit `SYNC_POLL_TIMEOUT_MS` before the sync actually finished, or a `match_*` command run by
hand. Reported against a real screenshot: 7 of 11 KPI cards stale, the 4 that agreed only because
nothing in their input had moved.

`checkFreshness()` polls each selected plant's own `/sync-status` (the endpoint the badges already
read - deliberately not a new one) every `FRESHNESS_POLL_MS` (60 s). `currentDataStamp()` builds a
stamp of the newest `finishedAt`/`startedAt` per plant; when it differs from `DATA_STAMP` (what the
current render was built on) the watcher calls `clearDataCaches()`, re-reads the badges and
re-renders. A **timestamp, not a row count**: matching can re-point a match row without any count
changing, and the KPI cards read match rows.

Three guards, each load-bearing:

- `MANUAL_SYNC_RUNNING` (set for the whole of `triggerRealSyncAndRefresh()` and by
  `resumeSyncIfRunning()`, cleared in `pollSyncUntilDone()`'s `finally`, and also released on the
  trigger's own failure path) stops the watcher fighting the manual reload. A timeout or a throw
  cannot disable the watcher for the session.
- An open `.modal-backdrop.open` defers it, so the list never rebuilds under a reviewer reading a PO.
- A failed poll returns quietly and tries again next tick.

**The hidden-tab check sits on the interval, not inside `checkFreshness()`** - it is a polling
policy, not a freshness rule. That placement is also what makes the function testable: an
embedded/automated browser can report `document.hidden` as `true` permanently, which made every call
a silent no-op while it lived inside. A `visibilitychange` listener re-checks the moment a tab is
fronted, so a page left open overnight is correct as soon as someone looks at it. Verified with a
throwaway in-browser harness (17 assertions) since there is no Node here to run a JS test runner; the
harness was deleted after.

#### Every refresh says what it did - the status line beside "Refresh Data"

Project owner: *"whenever I click on refresh the user is kind of in a black spot whether the data
refreshed or not until I hard reload it, same for the syncing too"*. Every path worked; none said so.
A viewer's refresh re-reads the DB and usually changes nothing on screen, identical to a click that
did not register; an admin sync sat on "Syncing..." for minutes, then re-rendered silently (its
completion went to `announce()`, i.e. screen readers only) and called itself complete even when a
step failed; the freshness watcher left no trace at all.

`main.js`'s `setRefreshStatus(kind, text, title)` now fills one `#refreshStatus` line on every path:
"Loaded at", "Reloaded at ... - already up to date (last Drive sync ...)" vs "- new data since your
last load" (decided by `DATA_STAMP` before/after), "Syncing from Drive... N of M steps done
(elapsed)", then "Synced at ... - N rows updated from Drive" / "Drive files had no changes" /
**"with errors in MIR (RTP-Vapi)"** with each step's `errorDetail` in the tooltip, and "Updated
automatically at ..." from the watcher. It is text that stays put, not a toast: someone who looks
back a minute later still gets the answer. There are no `alert()`s on these paths.

- **Progress counts steps against a baseline** (`syncBaseline()`: each step's `startedAt` before the
  trigger; `stepsDoneSince()` counts only runs that started after it), so it counts only this sync's
  runs. It can end below the total when a plant was already mid-sync (409); completion is decided by
  the in-progress flags, never by the count.
- **Only Drive-reading steps feed "rows updated"** (`DRIVE_SYNC_STEPS`: `po_csv`, `mir`, `stock`,
  `import_po_csv`); match/consumption re-derive every run, so their counts do not mean the source
  changed.
- **A sync already running when the page loads is picked up** (`resumeSyncIfRunning()`), for every
  role: button locked, elapsed time only, since steps finished before load cannot be told apart.
- **`loadAndRender()` returns whether the view loaded**, so no path reports "refreshed" over the
  view's own error panel.

Two real defects came with it. **The Import tab's Refresh re-rendered the same orders**: both manual
paths used to clear only the domestic PO and materials caches, never `IMPORT_PO_CACHE`, so there a
reload genuinely was the only way to see new data; all three paths (viewer refresh, admin sync, the
watcher) now call `clearDataCaches()`, which also drops `IMPORT_PO_DETAIL_CACHE`. And **a failed
sync trigger left `MANUAL_SYNC_RUNNING` set**, switching the freshness watcher off for the rest of the
session, because only `pollSyncUntilDone()` ever released it. The server side of "fresh" (every
`/api/` response defaults to `Cache-Control: no-store`) is in [architecture.md](architecture.md). The
frontend was verified with a throwaway in-browser harness driving the real `init()`, refresh button
and sync poll against a stubbed API; deleted after.

`shared.js` (loaded on every protected page right after `auth.js`) holds what would otherwise be
duplicated per page: `PLANTS`/`PLANT_KEYS`, the authenticated `apiForPlant()` wrapper (401 -> silent
session renewal via `auth.js`'s `authFetch()`, then `/login.html` only if renewal fails - see
[auth-security-email.md](auth-security-email.md)), `escapeHtml`/`formatInr`/`formatDateIN`, the
inline-edit helpers, and the accessibility helpers.

#### A header-filter keystroke re-renders the list region only - never the whole view

Reported as the Raw Materials search "refreshing/reloading every time I type something, it's
horrifying", and "somewhat in Purchase order too". Two separate defects, and the loud one was not
the obvious one:

- **`preserveFocus()` only ever looked at `data-cf`.** It is defined in `flags.js` and called from
  all three list views, but Materials' inputs carry `data-mcf` and Import's `data-icf`, so in those
  two views nothing re-focused the rebuilt input - the caret was gone after the first character and
  the next keystroke went nowhere. Domestic PO was the only view where it worked, which is exactly
  why the bug survived in the two files that call the helper without owning it. It now matches on
  all three attributes (`FILTER_ATTRS`), the same set `applyAccessibleNames()` reads.
- **Every keystroke rebuilt the entire view.** A text filter is table-only - it narrows `tableRecs`,
  never `filtered` - yet the `input` handler re-ran the whole render: KPI cards restarting their
  count-up animations from 0, `destroyPageCharts()` plus fresh `new Chart(...)` for every canvas,
  and in Materials the PO<->material linkage pass as well. That flash *is* what "it reloads" was
  describing.

Each of `po-list.js` / `import-po.js` / `materials.js` renders its list (heading, header filter row,
rows, pagination) into its own `#poListRegion` / `#importListRegion` / `#matListRegion` container via
`poListRegionHtml()` / `importListRegionHtml()` / `materialsListRegionHtml()`, reading a module-level
`PO_LIST_CTX` / `IMPORT_LIST_CTX` / `MAT_LIST_CTX` that the last **full** render filled with what a
text filter cannot affect. A keystroke re-renders that region alone, debounced through `shared.js`'s
`debounceRender()` (`LIST_FILTER_DEBOUNCE_MS = 150`; Search PO's and the MIR picker's 250 ms is
longer because those await a fetch). `renderPoListRegion()` and its siblings fall back to a full
render if the region or its context is missing.

**The split of which control takes which path is load-bearing, not cosmetic.** A control that writes
into a "global" filter - one the KPI row or the charts are built from, or whose KPI highlight must
stay in step - must still call the full render: Domestic's Created On (`createdFrom`/`createdTo`) and
Status, Materials' Category/Sub Category/Status, and every "Clear" that resets those. Everything else
is table-only and stays in the region (every Import header filter is table-only). If you add a header
filter, decide which it is by checking whether `filtered` or only `tableRecs` sees it; the cheap path
silently shows stale KPI numbers for a global filter. The state write itself is never debounced -
only the render - so the next full render always sees what was typed.

**The header filter row must be built inside the region function, never handed over in the context
object.** Each cell carries its filter's *current* value, so a snapshot taken at full-render time
rewrites the box being typed into back to its pre-keystroke value: the list narrows correctly while
the text vanishes under the cursor. All three files had exactly that bug for the length of one
verification run, which is the only reason it is written down here. (Option *lists* that depend only
on global filters, such as `MAT_LIST_CTX.catOptions`, are safe to carry.)

`debounceRender()` is a **factory**: call it once and keep the returned function. Calling it inside
an `input` handler builds a fresh timer per keystroke and debounces nothing (`no-po-panel.js` and
`po-modal.js`'s `wireMirPicker()` both comment on this).

Verified with a throwaway in-browser harness against the running dev server (~85 assertions: each
view rendered end to end from synthetic data, a real `input` event on its search box, then asserting
the list narrowed, focus and caret and typed text survived, and the KPI card / chart-panel elements
were *the same DOM nodes* as before the keystroke). The harness was not committed.

#### Search PO deep-links into the dashboard rather than "/"

Project owner: "instead of open entire dashboard can't we take user to the info about that PO, or
raw material". The detail panel's old `Open Full Dashboard` link landed the reader on All Plants with
no filters, leaving them to find by hand the record they had just searched for.
`search-po-page.js` builds `/?plant=<key>&po=<number>` (`dashboardPoHref()`) and, per line item,
`/?plant=all&material=<description>` (`dashboardMaterialHref()`); `main.js`'s `readDeepLinkParams()`
reads them in `init()` before the tabs render, `applyDeepLinkToState()` points the view/plant at the
target (so the right plant is fetched once, not twice), and `openDeepLinkTarget()` opens that PO's
or material's own modal after the first render. A `?po=` link means Domestic Purchases unless it
carries `kind=import`, which Search PO adds for an import order - one number can exist as both, so
the number alone cannot say which was clicked. `kind` is whitelisted (`import`, else Domestic), and
an import link resolves against `IMPORT_PO_CACHE` by number and plant, then `openImportPoModal()`.

Four details: `plant` is honoured only if it is a real plant key or `all` - **everything in a URL is
attacker-supplied**, and the miss message (`showDeepLinkMiss()`) goes through `textContent`, never
`innerHTML`; a PO link names its plant because PO numbers are not unique across plants, while a
material link is deliberately cross-plant (`plant=all`) since that view rolls a material up across
all three anyway (resolved by exact normalized description first, then `findMaterialLotsFor()`); a
target that no longer resolves (a **retired/renamed PO** is the realistic case - see
[data-sync.md](data-sync.md)) says so in a `.validation-note` instead of opening silently as if
nothing was asked for; and the params are **consumed** - `clearDeepLinkParams()` strips `po`,
`material`, `plant` and `kind` from the address bar (via `replaceState`, so Back is unaffected) once the link
has been acted on.

**That last one was the opposite way round for a few hours and was wrong.** The first version left
the URL in place, reasoning that `/?plant=hrs&po=3000001082` is "a real address for a PO" and ought
to survive a reload. The project owner reported the result the same day: *"i search this dashboard
and every time i reload it's get open don't know why"*. A modal is transient - something the reader
dismisses - so re-opening it on every refresh of what is, by then, just the dashboard reads as the
page being stuck, with no way out short of editing the URL by hand. **Sharing was never the thing at
risk**: the link still opens the PO for whoever follows it, once. The clear runs in a `finally`, so a
link that missed or threw is consumed too - otherwise the failure message replays on every refresh,
which is the more confusing half of it.

#### One line visible, detail one click away

Reported as *"instructions are everywhere on dashboard can we make them simpler"*. Three
near-identical 60-word amber `.validation-note` banners sat permanently above the PO dashboard, the
Raw Material list and the Material modal, all saying a version of "matching is automatic, verify
manually" - which every confidence badge and flag badge already says on hover. `shared.js`'s
`matchingDisclaimerHtml(summary, detailHtml)` replaces all three with one sentence plus a **How
matching works** disclosure (call sites: `main.js`'s `init()`, `materials.js`'s
`renderMaterialsView()`, `material-modal.js`'s `openMaterialModal()`). Built on a native
`<details>`/`<summary>` rather than a state flag + re-render like the Data Quality legend's
(`state.legendOpen`): the three live in three different render paths (one a modal that re-renders on
every field save), and the browser handles open/close with no wiring, no shared state and no
CSP-blocked inline style. `detailHtml` is trusted markup from a literal at each call site, never user
data. `.validation-note` survives for the deep-link miss message and the bucket help text in
`no-po-panel.js`.

`DISCREPANCY_LEGEND` entries (`flags.js`) have an optional `detail`, so the one-sentence definition
stays on the first line and the causes/caveats drop to a muted second line - six entries ran 60+
words with the definition buried mid-sentence. The moving-average explainer under the material price
chart is a `title` tooltip, and the "Click the pencil..." hints were shortened rather than deleted:
the hint is what explains an otherwise-empty correction box.

#### Modals: one shared shell, and the stale-response guard

`index.html` has one `#modalBackdrop` / `#modalBody` shell. Every dashboard modal (Domestic PO,
Import PO, Material, BL tracking, Export, No-PO panel, RoDTEP, Advance Licence) swaps
`#modalBody.innerHTML` rather than templating its own; only one is ever open. Openers add `.open`,
call `openModalA11y(backdrop)` (see [Accessibility conventions](#accessibility-conventions)), and set
`backdrop.onclick` to close on a click outside the panel. Every close button is a `.close-btn` caught
by one delegated listener in `charts.js`. `closeModal()` asks before discarding a half-written
correction, destroys modal charts and calls `closeModalA11y()`.

**Every modal opener that awaits a fetch needs the stale-response guard.** Clicking row A then row B
before A's fetch resolves can let A's stale response land after B's and overwrite the modal. Each
guarded opener captures `const myModalRequestId = ++modalRequestId` (`charts.js`) on entry and returns
early if it changed after **every** `await`. It covers `openPoModal()`, `openImportPoModal()`,
`openMaterialModal()` and `openRodtepScriptDetail()`. `openNoPoPanel()`, `openRodtepPanel()`,
`openAdvanceLicensePanel()` and `trackBlNumber()` also await a fetch and do **not** use it today (and
`trackBlNumber()` does not call `openModalA11y()`); apply both if you touch them or add an opener.
`openAdvanceLicenseDetail()` needs no guard because it renders from data already in hand.

Chart instances are split on purpose (`charts.js`): `pageCharts` belong to the view behind the modal
and are destroyed only right before that view re-renders; `modalCharts` belong to the open modal and
are destroyed on every close/reopen. They were once one registry, and closing any modal blanked the
dashboard's own charts with nothing to redraw them. **Do not merge them back.** Every `new Chart(...)`
is wrapped in `try/catch` that replaces only that canvas with a note, so a CDN or CSP failure to load
Chart.js cannot take the whole view down (a real failure hit on 2026-09-03).

#### CSS: two palettes, never merged

`brand.css` owns the shared top nav and page chrome (gold/navy, ported from TDS) and is loaded on
every page; `style.css` owns the dashboard's separate navy/blue/red palette and is loaded only by
`index.html`; each other page layers its own `css/<page>-page.css`. `index.html` is the only page
loading both - **do not merge the two palettes**, that separation is deliberate (see `brand.css`'s
header comment).

#### CSS custom-property collisions

**Two stylesheets must not define the same custom property.** Five properties were once defined with
*different* values in both files; `index.html` loads `style.css` second, so `style.css` silently won
every shared name - **including for `brand.css`'s own rules**, which reference those names to style
the shared top nav. The nav rendered in a different blue, green, purple and shadow on the dashboard
than on every other page (confirmed in a real browser by resolving computed values). It was a repeat
of an earlier `--navy` drift. `style.css`'s copies were renamed `--dash-*` (`--dash-blue`,
`--dash-red`, `--dash-green`, `--dash-purple`, `--dash-shadow`) with values unchanged, and
`apps/api/tests/test_css_token_collisions.py` fails the build on a new collision. Tokens both files
define with the **same** value (`--navy`, `--navy-solid`, `--border`) are deliberate. If you add a
token to either file, grep the other first. See [testing-deployment.md](testing-deployment.md) for the
test.

The reverse gap is not tested: `brand.css`'s utility classes `.text-slate-soft`, `.text-slate`,
`.text-gray`, `.sep-gray`, `.empty-note`/`.empty-note-sm` and `.skip-link:focus` read tokens
(`--slate-soft`, `--slate`, `--gray`, `--on-accent`) that only `style.css` defines, so on the four
non-dashboard protected pages they resolve to nothing (the skip link's text colour is inherited).

**`[hidden]` is enforced globally.** `brand.css` has `[hidden] { display: none !important; }`,
loaded on every page, so toggling `el.hidden` always wins over a same-specificity `display` rule. It
exists because a class rule once beat the browser's own `[hidden]` rule on a specificity tie and the
element stayed visible; a few component-level `[hidden]` rules (`.ov-error`, `.ov-cancel`,
`.mir-picker`, `.modal-tab-panel`, `.admin-tab-panel`) predate or duplicate it.

**Dark mode** is defined twice per stylesheet: once under `@media (prefers-color-scheme: dark)`
guarded by `:root:not([data-theme="light"])`, and once under `:root[data-theme="dark"]`, so the
toggle can force either theme. `--navy` flips light (it is text colour); `--navy-solid` never flips
(solid backgrounds). Colour must come from tokens for dark mode to follow.

#### Other traps

- **The same global name in two files silently overrides.** `userInitials()` is defined in both
  `auth.js` and `admin-page.js`; on `admin.html` the later one wins (the logic is identical today).
  ESLint's `no-redeclare` only sees one file at a time, so it cannot catch this.
- **Check `prompt()`'s result for `null` before defaulting it.** `wireDismissLinks()` once read the
  note as `window.prompt(...) || ''` and then tested `=== null`, which could never be true, so Cancel
  still dismissed. It now keeps the raw answer, returns on `null`, and only then uses it; an empty
  note still dismisses.
- **`admin-page.js` hardcodes `DELETE_USER_ALLOWED_EMAIL`** while the backend reads the setting of
  the same name from the environment. Changing the env var alone hides the Delete button from the new
  account; the backend remains the real gate.
- **Exact money is not `formatInr()`.** `formatInr()` rounds to whole rupees and abbreviates at a
  lakh/crore; reconciliation cards (`reconMoney()`) and review cards (`formatMoneyExact()`) use full
  precision on purpose.

### Accessibility conventions

The app had none of the four basics before a dedicated pass: no `aria-live` regions in an app built on
async sync/filter/save, ~50 generated `<input>`/`<select>` controls with no accessible name (several
identified only by the column above them, all sharing the placeholder "Search..."), no
`role="dialog"`/`aria-modal`/focus trap/Escape on any of the seven modals, 109 `<th>` with no `scope`.
(Div-based buttons and tabs already had keyboard activation and `:focus-visible` rings.)

`shared.js` holds `openModalA11y()`/`closeModalA11y()` (dialog semantics, `aria-labelledby` from the
modal's own heading, focus moved in and **restored to the opener** on close, Tab/Shift+Tab wrapped,
Escape to close, listeners torn down), `announce()` (one shared polite live region, `#sr-live-region`,
cleared first so a repeated message is re-announced), and `applyAccessibleNames()` driven by a
**MutationObserver rather than per-render calls** - same reasoning as `SafeCsvWriter` on the backend:
make it structural, not a discipline a future render site has to remember. It names every
`[data-cf]`/`[data-icf]`/`[data-mcf]` control from `FILTER_COLUMN_LABELS`, standalone controls by id
from `CONTROL_LABELS`, and adds `scope` to every `<th>`; it never overrides an explicit `aria-label`
or `<label for>`.

Content usually lands **after** `openModalA11y()` is called (every opener adds `.open` and calls it
before assigning `#modalBody.innerHTML`), so it focuses the panel itself, then a one-shot
MutationObserver applies the heading label and moves focus to the first control as soon as content
arrives, and only while focus is still on the panel. Escape backs out of a field correction first
(`SELECTED_FIELD`) and closes the modal only on a second press.

Two details worth keeping: `.sr-only` in `brand.css` uses the clip-rect technique, **not
`display:none`**, which would remove the live region from the accessibility tree and silence it. And
the labelling pass debounces with `setTimeout`, **not `requestAnimationFrame`** - browsers don't fire
rAF at all while a tab is hidden, so a render in a background tab stayed unlabelled until the tab was
fronted. Nothing here touches layout, so there was never a reason to wait for a frame.

Div-based controls (`[data-kpi]`, `[data-matkpi]`, `.search-result-card`, `.view-tab`, `.plant-tab`,
`.sub-tab`, `.modal-tab`) get Enter/Space activation from one delegated `keydown` listener in
`shared.js`, which re-dispatches a click; each render site supplies `tabindex="0"` and a `role`.
Other clickable spans (the edit pencil, `.mir-change-link`, `.mir-cand`, the no-PO badges and tabs,
the licence-panel tabs) wire their own Enter/Space handlers.

Skip links and `role="main"` are on all five protected pages (not `login.html`), annotating the
**existing** container rather than introducing a `<main>` wrapper, so there was no layout risk.

---

## File reference

### frontend/*.html (pages)

| Page | Served | Styles | Scripts (in order) |
| --- | --- | --- | --- |
| `index.html` (`/`) | Django template (`{% static %}`) | `brand.css`, `style.css` | head: `theme-init.js`, Chart.js 4.5.0 from jsdelivr with an SRI hash; body: `auth.js`, `shared.js`, `charts.js`, `flags.js`, `po-list.js`, `po-reconcile.js`, `po-modal.js`, `import-po.js`, `rodtep-panel.js`, `advance-license-panel.js`, `materials.js`, `material-modal.js`, `export-panel.js`, `no-po-panel.js`, `main.js` |
| `home.html` | WhiteNoise | `brand.css`, `home-page.css` | `theme-init.js`; `auth.js`, `shared.js`, `home-page.js` |
| `search-po.html` | WhiteNoise | `brand.css`, `search-po-page.css` | `theme-init.js`; `auth.js`, `shared.js`, `search-po-page.js` |
| `review.html` | WhiteNoise | `brand.css`, `review-page.css` | `theme-init.js`; `auth.js`, `shared.js`, `review-page.js` |
| `admin.html` | WhiteNoise | `brand.css`, `admin-page.css` | `theme-init.js`; `auth.js`, `shared.js`, `admin-page.js` |
| `login.html` | WhiteNoise | `brand.css`, `login-page.css` | `theme-init.js`; `login-theme-toggle.js`, `login.js` (no `auth.js`/`shared.js`) |

Every protected page has the same static `.topnav` markup (brand link, `#navTabs`, `#navUser`) that
`auth.js` fills, a skip link, and a `role="main" tabindex="-1"` container. `index.html`'s `#root` is
replaced wholesale by `main.js`'s `init()`. `admin.html` ships its create/edit-user modal
(`#uf-overlay`) and `#toastStack` as static markup, plus a `#deniedContent` panel for non-admins
(defence in depth; the endpoints enforce `IsAdmin`). `review.html` has two views (`#viewQueue`,
`#viewStats`) behind `review-viewtab` buttons. Page header comments in `home.html` still mention an
"inline script"; it is `home-page.js`.

### frontend/js/theme-init.js

Runs synchronously in every page's `<head>`. Applies a stored `pt-theme` (`light`/`dark`) to
`<html data-theme>` before first paint, and installs one **capture-phase** `error` listener that hides
any `img[data-hide-on-error]` that fails to load (the `error` event does not bubble, so bubble phase
would never fire). Replaces the old inline `onerror` attributes.

### frontend/js/auth.js

`requireAuth()` sends the reader to `/login.html` only for a 401/403. A network failure or a 5xx
(a deploy, a restart) shows `showAuthUnavailable()`'s notice with a Retry button instead - it used to
bounce everyone to the login page on every blip, and they signed in again.

`requireAuth()` calls `shared.js`'s `scopePlantKeysToUser()` once `CURRENT_USER` is known: it narrows
`PLANT_KEYS` in place to the user's `plants` (empty = every plant). Every page builds its tabs and
fetches from `PLANT_KEYS`, so a plant-scoped account used to request plants the server refuses - the
dashboard read "Couldn't load" and Home/Search warned on every load.

Session gate and shared nav chrome for every protected page. Exports `CURRENT_USER`, `authFetch`,
`refreshSession`, `requireAuth`, `logout`, `renderUserBadge`, `renderNavTabs`, `userInitials`,
`initThemeToggle`, `applyTheme`, `isEffectivelyDark`, `escapeHtmlAuth`. Depends on
`openChangePasswordModal()` from `shared.js` at click time only. Auth is the httpOnly `pt_access`
cookie; nothing here holds a token.

- `refreshSession()` - POST `/api/auth/token/refresh`. **Single-flight**: refresh tokens rotate and
  the spent one is revoked, so concurrent callers share the one in-flight promise.
- `authFetch(url, opts)` - drop-in `fetch()`; on a 401 it awaits `refreshSession()` and replays the
  request once, **even if renewal failed** (another tab may already have rotated the shared cookie).
  Every wrapper in the app (`apiForPlant`, `apiImports`, `apiReview`, `savePoField`, the
  password-change calls, every `admin-page.js` call) goes through it. Rationale:
  [auth-security-email.md](auth-security-email.md).
- `requireAuth()` - GET `/api/auth/me`, fills `CURRENT_USER`; any failure logs and redirects to
  `/login.html`. Each page bootstrap returns early when it resolves `null`.
- `logout()` - best-effort POST `/api/auth/logout`, then always redirects.
- `renderNavTabs(container, activePage)` - Home (`/home.html`), Dashboard (`/`), Search PO, Review
  Matches, and Admin only when `role === 'admin'`. `activePage` is `home`/`dashboard`/`search`/
  `review`/`admin`.
- `renderUserBadge(container)` - initials avatar coloured by role (`.avatar-role-*`), name/role, a
  dropdown with Change Password and Logout; a document click closes it.
- `initThemeToggle()` - inserts `#themeToggleBtn` before `.nav-user` and persists the choice to
  `localStorage['pt-theme']`. Also logs every unhandled promise rejection to the console.

### frontend/js/shared.js

Cross-page constants and helpers, loaded on every protected page after `auth.js`. No state beyond a
few module-level variables for the correction box and modal a11y.

- **Plants:** `PLANTS` (label, `apiPrefix` `/api` for HRS, `/api/achhad`, `/api/vapi`,
  `hasVendorOnMaterials`, sync command names used in empty-state hints), `PLANT_KEYS`,
  `ALL_PLANTS_LABEL`, `materialFieldsUrl(plantKey, lotId)` (`<prefix>/materials/<lot>/fields`),
  `materialRateFieldName()` (`rate` at Achhad, `basic_rate` elsewhere - a real schema difference).
- **`apiForPlant(plantKey, path, opts)`** - `authFetch` under the plant's prefix; 401 -> login;
  guards `res.json()` so a non-JSON proxy page becomes a readable `Error`; non-OK throws with
  `err.status` set (callers use it to treat a 409 as "already running"). **`apiImports(path, opts)`**
  is the same contract for `/api/imports` (here rather than in `main.js` because Search PO uses it
  too). Both, and `savePoField()`, build that non-JSON error with **`unexpectedResponseError(status)`**:
  a 502/504 is gunicorn killing the request at its worker timeout, and since a pin or correction is
  written before its synchronous re-match, the message says the change *may still have been saved -
  refresh to check* rather than inviting a blind retry (see
  [api-and-features.md](api-and-features.md#endpoint-conventions-worth-knowing-before-adding-one)).
- **Client ports of backend normalizers:** `normalizeMaterial`, `tokenizeMaterial`, `normalizeVendor`,
  `vendorContains` (containment with a 4-character floor). Kept in sync by hand with
  `parsers/common.py` / the matcher.
- **Formatting:** `escapeHtml`, `formatInr` (display only, rounds and abbreviates L/Cr),
  `formatDateIN` (ISO -> dd/mm/yyyy), `emptyStateHtml(message, tone)`, `infoTooltipHtml(tip)`.
- **`applyDynamicStyles(root)`** - consumes `data-dot-color`/`data-bg-color`, `data-text-color`,
  `data-height-px`, `data-width-pct` (clamped 0-100) and sets `.style`. Call right after assigning
  `innerHTML` for anything that might carry one.
- **Delegated listeners:** `.pw-toggle` show/hide password (a second copy lives in `login.js`),
  Enter/Space activation for div-based controls, and a capture-phase click that stops a click on
  `.info-tooltip` from also toggling the KPI card around it.
- **Change password:** `openChangePasswordModal()` builds and removes its own `#cpwOverlay` (most
  pages have no `#modalBackdrop`); POST `/api/auth/change-password/request`, then
  `/api/auth/change-password/confirm` with `{otp, newPassword}`; client check is length >= 10 and
  match.
- **KPIs for home/admin:** `animateCountUp(el, target, opts)` (skips animation under
  `prefers-reduced-motion`), `wireKpiCountUps(root)` (reads `data-count-target`/`data-count-fmt`
  `inr`/`locale`/`int` on `.kpi-card .val`), and `loadKpis()`: GET `/purchase-orders` for every
  plant in parallel, fills `#kpiTotalPos`/`#kpiSuppliers`/`#kpiThisMonth`/`#kpiThisWeek`, and adds a
  visible "N plant(s) failed to load" note rather than counting a failed plant as zero. This Month
  and This Week compare `createdDate` against `localISODate()` - the viewer's own calendar date.
  Never `toISOString().slice(0, 10)`: that is the UTC date, and local midnight in IST is the
  previous day in UTC, so "today" was yesterday (This Week dropped today's POs, and on the 1st This
  Month counted the old month).
- **Lists:** `LIST_FILTER_DEBOUNCE_MS`, `debounceRender(fn, ms)` (factory), `jumpToPageHtml()` /
  `wireJumpToPage()` (invalid page numbers are ignored, not clamped), `revealFilteredList(regionId)`
  (announces the list heading; scrolls only if the region's top is off-screen; jumps instead of
  gliding under reduced motion).
- **Disclaimer:** `matchingDisclaimerHtml(summary, detailHtml)`.
- **Permissions:** `canEditField(plantKey)` - admin/editor, and `CURRENT_USER.plants` empty (= all)
  or containing the plant. Mirrors the backend check so a pencil or dismiss link is never rendered
  for a write that would 403.
- **Inline "Edit Everywhere"** (feature rules in [api-and-features.md](api-and-features.md)):
  `plainLine`, `editableLine(plantKey, label, value, fieldName, itemId, fieldType, options)` and
  `editableCell(...)` emit `.editable-line`/`.cell-editable` with `data-field`, `data-item`,
  `data-plant`, `data-label`, `data-field-type`, `data-options` (URI-encoded JSON) and, for dates,
  `data-raw-value` (the ISO value the date picker needs; the display is dd/mm/yyyy).
  `distinctFieldValues(list, accessor)` supplies `select` options from loaded data, never a hardcoded
  list. `wireEditIcons(container, fieldsUrl, switchToTab, onSaved)` makes each pencil a keyboard
  button that calls `selectFieldForCorrection()`; `fieldsUrl` may be a string or `(lineEl) => url`.
  `overrideBoxHtml(hint)` renders the one `#overrideBox`; `_moveOverrideBoxTo()` re-parents it under
  the clicked line (or under the `.table-wrap` for a table cell) and `_restoreOverrideBoxHome()` puts
  it back; `switchToTab` is only a fallback when the move fails. `overrideBoxIsDirty()` /
  `confirmDiscardCorrection()` / `cancelFieldCorrection(force)` guard against losing typed work.
  `validateOverrideValue(field, value, inputEl)` runs **before** the write and returns `{error}`
  (blocks) or `{warn}` (confirm): `validity.badInput` on number inputs, numeric/negative checks, year
  2000-2100, clear-a-value confirm, and GSTIN/email regexes that mirror
  `apps/services/validation.py`. `wireOverrideBox()` calls `savePoField(fieldsUrl, itemId, field,
  value, reason)` (PATCH, body `{itemId, field, value, reason}`), clears the selection **before**
  `onSaved` re-renders, and shows "Remember to fix the source file too". `wireRevertLinks()` PATCHes a
  `.revert-link`'s `data-old-value` back with reason "Reverted an earlier correction.". There is no
  inline input or Save/Cancel icon pair beside a field any more; the old `startFieldEdit()` is gone.
- **Dismiss:** `dismissMatch(plantKey, matchType, matchId, dismissed, reason)` - PATCH
  `<prefix>/matches/<po-mir|mir-stock>/<id>/dismiss`, or for `import-po-mir`
  `/api/imports/matches/po-mir/<plant>/<id>/dismiss`. `dismissPoFlag(...)` - PATCH
  `<prefix>/purchase-orders/<po>/flags/dismiss` or `/api/imports/purchase-orders/<plant>/<po>/flags/dismiss`.
- **BL tracking:** `trackBlNumber(bl)` - opens the shared modal and GETs
  `/api/imports/track-bl?bl=`; `blTrackingResultHtml()` reads SafeCube's payload defensively and
  always offers the raw JSON in a `<details>`.
- **Material links from PO modals:** `findMaterialLotsFor()` and `materialAnalysisLinkHtml()` reuse
  `materials.js`'s `materialLinksToItem()` against `MATERIALS_BY_PLANT` (dashboard only; callers load
  every plant's materials first) and emit a `data-material-link="<plant>::<lotId>"` link.
- **Accessibility:** `applyAccessibleNames()` plus its MutationObserver, `focusableWithin()`,
  `openModalA11y(backdrop, dialog)`, `closeModalA11y()`, `announce(message)`.

### frontend/js/charts.js

Chart.js lifecycle, the shared chart look, and the modal close path. **One look for every page
chart:** font, ink and tooltip style are set once on `Chart.defaults`; `refreshChartTheme()` re-reads
`CHART_INK` / `CHART_GRID` / `CHART_STRONG` / `CHART_MUTED` from the stylesheet tokens and runs inside
`destroyPageCharts()`, so dark mode applies on the next render (the doughnut centre text used to be a
fixed dark navy, invisible in dark mode). **Legends are HTML, never the Chart.js canvas legend**,
which cannot wrap and clipped or overlapped long labels ("Delivery Date Unknow..."). **Tooltips are
HTML too:** `htmlChartTooltip()` is the `external` handler for every chart (canvas tooltip disabled
on `Chart.defaults`); the canvas-painted one came out soft on a 125% display, was cut off at the
canvas edge, and had the doughnut's centre label printed over it. Text goes in by `textContent`,
the colour dot and position by `el.style` (CSP-safe). The centre-label plugins draw in
`afterDatasetsDraw`, beneath any tooltip. Legends:
`chartLegendHtml(groups, selectedKey)` renders wrapping chips (a chip with a key is a button that
filters like its KPI card, wired by `wireChartLegend(root, onPick)`), `twoRingLegendHtml(rings,
selectedKey)` groups them by ring with count and share (`sharePct()` prints "<1%", not "0%", for a
small non-zero share). `chartHeadHtml(title, sub, asideLabel, asideValue)` is every panel's header
(title, one-line subtitle, headline figure) and `shortMonthLabel()` ("Jan '26") keeps month axes
untilted. Also exports `pageCharts`,
`modalCharts`, `destroyPageCharts()`, `destroyModalCharts()`, `modalRequestId`, `MONTH_NAMES`,
`formatMonthLabel()` (`YYYY-MM` -> "Mon YYYY"), `fillMonthRange(keys)` (every month from the
earliest to the latest key, so a month with no orders is an empty slot rather than skipped),
`renderTwoRingDoughnut(canvas, {outer, inner, selectedKey, onPick, centerPlugin})` (both list views'
status doughnut: two rings, each a partition of the same POs, slices `{key, label, val, color}`, a
null key not clickable; each ring carries zeros for the other ring's slices because Chart.js shares
one labels array), `centerTextPlugin` / `centerImportTextPlugin`
(per-chart doughnut centre labels "TOTAL POs" / "IMPORT POs"), `closeModal()`, `trailingPriceAvg()`
(calendar-day trailing average used by the material price chart), and the delegated `.close-btn`
click listener. `closeModal()` guards its calls with `typeof` because `SELECTED_FIELD` and
`closeModalA11y` live in `shared.js`.

### frontend/js/flags.js

Status, flag and badge logic shared by every list and modal. Feature semantics are in
[api-and-features.md](api-and-features.md); key implementation points:

- `FLAG_PCT = 0` - zero tolerance with strict `>`; mirrors the matchers' `FLAG_DIFF_PCT` by hand
  ([matching-engine.md](matching-engine.md)). `KPI_FLAG_COLORS`, `flagIconHtml(hex, cls)`.
- **Weighbridge tolerance:** `BULK_QTY_TOLERANCE_PCT = 10` mirrors `qty_tolerance.py` for wording
  only; the backend decides which lines qualify and sends `qtyWithinTolerance`. Every client-side
  qty check (KPI cards, over/short split, import BOE-vs-MIR, Raw Material cards and modal, the
  reconciliation dismiss link) goes through `isQtyMismatch(it)`, never `qtyDiffPct > FLAG_PCT`
  directly, or a tolerated line would flag again in the browser. It is also true when Madura's roll
  counts differ (`rollsDiffer(it)`, from `rollsOrdered` / `rollsReceived`), even at an exact weight. `tintDiffs(it)` gives the diffs
  that may tint a row (a tolerated qty diff, and its value diff unless `netValueMismatched`, are left
  out). `qtyToleranceNote(it)` is the "Qty matched: received 7% over ... within the 10% weighbridge
  tolerance" sentence the reconciliation cards show.
  ([flag thresholds](matching-engine.md#flag-thresholds))
- **Row flags:** `ROW_FLAG_BUCKETS` and `rowFlagsHtml({partial, onOrder, categories})` - at most four
  icons (partial beats on-order, then one red "Mismatch", then one purple "Data quality"), category
  names in a `data-tooltip` CSS tooltip. "PO Not Found in MIR" is dropped from red only when the row
  is on order and not partial - and `computePoFlags()` (domestic, via `po._status === 'pending'`) and
  `importOrderNotDueYet(po)` (import, used by `importCriticalFlagsFor()` and `import-po.js`'s
  `importCategoriesFor()`) no longer raise that category for such an order at all, so the Data
  Quality Flags counts agree with the icons. `rowFlagKeyHtml()` renders the four-colour key above the glossary.
- **Categories:** `FLAG_CATEGORY_RULES` + `categorizeFlag(remarks)` (regex, first match wins,
  otherwise "Other data quality issue"), `DISCREPANCY_LEGEND` (glossary with optional `detail`),
  `CATEGORY_COLORS` / `categoryColor()` (one hue per category for legend dots and modal chips; a
  missing key silently falls back to purple), `renderLegendHtml()` (reads `state.legendOpen`; dots
  via `data-dot-color`).
- **Domestic status:** `computePoFlags(po)` stamps `_qtyFlag`, `_rateFlag`, `_qtyOverFlag`,
  `_qtyUnderFlag`, `_maxDiffPct`, `_categories`, `_allCategories`, `_hasInfoFlag`, excluding dismissed
  matches. `_categories` and the four flags also exclude a dismissed PO-level flag
  (`poFlagDismissed(po, label)`); `_allCategories` keeps it, and is what `po-modal.js` lists so a
  dismissed flag can be reinstated.
  `computeStatus(po)` returns `received`/`partial`/`pending`/`unknown` (and `overdue` only when
  nothing arrived and the date passed) and **also stamps the overlays** `po._overdue` and
  `po._noDeliveryDate`. `lineItemArrived()` / `lineItemFullyReceived()` (short-delivered is not
  received; a null direction is treated as received). `STATUS_LABELS.pending` is "On Order".
  `computePoDeliveryDate()` (earliest unmatched line's date).
- **Import/material flags:** `poFlagDismissed(po, flagKey)` (the one reader of `po.flagDismissals`
  outside the renderers), `importCriticalFlagsFor(po)`, `importFlagHtml(f, po, plantKey)` (flag
  key `<code>:<item_id>`), `poFlagHtml(c, po, plantKey, isImport)` (flag key = label),
  `materialFlagHtml(m)`, `materialUomNoteHtml(lot)`, `dataQualityFlagHtml(f)` with
  `DATA_QUALITY_CHECK_LABELS`, `mirStockMatchHtml(lot, plantKey)` (a unit clash gets the amber
  `uomMismatchBadgeHtml()`, never green "matched").
- `wireDismissLinks(container, plantKey, onDone)` - one handler for every `.dismiss-link`; `data-plant`
  overrides `plantKey` per row; `po-flag`/`import-po-flag` go to `dismissPoFlag()`, everything else to
  `dismissMatch()`; `prompt()` for the dismiss note (Cancel aborts, an empty note still dismisses), `confirm()` to
  reinstate (see [Other traps](#other-traps)).
- `rowTintClass(rec)` (mild/moderate/severe at 5% and 20%, only when qty or rate is flagged; reads
  both `_qtyFlag` and material `qtyFlag` shapes), `rowTintLegendHtml()`, `miniStepperHtml(po)`,
  `materialStepperHtml(m)`, `applyColFilters(recs)` (Domestic table-only filters), `FILTER_ATTRS` and
  `preserveFocus(container, renderFn)`.

### frontend/js/po-list.js

The Domestic Purchases view. `renderPoList(el)` delegates to `renderImportPoList()` when
`state.purchaseType === 'import'`; otherwise it stamps status/flags on `currentPOs()`, applies the
global filters (created date range, material Category/Sub Category from `po.materialCategories`),
builds 11 KPI cards (`total`, `received`, `partial`, `qtydisc`, `qtyover`, `qtyunder`, `ratedisc`,
`overdue`, `pending`, `unknown`, `flags`; `total` clears), the "Filter by Flags" select (fixed options
plus one `cat:<label>` per category present), the month bar chart (click toggles
`state.chartMonthFilter`) and status doughnut. **The bar chart is pre-tax order value (`totalValue`)
per created month, stacked received / partial / nothing received**, with the overdue share in the
tooltip; it used to plot `totalInclTax` falling back to `totalValue`, which added GST-inclusive and
exclusive figures into one bar and matched no card. POs with no date or value are counted in a note
under it. **The doughnut has two rings (`statusRings`, drawn by `renderTwoRingDoughnut()`), and every
status card has a slice with its own number and key:** inner = what has arrived (`received`,
`partial`, `status:nothing`), outer = delivery date (`overdue` = `po._overdue`, `pending` = On Order,
`unknown` = `po._noDeliveryDate`, and an unclickable "dated, not overdue" remainder). The outer groups
are disjoint because overdue needs a passed date and Date Unknown has none. A single ring could only
show the "nothing received" part of the Overdue and Date Unknown overlays: production read 142 on the
Date Unknown card and 7 on its slice. A click toggles `state.statusFilter` then renders the list
region. `status:nothing` is also in the header Status select. `PO_LIST_CTX` carries `{el, filtered, totalPages}`. `poListRegionHtml()` applies the status
filter, month filter and `applyColFilters()`, sorts newest first, and renders a top-5 grid or a
paginated (10/page) "View all" table with the header filter row; row links carry
`data-po="<plant>::<poNumber>"` and open `openPoModal()`. Under the list,
`importCrossHitsHtml()` adds **"Also in Import Purchases (N)"** when the PO Number or Vendor header
filter matches an import order at the selected plants (same contains rule as `applyColFilters()`, up
to 10 shown): project owner, 2026-09-25 - searching Domestic for what turns out to be an import order
should still find it. The Domestic rows are untouched. `importCrossHits()` starts the import fetch on
the first such search (`IMPORT_CROSS_LOAD`, one in flight) and re-renders the region when it lands; a
failed fetch just means no hint. A hit's `data-import-po` link runs `openImportPoFromDomestic()`:
`switchPurchaseType('import', {importPoNumber})`, then `openImportPoModal()`. `wirePoListRegion()` wires pagination,
jump-to-page, the "N filters active - Clear" chip (full render) and the `[data-cf]` header filters
(`createdFrom`/`createdTo`/`status` full render, the rest debounced region render).

### frontend/js/po-reconcile.js

Renders the per-line reconciliation cards both PO modals show (Ordered / Received / Difference in
real figures, every matched MIR receipt with its MIR sheet row). The received side comes from the
API (`received`, `matchedMirs`); it is never summed in the browser, so units always agree with the
matcher. Two adapters produce one neutral line shape: `domesticReconLine(it, index, po, plantKey)`
and `importReconLine(...)` (BOE quantity, INR rate from `orderedRateInr`, PO net value x exchange rate
for value, and on a cleared line a `landed` pair from `landedRateInr` / `receivedFinalRate`, drawn as a
"Landed rate (duty + IGST)" row with the matcher's 0.01% allowance, `RECON_LANDED_RATE_REL_EPS`, so a
duty-only gap reads "matches" there while the pre-duty row above still shows it). The landed row is
left out for a shared receipt (`receiptShare`), which is compared at the BOE's blended rate; the card
shows `notes` instead - the line's share of the receipt (`receiptShareNote(share, tier, poolRefs)`:
a pooled line's share of its order's identical lines when `poolLineRefs` is set, a BOE share on tier
`boe_number`, otherwise a "Keep both" manual match's share; Domestic lines carry it too), and for an exchange-rate difference both rates. Tier `boe_number` gets the high-confidence badge, like `po_number`. `reconItemsHtml(lines, plantKey, currencyLabel)` = `reconSummaryHtml()` (fulfilled % caps
each line at its own ordered value) + one `reconLineHtml()` per line. `reconControlsHtml()` carries the
confidence badge (high = `po_number` tier, medium >= 0.75), "manual" tag, dismiss/reinstate link
(only when `reconAnyFlag(m, isFlagged)`, whose qty half is `isQtyMismatch()`) and the
`.mir-change-link` with `data-item-ref`/`data-current-mir`. A line with `qtyWithinTolerance`
(`reconToleranceFields()`) gets the green `recon-full` status "Qty matched · +7% within tolerance",
counts as fully received in the summary, shows its qty (and, unless the value still flagged, value)
difference in the matched colour as "over, within tolerance" (`reconDiffHtml(..., tolerated)`), and
carries `qtyToleranceNote()` in its notes. A line whose PO states rolls gets a Rolls row ("not
stated in MIR" when the receipts give none), and a differing count sets the status to "Partly
received · 5 of 6 rolls" or "Over-received · 7 of 6 rolls" whatever the weight.
`reconMoney()` is exact rupees; epsilons `RECON_QTY_EPS`, `RECON_RATE_EPS`, `RECON_VALUE_EPS` (Rs 1,
the matcher's value epsilon). Progress bars use `data-width-pct`, so callers run
`applyDynamicStyles()`. Must load after `flags.js` and before `po-modal.js`/`import-po.js`.

### frontend/js/po-modal.js

The Domestic PO modal and the shared MIR picker.

- `openPoModal("<plant>::<poNumber>")` - guarded by `modalRequestId`; looks the PO up in that plant's
  cache only (PO numbers are not unique across plants); best-effort loads every plant's materials for
  the material links. Tabs Overview / Item & Stock / Flags & Corrections (`poModalTab` persists across
  opens). Edits PATCH `<prefix>/purchase-orders/<po>/fields`; revert is offered for PO-level fields
  only (domestic line items have no stable `item_id`). After any save, pin or dismiss,
  `onDomesticFieldSaved()` nulls that plant's PO cache, refetches, reopens the modal and re-renders the
  list.
- **MIR picker** (feature: [api-and-features.md](api-and-features.md)): `mirPickerHtml()` renders one
  `#mirPicker`; `wireMirPicker(container, api, poNumber, onDone)` takes an injected
  `api = {candidates(q, itemRef), save(body)}` so Domestic (per-plant `<prefix>/purchase-orders/<po>/mir-candidates?q=`
  and PATCH `.../mir-match`) and Import (`/api/imports/purchase-orders/<plant>/<po>/...`, whose candidates call also sends `itemRef`) share one
  implementation. `openMirPicker()` moves the panel under the clicked `.recon-line`;
  `loadMirCandidates()` (search debounced 250 ms); `renderMirCandidates()` shows date, party,
  material, qty/rate, sheet rows and who currently holds the document (`claimedBy`, via
  `mirClaimSummary()`, which skips holders marked `sharesReceipt` - this line's Bill of Entry siblings,
  which keep their share either way). On a collision `chooseMirCandidate()` opens `#mirPickerChoice`
  (built from DOM nodes, since the claim text carries sheet descriptions) with **Keep both** (focused),
  **Move it here** and **Cancel**, each explained in a line below the buttons; `applyMirMatch()` sends
  `{itemRef, mirNo, clear, share, reason}` (`mirNo: ''` = "no MIR", `clear: true` = back to automatic,
  `share: true` = Keep both). `mirLabel()` prints a MIR number without doubling a "MIR" prefix Vapi's
  numbers already carry.
  When the response's `unfilledPins` names this line (every row of that MIR document is already
  held by a newer pin), it says so instead of "Matched" - in the status line and, because
  `onDone()` re-renders the modal and takes the status line with it, in a `window.alert()`.

### frontend/js/import-po.js

The Import Purchases list and modal. Constants `IMPORT_STAGES`, `IMPORT_STAGE_LABELS`,
`IMPORT_STAGE_PILL_CLASS`, `IMPORT_FLAG_LABELS` (F1-F7). `importCategoriesFor()` builds each PO's
categories client-side, leaving out dismissed PO-level flags; `poQtyDiscPo()` is the dismissal-aware
PO-vs-BOE check; `importRowFlags()` (partial and on-order can both be true here). Material Inwarded,
Partial Delivered, Overdue, On Order and Date Unknown read the server's receipt-based
`materialInwarded` / `partialDelivery` / `deliveryDateStatus` (see `import_flags.py`), the same rules
as Domestic. `renderImportPoList(el)` mirrors the domestic view with
13 KPI cards including the shipment-stage trio, the two charts below, the RoDTEP Ledger / Advance License buttons, and `#importListRegion`
(`IMPORT_LIST_CTX`, `importListRegionHtml()`, `renderImportListRegion()`, `wireImportListRegion()`;
every `[data-icf]` filter is table-only). **The bar chart is order value in INR before duty
(`importPoInrValue()`: each line's `netValue x exchangeRate`, null if any line lacks either, counted
in an "unplotted" note) per created month, stacked by `importReceiptStatus()` (inwarded / partial /
nothing), with the overdue share in the tooltip.** It used to plot the duty-paid
`totalInclusiveValue`, which only exists after customs clearance, so every open order counted as
zero. **The doughnut is "Delivery Status (MIR)", two rings like Domestic's:** inner = what has
arrived (`inwarded`, `partial`, `recv:nothing`), outer = the server's `deliveryDateStatus`, already
one value per PO (`overdue`, `onorder`, `unknowndate`, and an unclickable Delivered remainder), so each
slice equals its card. It replaced a shipment-stage doughnut (customs clearance is not delivery; on
production 40 of 43 import POs sat in "Cleared"). BL numbers render with a `data-track-bl` "Track" link.

`openImportPoModal("<plant>::<poNumber>")` is guarded, fetches the detail once via
`ensureImportPoDetailLoaded()` (GET `/api/imports/purchase-orders/<plant>/<po>`, cached in
`IMPORT_PO_DETAIL_CACHE`), and `renderImportPoModalBody()` renders Overview / Items & MIR / Shipment &
License / Flags & Corrections. Edits PATCH `/api/imports/purchase-orders/<plant>/<po>/fields`; revert
works for item-level fields too (import `item_id` is real). `onImportFieldSaved()` drops
`IMPORT_PO_CACHE`, refetches list and detail, and re-renders both.

### frontend/js/materials.js

Raw Material Analysis. `currentMaterials()`, `ensureMaterialsLoaded(keys)` (GET `<prefix>/materials`
into `MATERIALS_BY_PLANT`), `loadAndRenderMaterials()` (also loads domestic and import orders).

- **Linkage** (best effort, not ground truth): **each PO line links to its BEST material**
  (2026-09-25). `materialLinkScore()` is 2 for the same normalized name, else the token Jaccard
  (>= `MATERIAL_LINK_THRESHOLD`, 0.3), -1 for none; it applies the vendor gate (`vendorGatePasses()`)
  and a fabric-spec gate (`fabricSpec()` / `fabricSpecContradicts()`, ports of matching_core's: a
  fabric of another width or grade is another material). `buildLineLinks(materials, plantKeys)` finds
  each line's candidates through a token index, keeps only the top score (ties all link), and returns
  `{byNorm, linkedItems}`; `lineLinksFor(slot, materials, plantKeys)` memoizes it on the identity of the
  caches it reads, which `clearDataCaches()` replaces. It is built over the WHOLE plant scope
  (`materialScope()`), never the filtered list, so a filter cannot move a line to another material.
  Before, every line linked to every material it cleared 0.3 against: one Vapi fabric line linked to
  238 materials, the Quantity Mismatch / Data Quality Flags cards read 291 / 337 materials off 337
  open lines, and the pass was 1.3 million pair checks (~1.05 s of an ~1.15 s render). Measured in the
  browser on real data: All Plants first render 333 ms, cached redraw about 100 ms; Vapi lines on 10+
  materials 33 -> 1, Quantity Mismatches 229. `materialLinksToItem()` remains for `findMaterialLotsFor()`.
  **Anything added to this render path must not be per-pair.**
- `importPoAsMaterialOrder(po)` reshapes an import order into the domestic shape with INR prices
  (`importRateInr()`); `materialOrders(key)` returns domestic + import orders for a plant, memoized on
  the identity of `IMPORT_PO_CACHE` and the plant's domestic array so each order stays the same object
  between refreshes (callers stamp `_status`/`_categories` on it). `isOpenPoLine(po, item)` judges
  open-ness per line.
- `aggregateMaterialsByName(lots)` - one row per normalized name; consumption rates are summed
  **once per plant, not per lot** (the ledger is per material). **Days Left is each plant's own stock
  over its own rate, and the row shows the tightest plant** - pooling let one plant's idle stock hide
  another about to run out. When the lots name more than one unit (Rubber Process Oil 710: KG at HRS,
  LTR at Vapi), `qtyLabel` shows each unit's total instead of the sum and Days Left is left blank.
  Confidence = weakest band. `orderOnlyMaterials()` adds a row for open lines that link to no stock
  material (`orderOnly: true`, key `order::<normalized description>` via `materialModalKey()`).
- `openQtyOfLine(item)` / `openValueOfLine(item)` - what is still to come on an open line: ordered
  qty less the server's `received.qty` (already in the line's unit; `importPoAsMaterialOrder()`
  passes the import match's `received` through), or the whole qty when nothing comparable arrived
  (no match, a dismissed match, a unit clash). `summariseOpenQty(lines)` totals open qty per base
  unit through `MAT_UOM_FAMILIES`, a mirror of `parsers/common.py`'s `_UOM_FAMILIES` - keep the two
  in step. Unrecognised units are counted, never added.
- `computeMaterialPoLinkage(materials, plantKeys)` - per material: `links`, `openLinks`, `openValue`
  (still-to-come value), per-line-item `categories`, `qtyFlag`/`rateFlag`/`maxDiffPct`. "PO Not
  Found in MIR" is not raised for a line whose PO is `pending` (nothing arrived, not past due) -
  the same rule as `rowFlagsHtml()`'s On Order suppression; left in, every open order and every
  order-only row counted as a Data Quality Flag. `computeMaterialStatus()`
  (`overdue`/`partial`/`onorder`/`received`/`instock`), `isMaterialLowStock()` (< 15 days with a
  band other than `none`, or `daysToMsl === 0`), `daysLeftCellHtml()` (confidence dot; negative
  stock shows "Stock < 0" with a sheet-error tooltip, checked before the band; band `none` shows "-";
  a watched material that did not move shows "No movement").
- `renderMaterialsView()` - 8 KPI cards in Domestic Purchase Orders' own `.kpi-card` markup (flag
  icon, count-up value, uppercase label + info tooltip): `total`, `value`, `transit`, `qtyordered`,
  `qtydisc`, `ratedisc`, `lowstock`, `flags`. The breakdowns behind each figure (stock-sheet vs
  order-only counts, open line count, over/short split, other units) are in the tooltips.
  `total` and `value` clear; `transit` and `qtyordered` share `filterKey: 'openpo'` and light up
  together. **The two open-order totals are taken over DISTINCT open lines** (deduped on the line
  object), because the fuzzy link can attach one line to several materials ("SBR 1502" links to
  SBR 1712 too) and summing per material counted it once per material. `qtyordered` headlines
  weight, in MT from 10,000 KG (so it fits), with a `.mat-kpi-unit` beside the value. The row is
  `.kpi-grid.mat-kpi-grid` in `style.css`: an even grid, 8 across from 1500px, 4 below, 2 on a
  phone, with the value size scaling so nothing clips. Then
  Category/Sub Category/Flags selects, the drill-down
  chart (stock rows only), then `#matListRegion` (`MAT_LIST_CTX`, `materialsListRegionHtml()` sorted
  latest first by `materialLatestDate()`, `renderMaterialsListRegion()`, `wireMaterialsListRegion()`;
  `status`/`category`/`subCategory` header selects take the full render, `material` text and
  `progress` stay in the region).
- `renderMaterialsChart()` / `wireMaterialsChart()` - Inventory Value by Category -> Subcategory ->
  Material (top 5 by value via `materialsChartBars()`, no "Other" bar - a note under the chart says
  how many more there are and what they hold; breadcrumb, height via `data-height-px`); a material
  bar opens `openMaterialModal()`.

### frontend/js/material-modal.js

`openMaterialModal("<plant>::<lotId>" | "order::<normalized description>")` - guarded; for an
order-only key, `resolveOrderOnlyAnchor()` builds the anchor from the open lines. Always rolls up
across **all three plants**: sibling lots by exact normalized description, linked PO lines via
`linkedPoItemsForMaterial()` (the same best-material index the list reads, with the anchor lot's own
vendor gate on top). Its flags follow the list's rules - a dismissed match raises nothing and "PO Not
Found" is not raised on an order not yet due - and "Ordered, not yet delivered" and the open-orders
table count what is still to come (`openValueOfLine()` / `openQtyOfLine()`), not the full line. The
header counts stock lots and distinct plants separately, and "In stock" is summed per unit. Tabs Overview (Category/Sub Category pencils on the anchor lot itself,
Sub Category plain at Achhad, none for order-only), Stock by Plant (one row per lot with Vendor,
Received, Location, per-row Category and Rate pencils targeting that row's own plant, used-up lots
faded), Purchase Activity (open lines, import orders tagged with their INR conversion; each PO number is a
`[data-open-po]` link, keyboard-activatable, that replaces this modal with `openPoModal()` or
`openImportPoModal()` for that plant's order), Price Trend
(line chart plus 21/50/100-day trailing averages, into `modalCharts`), and Flags & Corrections (where
the correction box rests, plus per-row-plant revert links). Edits PATCH `materialFieldsUrl(plant,
lot)`; after a save or dismiss every plant's materials cache is dropped and the modal reopens. For a
real lot it then GETs `<prefix>/materials/<lot>/stock-trend` and appends a stock chart, re-checking
`modalRequestId` after that await.

### frontend/js/export-panel.js

`openExportPanel()` - the "Export Data" panel (button shown only when `PLANT_KEYS.some(canEditField)`;
each plant row gated by `canEditField`). Download is `window.open('<prefix>/stock-snapshots/export?from=&to=')`
in a new tab: the httpOnly cookie rides along and `Content-Disposition` does the rest, and a stale
session shows JSON in that tab instead of replacing the dashboard. Export rules:
[api-and-features.md](api-and-features.md).

### frontend/js/no-po-panel.js

`openNoPoPanel(plantKey, bucket)` - the drill-down behind `main.js`'s three badges, one per bucket:
"N received with no PO number", "N cite a PO not on file" and "N cite a PO on file, unmatched" (each
hidden at zero; they are never summed, since the buckets have different owners). One GET `<prefix>/mir-without-po`, three `.sub-tab` buckets (`no_po`,
`po_unknown`, `po_known_unmatched`); the server decides the bucket and this file never re-derives it.
`NO_PO_CTX` holds the loaded rows; `renderNoPoPanel()` re-renders from memory on tab switch (the
search text is reset per tab) or search (`rerenderNoPoPanelDebounced`, built once at load, which also
restores focus and caret). CSV via `window.open('<prefix>/mir-without-po?download=csv&bucket=...')` -
**never `?format=csv`**, which DRF reserves. Read-only.

### frontend/js/rodtep-panel.js

`openRodtepPanel()` - GET `/api/imports/rodtep`; `renderRodtepLedgerBody()` shows Scrips and "Needs
attention" tabs (`wireRodtepTabs()`); Total Used / Balance columns appear only when
`summary.hasLoggedUsage`. `openRodtepScriptDetail(scriptNo)` - guarded GET
`/api/imports/rodtep/<scriptNo>`. Headline numbers go in `.modal-meta`, **not `.kpi-card`**, because a
KPI card is a filter button everywhere else and nothing here filters. Also defines helpers shared with
the next file by load order: `formatInrOrDash`, `formatQtyOrDash`, `licenseGapsHtml(unknown,
unclassified, noun)`, `licenseImportsTableHtml(citations, totals)` (marks a line shared with another
licence instead of splitting its value). Read-only; syncing is "Refresh Data"'s job. Rules:
[api-and-features.md](api-and-features.md).

### frontend/js/advance-license-panel.js

`openAdvanceLicensePanel()` - GET `/api/imports/advance-license`, stored in `AL_CTX`;
`renderAdvanceLicenseLedgerBody()` (Licences and "Needs attention" tabs, BOE cross-check badge per
licence); `openAdvanceLicenseDetail(licenseNumber)` renders from `AL_CTX` with no fetch (so no
guard). `formatPctOrDash()` never shows a tiny real draw as "0.0%" and shows null as a dash;
`validityCellHtml()` marks expired / expiring soon. Depends on `rodtep-panel.js`'s helpers.

### frontend/js/main.js

Dashboard bootstrap, shared state and sync/refresh orchestration. Globals: `PURCHASE_TYPES`, `root`,
`state` (view, plant - default `all`, purchaseType, and every filter field for the three views),
`PURCHASE_ORDERS_BY_PLANT`, `MATERIALS_BY_PLANT`, `IMPORT_PO_CACHE` (one cross-plant array),
`IMPORT_PO_DETAIL_CACHE`, `DATA_STAMP`, `MANUAL_SYNC_RUNNING`, `FRESHNESS_TIMER`.

- Helpers: `resetFilters()`, `resetImportFilters()`, `isAllPlants()`, `selectedPlantKeys()`,
  `plantDisplayLabel()`, `plantKeyFor(item)` (a merged All-Plants row carries `_plantKey`; lookups
  always use `<plant>::<id>` because ids are per-plant), `currentPOs()`, `currentImportPOs()`,
  `ensurePOsLoaded(keys)` (GET `<prefix>/purchase-orders`), `ensureImportPOsLoaded()` (via
  `shared.js`'s `apiImports()`), `clearDataCaches()`.
- `init()` - `requireAuth()`, deep-link read, nav/user/theme, the `#root` shell (sync bar with
  `#syncBadges`, `#refreshStatus`, Export Data and Refresh Data buttons, the disclaimer, three tab
  rows, `#viewContent`), first `loadAndRender()`, deep-link open, `startFreshnessWatch()`,
  `resumeSyncIfRunning()`. Refresh Data for an admin runs `triggerRealSyncAndRefresh()`; for anyone
  else it clears caches and re-reads the DB.
- Tabs: `renderViewTabs()` (`.view-tab`; also re-reads sync status because some badges are
  view-specific), `renderPlantTabs()` (`.plant-tab`, All Plants first), `renderPurchaseTypeTabs()`
  (`.sub-tab`, Purchase Orders only), whose clicks go through **`switchPurchaseType(ptype, opts)`**:
  resets every list filter, loads whichever side is missing (Import on first visit; Domestic too
  when the page opened on an Import deep link), renders, and returns `false` on a failed load or if
  the reader switched again meanwhile. `opts.importPoNumber` pre-fills the Import PO Number filter. The three levels use three
  different components on purpose ([api-and-features.md](api-and-features.md)).
- `loadSyncStatus()` - GET `<prefix>/sync-status`. All Plants: one badge per plant plus syncing and
  snapshot-gap badges. Single plant: "PO Updated" / "MIR" / "RM" / "Matching" badges (display labels
  over `po_csv`/`mir`/`stock`/`match`), failures tooltipped with `errorDetail`, the two clickable
  no-PO badges (wired on each render because the container is rebuilt) and, only under Raw Material
  Analysis, "N not tracked in RM". A failed read shows "Sync status unavailable", never a blank.
- Sync: `triggerRealSyncAndRefresh(btn)` - per selected plant POST `<prefix>/sync-trigger` and
  `/api/imports/sync-trigger/<plant>`, then once per click POST `/api/imports/rodtep/sync-trigger` and
  `/api/imports/advance-license/sync-trigger` **in parallel** (both run synchronously server-side); a
  409 means "already running" and is not an error. `pollSyncUntilDone()` /
  `_pollSyncUntilDone()` poll every `SYNC_POLL_INTERVAL_MS` (4 s) up to `SYNC_POLL_TIMEOUT_MS`
  (5 min), also reading `/api/imports/sync-status` for the import and ledger in-progress flags; on
  timeout it reports "still running" and leaves the rest to the freshness watcher. Status text:
  `setRefreshStatus()`, `syncOutcome()`, `stepName()`, `clockTime()`, `latestSyncTime()`,
  `elapsedLabel()`, `DOMESTIC_SYNC_STEPS`, `SYNC_STEP_LABELS`, `DRIVE_SYNC_STEPS`.
- `loadAndRender()` -> `loadDashboard()` or `loadAndRenderMaterials()`, then refreshes `DATA_STAMP`;
  resolves `false` when the view showed its own error.
- Freshness: `currentDataStamp()`, `checkFreshness()`, `startFreshnessWatch()`.
- Deep links: `readDeepLinkParams()`, `applyDeepLinkToState()`, `clearDeepLinkParams()`,
  `showDeepLinkMiss()`, `openDeepLinkTarget()`.

Some of this file's header comments predate later work (for example that Import has "no
MIR-equivalent reconciliation" or that Vapi imports are unparsed); the code, not those comments, is
current.

### frontend/js/home-page.js

`home.html` bootstrap: `requireAuth()`, greeting from the user's first name, nav/user/theme, reveals
`#adminCard` for admins, `await loadKpis()` (in `shared.js`), then hides the loading overlay.
`loadKpis()` reads each plant's `purchase-orders/summary` (vendor and created date only, about 36 KB
for all three) - it used to download every plant's full PO list, about 1.9 MB, for four counts.

### frontend/js/search-po-page.js

`search-po.html`, deliberately self-contained (its own per-page cache, no `main.js`). `ensureLoaded()`
prefetches GET `<prefix>/purchase-orders` for all plants on page load into `POS_BY_PLANT` (a failed
plant is `null` and warned about, never treated as empty). Filters: PO number, vendor, material
(each needs `MIN_FILTER_LEN` = 2 characters to count) and a created date range; live search is
debounced `SEARCH_DEBOUNCE_MS` (250 ms), Enter/Search runs immediately, and `searchRequestId` drops a
stale result. `highlightMatch()` wraps the first hit in `<mark class="search-hit">` with every piece
still escaped. **Import orders are searched too** (project owner, 2026-09-25): `ensureLoaded()`
also fetches GET `/api/imports/purchase-orders` into `IMPORT_POS` (`null` on failure, named in the
same warning), the same `poMatchesFilters()` runs over them, and a hit carries an **Import** badge
beside its plant - a number can exist as both a Domestic and an Import order, and both are listed.
`itemIsMatched()` and `poValue()` bridge the two payload shapes (an import line's MIR match is the
`mirMatch` object; its value is `totalInclusiveValue`, in INR). `showDetail()` is a simpler panel than
the dashboard modal - for an import it shows country of origin and Bill of Lading instead of GSTIN,
PO quantity, and net price as a bare number in the PO currency - and links out with
`dashboardPoHref(plant, po, kind)` (adding `kind=import` for an import order) / per-line
`dashboardMaterialHref()`.

### frontend/js/review-page.js

`review.html`, self-contained. `apiReview(path, opts)` wraps `/api/review` (same contract as
`apiForPlant`). Queue: `loadNext()` (GET `/api/review/next`, up to 5 cards), `cardHtml()` (both sides'
identifying `refs`, UOM clash highlighted on both sides, "why these were paired" `signalsHtml()`,
diff pills), `submitVerdict()` (POST `/api/review`; `_pending` is set **synchronously before the POST**
and `firstUnreviewedIdx()` skips pending cards, so a held key cannot land five verdicts on one card),
`undoVerdict()` (DELETE `/api/review/<id>`), `renderBatchFooter()` (an explicit "Next 5" rather than
auto-advance, so Undo stays reachable for the last card), `renderGoal()` (progress bar width set from
JS). `initKeyboard()`: 1/2/3, U, N; inert in text fields and while the Accuracy view is open.
Accuracy view: `loadStats()` (GET `/api/review/stats`, refetched on every open), `renderStats()`,
`statRowsHtml()` (n = 0 renders "not sampled yet", never 0%), `formatScore()` (null -> en dash), CSV
link to `/api/review/stats/export`. `formatMoneyExact()` keeps full precision on purpose. Its own
`showToast(text)` targets `#reviewToast`.

### frontend/js/admin-page.js

`admin.html` bootstrap and Users panel. Shows `#deniedContent` for non-admins and stops. Sidebar tabs
via `switchAdminTab()`. Loads in parallel: `loadSyncCards()` (GET `<prefix>/sync-status` per plant,
labels "PO Updated"/"MIR"/"RM"/"Matching"), `loadUsers()` (GET `/api/auth/users`), `loadOverviewData()`
(GET `/api/auth/admin-overview`; Top Correctors / Top Vendors via `renderBarList()` with widths set
from JS, `renderRecentActivity()`), and `loadKpis()`. User form: `openForm()` (calls `openModalA11y()`
then focuses Full Name), `closeForm()`, `submitForm()` (password >= 10, optional on edit; `plants`
forced to `[]` for admins and the plants row hidden), `createUserApi()` (POST
`/api/auth/users/create`), `patchUser()` (PATCH `/api/auth/users/<id>`), `toggleActive()`,
`deleteUser()` (DELETE `/api/auth/users/<id>`, button only for `DELETE_USER_ALLOWED_EMAIL`),
`loadDevices()` / `renderDevices()` / `revokeDevice()` (GET and DELETE
`/api/auth/users/<id>/devices[/<deviceId>]`; `renderDevices()` drops a response for a user no longer
open). `showToast(message, kind)` targets `#toastStack`. Redefines `userInitials()` (see
[Other traps](#other-traps)).

### frontend/js/login.js

Drives `login.html` without `auth.js` or `shared.js`, using plain `fetch` via `postJson()`. Password
step POST `/api/auth/login` (`status: 'ok'` -> `/home.html`, `device_verify` -> OTP step); OTP step
POST `/api/auth/device-verify`; "Resend code" re-posts the login. `handleOAuthRedirect()` reads
`?oauth_error=` (messages in `OAUTH_ERROR_MESSAGES`), `?step=device_verify`, or `?oauth_ready=1`
(one GET `/api/auth/google/session-token` to consume the server-side stash; the Google button is a
plain link to `/api/auth/google/login/`). "Remember me" hands the credential to the browser's own
vault through the Credential Management API (`storeCredentialIfRemembered()`,
`trySilentSignIn()`) and never stores it itself. Has its own `.pw-toggle` delegation.

### frontend/js/login-theme-toggle.js

Standalone copy of the theme toggle for `login.html`, which has no `.nav-user` for `initThemeToggle()`
to anchor on; wires the static `#themeToggleBtn` with the same `pt-theme` key.

### frontend/css/*.css (stylesheets)

- **`brand.css`** (every page) - brand tokens, top nav, user menu, theme toggle, buttons, forms,
  stat/action/user cards, search results (`.search-result-badges`, and `.search-result-import` for the Import badge), admin user form (`.uf-*`, also used by the Change Password
  modal), toasts, the global `[hidden]` rule, the closed set of CSP utility classes (add one only for
  a real call site), `.sr-only` / `.sr-only-focusable` / `.skip-link`, and its dark-mode blocks.
- **`style.css`** (`index.html` only) - `--dash-*` and dashboard tokens, sync bar and refresh status,
  the three tab components, KPI cards (flexbox with a fixed basis, not grid - both grid variants were
  tried and looked wrong with 8 vs 13 cards), info and row-flag CSS tooltips (instant, unlike native
  `title`), tables (sticky headers inside `.table-wrap`, which is a two-axis scroll container, so a CSS
  tooltip inside it is clipped), status pills and badges, legend, disclaimer, modal shell, correction
  box, MIR picker and its "already matched" choice (`.mir-choice*`), reconciliation cards, steppers, the Domestic list's "Also in Import Purchases"
  box (`.cross-kind-*`), chart panels (`.chart-head`, `.chart-legend` / `.legend-chip`, `.chart-foot`
  and its month `.chart-filter-chip`; `.doughnut-layout` puts the rings beside their legend through a
  container query on the panel at 540px, and a bar chart's `.chart-box` grows to its panel's height),
  filter bars (one 32px control height, uppercase labels, stacking per group under 560px), dark mode.
- **Page files** (`home-page.css`, `search-po-page.css`, `review-page.css`, `admin-page.css`,
  `login-page.css`) - extracted from inline `<style>` blocks for CSP; they use `brand.css` tokens
  (several review/admin rules carry literal fallbacks, e.g. `var(--green, #16a34a)`, because the
  dashboard tokens are not loaded there).

### .eslintrc.json

A deliberately narrow rule set (real-defect rules only, no style rules) run in CI as
`npx --yes eslint@8 frontend/js --ext .js --max-warnings 0` - a red/green gate. There is no Node in
the development environment, so CI is the only place it runs. `sourceType: "script"`,
`globals: { Chart: "readonly" }`. **`no-undef` is off on purpose**: all frontend files share one
global scope, so it would either flag every legitimate cross-file call or need a hand-maintained
globals list that becomes a second source of truth. The notes live in a `/* */` comment at the top
(ESLint's JSON config allows comments); **do not move them back into a `"//"` key**, which ESLint 8's
schema rejects with exit code 2. Its comment still says "14 files"; there are 22. CI details:
[testing-deployment.md](testing-deployment.md).

### Minor fixes from the 2026-09-25 audit

Small rules that hold across files, recorded once here:

- **Stale responses.** The MIR picker's search (`mirCandidatesRequestId`), the no-PO panel
  (`modalRequestId`) and the BL tracking view (`modalRequestId`) drop a response that a newer request
  superseded, like every other modal opener.
- **A save is not a reload.** `wireOverrideBox()` reports a failure of the reload after a successful
  save as "Saved - refresh to see it", never "Could not save".
- **Dates from timestamps** go through `shared.js`'s `localDateOf()`: slicing the date off a UTC ISO
  string read a day early for anything done between 00:00 and 05:30 IST.
- **Category options** show the fallback bucket 'Uncategorized' as "No category on file"
  (`categoryLabel()`), so it no longer reads the same as the reference file's own
  "Others / Uncategorized". Values are unchanged. The Sub Category filter requires the sub-category
  to sit under the chosen category on the same entry.
- **PO list Value column and chart.** `poValueCellHtml()` shows the value before tax, marked, when a PO
  has no tax-inclusive total - the figure the month chart already plotted; POs the chart cannot place
  are counted under it.
- **Raw Material** labels match the cards ("Value in Transit", "Quantity to Come"), the header Status
  select carries the "On an open order" and flag-chip filters, an order-only row's Days Left says it is
  on order only, and the no-PO panel shows the value of the rows shown beside their count.
- **Imports.** "Vendor Name Mismatch in MIR" is in `importCategoriesFor()` as well as the modal; the BL
  tracking view has a close button and, opened from a PO, a "Back to the purchase order" link; unknown
  RoDTEP scrips open their detail.
- **Admin.** Deactivating a user asks first, like Delete and Revoke device.
- **Background re-match.** A pin (`applyMirMatch()`) or a correction to a matching field
  (`wireOverrideBox()`) that comes back with `rematch` pending says "Saved - re-matching in the
  background" and awaits `shared.js`'s `waitForRematch(plantKey)` (polls sync-status every 2 s, up to
  3 minutes) before reloading; the pin picker then checks the finished run's `unfilledPins`. The MIR
  picker's `api` object carries `plantKey` for this. Refresh Data triggers one job per plant - the
  imports pipeline is part of it (see [data-sync.md](data-sync.md)).
