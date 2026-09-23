# Dashboard design system

## Surface and mode

The local dashboard is an Operate surface. Its primary artifact is a precise
inventory table with a persistent filter rail. Snapshot comparisons and
collection settings are adjacent views, not a separate application.

## Visual system

- System sans-serif: Segoe UI, system-ui, Arial; fixed 14px base size.
- Ink `#162a3b`, muted copy `#526477`, cool page `#f2f5f8`, white data surfaces.
- Navigation `#152b3d`; teal `#087c80` identifies actions and active selections.
- Borders `#dce3ea`, small 4-5px corners, no decorative gradients or glass.
- Status always has text: green complete, amber partial, red failed,
  blue running. Color is never the only indication.
- Tabular numbers and explicit units make quota values comparable without
  implying different unit types can be summed.
- Appearance defaults to the system color scheme. Dark surfaces use ink-blue
  `#111b24` / `#192733`, readable light text and a teal `#43bfb7` action accent.
  Semantic tokens also cover notices, native controls and lifecycle badges;
  changing appearance does not change the layout or meaning of colors.

## Structure

The 66px application bar holds identity and collection. A 248px filter rail
holds tenant, subscription, model and deployment filters plus browser-local saved views.
The main area contains a snapshot selector, three view tabs, a compact summary,
and the inventory table. A compact, clickable bar chart and grouped table views
expose family -> model -> model/version -> deployment options. Breadcrumbs
retain non-model scope filters. Model details open alongside or below the table.

Quota pools are the default working view. One row represents one named
subscription/region/pool allocation, not one budget per model version. Inline
allocated/available bars replace the separate catalog-count chart in this view.
Amounts keep their units, positive quota is distinct from deployability, and
pool sharing and lifecycle review are visible without another dashboard page.

Narrow screens collapse the rail behind a Filters control. Tables scroll
horizontally inside their own labeled region without overflowing the page.
Settings become one column; typography stays readable rather than shrinking.

## Interaction and accessibility

Use semantic table headers, native controls, visible keyboard focus, status
announcements and explicit empty/error states. Search is debounced; pagination
and filtering are server-side. Dynamic data is inserted as text, never HTML.
Only toast feedback animates; reduced-motion users receive instant changes.
Categorical filters use searchable checkbox disclosures instead of modifier-key
list boxes. Multiple values are ORed within a field and ANDed across fields.
Version selection uses complete model/version pairs rather than independent
sets of model names and version strings. It is a dependent step: choose model
names first, then their versions. Changing parent models resets version choices
without clearing unrelated scope filters.

Collection settings use an explicit private CLI profile to discover each tenant.
Saving a tenant's subscription choices preserves other configured tenants.
The status surface distinguishes the configured scope from an older complete
snapshot that does not yet contain it.

Do not replace snapshot health with a generic success indicator. Do not label
unallocated quota as guaranteed deployable capacity.
