const instances = new Set();

function node(tag, className, text) {
  const result = document.createElement(tag);
  if (className) result.className = className;
  if (text !== undefined) result.textContent = text;
  return result;
}

export class MultiSelect {
  constructor(root, { label, placeholder, onChange, describeValue = (value) => value }) {
    this.root = root;
    this.label = label;
    this.placeholder = placeholder;
    this.onChange = onChange;
    this.describeValue = describeValue;
    this.disabled = false;
    this.options = [];
    this.selected = new Set();
    root.classList.add("multi-select");
    const caption = node("span", "field-label", label);
    caption.id = `${root.id}-label`;
    this.details = node("details", "multi-disclosure");
    this.summary = node("summary", "multi-trigger");
    this.summary.id = `${root.id}-trigger`;
    this.selectionText = node("span", "multi-selection", placeholder);
    this.selectionText.id = `${root.id}-selection`;
    this.summary.setAttribute("aria-labelledby", `${caption.id} ${this.selectionText.id}`);
    const arrow = node("span", "multi-arrow", "⌄");
    arrow.setAttribute("aria-hidden", "true");
    this.summary.append(this.selectionText, arrow);
    const panel = node("div", "multi-panel");
    panel.setAttribute("role", "group");
    panel.setAttribute("aria-labelledby", caption.id);
    this.search = node("input", "multi-search");
    this.search.type = "search";
    this.search.placeholder = "Search options";
    this.search.autocomplete = "off";
    this.search.setAttribute("aria-label", `Search ${label.toLowerCase()} options`);
    const tools = node("div", "multi-tools");
    this.selectMatching = node("button", "text-button", "Select matching");
    this.selectMatching.type = "button";
    const clear = node("button", "text-button", "Clear");
    clear.type = "button";
    clear.setAttribute("aria-label", `Clear ${label.toLowerCase()} selections`);
    tools.append(this.selectMatching, clear);
    this.list = node("div", "multi-options");
    this.count = node("p", "multi-count");
    this.count.setAttribute("aria-live", "polite");
    panel.append(this.search, tools, this.list, this.count);
    this.details.append(this.summary, panel);
    this.hint = node("p", "multi-hint");
    this.hint.id = `${root.id}-hint`;
    this.hint.hidden = true;
    root.replaceChildren(caption, this.details, this.hint);
    instances.add(this);

    this.summary.addEventListener("click", (event) => {
      if (this.disabled) event.preventDefault();
    });
    this.summary.addEventListener("keydown", (event) => {
      if (this.disabled && ["Enter", " "].includes(event.key)) event.preventDefault();
    });
    this.details.addEventListener("toggle", () => {
      if (!this.details.open) return;
      if (this.disabled) {
        this.details.open = false;
        return;
      }
      for (const other of instances) if (other !== this) other.details.open = false;
      this.search.focus();
    });
    this.search.addEventListener("input", () => this.renderOptions());
    clear.addEventListener("click", () => {
      this.selected.clear();
      this.changed();
    });
    this.selectMatching.addEventListener("click", () => {
      for (const option of this.matches()) this.selected.add(option.value);
      this.changed();
    });
  }

  allOptions() {
    const known = new Set(this.options.map((option) => option.value));
    return [
      ...this.options,
      ...[...this.selected].filter((value) => !known.has(value)).map((value) => ({
        value, label: this.describeValue(value), description: "Not in this snapshot",
      })),
    ];
  }

  matches() {
    const search = this.search.value.trim().toLocaleLowerCase();
    return this.allOptions().filter((option) => !search ||
      `${option.label} ${option.description || ""} ${option.searchText || ""}`.toLocaleLowerCase().includes(search));
  }

  setContext({ placeholder, disabled = false, hint = "" }) {
    if (placeholder !== undefined) this.placeholder = placeholder;
    this.disabled = disabled;
    this.summary.setAttribute("aria-disabled", String(disabled));
    this.root.classList.toggle("is-disabled", disabled);
    this.hint.textContent = hint;
    this.hint.hidden = !hint;
    if (hint) this.summary.setAttribute("aria-describedby", this.hint.id);
    else this.summary.removeAttribute("aria-describedby");
    if (disabled) this.details.open = false;
    this.updateSummary();
    this.renderOptions();
  }

  clearSearch() {
    this.search.value = "";
    this.renderOptions();
  }

  setOptions(options) {
    this.options = options.map((option) => typeof option === "string" ?
      { value: option, label: option } : option);
    this.updateSummary();
    this.renderOptions();
  }

  setSelected(values) {
    this.selected = new Set(values);
    this.updateSummary();
    this.renderOptions();
  }

  changed() {
    this.updateSummary();
    this.renderOptions();
    this.onChange([...this.selected]);
  }

  updateSummary() {
    const values = [...this.selected];
    const labels = new Map(this.allOptions().map((option) => [option.value, option.label]));
    this.selectionText.textContent = values.length === 0 ? this.placeholder :
      values.length === 1 ? labels.get(values[0]) : `${values.length} selected`;
    this.summary.title = values.length ? values.map((value) => labels.get(value)).join(", ") : this.placeholder;
    this.root.classList.toggle("has-selection", values.length > 0);
  }

  renderOptions() {
    const active = this.list.contains(document.activeElement) ? document.activeElement.value : null;
    const matches = this.matches();
    const fragment = document.createDocumentFragment();
    let restoreFocus;
    for (const option of matches) {
      const label = node("label", "multi-option");
      const checkbox = node("input");
      checkbox.type = "checkbox";
      checkbox.value = option.value;
      checkbox.checked = this.selected.has(option.value);
      checkbox.disabled = this.disabled;
      checkbox.addEventListener("change", () => {
        if (checkbox.checked) this.selected.add(option.value);
        else this.selected.delete(option.value);
        this.changed();
      });
      const text = node("span", "multi-option-label", option.label);
      if (option.description) text.append(node("small", "multi-option-description", option.description));
      label.append(checkbox, text);
      fragment.append(label);
      if (active === option.value) restoreFocus = checkbox;
    }
    if (!matches.length) fragment.append(node("p", "multi-empty", "No matching options."));
    this.list.replaceChildren(fragment);
    this.selectMatching.disabled = this.disabled || !matches.length;
    this.count.textContent = `${this.selected.size} selected · ${matches.length} shown`;
    if (restoreFocus) restoreFocus.focus({ preventScroll: true });
  }

  focus() {
    this.summary.focus();
  }
}

// Lays out every choice in a hidden copy of the control to find the width that keeps each
// label on one line, both as the collapsed selection and inside the option list.
export function measureChoices(choices) {
  const probe = node("div", "multi-probe");
  probe.setAttribute("aria-hidden", "true");
  probe.inert = true;
  const selection = node("span", "multi-selection");
  const list = node("div", "multi-options");
  for (const { label, description } of choices) {
    selection.append(node("span", "", label));
    const checkbox = node("input");
    checkbox.type = "checkbox";
    const text = node("span", "multi-option-label", label);
    if (description) text.append(node("small", "multi-option-description", description));
    const option = node("label", "multi-option");
    option.append(checkbox, text);
    list.append(option);
  }
  const trigger = node("div", "multi-trigger");
  trigger.append(selection, node("span", "multi-arrow", "⌄"));
  const panel = node("div", "multi-panel");
  panel.append(list);
  probe.append(trigger, panel);
  document.body.append(probe);
  const width = probe.getBoundingClientRect().width;
  probe.remove();
  return Math.ceil(width);
}

export function closeMultiSelects(returnFocus = false) {
  let closed = false;
  for (const instance of instances) {
    if (!instance.details.open) continue;
    instance.details.open = false;
    if (returnFocus) instance.summary.focus();
    closed = true;
  }
  return closed;
}

document.addEventListener("click", (event) => {
  if (event.composedPath().some((node) => node instanceof Element && node.classList.contains("multi-select"))) return;
  closeMultiSelects();
});
