import { MultiSelect, closeMultiSelects, measureChoices } from "./controls.js";

const $ = (id) => document.getElementById(id);
const multiKeys = ["tenant", "subscription", "region", "family", "model", "model_version", "deployment_type",
  "capacity_type", "availability", "lifecycle", "unit"];
const filterKeys = [...multiKeys, "version", "minimum", "quota_name", "sku"];
const filterLabels = {
  tenant: "Tenant", subscription: "Subscription", region: "Region", family: "Family", model: "Model",
  model_version: "Model + version", version: "Legacy version", deployment_type: "Geography", capacity_type: "Capacity",
  availability: "Headroom", lifecycle: "Lifecycle", unit: "Unit", minimum: "Minimum",
  quota_name: "Quota pool", sku: "SKU",
};
const defaultOptions = {
  tenant: "All tenants", subscription: "All subscriptions", region: "All regions", family: "All families",
  model: "All models", model_version: "Choose a model first", deployment_type: "All geographies",
  capacity_type: "All capacity types", availability: "Any quota state",
  lifecycle: "All lifecycle states", unit: "All units",
};
const availabilityOptions = [
  { value: "available", label: "Has remaining quota" },
  { value: "exhausted", label: "No remaining quota" },
  { value: "unknown", label: "Quota unknown" },
];
const controls = new Map();
const viewNames = { quota: "quota entries", deployments: "catalog options", family: "families", model: "models", model_version: "model/version choices" };
const state = {
  filters: {}, snapshot: "latest", page: 1, pageSize: 50, sort: "remaining", direction: "desc",
  q: "", tab: "inventory", csrf: "", status: null, scans: [], rows: [],
  subscriptions: [], config: null, discoveredProfiles: new Map(),
  request: 0, compareRequest: 0, comparePage: 1, initialized: false,
  facets: {}, view: "quota", trail: [], chartVisible: true,
};
const numberFormat = new Intl.NumberFormat(undefined, { maximumFractionDigits: 3 });
const dateFormat = new Intl.DateTimeFormat(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
const savedKey = "foundry-inventory.saved-views.v1";
const railKey = "foundry-inventory.rail-width.v1";
let toastTimeout;
let searchTimeout;
let pollPending = false;
let coverageLoaded = false;
let filterTimeout;
let railFrame = 0;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function fmt(value) {
  return value === null || value === undefined || value === "" ? "—" : numberFormat.format(Number(value));
}

function when(value) {
  if (!value) return "Not yet";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : dateFormat.format(date);
}

function selected(key) {
  const value = state.filters[key];
  return Array.isArray(value) ? value : value ? String(value).split(",").filter(Boolean) : [];
}

function resetSort() {
  state.sort = state.view === "quota" ? "remaining" : state.view === "deployments" ? "model" : "label";
  state.direction = state.view === "quota" ? "desc" : "asc";
}

function modelLabel(row) {
  return `${row.model} ${row.version || "(version not reported)"}`;
}

function pairLabel(value) {
  try {
    const [, model, version] = JSON.parse(value);
    return modelLabel({ model, version });
  } catch {
    return value;
  }
}

function normalizeFilters(filters) {
  const result = {};
  for (const key of filterKeys) {
    const value = filters[key];
    if (key === "minimum") {
      result[key] = value === undefined || value === null ? "" : String(value);
    } else if (Array.isArray(value)) {
      result[key] = [...new Set(value.filter((item) => typeof item === "string" && item))];
    } else {
      result[key] = typeof value === "string" && value ? value.split(",").filter(Boolean) : [];
    }
  }
  result.family = [...new Set((result.family || []).map((family) => family.toLowerCase() === "mistral ai" ? "Mistral" : family))];
  const pairs = result.model_version.map((value) => {
    const pair = JSON.parse(value);
    if (!Array.isArray(pair) || pair.length !== 3 || pair.some((part) => typeof part !== "string") || !pair[1].trim()) {
      throw new Error("This view contains an invalid model/version selection. Reset its filters.");
    }
    return pair;
  });
  if (!result.model.length && pairs.length) {
    result.model = [...new Set(pairs.map((pair) => pair[1]))];
  }
  return result;
}

function toast(message, error = false) {
  clearTimeout(toastTimeout);
  $("toast").textContent = message;
  $("toast").className = `toast${error ? " error" : ""}`;
  $("toast").hidden = false;
  toastTimeout = setTimeout(() => { $("toast").hidden = true; }, error ? 10000 : 4500);
}

function notice(message, type = "warning") {
  $("notice").textContent = message;
  $("notice").className = `notice ${type}`;
  $("notice").hidden = !message;
}

function handled(fn) {
  return (...args) => Promise.resolve().then(() => fn(...args)).catch((error) => {
    console.error(error);
    toast(error.message, true);
  });
}

async function api(path, body) {
  const options = { credentials: "same-origin", cache: "no-store" };
  if (body !== undefined) {
    options.method = "POST";
    options.headers = { "Content-Type": "application/json", "X-Local-Token": state.csrf };
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch {
    throw new Error(state.status?.read_only ?
      "The hosted dashboard is unreachable or your sign-in expired. Reload the page to sign in again." :
      "The local server is unreachable. Start the dashboard and try again.");
  }
  const contentType = response.headers.get("content-type") || "";
  const data = contentType.includes("application/json") ? await response.json() : null;
  if (!response.ok) throw new Error(data?.error || `Request failed (HTTP ${response.status}).`);
  if (data === null) throw new Error("The local server returned an unexpected response.");
  return data;
}

function query(includePaging = true) {
  const params = new URLSearchParams({ snapshot: state.snapshot });
  if (state.q) params.set("q", state.q);
  for (const key of filterKeys) {
    if (key === "minimum") {
      if (state.filters.minimum !== "" && state.filters.minimum !== undefined) params.set(key, state.filters.minimum);
    } else if (key === "model_version") {
      if (selected(key).length) params.set(key, JSON.stringify(selected(key).map((value) => JSON.parse(value))));
    } else {
      for (const value of selected(key)) params.append(key, value);
    }
  }
  if (includePaging) {
    params.set("page", String(state.page));
    params.set("page_size", String(state.pageSize));
    params.set("sort", state.sort);
    params.set("direction", state.direction);
  }
  return params;
}

function rememberLocation() {
  const params = query(false);
  params.set("tab", state.tab);
  params.set("view", state.view);
  if (!state.chartVisible) params.set("chart", "0");
  history.replaceState(null, "", `/?${params}`);
}

function readLocation() {
  const params = new URLSearchParams(location.search);
  state.snapshot = params.get("snapshot") || "latest";
  if (!/^(latest|\d+)$/.test(state.snapshot)) state.snapshot = "latest";
  state.q = (params.get("q") || "").slice(0, 200);
  for (const key of filterKeys) {
    if (key === "minimum") state.filters[key] = params.get(key) || "";
    else if (key === "model_version") {
      const pairs = params.get(key);
      state.filters[key] = [];
      if (pairs) {
        try {
          const parsed = JSON.parse(pairs);
          if (!Array.isArray(parsed) || parsed.length > 256 ||
              parsed.some((item) => !Array.isArray(item) || item.length !== 3 ||
                item.some((part) => typeof part !== "string") || !item[1].trim())) {
            throw new Error("Invalid model/version choices");
          }
          state.filters[key] = parsed.map((item) => JSON.stringify(item));
        } catch {
          toast("The link contained an invalid model/version selection; that selection was not applied.", true);
        }
      }
    } else {
      const values = params.getAll(key);
      state.filters[key] = values.length === 1 ? values[0].split(",").filter(Boolean) : values;
    }
  }
  if (Object.hasOwn(viewNames, params.get("view"))) state.view = params.get("view");
  state.chartVisible = params.get("chart") !== "0";
  const tab = params.get("tab");
  if (["inventory", "changes", "collection"].includes(tab)) state.tab = tab;
  state.filters = normalizeFilters(state.filters);
  $("inventory-search").value = state.q;
  $("table-view").value = state.view;
  resetSort();
  syncFilterControls();
}

function syncFilterControls() {
  for (const [key, control] of controls) control.setSelected(selected(key));
  $("filter-minimum").value = state.filters.minimum || "";
  $("filter-minimum").disabled = selected("unit").length !== 1;
}

function populateSelect(select, values, placeholder, selected) {
  select.replaceChildren();
  if (placeholder !== null) select.add(new Option(placeholder, ""));
  for (const item of values) {
    const value = typeof item === "object" ? item.value : item;
    const label = typeof item === "object" ? item.label : item;
    select.add(new Option(String(label), String(value)));
  }
  if (selected && ![...select.options].some((option) => option.value === String(selected))) {
    select.add(new Option(`${selected} (not in this snapshot)`, String(selected)));
  }
  select.value = selected || "";
}

function renderFacets(facets) {
  state.facets = facets;
  state.facets.model_version = (facets.model_version || []).map((option) => ({
    ...option, value: JSON.stringify(JSON.parse(option.value)), description: option.format,
  }));
  if (selected("version").length && !selected("model_version").length) {
    const versions = new Set(selected("version"));
    const models = new Set(selected("model"));
    const families = new Set(selected("family"));
    const pairs = state.facets.model_version.filter((option) => versions.has(option.version) &&
      (!models.size || models.has(option.model)) && (!families.size || families.has(option.family)));
    if (pairs.length) {
      state.filters.model_version = pairs.map((option) => option.value);
      state.filters.version = [];
      if (!models.size) state.filters.model = [...new Set(pairs.map((option) => option.model))];
      reconstructTrail();
    }
  }
  updateChoiceOptions();
  syncFilterControls();
  renderFilterChips();
  fitFilterRail();
}

function versionChoice(option, singleModel) {
  return {
    ...option,
    label: singleModel ? option.version || "(version not reported)" : option.label,
    description: singleModel ? `${option.model} · ${option.format}` : option.format,
    searchText: option.label,
  };
}

function updateChoiceOptions() {
  const pairs = state.facets.model_version || [];
  const families = new Set(selected("family"));
  const models = new Set(selected("model"));
  const eligiblePairs = models.size ? pairs.filter((option) =>
    (!families.size || families.has(option.family)) && models.has(option.model))
    .map((option) => versionChoice(option, models.size === 1)) : [];
  const eligibleModels = families.size ?
    [...new Set(pairs.filter((option) => families.has(option.family)).map((option) => option.model))].sort() :
    state.facets.model || [];
  for (const [key, control] of controls) {
    control.setOptions(key === "model_version" ? eligiblePairs :
      key === "model" ? eligibleModels : key === "availability" ? availabilityOptions : state.facets[key] || []);
  }
  controls.get("model_version").setContext({
    disabled: !models.size,
    placeholder: models.size ? "All versions" : "Choose a model first",
    hint: models.size === 1 ? `Only versions of ${[...models][0]} are shown.` :
      models.size > 1 ? `Versions are scoped to your ${models.size} selected models.` : "",
  });
}

// The longest labels any filter can show for this snapshot, independent of the current selection, so
// the rail keeps one width while the user filters. Character count tracks rendered width closely
// enough that only these candidates need a layout pass.
function railChoices(facets, limit = 48) {
  const choices = [...Object.values(defaultOptions), "All versions"].map((label) => ({ label }));
  for (const key of multiKeys) {
    const options = key === "availability" ? availabilityOptions : key === "model_version" ? [] : facets[key] || [];
    for (const option of options) choices.push({ label: typeof option === "string" ? option : option.label });
  }
  for (const option of facets.model_version || []) choices.push(versionChoice(option, false), versionChoice(option, true));
  const size = ({ label, description }) => Math.max(String(label ?? "").length, String(description ?? "").length);
  return choices.sort((a, b) => size(b) - size(a)).slice(0, limit);
}

function setRailContent(width) {
  document.querySelector(".workspace").style.setProperty("--rail-content", `${width}px`);
  syncRailGutter();
}

function syncRailGutter() {
  const rail = document.querySelector(".filter-rail");
  const style = getComputedStyle(rail);
  if (!rail.offsetWidth || style.overflowY === "visible") return;
  const gutter = rail.offsetWidth - rail.clientWidth -
    parseFloat(style.borderLeftWidth) - parseFloat(style.borderRightWidth);
  rail.parentElement.style.setProperty("--rail-gutter", `${Math.max(0, gutter)}px`);
}

function fitFilterRail() {
  const width = measureChoices(railChoices(state.facets)) + 1;
  setRailContent(width);
  try {
    localStorage.setItem(railKey, String(width));
  } catch {
    // Without storage the rail still fits; the next page load starts from the default width.
  }
}

function restoreRailWidth() {
  try {
    const width = Number(localStorage.getItem(railKey));
    if (width > 0 && width <= 1000) setRailContent(width);
  } catch {
    // Browser storage only avoids a width change on load; the rail is fitted after data arrives.
  }
}

function changeFilter(key, values) {
  const previousModels = selected("model");
  const previousVersionCount = selected("model_version").length + selected("version").length;
  state.filters[key] = values;
  if (["family", "model", "model_version"].includes(key)) {
    state.trail = [];
    if (key === "model_version") state.filters.version = [];
  }
  let removed = 0;
  const pairs = state.facets.model_version || [];
  if (key === "family" && values.length) {
    const eligibleModels = new Set(pairs.filter((pair) => values.includes(pair.family)).map((pair) => pair.model));
    const retained = selected("model").filter((model) => eligibleModels.has(model));
    removed += selected("model").length - retained.length;
    state.filters.model = retained;
  }
  const modelChanged = ["family", "model"].includes(key) &&
    (previousModels.length !== selected("model").length || previousModels.some((model) => !selected("model").includes(model)));
  if (modelChanged) {
    state.filters.model_version = [];
    state.filters.version = [];
    controls.get("model_version").clearSearch();
  } else if (key === "family") {
    const families = selected("family");
    const models = selected("model");
    const eligible = new Set(pairs.filter((pair) => (!families.length || families.includes(pair.family)) &&
      (!models.length || models.includes(pair.model))).map((pair) => pair.value));
    const retained = selected("model_version").filter((pair) => eligible.has(pair));
    removed += selected("model_version").length - retained.length;
    state.filters.model_version = retained;
  }
  if (key === "unit" && values.length !== 1 && state.filters.minimum) {
    state.filters.minimum = "";
    toast("The minimum was cleared: select exactly one quota unit to use a threshold.");
  } else if (modelChanged && previousVersionCount) {
    toast("Version selection reset because the selected models changed.");
  } else if (removed) {
    toast(`${removed} model selection(s) outside the selected family or model were cleared.`);
  }
  state.page = 1;
  updateChoiceOptions();
  syncFilterControls();
  renderFilterChips();
  clearTimeout(filterTimeout);
  filterTimeout = setTimeout(handled(async () => {
    await loadInventory();
    if (state.tab === "changes") await compareSnapshots();
  }), 180);
}

function renderFilterChips() {
  const fragment = document.createDocumentFragment();
  for (const key of filterKeys) {
    if (key === "model" && selected("model_version").length) continue;
    const values = key === "minimum" ? (state.filters.minimum ? [state.filters.minimum] : []) : selected(key);
    const options = new Map((controls.get(key)?.allOptions() || []).map((option) => [option.value, option.label]));
    for (const value of values) {
      const label = key === "model_version" ? pairLabel(value) : options.get(value) || value;
      const chip = el("button", "filter-chip", `${filterLabels[key]}: ${label} ×`);
      chip.type = "button";
      chip.setAttribute("aria-label", `Remove ${label} from ${filterLabels[key]}`);
      chip.addEventListener("click", () => {
        if (key === "minimum") {
          state.filters.minimum = "";
          changeFilter("unit", selected("unit"));
        } else changeFilter(key, selected(key).filter((item) => item !== value));
      });
      fragment.append(chip);
    }
  }
  $("active-filters").replaceChildren(fragment);
}

function summaryItem(count, label, className = "") {
  const item = el("span", className);
  item.append(el("strong", "", fmt(count)), document.createTextNode(label));
  return item;
}

function emptyRow(columns, title, message, action) {
  const row = el("tr");
  const cell = el("td", "empty-cell");
  cell.colSpan = columns;
  cell.append(el("h3", "", title), el("p", "", message));
  if (action) cell.append(action);
  row.append(cell);
  return row;
}

function lifecycleLabel(value) {
  const labels = { GenerallyAvailable: "GA", Stable: "Stable", Deprecated: "Deprecated",
    Deprecating: "Retiring", Preview: "Preview", Legacy: "Legacy" };
  return labels[value] || value || "Not reported";
}

function renderTableHeader() {
  const columns = state.view === "quota" ? [
    ["Region / subscription", "region"], ["Deployment"], ["Quota allocation", "remaining"], ["Matching models"],
  ] : state.view === "deployments" ? [
    ["Model + version", "model"], ["Subscription", "subscription"], ["Region", "region"],
    ["Deployment"], ["Capacity"], ["Remaining", "remaining", true], ["Lifecycle"],
  ] : state.view === "family" ? [
    ["Model family", "label"], ["Models", "models", true], ["Model/version choices", "versions", true],
    ["Regions", "regions", true], ["Subscriptions", "subscriptions", true],
    ["Options", "entries", true], ["Pools with headroom", "with_headroom", true],
  ] : state.view === "model" ? [
    ["Model", "label"], ["Model/version choices", "versions", true], ["Regions", "regions", true],
    ["Subscriptions", "subscriptions", true], ["Options", "entries", true], ["Pools with headroom", "with_headroom", true],
  ] : [
    ["Model + version", "label"], ["Regions", "regions", true], ["Subscriptions", "subscriptions", true],
    ["Options", "entries", true], ["Quota pools", null, true], ["Pools with headroom", "with_headroom", true],
  ];
  const row = el("tr");
  for (const [label, sort, numeric] of columns) {
    const header = el("th", numeric ? "number-cell" : "");
    header.scope = "col";
    if (sort) {
      header.dataset.sort = sort;
      const button = el("button", "", label);
      button.type = "button";
      const active = state.sort === sort;
      if (active) header.setAttribute("aria-sort", state.direction === "asc" ? "ascending" : "descending");
      button.append(el("span", "", active ? state.direction === "asc" ? "↑" : "↓" : ""));
      header.append(button);
    } else header.textContent = label;
    row.append(header);
  }
  $("inventory-head").replaceChildren(row);
  document.querySelector(".inventory-table").classList.toggle("grouped-table", !["deployments", "quota"].includes(state.view));
  document.querySelector(".inventory-table").classList.toggle("quota-table", state.view === "quota");
}

function renderInventorySummary(summary) {
  $("inventory-summary").classList.toggle("quota-summary-mode", state.view === "quota");
  const choices = summaryItem(summary.versions, "model/version choices");
  choices.title = `${fmt(summary.models)} model names in this selection`;
  const quotaSummary = [
    summaryItem(summary.with_headroom, "pools with quota", "quota-summary"),
    summaryItem(summary.regions, "regions"), summaryItem(summary.subscriptions, "subscriptions"),
  ];
  if (summary.unknown_quota) quotaSummary.push(summaryItem(summary.unknown_quota, "unknown"));
  $("inventory-summary").replaceChildren(...(state.view === "quota" ? quotaSummary : [
    choices, summaryItem(summary.regions, "regions"), summaryItem(summary.subscriptions, "subscriptions"),
    summaryItem(summary.with_headroom, "quota pools with headroom", "quota-summary"),
  ]));
}

function finishTable(data) {
  const start = data.total ? (data.page - 1) * data.page_size + 1 : 0;
  const end = Math.min(data.page * data.page_size, data.total);
  $("result-count").textContent = `${fmt(start)}–${fmt(end)} of ${fmt(data.total)} ${viewNames[state.view]}`;
  $("page-number").textContent = `${data.page} / ${Math.max(1, Math.ceil(data.total / data.page_size))}`;
  $("previous-page").disabled = data.page <= 1;
  $("next-page").disabled = end >= data.total;
  $("export-button").disabled = !data.total;
  $("export-button").title = state.view === "quota" ? "Export each matching quota pool once." :
    "Export all matching deployment options, including their quota data.";
  $("quota-guide").hidden = state.view !== "quota";
  $("quota-guide-text").textContent = data.snapshot ?
    `Named pools shown once · snapshot ${when(data.snapshot.started_at)}` : "No quota snapshot yet";
  $("inventory-footnote").textContent = state.view === "quota" ?
    "Quota is not a deployment guarantee. Models share the amount shown; Azure capacity, lifecycle and access still need checking." :
    "Catalog listings are not a deployment guarantee. Groups may share quota pools; do not add their quota counts.";
  $("last-update").textContent = data.snapshot ? `Snapshot: ${when(data.snapshot.started_at)}` : "No snapshot collected yet";
  closeDetail();
  renderFilterChips();
  renderBreadcrumbs();
}

function emptyEstate() {
  return state.status?.read_only ? {
    title: "Waiting for the first hosted collection",
    text: "The hosted service collects every morning with read-only access. Collection & history shows its latest attempt and next run.",
    action: "View collection status",
  } : null;
}

function renderInventory(data) {
  state.rows = data.rows;
  renderTableHeader();
  renderInventorySummary(data.summary);
  const body = $("inventory-body");
  body.replaceChildren();
  if (!data.rows.length) {
    if (!data.snapshot) {
      const hosted = emptyEstate();
      const button = el("button", "button primary", hosted?.action || "Set up collection");
      button.type = "button";
      button.addEventListener("click", () => selectTab("collection"));
      body.append(emptyRow(7, hosted?.title || "Your estate, ready to explore", hosted?.text || "Choose the subscriptions to collect, then take your first snapshot. Existing CSVs can also be imported from the command line.", button));
    } else {
      body.append(emptyRow(7, "No entries match these filters", "Try a different region or capacity type, or reset your filters. Unknown quota is kept separate from zero."));
    }
  }
  const fragment = document.createDocumentFragment();
  for (const row of data.rows) {
    const tr = el("tr");
    const modelCell = el("td");
    const modelButton = el("button", "model-button", modelLabel(row));
    modelButton.type = "button";
    modelButton.addEventListener("click", () => openDetail(row, tr));
    modelCell.append(modelButton, el("span", "cell-secondary", row.family || row.format));
    const subscription = el("td", "subscription-cell", row.subscription || row.subscription_id);
    const region = el("td", "region-cell", row.region);
    const deployment = el("td");
    deployment.append(el("span", "scope-label", row.deployment_type),
      el("span", "cell-secondary", row.sku || "No deployment SKU"));
    const capacity = el("td");
    capacity.append(el("span", `capacity-label ${String(row.capacity_type).toLowerCase()}`, row.capacity_type || "Other"));
    const quota = el("td", "number-cell");
    const known = row.remaining !== null && row.remaining !== undefined && row.quota_status === "Reported";
    const quotaClass = !known ? "unknown" : row.remaining === 0 ? "zero" : "";
    quota.append(el("span", `quota-value ${quotaClass}`, known ? fmt(row.remaining) : "Unknown"),
      el("span", "cell-secondary", known ? `of ${fmt(row.quota_limit)} ${row.unit || ""}` : (row.quota_status === "ERROR" ? "Collection error" : "Not reported")));
    const lifecycle = el("td");
    const lifeClass = ["Deprecated", "Deprecating", "Legacy"].includes(row.lifecycle) ? "retiring" :
      row.lifecycle === "Preview" ? "preview" : "stable";
    lifecycle.append(el("span", `lifecycle ${lifeClass}`, lifecycleLabel(row.lifecycle)));
    tr.append(modelCell, subscription, region, deployment, capacity, quota, lifecycle);
    fragment.append(tr);
  }
  body.append(fragment);
  finishTable(data);
}

function barGraphic(parts) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 200 12");
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("aria-hidden", "true");
  const rectangle = (className, x, width) => {
    const rect = document.createElementNS(svg.namespaceURI, "rect");
    rect.setAttribute("x", String(x));
    rect.setAttribute("width", String(width));
    rect.setAttribute("height", "12");
    rect.setAttribute("rx", "2");
    rect.classList.add(className);
    svg.append(rect);
  };
  rectangle("bar-track", 0, 200);
  let offset = 0;
  for (const part of parts) {
    const width = Math.max(0, Math.min(200 - offset, part.share * 200));
    rectangle(part.className, offset, width);
    offset += width;
  }
  return svg;
}

function renderQuota(data) {
  state.rows = data.rows;
  renderTableHeader();
  renderInventorySummary(data.summary);
  const body = $("inventory-body");
  body.replaceChildren();
  if (!data.rows.length) {
    const hosted = emptyEstate();
    const setup = el("button", "button primary", hosted?.action || "Set up collection");
    setup.type = "button";
    setup.addEventListener("click", () => selectTab("collection"));
    body.append(emptyRow(4, data.snapshot ? "No quota entries match this selection" : hosted?.title || "Collect your first quota snapshot",
      data.snapshot ? "Try another region, model or deployment type, or lower the minimum. Missing quota is never treated as zero." :
        hosted?.text || "Choose your subscriptions in Collection & history to see reported quota here.", data.snapshot ? null : setup));
  }
  for (const pool of data.rows) {
    const row = el("tr");
    const scope = el("td");
    scope.append(el("strong", "scope-label", pool.region), el("span", "cell-secondary subscription-cell", pool.subscription));
    const deployment = el("td");
    deployment.append(el("span", "scope-label", pool.skus.filter(Boolean).join(", ") || "SKU not reported"),
      el("span", "cell-secondary", `${pool.deployment_types.join(", ")} · ${pool.capacity_types.join(", ")}`));
    const allocation = el("td", "allocation-cell");
    const review = pool.availability === "available" && !pool.current_choices;
    const status = review ? "Review lifecycle" : pool.availability === "available" ? "Quota available" :
      pool.availability === "exhausted" ? pool.quota_limit === 0 ? "No quota assigned" : "Fully allocated" : "Quota unknown";
    const heading = el("div", "allocation-heading");
    heading.append(el("span", `quota-status ${review ? "review" : pool.availability}`, status));
    if (pool.remaining !== null) {
      heading.append(el("strong", "allocation-amount", `${fmt(pool.remaining)} ${pool.unit}`));
      const limit = pool.quota_limit || 0;
      allocation.append(heading, barGraphic([
        { className: "bar-allocated", share: limit ? Math.min(1, pool.allocated / limit) : 0 },
        { className: "bar-value", share: limit ? pool.remaining / limit : 0 },
      ]), el("span", "cell-secondary", `${fmt(pool.allocated)} allocated · ${fmt(pool.quota_limit)} limit`));
    } else {
      allocation.append(heading, el("span", "cell-secondary", pool.unit === "Mixed" ? "Conflicting units; no amount inferred" : "Not reported consistently"));
    }
    const models = el("td", "pool-models");
    const scopedModel = selected("model").length === 1 || selected("model_version").length === 1;
    const label = pool.choices.length === 1 && !scopedModel ? pool.choices[0].label :
      `${fmt(pool.choices.length)} matching ${pool.choices.length === 1 ? "choice" : "choices"}`;
    const button = el("button", "group-button", label);
    button.type = "button";
    button.setAttribute("aria-label", `Review models using quota in ${pool.region}, ${pool.subscription}`);
    button.addEventListener("click", () => openPoolDetail(pool, row));
    models.append(button);
    if (pool.sharing_choices > 1) {
      models.append(el("span", "cell-secondary", `Shared by ${fmt(pool.sharing_choices)} catalog choices`));
    } else if (pool.choices.length) {
      models.append(el("span", "cell-secondary", pool.choices[0].lifecycles.map(lifecycleLabel).join(", ")));
    }
    row.append(scope, deployment, allocation, models);
    body.append(row);
  }
  finishTable(data);
}

function openPoolDetail(pool, tableRow) {
  closeDetail();
  tableRow.classList.add("selected");
  $("detail-title").textContent = "Quota pool";
  $("row-detail").hidden = false;
  $("data-workspace").classList.add("with-detail");
  const content = $("detail-content");
  content.replaceChildren(el("p", "detail-model", pool.region), el("p", "muted", pool.subscription));
  content.append(el("p", "", pool.remaining === null ? "The available amount is unknown." :
    `${fmt(pool.remaining)} ${pool.unit} unallocated out of a limit of ${fmt(pool.quota_limit)}.`));
  if (pool.sharing_choices > 1) {
    content.append(el("p", "muted", `This amount is shared by ${pool.sharing_choices} model/version choices in the snapshot. It is not a separate budget for each model.`));
  }
  if (pool.notes) content.append(el("p", "muted", pool.notes));
  if (!pool.current_choices) content.append(el("p", "muted", "The matching entries need a lifecycle or SKU review before considering a deployment."));
  const list = el("ul", "pool-choice-list");
  for (const choice of pool.choices.slice(0, 8)) {
    const item = el("li");
    item.append(el("strong", "", choice.label),
      el("span", "cell-secondary", `${choice.format} · ${choice.lifecycles.map(lifecycleLabel).join(", ")}`));
    list.append(item);
  }
  content.append(el("h3", "", "Matching catalog choices"), list);
  if (pool.choices.length > 8) content.append(el("p", "muted", `${fmt(pool.choices.length - 8)} more choices are available in the table.`));
  const review = el("button", "button small", "Review matching models");
  review.type = "button";
  review.addEventListener("click", handled(async () => {
    state.filters.subscription = [pool.subscription_id];
    state.filters.region = [pool.region];
    state.filters.quota_name = pool.quota_name ? [pool.quota_name] : [];
    state.filters.sku = pool.skus.filter(Boolean);
    if (!pool.quota_name) {
      state.filters.model_version = pool.choices.map((choice) => choice.value);
      state.filters.model = [...new Set(pool.choices.map((choice) => choice.model))];
    }
    state.view = "deployments";
    state.page = 1;
    resetSort();
    $("table-view").value = state.view;
    updateChoiceOptions();
    syncFilterControls();
    reconstructTrail();
    await loadInventory();
  }));
  content.append(review, el("p", "muted", "Quota alone does not confirm Azure capacity, model access or deployment eligibility."));
  const technical = el("details", "pool-technical");
  technical.append(el("summary", "", "Pool identifier"), el("p", "muted", pool.quota_name || "Not reported"));
  content.append(technical);
  if (pool.choices.length === 1) content.append(pricingReference(pool.choices[0].model, pool.region));
}

function pricingReference(model, region) {
  const section = el("details", "pool-technical");
  section.append(el("summary", "", "Public unit pricing"));
  const quoted = (value) => String(value).replaceAll("'", "''");
  const filter = `serviceName eq 'Foundry Models' and armRegionName eq '${quoted(region)}' and priceType eq 'Consumption' and contains(meterName, '${quoted(model)}')`;
  const parameters = new URLSearchParams({
    "api-version": "2023-01-01-preview", currencyCode: "USD", $filter: filter,
  });
  const link = el("a", "", "Search published price meters");
  link.href = `https://prices.azure.com/api/retail/prices?${parameters}`;
  link.target = "_blank";
  link.rel = "noopener noreferrer";
  section.append(el("p", "muted", "Public rates include input, output and cache meters where published, with their original billing units. No usage data is required."), link,
    el("p", "muted", "Search results may include other versions or fine-tuned SKUs. They are not verified prices for this selection or your negotiated rates. Quota is not a prepaid balance."));
  return section;
}

function openDetail(row, tableRow) {
  $("detail-title").textContent = "Model details";
  document.querySelectorAll(".inventory-table tr.selected").forEach((item) => item.classList.remove("selected"));
  tableRow.classList.add("selected");
  $("row-detail").hidden = false;
  $("data-workspace").classList.add("with-detail");
  const container = $("detail-content");
  container.replaceChildren(el("p", "detail-model", modelLabel(row)), el("p", "muted", row.format));
  const versions = el("button", "text-button", "Browse all versions of this model");
  versions.type = "button";
  versions.addEventListener("click", handled((event) => drillInto({ label: row.model, filters: { model: [row.model] } }, "model", event.detail === 0)));
  container.append(versions);
  const stats = el("div", "detail-quota");
  for (const [label, value] of [["Limit", row.quota_limit], ["Allocated", row.allocated], ["Remaining", row.remaining]]) {
    const block = el("div");
    block.append(el("small", "", label), el("strong", "", fmt(value)));
    stats.append(block);
  }
  container.append(stats, el("p", "muted", row.unit || "Quota unit not reported"));
  const fields = [
    ["Subscription", row.subscription], ["Region", row.region], ["Deployment SKU", row.sku],
    ["Lifecycle", row.lifecycle], ["Account kinds", (row.account_kinds || []).join(", ")],
    ["Quota pool", row.quota_name], ["Quota description", row.quota_description],
    ["Model retirement", row.inference_deprecation], ["SKU retirement", row.sku_deprecation],
    ["Collection notes", row.notes],
  ];
  const dl = el("dl");
  for (const [label, value] of fields) {
    if (!value) continue;
    dl.append(el("dt", "", label), el("dd", "", value));
  }
  container.append(dl, el("p", "muted", "This quota pool may be shared with other models or versions. It is not capacity reserved for this row."));
  container.append(pricingReference(row.model, row.region));
}

function closeDetail() {
  $("row-detail").hidden = true;
  $("data-workspace").classList.remove("with-detail");
  document.querySelectorAll(".inventory-table tr.selected").forEach((item) => item.classList.remove("selected"));
}

async function loadInventory(reloadFacets = false) {
  const request = ++state.request;
  rememberLocation();
  const parameters = query();
  const grouped = !["deployments", "quota"].includes(state.view);
  if (grouped) parameters.set("group_by", state.view);
  const endpoint = state.view === "quota" ? "quota" : grouped ? "groups" : "inventory";
  const dataPromise = api(`/api/${endpoint}?${parameters}`);
  const facetsPromise = reloadFacets ? api(`/api/facets?snapshot=${encodeURIComponent(state.snapshot)}`) : Promise.resolve(null);
  const level = chartLevel();
  const chartParameters = query(false);
  chartParameters.set("group_by", level);
  chartParameters.set("sort", level === "model_version" ? "regions" : "versions");
  chartParameters.set("direction", "desc");
  chartParameters.set("page_size", "12");
  const chartPromise = state.chartVisible && state.view !== "quota" ? api(`/api/groups?${chartParameters}`) : Promise.resolve(null);
  const [data, facets, chart] = await Promise.all([dataPromise, facetsPromise, chartPromise]);
  if (request !== state.request) return;
  if (facets) renderFacets(facets);
  if (state.view === "quota") renderQuota(data);
  else if (state.view === "deployments") renderInventory(data);
  else renderGroups(data);
  renderChart(chart, level);
  coverageLoaded = false;
}

function renderGroups(data) {
  state.rows = data.rows;
  const level = state.view;
  renderTableHeader();
  renderInventorySummary(data.summary);
  const columns = state.view === "family" ? ["models", "versions", "regions", "subscriptions", "entries", "with_headroom"] :
    state.view === "model" ? ["versions", "regions", "subscriptions", "entries", "with_headroom"] :
      ["regions", "subscriptions", "entries", "quota_pools", "with_headroom"];
  const body = $("inventory-body");
  body.replaceChildren();
  if (!data.rows.length) {
    body.append(emptyRow(columns.length + 1, "No groups match these filters", "Clear a selection or choose a different snapshot. Each model/version choice remains tied together."));
  }
  for (const group of data.rows) {
    const row = el("tr");
    const label = el("td");
    const button = el("button", "group-button", group.label);
    button.type = "button";
    button.addEventListener("click", handled((event) => drillInto(group, level, event.detail === 0)));
    label.append(button);
    if (group.format) label.append(el("span", "cell-secondary", group.format));
    row.append(label);
    for (const column of columns) {
      const cell = el("td", "number-cell", fmt(group[column]));
      if (column === "with_headroom" && group.unknown_quota) {
        cell.append(el("span", "cell-secondary", `${fmt(group.unknown_quota)} unknown`));
      }
      row.append(cell);
    }
    body.append(row);
  }
  finishTable(data);
}

function chartLevel() {
  if (state.view !== "deployments") return state.view;
  if (selected("model_version").length) return "model_version";
  if (selected("model").length || selected("family").length === 1) return "model";
  return "family";
}

function renderChart(data, level) {
  $("chart-toggle").hidden = state.view === "quota";
  $("model-chart").hidden = !state.chartVisible || state.view === "quota";
  $("chart-toggle").textContent = state.chartVisible ? "Hide chart" : "Show chart";
  $("chart-toggle").setAttribute("aria-expanded", String(state.chartVisible));
  if (!data) return;
  const metric = level === "model_version" ? "regions" : "versions";
  const metricLabel = metric === "regions" ? "regions" : "model/version choices";
  $("chart-title").textContent = level === "family" ? "Model/version choices by family" :
    level === "model" ? "Catalog versions by model" : "Regions by model/version choice";
  $("chart-scale").textContent = `${fmt(data.total)} ${viewNames[level]}`;
  $("chart-caption").textContent = `${data.total > data.rows.length ? `Top ${data.rows.length} shown; explore every group in the table. ` : ""}` +
    "Select a bar to drill down. Catalog counts only; repeated subscriptions and regions do not inflate model/version counts.";
  const container = $("chart-bars");
  container.replaceChildren();
  if (!data.rows.length) {
    container.append(el("p", "muted", "No chart data for this selection."));
    return;
  }
  const maximum = Math.max(1, ...data.rows.map((row) => row[metric]));
  for (const group of data.rows) {
    const button = el("button", "chart-bar");
    button.type = "button";
    button.setAttribute("aria-label", `Explore ${group.label}: ${group[metric]} ${metricLabel}`);
    button.title = `${group.label}: ${fmt(group[metric])} ${metricLabel}`;
    button.addEventListener("click", handled((event) => drillInto(group, level, event.detail === 0)));
    const svg = barGraphic([{ className: "bar-value", share: group[metric] / maximum }]);
    button.append(el("span", "chart-label", group.label), svg, el("strong", "chart-count", fmt(group[metric])));
    container.append(button);
  }
}

function reconstructTrail() {
  state.trail = [];
  if (selected("family").length === 1) state.trail.push({ level: "family", value: selected("family")[0], label: selected("family")[0] });
  if (selected("model").length === 1) state.trail.push({ level: "model", value: selected("model")[0], label: selected("model")[0] });
  if (selected("model_version").length === 1) {
    const value = selected("model_version")[0];
    const pair = JSON.parse(value);
    if (!state.trail.some((item) => item.level === "model")) state.trail.push({ level: "model", value: pair[1], label: pair[1] });
    state.trail.push({ level: "model_version", value, label: pairLabel(value) });
  }
}

async function drillInto(group, level, moveFocus = false) {
  closeMultiSelects();
  state.filters.version = [];
  if (level === "family") {
    state.filters.family = group.filters.family;
    state.filters.model = [];
    state.filters.model_version = [];
  } else if (level === "model") {
    state.filters.model = group.filters.model;
    state.filters.model_version = [];
  } else {
    state.filters.model_version = group.filters.model_version.map((pair) => JSON.stringify(pair));
    state.filters.model = [group.filters.model_version[0][1]];
  }
  reconstructTrail();
  state.view = level === "family" ? "model" : level === "model" ? "model_version" : "quota";
  resetSort();
  state.page = 1;
  $("table-view").value = state.view;
  updateChoiceOptions();
  syncFilterControls();
  await loadInventory();
  if (moveFocus) $("inventory-body").querySelector("button")?.focus();
}

function renderBreadcrumbs() {
  const container = $("drill-breadcrumbs");
  container.replaceChildren();
  if (!state.trail.length) {
    container.append(el("span", "muted", state.view === "quota" ? "Select a model to find its quota." :
      state.view === "deployments" ? "Select a model for details." : "Select a row to drill down."));
    return;
  }
  const all = el("button", "text-button", "All families");
  all.type = "button";
  all.addEventListener("click", handled(async () => {
    for (const key of ["family", "model", "model_version", "version"]) state.filters[key] = [];
    state.trail = [];
    state.view = "family";
    state.page = 1;
    state.sort = "label";
    $("table-view").value = state.view;
    updateChoiceOptions();
    syncFilterControls();
    await loadInventory();
  }));
  container.append(all);
  for (const item of state.trail) {
    const separator = el("span", "breadcrumb-separator", "›");
    separator.setAttribute("aria-hidden", "true");
    const button = el("button", "text-button", item.label);
    button.type = "button";
    button.addEventListener("click", handled((event) => drillInto({
      filters: item.level === "model_version" ? { model_version: [JSON.parse(item.value)] } : { [item.level]: [item.value] },
    }, item.level, event.detail === 0)));
    container.append(separator, button);
  }
}

function scanLabel(scan) {
  return `${when(scan.started_at)} · ${scan.status}${scan.source === "import" ? " · imported" : ""}`;
}

function renderScans(scans) {
  state.scans = scans;
  const snapshotOptions = scans.filter((scan) => !["running", "failed"].includes(scan.status))
    .map((scan) => ({ value: String(scan.id), label: scanLabel(scan) }));
  populateSelect($("snapshot-select"), [{ value: "latest", label: "Latest complete snapshot" }, ...snapshotOptions], null, state.snapshot);
  const compareOptions = snapshotOptions;
  const from = $("compare-from").value || compareOptions[1]?.value || compareOptions[0]?.value || "";
  const to = $("compare-to").value || compareOptions[0]?.value || "";
  populateSelect($("compare-from"), compareOptions, compareOptions.length ? null : "No snapshots yet", from);
  populateSelect($("compare-to"), compareOptions, compareOptions.length ? null : "No snapshots yet", to);
  $("compare-form").querySelector("button").disabled = compareOptions.length < 2;
  renderHistory(scans);
}

function renderHistory(scans) {
  const body = $("history-body");
  body.replaceChildren();
  if (!scans.length) body.append(emptyRow(6, "No collection history yet", "Your first collection will appear here. Failed attempts stay visible alongside successful snapshots."));
  for (const scan of scans) {
    const row = el("tr");
    const status = el("td");
    status.append(el("span", `status-label ${scan.status}`, scan.status));
    const action = el("td");
    if (scan.status !== "running" && scan.status !== "failed") {
      const button = el("button", "text-button", "View snapshot");
      button.type = "button";
      button.addEventListener("click", handled(() => viewSnapshot(scan.id)));
      action.append(button);
    }
    if (scan.error_count) action.append(el("span", "cell-secondary", `${fmt(scan.error_count)} collection issue(s)`));
    const errors = scan.errors || [];
    if (errors.length) {
      const details = el("details");
      details.append(el("summary", "", "Collection messages"));
      for (const error of errors) details.append(el("p", "muted", typeof error === "string" ? error : error.message || JSON.stringify(error)));
      action.append(details);
    }
    row.append(el("td", "", when(scan.started_at)), el("td", "", scan.source), status,
      el("td", "number-cell", fmt(scan.model_count)), el("td", "number-cell", fmt(scan.record_count)), action);
    body.append(row);
  }
  const chart = $("history-chart");
  chart.replaceChildren();
  const recent = scans.filter((scan) => scan.status === "complete").slice(0, 8).reverse();
  for (const scan of recent) {
    const active = String(scan.id) === state.snapshot || (state.snapshot === "latest" && scan.id === state.status?.latest?.id);
    const button = el("button", `history-point${active ? " current" : ""}`);
    button.type = "button";
    button.append(el("small", "", when(scan.started_at)), el("strong", "", `${fmt(scan.model_count)} models`),
      el("small", "", active ? "Selected snapshot" : "Open snapshot"));
    button.setAttribute("aria-label", `View snapshot ${when(scan.started_at)}, ${scan.model_count} models`);
    button.addEventListener("click", handled(() => viewSnapshot(scan.id)));
    chart.append(button);
  }
}

async function viewSnapshot(id) {
  state.snapshot = String(id);
  state.page = 1;
  $("snapshot-select").value = state.snapshot;
  selectTab("inventory");
  await loadInventory(true);
  renderHistory(state.scans);
}

function hostedView(status) {
  if (!status || status.read_only !== true) return null;
  const hosted = status.hosted || {};
  const time = /^([01]\d|2[0-3]):[0-5]\d$/.test(hosted.collection_time || "") ? hosted.collection_time : "07:00";
  const zone = typeof hosted.timezone === "string" && hosted.timezone.trim() ? hosted.timezone.trim() : "UTC";
  const commit = /^[0-9a-f]{7,40}$/.test(hosted.commit || "") ? hosted.commit : "";
  const subscriptions = Array.isArray(status.config?.subscriptions) ? status.config.subscriptions : [];
  const tenants = new Set(subscriptions.map((sub) => sub.tenant_id || status.config?.tenant_id || "")).size;
  const attempt = hosted.last_attempt && typeof hosted.last_attempt.status === "string" ? hosted.last_attempt : null;
  const backup = hosted.backup && typeof hosted.backup === "object" ? hosted.backup : null;
  const count = (value, noun) => `${value} ${noun}${value === 1 ? "" : "s"}`;
  return {
    label: "Hosted · read-only",
    schedule: `Daily at ${time} · ${zone}`,
    explanation: `This hosted service collects every morning at ${time} (${zone}) with its own read-only Azure identity. ` +
      "Collection, scope and schedule changes are made through its deployment, not in the browser.",
    scope: `${count(subscriptions.length, "subscription")} in ${count(tenants, "tenant")}, collected by the hosted service.`,
    subscriptions: subscriptions.map((sub) => ({ id: String(sub.id || ""), name: String(sub.name || sub.id || "") })),
    commit: commit ? commit.slice(0, 7) : "not recorded",
    commitTitle: commit || "This build did not record its commit.",
    nextRun: typeof hosted.next_run === "string" ? hosted.next_run : null,
    attempt: attempt ? { status: attempt.status, at: attempt.completed_at || attempt.started_at || null } : null,
    backup: backup?.error ? { error: String(backup.error) } :
      backup?.saved_at ? { savedAt: backup.saved_at } :
        backup?.restored_at ? { restoredAt: backup.restored_at } : null,
    user: typeof hosted.user === "string" ? hosted.user.slice(0, 256) : "",
  };
}

function statusNotice(data, now = Date.now()) {
  const collecting = Boolean(data.collection?.running);
  const hosted = data.read_only === true;
  if (!data.configured) {
    return hosted ?
      { message: "The hosted service has no collection scope. Redeploy it with the subscriptions to collect.", type: "error" } :
      { message: "Choose your tenant and subscriptions in Collection & history before starting a scan.", type: "" };
  }
  if (data.collection?.last_error && !collecting) {
    const detail = String(data.collection.last_error).replace(/[.\s]+$/, "");
    return { message: `The last collection needs attention: ${detail}. The latest complete snapshot is still available.`, type: "error" };
  }
  if (data.scope_pending && !collecting) {
    return { message: hosted ?
      "The latest complete snapshot does not cover every configured subscription yet. The next hosted collection includes them." :
      "The latest complete snapshot does not cover the current tenant and subscription selection. Collect now to refresh the full scope.",
    type: "warning" };
  }
  if (data.latest && now - new Date(data.latest.started_at).getTime() > 30 * 60 * 60 * 1000) {
    return { message: hosted ?
      "The latest complete snapshot is more than 30 hours old. Collection & history shows the hosted service's latest attempt." :
      "The latest complete snapshot is more than 30 hours old. Collect now or check the morning schedule.",
    type: "warning" };
  }
  return { message: "", type: "" };
}

function applyMode(hosted) {
  if (!hosted) return;
  document.title = "Foundry inventory — hosted";
  $("workspace-mode").textContent = hosted.label;
  $("workspace-note").replaceChildren("Hosted in your Azure subscription.", el("br"), "Microsoft Entra sign-in, read-only access.");
  $("workspace-footer-note").textContent = `SQLite snapshots · read-only Azure access · commit ${hosted.commit}`;
  $("workspace-footer-note").title = hosted.commitTitle;
  $("scan-button").hidden = true;
  $("discover-button").hidden = true;
  $("config-form").hidden = true;
  $("schedule-form").hidden = true;
  const signOut = $("sign-out");
  signOut.hidden = false;
  signOut.title = hosted.user ? `Signed in as ${hosted.user}` : "Sign out of the hosted dashboard";
  signOut.setAttribute("aria-label", hosted.user ? `Sign out ${hosted.user}` : "Sign out");
}

function renderHosted(hosted) {
  $("scope-summary").textContent = hosted.scope;
  const list = $("scope-list");
  list.replaceChildren(...[...hosted.subscriptions].sort((a, b) => a.name.localeCompare(b.name)).map((sub) => {
    const item = el("li", "", sub.name);
    item.append(el("span", "cell-secondary", sub.id));
    return item;
  }));
  list.hidden = !hosted.subscriptions.length;
  const summary = $("schedule-summary");
  summary.replaceChildren(el("span", "status-label complete", hosted.schedule));
  summary.append(el("p", "", `Next collection: ${hosted.nextRun ? when(hosted.nextRun) : "being scheduled"}`));
  if (hosted.attempt) {
    const attempt = el("p", "", "Latest attempt: ");
    attempt.append(el("span", `status-label ${hosted.attempt.status}`, hosted.attempt.status), ` ${when(hosted.attempt.at)}`);
    summary.append(attempt);
  } else {
    summary.append(el("p", "", "Latest attempt: none yet. The first collection starts shortly after deployment."));
  }
  if (hosted.backup?.error) summary.append(el("p", "backup-warning", hosted.backup.error));
  else if (hosted.backup?.savedAt) summary.append(el("p", "", `Persistent copy saved: ${when(hosted.backup.savedAt)}`));
  else if (hosted.backup?.restoredAt) summary.append(el("p", "", `Restored from the persistent copy: ${when(hosted.backup.restoredAt)}`));
  const commit = el("p", "", `Deployed commit: ${hosted.commit}`);
  commit.title = hosted.commitTitle;
  summary.append(commit);
  $("schedule-note").textContent = hosted.explanation;
}

function applyStatus(data) {
  const previous = state.status;
  state.status = data;
  state.csrf = data.csrf_token;
  const hosted = hostedView(data);
  applyMode(hosted);
  const collecting = Boolean(data.collection?.running);
  $("scan-button").disabled = collecting;
  $("scan-button").querySelector("span").textContent = collecting ? "Collecting…" : "Collect now";
  $("scan-state").textContent = collecting ? "Scan in progress" : data.latest ? `Updated ${when(data.latest.completed_at || data.latest.started_at)}` : "No snapshot yet";
  $("scan-progress").hidden = !collecting;
  if (collecting) {
    const progress = data.collection.progress || {};
    $("scan-progress-message").textContent = data.collection.message || "Reading catalogs and quota from Azure.";
    $("scan-progress-bar").max = Math.max(1, progress.total || 1);
    $("scan-progress-bar").value = progress.completed || 0;
  }
  if (!hosted) renderSchedule(data.schedule);
  if (!state.initialized) renderConfig(data.config);
  if (hosted) renderHosted(hosted);
  const { message, type } = statusNotice(data);
  notice(message, type);
  return previous && (previous.collection?.running && !collecting ||
    previous.latest?.id !== data.latest?.id);
}

function renderSchedule(schedule) {
  const container = $("schedule-summary");
  container.replaceChildren();
  if (!schedule || schedule.available === false) {
    container.append(el("span", "status-label", "Schedule unavailable"));
    if (schedule?.note || schedule?.error) container.append(el("p", "", schedule.note || schedule.error));
    return;
  }
  container.append(el("span", `status-label ${schedule.enabled ? "complete" : ""}`,
    schedule.enabled ? `Daily at ${schedule.time || "07:00"}` : "Automatic collection is off"));
  if (schedule.next_run) container.append(el("p", "", `Next run: ${when(schedule.next_run)}`));
  if (schedule.last_run) container.append(el("p", "", `Last task run: ${when(schedule.last_run)} · result ${schedule.last_result ?? "not reported"}`));
  if (schedule.note) container.append(el("p", "", schedule.note));
  if (!$("schedule-form").contains(document.activeElement)) {
    $("schedule-enabled").checked = Boolean(schedule.enabled);
    $("schedule-time").value = schedule.time || "07:00";
  }
}

function collectionTenant(config, subscription) {
  return subscription.tenant_id || config?.tenant_id || "";
}

function collectionProfile(config, tenant) {
  if (!tenant) return "";
  return tenant === config?.tenant_id ? config?.azure_config_dir || "" :
    config?.tenant_profiles?.[tenant] || "";
}

function collectionPayload(config, tenant, subscriptions, profile, morningTime) {
  if (!tenant || !subscriptions.length) throw new Error("Select a tenant and at least one enabled subscription.");
  const primary = config?.tenant_id || tenant;
  if (tenant !== primary && !profile) {
    throw new Error("An additional tenant needs an explicit private Azure CLI profile.");
  }
  const retained = (config?.subscriptions || []).filter((item) => collectionTenant(config, item) !== tenant);
  const selected = subscriptions.map(({ id, name }) =>
    tenant === primary ? { id, name } : { id, name, tenant_id: tenant });
  const updated = {
    tenant_id: primary,
    subscriptions: [...retained, ...selected],
    morning_time: morningTime || config?.morning_time || "07:00",
  };
  const primaryProfile = tenant === primary ? profile || config?.azure_config_dir : config?.azure_config_dir;
  if (primaryProfile) updated.azure_config_dir = primaryProfile;
  const profiles = { ...config?.tenant_profiles };
  if (tenant !== primary) profiles[tenant] = profile;
  if (Object.keys(profiles).length) updated.tenant_profiles = profiles;
  return updated;
}

function renderConfig(config, preferredTenant) {
  state.config = config;
  if (!config) return;
  const previousTenant = preferredTenant || $("tenant-select").value;
  for (const sub of config.subscriptions || []) {
    if (!state.subscriptions.some((item) => item.id === sub.id)) {
      state.subscriptions.push({ ...sub, tenant_id: collectionTenant(config, sub), state: "Enabled" });
    }
  }
  const tenants = [...new Set(state.subscriptions.map((sub) => sub.tenant_id))].sort();
  const selectedTenant = tenants.includes(previousTenant) ? previousTenant : config.tenant_id || tenants[0];
  populateSelect($("tenant-select"), tenants, "Select tenant", selectedTenant);
  $("profile-directory").value = state.discoveredProfiles.get(selectedTenant) ??
    collectionProfile(config, selectedTenant);
  const scope = config.subscriptions || [];
  const tenantCount = new Set(scope.map((sub) => collectionTenant(config, sub))).size;
  $("scope-summary").textContent = scope.length ?
    `${tenantCount} tenant${tenantCount === 1 ? "" : "s"} · ${scope.length} subscription${scope.length === 1 ? "" : "s"} saved for collection.` :
    "Discover subscriptions with your Azure CLI sign-in, then save the scope.";
  renderSubscriptionChoices();
}

function renderSubscriptionChoices(selectedIds) {
  const tenant = $("tenant-select").value;
  const selected = selectedIds || new Set((state.config?.subscriptions || [])
    .filter((sub) => collectionTenant(state.config, sub) === tenant).map((sub) => sub.id));
  const available = state.subscriptions.filter((sub) => sub.tenant_id === tenant && sub.state === "Enabled");
  const container = $("subscription-choices");
  container.replaceChildren();
  if (!available.length) container.append(el("p", "muted", "No enabled subscriptions in this profile. Sign in with Azure CLI, then discover again."));
  for (const sub of available.sort((a, b) => a.name.localeCompare(b.name))) {
    const label = el("label", "checkbox-label");
    const input = el("input");
    input.type = "checkbox";
    input.value = sub.id;
    input.checked = selected.has(sub.id);
    const name = el("span", "", sub.name);
    name.append(el("span", "cell-secondary", sub.id));
    label.append(input, name);
    container.append(label);
  }
}

async function loadCoverage() {
  if (coverageLoaded) return;
  const data = await api(`/api/coverage?snapshot=${encodeURIComponent(state.snapshot)}`);
  const body = $("coverage-body");
  body.replaceChildren();
  if (!data.rows.length) body.append(emptyRow(5, "No region coverage recorded", "Collect a snapshot or select one from history."));
  for (const row of data.rows) {
    const tr = el("tr");
    const quotaErrors = row.quota_errors ?? row.QuotaErrors ?? 0;
    const unknown = row.quota_unknown ?? row.QuotaUnknown ?? 0;
    const health = quotaErrors ? `${quotaErrors} errors` : unknown ? `${unknown} unknown` :
      row.quota_status || (row.catalog_status === "Empty" ? "Not applicable" : "Not reported");
    const messages = (row.errors || []).map((error) => typeof error === "string" ? error : error.message || "");
    tr.append(el("td", "", row.subscription || row.subscription_id),
      el("td", "", row.region), el("td", "", row.catalog_status || row.status),
      el("td", "", health), el("td", "", row.notes || messages.join("; ")));
    body.append(tr);
  }
  coverageLoaded = true;
}

async function compareSnapshots(resetPage = true) {
  const older = $("compare-from").value;
  const newer = $("compare-to").value;
  if (!older || !newer || older === newer) {
    toast("Choose two different snapshots to compare.");
    return;
  }
  if (resetPage) state.comparePage = 1;
  const request = ++state.compareRequest;
  const params = query(false);
  params.set("from", older);
  params.set("to", newer);
  params.set("page", String(state.comparePage));
  params.set("page_size", "50");
  const data = await api(`/api/compare?${params}`);
  if (request !== state.compareRequest) return;
  $("compare-summary").replaceChildren(
    summaryItem(data.summary.added, "added"),
    summaryItem(data.summary.removed, "removed"),
    summaryItem(data.summary.changed, "changed"),
    summaryItem(data.summary.quota_changed, "quota changes"),
  );
  $("compare-warnings").textContent = (data.warnings || []).join(" ");
  $("compare-warnings").hidden = !data.warnings?.length;
  const body = $("compare-body");
  body.replaceChildren();
  if (!data.rows.length) {
    const uncertain = data.summary.uncomparable > 0;
    body.append(emptyRow(5, uncertain ? "No comparable differences to show" : "No differences in this selection",
      uncertain ? "Some entries could not be compared. Review the scope warnings above before drawing conclusions." :
        "The model and quota entries in the common observed scope are unchanged."));
  }
  for (const item of data.rows) {
    const record = item.after || item.before;
    const tr = el("tr");
    const change = el("td");
    change.append(el("span", `change-label ${item.change}`, item.change));
    const model = el("td", "", modelLabel(record));
    model.append(el("span", "cell-secondary", record.family || record.format));
    const scope = el("td", "subscription-cell", record.subscription || record.subscription_id);
    scope.append(el("span", "cell-secondary", record.region));
    const details = el("td");
    if (item.before && item.after) {
      for (const field of (item.fields || []).slice(0, 8)) {
        const before = item.before[field];
        const after = item.after[field];
        details.append(el("span", "cell-secondary", `${field.replaceAll("_", " ")}: ${before ?? "unknown"} → ${after ?? "unknown"}`));
      }
    } else details.append(el("span", "muted", item.after ? "New in this observed scope" : "No longer in this observed scope"));
    if (record.unit) details.append(el("span", "cell-secondary", `Quota unit: ${record.unit}`));
    tr.append(change, model, scope, el("td", "", record.sku), details);
    body.append(tr);
  }
  const first = data.total ? (data.page - 1) * data.page_size + 1 : 0;
  const last = Math.min(data.page * data.page_size, data.total);
  $("compare-pagination").hidden = false;
  $("compare-count").textContent = `${fmt(first)}–${fmt(last)} of ${fmt(data.total)} differences`;
  $("compare-page-number").textContent = `${data.page} / ${Math.max(1, Math.ceil(data.total / data.page_size))}`;
  $("compare-previous").disabled = data.page <= 1;
  $("compare-next").disabled = last >= data.total;
}

function selectTab(tab) {
  state.tab = tab;
  document.querySelector(".snapshot-picker").hidden = tab === "changes";
  document.querySelectorAll(".section-tab").forEach((button) => {
    const active = button.dataset.tab === tab;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  });
  for (const name of ["inventory", "changes", "collection"]) $(`panel-${name}`).hidden = name !== tab;
  rememberLocation();
  if (tab === "changes" && state.initialized && $("compare-from").value &&
      $("compare-to").value && $("compare-from").value !== $("compare-to").value) {
    handled(() => compareSnapshots())();
  }
}

function readSavedViews() {
  let raw;
  try {
    raw = localStorage.getItem(savedKey);
  } catch {
    toast("Saved views are unavailable because browser storage could not be accessed.", true);
    return [];
  }
  if (!raw) return [];
  try {
    const views = JSON.parse(raw);
    if (!Array.isArray(views)) throw new Error("Invalid saved views");
    return views.filter((view) => typeof view.name === "string" && view.filters && typeof view.filters === "object").slice(0, 20);
  } catch {
    toast("Saved views in this browser could not be read. Saving a new view will replace the invalid list.", true);
    return [];
  }
}

function renderSavedViews() {
  const views = readSavedViews();
  const container = $("saved-view-list");
  container.replaceChildren();
  if (!views.length) container.append(el("p", "muted", "Keep a useful set of filters here."));
  views.forEach((view, index) => {
    const line = el("div", "saved-view");
    const open = el("button", "text-button", view.name);
    open.type = "button";
    open.addEventListener("click", handled(async () => {
      state.filters = normalizeFilters(view.filters);
      state.q = String(view.q || "");
      state.view = Object.hasOwn(viewNames, view.view) ? view.view : "deployments";
      state.chartVisible = view.chartVisible !== false;
      state.page = 1;
      resetSort();
      reconstructTrail();
      $("table-view").value = state.view;
      $("inventory-search").value = state.q;
      selectTab("inventory");
      await loadInventory(true);
    }));
    const remove = el("button", "icon-button", "×");
    remove.type = "button";
    remove.setAttribute("aria-label", `Remove saved view ${view.name}`);
    remove.addEventListener("click", handled(() => {
      views.splice(index, 1);
      localStorage.setItem(savedKey, JSON.stringify(views));
      renderSavedViews();
    }));
    line.append(open, remove);
    container.append(line);
  });
}

async function refresh() {
  const [status, scans] = await Promise.all([api("/api/status"), api("/api/scans")]);
  applyStatus(status);
  renderScans(scans.scans);
  await loadInventory(true);
  state.initialized = true;
}

async function pollStatus() {
  if (pollPending || document.hidden) return;
  pollPending = true;
  try {
    const status = await api("/api/status");
    const changed = applyStatus(status);
    if (changed) {
      const scans = await api("/api/scans");
      renderScans(scans.scans);
      await loadInventory(true);
      if (!status.collection?.last_error) toast("Collection finished. Your snapshot is ready.");
    }
  } catch (error) {
    $("scan-state").textContent = state.status?.read_only ? "Connection lost" : "Server offline";
    notice(error.message, "error");
  } finally {
    pollPending = false;
  }
}

function wireEvents() {
  $("filters-form").addEventListener("submit", (event) => event.preventDefault());
  $("filters-form").addEventListener("change", handled(async (event) => {
    const key = event.target.name;
    if (key !== "minimum") return;
    state.filters.minimum = event.target.value;
    state.page = 1;
    await loadInventory();
    if (state.tab === "changes") await compareSnapshots();
  }));
  $("inventory-search").addEventListener("input", () => {
    clearTimeout(searchTimeout);
    searchTimeout = setTimeout(handled(async () => {
      state.q = $("inventory-search").value.trim();
      state.page = 1;
      await loadInventory();
    }), 280);
  });
  $("reset-filters").addEventListener("click", handled(async () => {
    state.filters = {};
    state.trail = [];
    state.q = "";
    state.page = 1;
    $("inventory-search").value = "";
    updateChoiceOptions();
    syncFilterControls();
    await loadInventory(true);
    if (state.tab === "changes") await compareSnapshots();
  }));
  $("snapshot-select").addEventListener("change", handled(async () => {
    state.snapshot = $("snapshot-select").value;
    state.page = 1;
    await loadInventory(true);
    renderHistory(state.scans);
    if ($("coverage-details").open) await loadCoverage();
  }));
  document.querySelectorAll(".section-tab").forEach((button) => button.addEventListener("click", () => selectTab(button.dataset.tab)));
  $("inventory-head").addEventListener("click", handled(async (event) => {
    const th = event.target.closest("th[data-sort]");
    if (!th) return;
    const key = th.dataset.sort;
    state.direction = state.sort === key && state.direction === "asc" ? "desc" : "asc";
    state.sort = key;
    state.page = 1;
    await loadInventory();
  }));
  $("table-view").addEventListener("change", handled(async () => {
    state.view = $("table-view").value;
    state.page = 1;
    resetSort();
    await loadInventory();
  }));
  $("chart-toggle").addEventListener("click", handled(async () => {
    state.chartVisible = !state.chartVisible;
    await loadInventory();
  }));
  $("appearance-select").addEventListener("change", () => {
    const persisted = window.InventoryTheme.set($("appearance-select").value);
    if (!persisted) toast("Appearance applied for this page, but browser storage is unavailable.", true);
  });
  $("page-size").addEventListener("change", handled(async () => {
    state.pageSize = Number($("page-size").value);
    state.page = 1;
    await loadInventory();
  }));
  $("previous-page").addEventListener("click", handled(async () => { state.page--; await loadInventory(); }));
  $("next-page").addEventListener("click", handled(async () => { state.page++; await loadInventory(); }));
  $("close-detail").addEventListener("click", closeDetail);
  $("mobile-filters").addEventListener("click", () => {
    const rail = document.querySelector(".filter-rail");
    const visible = rail.classList.toggle("visible");
    $("mobile-filters").setAttribute("aria-expanded", String(visible));
    if (visible) controls.get("subscription").focus();
  });
  $("export-button").addEventListener("click", handled(async () => {
    const endpoint = state.view === "quota" ? "/api/quota.csv" : "/api/export.csv";
    const response = await fetch(`${endpoint}?${query(false)}`, { credentials: "same-origin", cache: "no-store" });
    if (!response.ok) {
      const failure = await response.json();
      throw new Error(failure.error || "CSV export failed.");
    }
    const objectUrl = URL.createObjectURL(await response.blob());
    const link = el("a");
    link.href = objectUrl;
    link.download = `foundry-${state.view === "quota" ? "quota" : "inventory"}-${state.snapshot}.csv`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
    toast("Filtered CSV downloaded.");
  }));
  $("scan-button").addEventListener("click", handled(async () => {
    if (!state.status?.configured) {
      selectTab("collection");
      toast("Save a collection scope first.");
      return;
    }
    $("scan-button").disabled = true;
    try {
      await api("/api/scan", {});
      applyStatus(await api("/api/status"));
      renderScans((await api("/api/scans")).scans);
      toast("Collection started. You can keep exploring the previous snapshot.");
    } finally {
      if (!state.status?.collection?.running) $("scan-button").disabled = false;
    }
  }));
  $("compare-form").addEventListener("submit", (event) => {
    event.preventDefault();
    handled(compareSnapshots)();
  });
  $("compare-previous").addEventListener("click", handled(async () => { state.comparePage--; await compareSnapshots(false); }));
  $("compare-next").addEventListener("click", handled(async () => { state.comparePage++; await compareSnapshots(false); }));
  $("discover-button").addEventListener("click", handled(async () => {
    const button = $("discover-button");
    button.disabled = true;
    button.textContent = "Discovering…";
    try {
      const profile = $("profile-directory").value.trim();
      const found = (await (profile ?
        api("/api/subscriptions", { azure_config_dir: profile }) :
        api("/api/subscriptions"))).subscriptions;
      for (const sub of found) {
        state.discoveredProfiles.set(sub.tenant_id, profile);
        const existing = state.subscriptions.findIndex((item) => item.id === sub.id);
        if (existing === -1) state.subscriptions.push(sub);
        else state.subscriptions[existing] = sub;
      }
      const foundTenants = [...new Set(found.map((sub) => sub.tenant_id))];
      renderConfig(state.config || { tenant_id: "", subscriptions: [], morning_time: "07:00" },
        foundTenants.length === 1 ? foundTenants[0] : $("tenant-select").value);
      toast(`Found ${found.length} subscriptions in this Azure CLI profile.`);
    } finally {
      button.disabled = false;
      button.textContent = "Discover subscriptions";
    }
  }));
  $("tenant-select").addEventListener("change", () => {
    const tenant = $("tenant-select").value;
    $("profile-directory").value = state.discoveredProfiles.get(tenant) ??
      collectionProfile(state.config, tenant);
    renderSubscriptionChoices();
  });
  $("config-form").addEventListener("submit", (event) => {
    event.preventDefault();
    handled(async () => {
      const ids = new Set([...$("subscription-choices").querySelectorAll("input:checked")].map((input) => input.value));
      const tenant = $("tenant-select").value;
      const subscriptions = state.subscriptions.filter((sub) => sub.tenant_id === tenant && ids.has(sub.id))
        .map((sub) => ({ id: sub.id, name: sub.name }));
      const profile = $("profile-directory").value.trim();
      if (profile !== collectionProfile(state.config, tenant) &&
          profile !== state.discoveredProfiles.get(tenant)) {
        throw new Error("Discover this tenant's subscriptions using the selected Azure CLI profile first.");
      }
      const payload = collectionPayload(
        state.config, tenant, subscriptions, profile, $("schedule-time").value,
      );
      const config = await api("/api/config", payload);
      state.discoveredProfiles.delete(tenant);
      renderConfig(config.config || config, tenant);
      applyStatus(await api("/api/status"));
      toast("Tenant scope saved locally. Collect now to refresh the full estate.");
    })();
  });
  $("schedule-form").addEventListener("submit", (event) => {
    event.preventDefault();
    handled(async () => {
      const button = $("schedule-form").querySelector("button");
      button.disabled = true;
      try {
        const schedule = await api("/api/schedule", { enabled: $("schedule-enabled").checked, time: $("schedule-time").value });
        renderSchedule(schedule.schedule || schedule);
        toast("Morning schedule updated.");
      } finally { button.disabled = false; }
    })();
  });
  $("coverage-details").addEventListener("toggle", handled(async () => { if ($("coverage-details").open) await loadCoverage(); }));
  $("save-view-form").addEventListener("submit", (event) => {
    event.preventDefault();
    handled(() => {
      const name = $("saved-view-name").value.trim();
      if (!name) return;
      const views = readSavedViews().filter((view) => view.name !== name);
      if (views.length >= 20) throw new Error("You can save up to 20 views. Remove an unused view first.");
      views.push({ name, filters: { ...state.filters }, q: state.q, view: state.view, chartVisible: state.chartVisible });
      localStorage.setItem(savedKey, JSON.stringify(views));
      $("saved-view-name").value = "";
      $("save-view-details").open = false;
      renderSavedViews();
      toast("View saved in this browser.");
    })();
  });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) pollStatus(); });
  window.addEventListener("resize", () => {
    cancelAnimationFrame(railFrame);
    railFrame = requestAnimationFrame(syncRailGutter);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !closeMultiSelects(true)) closeDetail();
  });
}

async function boot() {
  restoreRailWidth();
  for (const key of multiKeys) {
    controls.set(key, new MultiSelect($(`filter-${key}`), {
      label: key === "family" ? "Model family" : key === "deployment_type" ? "Deployment geography" :
        key === "capacity_type" ? "Capacity type" : key === "availability" ? "Quota headroom" :
          key === "unit" ? "Quota unit" : key === "model_version" ? "Version" : filterLabels[key],
      placeholder: defaultOptions[key], onChange: (values) => changeFilter(key, values),
      describeValue: key === "model_version" ? pairLabel : (value) => value,
    }));
  }
  readLocation();
  updateChoiceOptions();
  syncFilterControls();
  reconstructTrail();
  $("appearance-select").value = window.InventoryTheme.get();
  wireEvents();
  renderSavedViews();
  selectTab(state.tab);
  try {
    await refresh();
  } catch (error) {
    console.error(error);
    notice(error.message, "error");
    $("inventory-body").replaceChildren(emptyRow(7, "Could not load the local inventory", "Check that the server is running, then refresh this page. No data has been changed."));
    $("result-count").textContent = "Inventory unavailable";
    $("inventory-summary").replaceChildren();
  }
  setInterval(pollStatus, 8000);
}

boot();
