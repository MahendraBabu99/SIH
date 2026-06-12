/**
 * Unit tests for AIFT settings panel (settings.js).
 *
 * Covers:
 *  - openSettings / closeSettings visibility and aria
 *  - Settings tab switching (basic / advanced)
 *  - Removal of the retired CSV output directory setting
 *  - Provider field sync (show/hide API key, endpoint rows)
 *  - setProvider display updates
 *  - Focus trap setup
 *  - Background inert during modal
 *
 * @jest-environment jsdom
 */

"use strict";

const { setupAift, mustGet, mustQuery, flushMicrotasks } = require("./harness");

let A;

beforeEach(() => {
  A = setupAift();
});

function jsonResponse(payload, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name) => (String(name || "").toLowerCase() === "content-type" ? "application/json" : "") },
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  });
}

function flushPromises() {
  return flushMicrotasks();
}

// ── openSettings / closeSettings ────────────────────────────────────────────

describe("openSettings and closeSettings", () => {
  test("openSettings makes panel visible", () => {
    const panel = mustGet("settings-panel");
    panel.hidden = true;
    A.openSettings();
    expect(panel.hidden).toBe(false);
  });

  test("openSettings sets aria-expanded to true", () => {
    const button = mustGet("settings-button");
    A.openSettings();
    expect(button.getAttribute("aria-expanded")).toBe("true");
  });

  test("openSettings shows backdrop", () => {
    A.openSettings();
    expect(mustGet("settings-backdrop").hidden).toBe(false);
  });

  test("openSettings sets background inert", () => {
    A.openSettings();
    expect(mustQuery(document, "main#wizard").hasAttribute("inert")).toBe(true);
  });
});

describe("closeSettings", () => {
  test("hides settings panel", () => {
    const panel = mustGet("settings-panel");
    A.openSettings();
    // Simulate close - need to manually call since closeSettings is internal
    // but we can trigger via Escape key
    const event = new KeyboardEvent("keydown", { key: "Escape" });
    document.dispatchEvent(event);
    expect(panel.hidden).toBe(true);
  });

  test("hides backdrop on Escape", () => {
    A.openSettings();
    const event = new KeyboardEvent("keydown", { key: "Escape" });
    document.dispatchEvent(event);
    expect(mustGet("settings-backdrop").hidden).toBe(true);
  });

  test("removes background inert on close", () => {
    A.openSettings();
    const event = new KeyboardEvent("keydown", { key: "Escape" });
    document.dispatchEvent(event);
    expect(mustQuery(document, "main#wizard").hasAttribute("inert")).toBe(false);
  });

  test("sets aria-expanded to false on close", () => {
    const button = mustGet("settings-button");
    A.openSettings();
    const event = new KeyboardEvent("keydown", { key: "Escape" });
    document.dispatchEvent(event);
    expect(button.getAttribute("aria-expanded")).toBe("false");
  });
});

// ── Settings tab switching ──────────────────────────────────────────────────

describe("settings tab switching", () => {
  test("defaults to basic tab", () => {
    expect(A.st.settingsTab).toBe("basic");
  });

  test("switching tabs updates settingsTab state", () => {
    const advancedBtn = mustQuery(document, '[data-settings-tab="advanced"]');
    advancedBtn.click();
    expect(A.st.settingsTab).toBe("advanced");
  });

  test("active tab button has is-active class", () => {
    const basicBtn = mustQuery(document, '[data-settings-tab="basic"]');
    expect(basicBtn.classList.contains("is-active")).toBe(true);
  });

  test("active tab button has aria-selected true", () => {
    const basicBtn = mustQuery(document, '[data-settings-tab="basic"]');
    expect(basicBtn.getAttribute("aria-selected")).toBe("true");
  });

  test("inactive tab button has aria-selected false", () => {
    const advancedBtn = mustQuery(document, '[data-settings-tab="advanced"]');
    expect(advancedBtn.getAttribute("aria-selected")).toBe("false");
  });

  test("only active tab panel is visible", () => {
    const basicPanel = mustQuery(document, '[data-settings-panel="basic"]');
    const advancedPanel = mustQuery(document, '[data-settings-panel="advanced"]');
    expect(basicPanel.hidden).toBe(false);
    expect(advancedPanel.hidden).toBe(true);
  });
});

// ── Retired CSV output directory setting ───────────────────────────────────

describe("retired CSV output directory setting", () => {
  test("settings form has no CSV output directory field or help text", () => {
    expect(document.getElementById("setting-csv-output-dir")).toBeNull();
    expect(document.getElementById("setting-csv-output-help")).toBeNull();
  });

  test("no CSV output help updater is exported", () => {
    expect(A.updateCsvOutputHelp).toBeUndefined();
  });
});

// ── Provider field visibility ───────────────────────────────────────────────

describe("provider field visibility", () => {
  test("hides API key row when local provider is selected", () => {
    const provider = mustGet("setting-provider");
    provider.value = "local";
    provider.dispatchEvent(new Event("change"));
    expect(mustGet("setting-api-key").closest(".form-row").hidden).toBe(true);
  });

  test("shows API key row for anthropic provider", () => {
    const provider = mustGet("setting-provider");
    provider.value = "anthropic";
    provider.dispatchEvent(new Event("change"));
    expect(mustGet("setting-api-key").closest(".form-row").hidden).toBe(false);
  });

  test("shows endpoint row for local provider", () => {
    const provider = mustGet("setting-provider");
    provider.value = "local";
    provider.dispatchEvent(new Event("change"));
    expect(mustGet("setting-local-url").closest(".form-row").hidden).toBe(false);
  });

  test("shows endpoint row for kimi provider", () => {
    const provider = mustGet("setting-provider");
    provider.value = "kimi";
    provider.dispatchEvent(new Event("change"));
    expect(mustGet("setting-local-url").closest(".form-row").hidden).toBe(false);
  });

  test("hides endpoint row for openai provider", () => {
    const provider = mustGet("setting-provider");
    provider.value = "openai";
    provider.dispatchEvent(new Event("change"));
    expect(mustGet("setting-local-url").closest(".form-row").hidden).toBe(true);
  });

  test("updates API key label for anthropic", () => {
    const provider = mustGet("setting-provider");
    provider.value = "anthropic";
    provider.dispatchEvent(new Event("change"));
    expect(mustQuery(document, 'label[for="setting-api-key"]').textContent).toContain("Anthropic");
  });

  test("updates API key label for kimi", () => {
    const provider = mustGet("setting-provider");
    provider.value = "kimi";
    provider.dispatchEvent(new Event("change"));
    expect(mustQuery(document, 'label[for="setting-api-key"]').textContent).toContain("Moonshot");
  });

  test("updates model placeholder for each provider", () => {
    const provider = mustGet("setting-provider");
    const model = mustGet("setting-model");

    provider.value = "anthropic";
    provider.dispatchEvent(new Event("change"));
    expect(model.placeholder).toContain("claude");

    provider.value = "openai";
    provider.dispatchEvent(new Event("change"));
    expect(model.placeholder).toContain("gpt");

    provider.value = "local";
    provider.dispatchEvent(new Event("change"));
    expect(model.placeholder).toContain("llama");
  });
});

// ── Test Connection button ──────────────────────────────────────────────────

describe("test connection button", () => {
  test("test button is created during setup", () => {
    const btn = mustGet("test-connection");
    expect(btn.textContent).toBe("Test Connection");
  });

  test("test button is a regular button (not submit)", () => {
    expect(mustGet("test-connection").type).toBe("button");
  });

  test("form submit saves provider and analysis settings", async () => {
    const savedPayload = {
      ai: { provider: "openai", openai: { model: "gpt-test", api_key: "sk-test" } },
      analysis: { ai_max_tokens: 64000, artifact_csv_row_limit: 250 },
      automation: { run_retention_seconds: 172800 },
      evidence: { compute_hashes: true },
      server: { port: 5050 },
    };
    global.fetch = jest.fn(() => jsonResponse(savedPayload));

    mustGet("setting-provider").value = "openai";
    mustGet("setting-api-key").value = "sk-test";
    mustGet("setting-model").value = "gpt-test";
    mustGet("setting-ai-max-tokens").value = "64000";
    mustGet("setting-artifact-csv-row-limit").value = "250";
    mustGet("setting-automation-run-retention-seconds").value = "172800";
    mustGet("settings-form").dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
    await flushPromises();

    expect(global.fetch).toHaveBeenCalledWith("/api/settings", expect.objectContaining({ method: "POST" }));
    const body = JSON.parse(global.fetch.mock.calls[0][1].body);
    expect(body.ai.provider).toBe("openai");
    expect(body.ai.openai).toMatchObject({ api_key: "sk-test", model: "gpt-test" });
    expect(body.analysis).toMatchObject({ ai_max_tokens: 64000, artifact_csv_row_limit: 250 });
    expect(body.automation.run_retention_seconds).toBe(172800);
    expect(body.evidence.csv_output_dir).toBeUndefined();
    expect(A.el.settingsMsg.textContent).toContain("Settings saved");
  });

  test("connection test saves settings before checking provider", async () => {
    global.fetch = jest.fn((url) => {
      if (url === "/api/settings") {
        return jsonResponse({
          ai: { provider: "local", local: { base_url: "http://127.0.0.1:11434/v1", model: "llama-test" } },
          analysis: {},
          evidence: {},
          server: {},
        });
      }
      if (url === "/api/settings/test-connection") {
        return jsonResponse({ success: true, model_info: { provider: "local", model: "llama-test" } });
      }
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    const button = mustGet("test-connection");
    button.click();
    await flushPromises();
    await flushPromises();

    expect(global.fetch.mock.calls.map((call) => call[0])).toEqual([
      "/api/settings",
      "/api/settings/test-connection",
    ]);
    expect(A.el.settingsMsg.textContent).toContain("Connection test succeeded: Local (llama-test)");
    expect(button.disabled).toBe(false);
    expect(button.getAttribute("aria-busy")).toBeNull();
  });
});

describe("advanced CSV row limit setting", () => {
  test("artifact CSV row limit input is available and defaults to unlimited", () => {
    expect(A.el.setArtifactCsvRowLimit).not.toBeNull();
    expect(A.el.setArtifactCsvRowLimit.value).toBe("0");
    expect(A.el.setArtifactCsvRowLimit.getAttribute("min")).toBe("0");
  });

  test("artifact CSV row limit help describes forensic default and explicit cap", () => {
    const tooltip = mustQuery(document, 'label[for="setting-artifact-csv-row-limit"] .setting-help-icon');
    const help = mustGet("setting-artifact-csv-row-limit-help");
    expect(tooltip.dataset.tooltip).toContain("0 preserves all rows");
    expect(tooltip.dataset.tooltip).toContain("positive values intentionally cap parsed CSV output");
    expect(help.textContent).toContain("0 preserves all rows");
    expect(help.textContent).toContain("positive values intentionally cap parsed CSV output");
  });

  test("shortened prompt cutoff reads only the canonical setting key", async () => {
    global.fetch = jest.fn(() => jsonResponse({
      ai: { provider: "openai", openai: {} },
      analysis: { statistics_section_cutoff_tokens: 1234 },
      evidence: {},
      server: {},
    }));

    await A.loadSettings();

    expect(A.el.setShortenedPromptCutoffTokens.value).toBe("64000");
  });
});

describe("advanced automation retention setting", () => {
  test("automation retention input is available and defaults to 24 hours", () => {
    expect(A.el.setAutomationRunRetentionSeconds).not.toBeNull();
    expect(A.el.setAutomationRunRetentionSeconds.value).toBe("86400");
    expect(A.el.setAutomationRunRetentionSeconds.getAttribute("min")).toBe("60");
  });
});

describe("advanced evidence size threshold setting", () => {
  test("size threshold input accepts fractional GB values", () => {
    const input = mustGet("setting-size-threshold");
    expect(input.getAttribute("type")).toBe("number");
    expect(input.getAttribute("min")).toBe("0");
    expect(input.getAttribute("step")).toBe("any");
  });
});

// -- Help tooltips ------------------------------------------------------------

describe("help tooltips", () => {
  test("scan directory help appears on keyboard focus", () => {
    const help = mustQuery(document, ".evidence-scan-help");
    const tip = mustGet("setting-tooltip");

    help.dispatchEvent(new FocusEvent("focusin", { bubbles: true }));
    expect(tip.classList.contains("is-visible")).toBe(true);
    expect(tip.textContent).toContain("absolute local directory path");

    help.dispatchEvent(new FocusEvent("focusout", { bubbles: true }));
    expect(tip.classList.contains("is-visible")).toBe(false);
  });
});

// ── Settings panel initial state ────────────────────────────────────────────

describe("settings panel initial state", () => {
  test("settings panel is hidden on load", () => {
    expect(mustGet("settings-panel").hidden).toBe(true);
  });

  test("settings button exists", () => {
    expect(mustGet("settings-button")).toBe(A.el.settingsBtn);
  });
});
