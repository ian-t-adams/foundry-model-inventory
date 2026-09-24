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
    @unittest.skipUnless(shutil.which("node"), "Node is optional; required only for this browser-logic unit test")
    def test_collection_scope_keeps_other_tenants_and_their_profiles(self):
        script = r"""
const fs = require("node:fs");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const source = fs.readFileSync(process.argv[1], "utf8")
  .replace(/^import .*\r?\n/, "")
  .replace(/\r?\nboot\(\);\s*$/, "");
const context = {};
vm.runInNewContext(source, context);
const primary = {
  tenant_id: "tenant-a",
  azure_config_dir: "D:\\fixture\\data\\primary",
  subscriptions: [{ id: "sub-a", name: "First" }],
  morning_time: "07:00",
};
const added = context.collectionPayload(
  primary, "tenant-b", [{ id: "sub-b", name: "Second" }],
  "D:\\fixture\\data\\secondary", "08:00"
);
assert.deepEqual(JSON.parse(JSON.stringify(added.subscriptions)), [
  { id: "sub-a", name: "First" },
  { id: "sub-b", name: "Second", tenant_id: "tenant-b" },
]);
assert.equal(added.tenant_id, primary.tenant_id);
assert.equal(added.azure_config_dir, primary.azure_config_dir);
assert.equal(added.tenant_profiles["tenant-b"], "D:\\fixture\\data\\secondary");
assert.equal(added.morning_time, "08:00");
assert.equal(primary.subscriptions.length, 1);
assert.equal(context.collectionProfile(added, "tenant-b"), "D:\\fixture\\data\\secondary");
assert.equal(context.collectionProfile(added, "tenant-a"), primary.azure_config_dir);
const edited = context.collectionPayload(
  added, "tenant-a", [{ id: "sub-c", name: "Third" }], primary.azure_config_dir, "08:00"
);
assert.deepEqual(JSON.parse(JSON.stringify(edited.subscriptions)), [
  { id: "sub-b", name: "Second", tenant_id: "tenant-b" },
  { id: "sub-c", name: "Third" },
]);
assert.equal(edited.tenant_profiles["tenant-b"], "D:\\fixture\\data\\secondary");
assert.throws(() => context.collectionPayload(
  primary, "tenant-b", [{ id: "sub-b", name: "Second" }], "", "07:00"
), /private Azure CLI profile/);
const initial = context.collectionPayload(
  { tenant_id: "", subscriptions: [], morning_time: "07:00" },
  "tenant-new", [{ id: "sub-new", name: "New" }], "", "07:00"
);
assert.equal(initial.tenant_id, "tenant-new");
assert.equal(initial.subscriptions[0].tenant_id, undefined);
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", script, str(STATIC / "app.js")],
            text=True, capture_output=True, timeout=30, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node is optional; required only for this browser-logic unit test")
    def test_filter_rail_measures_the_longest_labels_every_filter_can_show(self):
        script = r"""
const fs = require("node:fs");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const source = fs.readFileSync(process.argv[1], "utf8")
  .replace(/^import .*\r?\n/, "")
  .replace(/\r?\nboot\(\);\s*$/, "");
const context = {};
vm.runInNewContext(source, context);
const tenant = "10000000-0000-4000-8000-000000000001";
const subscriptionId = "30000000-0000-4000-8000-000000000001";
const model = "Example-Model-With-A-Long-Descriptive-Name";
const facets = {
  tenant: [tenant],
  subscription: [{ value: subscriptionId, label: "Example subscription" }],
  region: ["example-region"],
  model: [model],
  model_version: [{ label: `${model} 2026-01-01`, model, version: "2026-01-01", format: "Example" }],
};
const choices = context.railChoices(facets);
const labels = choices.map((choice) => choice.label);
for (const expected of [tenant, "Example subscription", model, `${model} 2026-01-01`, "2026-01-01",
  "Choose a model first", "Has remaining quota"]) {
  assert.ok(labels.includes(expected), expected);
}
assert.ok(!labels.includes(subscriptionId), "subscriptions are shown by name");
assert.ok(choices.some((choice) => choice.label === "2026-01-01" && choice.description === `${model} · Example`));
const many = { region: Array.from({ length: 200 }, (_, index) => `region-${index}`), model: [model] };
const bounded = context.railChoices(many, 3);
assert.equal(bounded.length, 3);
assert.equal(bounded[0].label, model);
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", script, str(STATIC / "app.js")],
            text=True, capture_output=True, timeout=30, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_filter_rail_width_is_fitted_within_bounds_at_every_breakpoint(self):
        css = (STATIC / "styles.css").read_text(encoding="utf-8")
        columns = re.findall(r"\.workspace\s*\{[^}]*grid-template-columns:\s*([^;]+);", css)
        self.assertEqual(len(columns), 1, "breakpoints adjust the rail bounds instead of fixing its width")
        self.assertRegex(columns[0], r"^clamp\(var\(--rail-min\),.*var\(--rail-content\).*,\s*var\(--rail-max\)\)")
        self.assertRegex(css, r"--rail-max:\s*clamp\(224px,\s*100vw - var\(--rail-reserve\),\s*400px\)")
        self.assertRegex(css, r"\.filter-rail\s*\{[^}]*scrollbar-gutter:\s*stable")

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

    @unittest.skipUnless(shutil.which("node"), "Node is optional; required only for this browser-logic unit test")
    def test_hosted_read_only_view_and_notices(self):
        script = r"""
const fs = require("node:fs");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const source = fs.readFileSync(process.argv[1], "utf8")
  .replace(/^import .*\r?\n/, "")
  .replace(/\r?\nboot\(\);\s*$/, "");
const context = {};
vm.runInNewContext(source, context);
const status = {
  read_only: true, configured: true, scope_pending: false,
  config: { tenant_id: "tenant-a", morning_time: "07:00", subscriptions: [
    { id: "sub-b", name: "Second" }, { id: "sub-a", name: "First" }] },
  collection: { running: false, last_error: null },
  latest: { id: 4, started_at: "2026-09-23T12:00:00+00:00" },
  hosted: {
    commit: "0123456789abcdef0123456789abcdef01234567", timezone: "America/Chicago",
    collection_time: "07:00", next_run: "2026-09-24T07:00:00-05:00", user: "ada@example.test",
    last_attempt: { status: "failed", started_at: "2026-09-23T12:00:00+00:00", completed_at: "2026-09-23T12:01:00+00:00" },
    backup: { saved_at: "2026-09-23T12:02:00+00:00", error: null },
  },
};
const view = context.hostedView(status);
assert.equal(view.label, "Hosted · read-only");
assert.equal(view.schedule, "Daily at 07:00 · America/Chicago");
assert.match(view.explanation, /every morning at 07:00 \(America\/Chicago\)/);
assert.match(view.explanation, /not in the browser/);
assert.equal(view.commit, "0123456");
assert.equal(view.commitTitle, status.hosted.commit);
assert.equal(view.scope, "2 subscriptions in 1 tenant, collected by the hosted service.");
assert.equal(view.nextRun, "2026-09-24T07:00:00-05:00");
assert.deepEqual(JSON.parse(JSON.stringify(view.attempt)), { status: "failed", at: "2026-09-23T12:01:00+00:00" });
assert.equal(view.backup.savedAt, "2026-09-23T12:02:00+00:00");
assert.equal(view.user, "ada@example.test");
assert.equal(context.hostedView({ ...status, read_only: false }), null);
assert.equal(context.hostedView({ configured: true }), null);
const odd = context.hostedView({ read_only: true, hosted: { commit: "<b>", collection_time: "7am",
  backup: { error: "The latest database backup failed: share unavailable" } } });
assert.equal(odd.commit, "not recorded");
assert.equal(odd.schedule, "Daily at 07:00 · UTC");
assert.equal(odd.backup.error, "The latest database backup failed: share unavailable");
assert.equal(odd.attempt, null);
assert.equal(odd.scope, "0 subscriptions in 0 tenants, collected by the hosted service.");
const restored = context.hostedView({ read_only: true, hosted: { backup: { restored_at: "2026-09-23T12:03:00+00:00" } } });
assert.equal(restored.backup.restoredAt, "2026-09-23T12:03:00+00:00");

const now = Date.parse("2026-09-23T15:00:00Z");
assert.deepEqual(JSON.parse(JSON.stringify(context.statusNotice(status, now))), { message: "", type: "" });
const stale = context.statusNotice(status, Date.parse("2026-09-25T00:00:00Z"));
assert.equal(stale.type, "warning");
assert.match(stale.message, /hosted service's latest attempt/);
assert.doesNotMatch(stale.message, /Collect now/);
const pending = context.statusNotice({ ...status, scope_pending: true }, now);
assert.match(pending.message, /next hosted collection/);
const failed = context.statusNotice({ ...status, collection: { running: false,
  last_error: "Hosted collection could not sign in. It retries later." } }, now);
assert.equal(failed.type, "error");
assert.equal(failed.message, "The last collection needs attention: Hosted collection could not sign in. " +
  "It retries later. The latest complete snapshot is still available.");
const local = { ...status, read_only: undefined, hosted: undefined };
assert.match(context.statusNotice(local, Date.parse("2026-09-25T00:00:00Z")).message, /Collect now/);
assert.match(context.statusNotice({ ...local, configured: false }, now).message, /Choose your tenant/);
assert.match(context.statusNotice({ ...status, configured: false }, now).message, /Redeploy/);
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", script, str(STATIC / "app.js")],
            text=True, capture_output=True, timeout=30, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node is optional; required only for this browser-logic unit test")
    def test_saved_views_tolerate_unavailable_browser_storage(self):
        script = r"""
const fs = require("node:fs");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const source = fs.readFileSync(process.argv[1], "utf8")
  .replace(/^import .*\r?\n/, "")
  .replace(/\r?\nboot\(\);\s*$/, "");
function setup(saved, blocked = false) {
  const feedback = { hidden: true };
  const context = {
    document: { getElementById: (id) => { assert.equal(id, "toast"); return feedback; } },
    localStorage: {
      getItem: (key) => {
        assert.equal(key, "foundry-inventory.saved-views.v1");
        if (blocked) throw new Error("Access is denied.");
        return saved;
      },
    },
    setTimeout: () => 0,
    clearTimeout: () => {},
  };
  vm.runInNewContext(source, context);
  return { read: () => context.readSavedViews(), feedback };
}
const blocked = setup(null, true);
assert.equal(blocked.read().length, 0);
assert.equal(blocked.feedback.hidden, false);
assert.equal(blocked.feedback.className, "toast error");
assert.match(blocked.feedback.textContent, /browser storage/i);

const empty = setup(null);
assert.equal(empty.read().length, 0);
assert.equal(empty.feedback.hidden, true);
const saved = [{ name: "Example view", filters: { region: ["example-region"] }, view: "quota" }];
const valid = setup(JSON.stringify(saved));
assert.equal(JSON.stringify(valid.read()), JSON.stringify(saved));
assert.equal(valid.feedback.hidden, true);
const malformed = setup("{");
assert.equal(malformed.read().length, 0);
assert.equal(malformed.feedback.hidden, false);
assert.match(malformed.feedback.textContent, /invalid list/i);
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", script, str(STATIC / "app.js")],
            text=True, capture_output=True, timeout=30, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
