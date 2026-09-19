from pathlib import Path
import re
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "dashboard" / "static"


def luminance(color):
    color = color.removeprefix("#")
    if len(color) == 3:
        color = "".join(part * 2 for part in color)
    channels = [int(color[index:index + 2], 16) / 255 for index in (0, 2, 4)]
    linear = [channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
              for channel in channels]
    return sum(weight * channel for weight, channel in zip((0.2126, 0.7152, 0.0722), linear))


class FrontendTests(unittest.TestCase):
    def test_semantic_text_and_chart_contrast_in_both_themes(self):
        css = (STATIC / "styles.css").read_text(encoding="utf-8")
        light = re.search(r":root\s*\{([^}]+)\}", css).group(1)
        dark = re.search(r':root\[data-theme="dark"\]\s*\{([^}]+)\}', css).group(1)
        tokens = dict(re.findall(r"--([\w-]+):\s*(#[\da-fA-F]+)", light))
        pairs = [
            ("ink", "surface"), ("muted", "surface"), ("muted", "rail"), ("muted", "disabled"),
            ("accent-dark", "surface"), ("on-accent", "accent"),
            ("chip-ink", "chip-bg"), ("ptu-ink", "ptu-bg"), ("batch-ink", "batch-bg"),
            ("stable", "surface"), ("retiring", "surface"),
            ("warning", "warning-wash"), ("danger", "danger-wash"),
            ("success-ink", "success-bg"), ("info-ink", "info-bg"),
        ]
        for mode, overrides in (("light", {}), ("dark", dict(re.findall(r"--([\w-]+):\s*(#[\da-fA-F]+)", dark)))):
            palette = tokens | overrides
            for foreground, background in pairs:
                with self.subTest(theme=mode, foreground=foreground, background=background):
                    values = sorted((luminance(palette[foreground]), luminance(palette[background])))
                    self.assertGreaterEqual((values[1] + 0.05) / (values[0] + 0.05), 4.5)
            values = sorted((luminance(palette["accent"]), luminance(palette["accent-wash"])))
            self.assertGreaterEqual((values[1] + 0.05) / (values[0] + 0.05), 3)

    @unittest.skipUnless(shutil.which("node"), "Node is optional; required only for this browser-logic unit test")
    def test_appearance_preference_and_system_updates(self):
        script = r"""
const fs = require("node:fs");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const source = fs.readFileSync(process.argv[1], "utf8");
function setup(saved, dark = false, blocked = false) {
  const listeners = [];
  const media = { matches: dark, addEventListener: (_, callback) => listeners.push(callback) };
  const values = new Map(saved ? [["foundry-inventory.appearance.v1", saved]] : []);
  const context = {
    matchMedia: () => media,
    document: { documentElement: { dataset: {} } },
    window: { dispatchEvent: () => true },
    console: { warn: () => {} },
    CustomEvent: class { constructor(type, options) { this.type = type; this.detail = options.detail; } },
    localStorage: {
      getItem: (key) => { if (blocked) throw new Error("Denied"); return values.get(key) || null; },
      setItem: (key, value) => { if (blocked) throw new Error("Denied"); values.set(key, value); },
    },
  };
  vm.runInNewContext(source, context);
  return { api: context.window.InventoryTheme, data: context.document.documentElement.dataset,
    change: (dark) => { media.matches = dark; listeners.forEach((callback) => callback()); }, values };
}
const system = setup(null, true);
assert.equal(system.api.get(), "system");
assert.equal(system.data.theme, "dark");
system.change(false);
assert.equal(system.data.theme, "light");
assert.equal(system.api.set("dark"), true);
system.change(false);
assert.equal(system.data.theme, "dark");
assert.equal(system.values.get("foundry-inventory.appearance.v1"), "dark");
assert.equal(setup("dark", false).data.theme, "dark");
assert.equal(setup("invalid", true).api.get(), "system");
assert.throws(() => system.api.set("invalid"), /Unknown appearance/);
const blocked = setup(null, false, true);
assert.equal(blocked.api.set("dark"), false);
assert.equal(blocked.data.theme, "dark");
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", script, str(STATIC / "theme.js")],
            text=True, capture_output=True, timeout=30, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
