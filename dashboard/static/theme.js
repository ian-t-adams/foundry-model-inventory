(() => {
  "use strict";
  const key = "foundry-inventory.appearance.v1";
  const media = matchMedia("(prefers-color-scheme: dark)");
  const choices = ["system", "light", "dark"];
  let preference = "system";
  try {
    const saved = localStorage.getItem(key);
    if (saved && choices.includes(saved)) preference = saved;
    else if (saved) console.warn("Invalid saved appearance; following the system setting.");
  } catch (error) {
    console.warn("Appearance preferences are unavailable in this browser.", error);
  }
  function apply() {
    const resolved = preference === "system" ? (media.matches ? "dark" : "light") : preference;
    document.documentElement.dataset.theme = resolved;
    document.documentElement.dataset.appearance = preference;
    window.dispatchEvent(new CustomEvent("inventory-theme-change", { detail: { preference, resolved } }));
  }
  window.InventoryTheme = Object.freeze({
    get: () => preference,
    set: (value) => {
      if (!choices.includes(value)) throw new Error("Unknown appearance setting.");
      preference = value;
      apply();
      try {
        localStorage.setItem(key, preference);
        return true;
      } catch (error) {
        console.warn("The appearance could not be saved.", error);
        return false;
      }
    },
  });
  media.addEventListener("change", apply);
  apply();
})();
