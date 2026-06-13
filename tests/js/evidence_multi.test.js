/**
 * Unit tests for AIFT multi-image evidence intake and per-image artifact
 * tab management (evidence_multi.js).
 *
 * Covers:
 *  - addImageForm / removeImageForm card management
 *  - submitEvidence prior-case state retirement and parse/analysis cancel
 *  - renderImageSummaries display for single and multiple images
 *  - buildMultiImageArtifactTabs with single and multiple images
 *  - OS-aware fieldset cloning (Windows-only, Linux-only, mixed)
 *  - switchArtifactTab tab activation
 *  - activeArtifactTabImageId returns correct ID
 *  - selectedArtifactOptionsForImage collects checked artifacts
 *  - allImageArtifactSelections aggregates per-image selections
 *  - isMultiImage reflects image count
 *  - applyRecommendedToAllImages applies preset across all panels
 *  - applyCurrentSelectionToAllImages mirrors active tab to others
 *  - applyPresetMultiAware dispatches to correct handler
 *
 * @jest-environment jsdom
 */

"use strict";

const { setupAift, mustGet, mustQuery, flushMicrotasks } = require("./harness");

let A;

beforeEach(() => {
  A = setupAift();
});

/**
 * Helper: populate st.images with the given image entries and build tabs.
 *
 * @param {Object[]} images - Array of image entry objects.
 */
function setImagesAndBuildTabs(images) {
  A.st.images = images;
  /* Populate artifacts so the main form is aware of all artifact keys. */
  const allArts = [];
  for (const img of images) {
    for (const a of img.available_artifacts || []) {
      if (!allArts.find((x) => x.key === a.key)) allArts.push(Object.assign({}, a));
    }
  }
  A.st.artifacts = allArts;
  A.buildMultiImageArtifactTabs();
}

/**
 * Helper: create a standard two-image Windows+Linux setup.
 *
 * @returns {Object[]} Array of two image entries.
 */
function makeWindowsLinuxImages() {
  return [
    {
      image_id: "img-win",
      label: "Windows PC",
      os_type: "windows",
      metadata: { hostname: "WIN-PC", os_version: "Windows 10" },
      hashes: { sha256: "abc123" },
      available_artifacts: [
        { key: "runkeys", name: "Run/RunOnce Keys", available: true },
        { key: "shimcache", name: "Shimcache", available: true },
        { key: "evtx", name: "Event Logs", available: true },
        { key: "mft", name: "MFT", available: true },
      ],
    },
    {
      image_id: "img-linux",
      label: "Linux Server",
      os_type: "linux",
      metadata: { hostname: "SRV-01", os_version: "Ubuntu 22.04" },
      hashes: { sha256: "def456" },
      available_artifacts: [
        { key: "cronjobs", name: "Cron Jobs", available: true },
        { key: "bash_history", name: "Bash History", available: true },
        { key: "services", name: "Systemd Services", available: true },
      ],
    },
  ];
}

/**
 * Helper: create a mixed-OS setup where both OSes expose the same artifact key.
 *
 * @returns {Object[]} Array of two image entries.
 */
function makeWindowsLinuxServiceImages() {
  return [
    {
      image_id: "img-win",
      label: "Windows PC",
      os_type: "windows",
      metadata: { hostname: "WIN-PC" },
      hashes: {},
      available_artifacts: [
        { key: "services", name: "Services", available: true },
        { key: "runkeys", name: "Run/RunOnce Keys", available: true },
      ],
    },
    {
      image_id: "img-linux",
      label: "Linux Server",
      os_type: "linux",
      metadata: { hostname: "SRV-01" },
      hashes: {},
      available_artifacts: [
        { key: "services", name: "Systemd Services", available: true },
        { key: "cronjobs", name: "Cron Jobs", available: true },
      ],
    },
  ];
}

/**
 * Helper: create a two-Windows-image setup.
 *
 * @returns {Object[]} Array of two image entries.
 */
function makeTwoWindowsImages() {
  return [
    {
      image_id: "img-w1",
      label: "Workstation 1",
      os_type: "windows",
      metadata: { hostname: "WS-01" },
      hashes: {},
      available_artifacts: [
        { key: "runkeys", name: "Run/RunOnce Keys", available: true },
        { key: "shimcache", name: "Shimcache", available: true },
        { key: "prefetch", name: "Prefetch", available: false },
      ],
    },
    {
      image_id: "img-w2",
      label: "Workstation 2",
      os_type: "windows",
      metadata: { hostname: "WS-02" },
      hashes: {},
      available_artifacts: [
        { key: "runkeys", name: "Run/RunOnce Keys", available: true },
        { key: "shimcache", name: "Shimcache", available: false },
        { key: "prefetch", name: "Prefetch", available: true },
      ],
    },
  ];
}

// ── addImageForm / removeImageForm ──────────────────────────────────────────

describe("addImageForm", () => {
  test("adds a new image form card to the container", () => {
    const before = A.getImageForms().length;
    A.addImageForm();
    expect(A.getImageForms().length).toBe(before + 1);
  });

  test("each added card has the required child elements", () => {
    A.addImageForm();
    const cards = A.getImageForms();
    const last = cards[cards.length - 1];
    expect(last.querySelector(".image-form-title")).not.toBeNull();
    expect(last.querySelector(".image-label-input")).not.toBeNull();
    expect(last.querySelector(".image-mode-upload")).not.toBeNull();
    expect(last.querySelector(".image-mode-path")).not.toBeNull();
    expect(last.querySelector(".image-path-input")).not.toBeNull();
    expect(last.querySelector(".image-file-input")).not.toBeNull();
    expect(last.querySelector(".image-metadata-card")).not.toBeNull();
  });

  test("renumbers titles after adding", () => {
    A.addImageForm();
    A.addImageForm();
    const cards = A.getImageForms();
    const titles = Array.from(cards).map((c) => c.querySelector(".image-form-title").textContent);
    titles.forEach((t, i) => {
      expect(t).toBe(`Image ${i + 1}`);
    });
  });
});

describe("removeImageForm", () => {
  test("removes a card when multiple exist", () => {
    A.addImageForm();
    const cards = A.getImageForms();
    const countBefore = cards.length;
    expect(countBefore).toBeGreaterThanOrEqual(2);
    A.removeImageForm(cards[cards.length - 1]);
    expect(A.getImageForms().length).toBe(countBefore - 1);
  });

  test("does not remove the last remaining card", () => {
    const cards = A.getImageForms();
    /* Remove extras until only one remains. */
    while (A.getImageForms().length > 1) {
      A.removeImageForm(A.getImageForms()[A.getImageForms().length - 1]);
    }
    expect(A.getImageForms().length).toBe(1);
    A.removeImageForm(A.getImageForms()[0]);
    expect(A.getImageForms().length).toBe(1);
  });

  test("renumbers titles after removing", () => {
    A.addImageForm();
    A.addImageForm();
    const cards = A.getImageForms();
    /* Remove the middle card. */
    A.removeImageForm(cards[1]);
    const remaining = A.getImageForms();
    remaining.forEach((c, i) => {
      expect(c.querySelector(".image-form-title").textContent).toBe(`Image ${i + 1}`);
    });
  });
});

// -- scanEvidenceDirectory -------------------------------------------------

describe("scanEvidenceDirectory", () => {
  function deferred() {
    let resolve;
    let reject;
    const promise = new Promise((res, rej) => {
      resolve = res;
      reject = rej;
    });
    return { promise, resolve, reject };
  }

  function mockJsonFetch(payload) {
    global.fetch = jest.fn(() => Promise.resolve({
      ok: true,
      status: 200,
      headers: { get: () => "application/json" },
      json: async () => payload,
      text: async () => JSON.stringify(payload),
    }));
  }

  test("renders a visible standalone scan path panel", () => {
    const panel = document.getElementById("scan-directory-panel");
    const pathInput = document.getElementById("scan-directory-path");
    const scanButton = document.getElementById("scan-directory-btn");

    expect(panel).not.toBeNull();
    expect(pathInput).not.toBeNull();
    expect(scanButton).not.toBeNull();
  });

  test("populates one local-path form per backend-discovered evidence target", async () => {
    const pathInput = document.getElementById("scan-directory-path");
    pathInput.value = "E:\\AIFT-Public2\\AIFT\\test_data\\Small_evidence_folder";
    mockJsonFetch({
      success: true,
      evidence: [
        { path: "E:\\data\\SUSPECT.E01", label: "SUSPECT" },
        { path: "E:\\data\\Unzipped_C_Drive (UNZIPPED KAPE OUTPUT)\\C", label: "C" },
      ],
    });

    await A.scanEvidenceDirectory();

    expect(global.fetch).toHaveBeenCalled();
    expect(global.fetch.mock.calls[0][0]).toBe("/api/evidence/discover");
    const body = JSON.parse(global.fetch.mock.calls[0][1].body);
    expect(body.path).toBe("E:\\AIFT-Public2\\AIFT\\test_data\\Small_evidence_folder");

    const forms = A.getImageForms();
    expect(forms.length).toBe(2);
    expect(forms[0].querySelector(".image-label-input").value).toBe("SUSPECT");
    expect(forms[0].querySelector(".image-mode-path").checked).toBe(true);
    expect(forms[0].querySelector(".image-path-input").value).toBe("E:\\data\\SUSPECT.E01");
    expect(forms[1].querySelector(".image-label-input").value).toBe("C");
    expect(forms[1].querySelector(".image-mode-path").checked).toBe(true);
    expect(forms[1].querySelector(".image-path-input").value).toContain("Unzipped_C_Drive");

    const panelMsg = document.getElementById("scan-directory-message");
    const results = document.getElementById("scan-directory-results");
    expect(panelMsg.hidden).toBe(false);
    expect(panelMsg.dataset.status).toBe("success");
    expect(panelMsg.textContent).toContain("Found 2 evidence targets");
    expect(results.hidden).toBe(false);
    expect(results.querySelectorAll("li").length).toBe(2);
    expect(results.textContent).toContain("SUSPECT");
  });

  test("submits preserved discovery descriptor fields for archive fallback targets", async () => {
    const pathInput = document.getElementById("scan-directory-path");
    pathInput.value = "E:\\evidence\\archives";
    const descriptor = {
      path: "E:\\AIFT\\cases\\_managed_discovery\\discovery_abc\\extracted_bundle_0001\\nested\\SUSPECT.E01",
      dissect_path: "E:\\AIFT\\cases\\_managed_discovery\\discovery_abc\\extracted_bundle_0001\\nested\\SUSPECT.E01",
      source_path: "E:\\evidence\\bundle.zip",
      label: "SUSPECT",
      source_mode: "path",
      files_to_hash: ["E:\\evidence\\bundle.zip"],
      extracted_from: "E:\\evidence\\bundle.zip",
      extraction_root: "E:\\AIFT\\cases\\_managed_discovery\\discovery_abc\\extracted_bundle_0001",
    };

    global.fetch = jest
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: { get: () => "application/json" },
        json: async () => ({ success: true, evidence: [descriptor] }),
        text: async () => JSON.stringify({ success: true, evidence: [descriptor] }),
      })
      .mockResolvedValueOnce({
        ok: true,
        status: 201,
        headers: { get: () => "application/json" },
        json: async () => ({ success: true, case_id: "case-1", case_name: "Case 1" }),
        text: async () => JSON.stringify({ success: true, case_id: "case-1", case_name: "Case 1" }),
      })
      .mockResolvedValueOnce({
        ok: true,
        status: 201,
        headers: { get: () => "application/json" },
        json: async () => ({ success: true, image_id: "image-1", label: "SUSPECT" }),
        text: async () => JSON.stringify({ success: true, image_id: "image-1", label: "SUSPECT" }),
      })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        headers: { get: () => "application/json" },
        json: async () => ({
          success: true,
          metadata: { hostname: "host" },
          hashes: {},
          os_type: "windows",
          available_artifacts: [],
        }),
        text: async () => JSON.stringify({ success: true, metadata: {}, hashes: {}, os_type: "windows", available_artifacts: [] }),
      });

    await A.scanEvidenceDirectory();
    await A.submitEvidence();

    const evidenceRequest = global.fetch.mock.calls.find((call) => (
      String(call[0]).includes("/images/") && String(call[0]).includes("/evidence")
    ));
    expect(evidenceRequest).toBeTruthy();
    const body = JSON.parse(evidenceRequest[1].body);
    expect(body.path).toBe(descriptor.path);
    expect(body.evidence_descriptor).toMatchObject({
      dissect_path: descriptor.dissect_path,
      source_path: descriptor.source_path,
      source_mode: "path",
      extracted_from: descriptor.extracted_from,
      extraction_root: descriptor.extraction_root,
    });
    expect(body.evidence_descriptor.files_to_hash).toEqual(["E:\\evidence\\bundle.zip"]);
  });

  test("shows an error when no scan path is entered", async () => {
    global.fetch = jest.fn();
    await A.scanEvidenceDirectory();

    expect(global.fetch).not.toHaveBeenCalled();
    const msg = document.getElementById("scan-directory-message");
    expect(msg.hidden).toBe(false);
    expect(msg.dataset.status).toBe("failed");
    expect(msg.textContent).toContain("local directory path");
  });

  test("shows an error when the backend finds no targets", async () => {
    const pathInput = document.getElementById("scan-directory-path");
    pathInput.value = "E:\\empty";
    mockJsonFetch({ success: true, evidence: [] });

    await A.scanEvidenceDirectory();

    const msg = document.getElementById("evidence-message");
    const dialogMsg = document.getElementById("scan-directory-message");
    expect(msg.hidden).toBe(true);
    expect(dialogMsg.hidden).toBe(false);
    expect(dialogMsg.dataset.status).toBe("failed");
    expect(dialogMsg.textContent).toContain("No supported evidence targets");
  });

  test("surfaces backend warnings for archives skipped during the scan", async () => {
    const pathInput = document.getElementById("scan-directory-path");
    pathInput.value = "E:\\evidence";
    mockJsonFetch({
      success: true,
      evidence: [{ path: "E:\\evidence\\disk.E01", label: "disk" }],
      warnings: [
        "Skipped archive 'corrupt.zip' during evidence discovery: Invalid ZIP evidence file: corrupt.zip",
      ],
    });

    await A.scanEvidenceDirectory();

    const panelMsg = document.getElementById("scan-directory-message");
    expect(panelMsg.hidden).toBe(false);
    expect(panelMsg.dataset.status).toBe("warning");
    expect(panelMsg.textContent).toContain("Found 1 evidence target");
    expect(panelMsg.textContent).toContain("corrupt.zip");
    const evidenceMsg = document.getElementById("evidence-message");
    expect(evidenceMsg.hidden).toBe(false);
    expect(evidenceMsg.dataset.status).toBe("warning");
    expect(evidenceMsg.textContent).toContain("corrupt.zip");
    expect(A.getImageForms()).toHaveLength(1);
    expect(A.getImageForms()[0].querySelector(".image-path-input").value)
      .toBe("E:\\evidence\\disk.E01");
  });

  test("includes backend warnings when every target was skipped", async () => {
    const pathInput = document.getElementById("scan-directory-path");
    pathInput.value = "E:\\evidence";
    mockJsonFetch({
      success: true,
      evidence: [],
      warnings: [
        "Skipped archive 'corrupt.zip' during evidence discovery: Invalid ZIP evidence file: corrupt.zip",
      ],
    });

    await A.scanEvidenceDirectory();

    const dialogMsg = document.getElementById("scan-directory-message");
    expect(dialogMsg.hidden).toBe(false);
    expect(dialogMsg.dataset.status).toBe("failed");
    expect(dialogMsg.textContent).toContain("No supported evidence targets");
    expect(dialogMsg.textContent).toContain("corrupt.zip");
  });

  test("ignores a directory scan response after reset", async () => {
    const pathInput = document.getElementById("scan-directory-path");
    pathInput.value = "E:\\cases";
    const scan = deferred();
    global.fetch = jest.fn(() => scan.promise);

    const scanPromise = A.scanEvidenceDirectory();
    A.resetCaseUi();
    scan.resolve({
      ok: true,
      status: 200,
      headers: { get: () => "application/json" },
      json: async () => ({ success: true, evidence: [{ path: "E:\\cases\\late.E01", label: "late" }] }),
      text: async () => JSON.stringify({ success: true, evidence: [{ path: "E:\\cases\\late.E01", label: "late" }] }),
    });
    await scanPromise;

    expect(A.getImageForms()).toHaveLength(1);
    expect(A.getImageForms()[0].querySelector(".image-path-input").value).toBe("");
    expect(document.getElementById("scan-directory-results").hidden).toBe(true);
  });
});

describe("submitEvidence stale operation handling", () => {
  function deferred() {
    let resolve;
    let reject;
    const promise = new Promise((res, rej) => {
      resolve = res;
      reject = rej;
    });
    return { promise, resolve, reject };
  }

  function jsonResponse(payload, status = 200) {
    return Promise.resolve({
      ok: status >= 200 && status < 300,
      status,
      headers: { get: (name) => String(name || "").toLowerCase() === "content-type" ? "application/json" : "" },
      json: async () => payload,
      text: async () => JSON.stringify(payload),
    });
  }

  function configurePathImageForms(paths) {
    while (A.getImageForms().length < paths.length) A.addImageForm();
    A.getImageForms().forEach((card, index) => {
      card.querySelector(".image-mode-path").checked = true;
      card.querySelector(".image-mode-upload").checked = false;
      card.querySelector(".image-path-input").value = paths[index] || "";
      const label = card.querySelector(".image-label-input");
      if (label) label.value = `Image ${index + 1}`;
    });
  }

  function seedUsableOldMultiImageState() {
    A.setCaseId("old-case");
    setImagesAndBuildTabs(makeTwoWindowsImages());
    A.renderImageSummaries(A.st.images);
    A.applyRecommendedToAllImages();
    A.st.parsedSelections = {
      caseId: "old-case",
      runId: "old-parse",
      mode: "multi",
      artifactOptions: [],
      artifacts: ["runkeys"],
      aiArtifacts: ["runkeys"],
      images: { "img-w1": { image_id: "img-w1", artifacts: ["runkeys"], aiArtifacts: ["runkeys"] } },
    };
    A.st.chat.allMessages = [{ role: "user", content: "old question" }];
    A.showStep(1);
  }

  test("does not set an old case id when create-case response resolves after reset", async () => {
    const card = A.getImageForms()[0];
    card.querySelector(".image-mode-path").checked = true;
    card.querySelector(".image-mode-upload").checked = false;
    card.querySelector(".image-path-input").value = "E:\\evidence\\disk.E01";

    const createCase = deferred();
    global.fetch = jest.fn(() => createCase.promise);

    const submitPromise = A.submitEvidence();
    A.resetCaseUi();
    createCase.resolve({
      ok: true,
      status: 200,
      headers: { get: () => "application/json" },
      json: async () => ({ case_id: "late-case", case_name: "Late Case" }),
      text: async () => JSON.stringify({ case_id: "late-case", case_name: "Late Case" }),
    });
    await submitPromise;

    expect(A.activeCaseId()).toBe("");
    expect(A.st.images).toEqual([]);
    expect(A.el.submitEvidence.disabled).toBe(false);
  });

  test("a second evidence submit retires a slower first submit", async () => {
    const card = A.getImageForms()[0];
    card.querySelector(".image-mode-path").checked = true;
    card.querySelector(".image-mode-upload").checked = false;
    card.querySelector(".image-path-input").value = "E:\\evidence\\disk.E01";

    const first = deferred();
    const second = deferred();
    global.fetch = jest
      .fn()
      .mockImplementationOnce(() => first.promise)
      .mockImplementationOnce(() => second.promise)
      .mockImplementationOnce(() => jsonResponse({ success: true, image_id: "second-image", label: "Image 1" }, 201))
      .mockImplementationOnce(() => jsonResponse({
        success: true,
        metadata: { hostname: "SECOND", os_version: "Windows 10" },
        hashes: {},
        os_type: "windows",
        available_artifacts: [],
      }));

    const firstSubmit = A.submitEvidence();
    const secondSubmit = A.submitEvidence();
    first.resolve({
      ok: true,
      status: 200,
      headers: { get: () => "application/json" },
      json: async () => ({ case_id: "first-case", case_name: "First" }),
      text: async () => JSON.stringify({ case_id: "first-case", case_name: "First" }),
    });
    second.resolve({
      ok: true,
      status: 200,
      headers: { get: () => "application/json" },
      json: async () => ({ case_id: "second-case", case_name: "Second" }),
      text: async () => JSON.stringify({ case_id: "second-case", case_name: "Second" }),
    });

    await Promise.all([firstSubmit, secondSubmit]);

    expect(A.activeCaseId()).toBe("second-case");
  });

  test("clears allocated case and stale artifact UI when the first image evidence request fails", async () => {
    seedUsableOldMultiImageState();
    configurePathImageForms(["E:\\evidence\\broken.E01"]);

    global.fetch = jest.fn((url) => {
      if (url === "/api/cases") {
        return jsonResponse({ success: true, case_id: "case-incomplete", case_name: "Incomplete" }, 201);
      }
      if (url === "/api/cases/case-incomplete/images") {
        return jsonResponse({ success: true, image_id: "img-1", label: "Image 1" }, 201);
      }
      if (url === "/api/cases/case-incomplete/images/img-1/evidence") {
        return jsonResponse({ success: false, error: "image intake failed" }, 500);
      }
      return jsonResponse({ success: true });
    });

    await A.submitEvidence();

    expect(A.activeCaseId()).toBe("");
    expect(A.st.step).toBe(1);
    expect(document.getElementById("evidence-loaded-banner").hidden).toBe(true);
    expect(A.el.evidenceMsg.hidden).toBe(false);
    expect(A.el.evidenceMsg.dataset.status).toBe("failed");
    expect(A.el.evidenceMsg.textContent).toContain("image intake failed");
    expect(document.querySelector(".wizard-nav button[data-next-step='2']").disabled).toBe(true);
    expect(document.getElementById("indicator-artifacts").classList.contains("is-disabled")).toBe(true);
    expect(A.st.images).toEqual([]);
    expect(A.st.selected).toEqual([]);
    expect(A.st.selectedAi).toEqual([]);
    expect(A.st.parsedSelections).toMatchObject({ caseId: "", runId: "", mode: "", images: {} });
    expect(A.st.chat.allMessages).toEqual([]);
    expect(document.getElementById("artifact-image-tabs").hidden).toBe(true);
    expect(document.getElementById("artifact-image-panels").children).toHaveLength(0);
    expect(document.getElementById("evidence-summaries-container").hidden).toBe(true);
    expect(document.getElementById("evidence-summaries-list").children).toHaveLength(0);
  });

  test("clears partial image state when a later image evidence request fails", async () => {
    seedUsableOldMultiImageState();
    configurePathImageForms(["E:\\evidence\\first.E01", "E:\\evidence\\second.E01"]);

    global.fetch = jest.fn((url) => {
      if (url === "/api/cases") {
        return jsonResponse({ success: true, case_id: "case-partial", case_name: "Partial" }, 201);
      }
      if (url === "/api/cases/case-partial/images") {
        const imageCallCount = global.fetch.mock.calls.filter((call) => String(call[0]).endsWith("/images")).length;
        return jsonResponse({
          success: true,
          image_id: imageCallCount === 1 ? "img-1" : "img-2",
          label: imageCallCount === 1 ? "First" : "Second",
        }, 201);
      }
      if (url === "/api/cases/case-partial/images/img-1/evidence") {
        return jsonResponse({
          success: true,
          metadata: { hostname: "FIRST", os_version: "Windows 10" },
          hashes: { sha256: "firsthash" },
          os_type: "windows",
          available_artifacts: [{ key: "runkeys", name: "Run/RunOnce Keys", available: true }],
        });
      }
      if (url === "/api/cases/case-partial/images/img-2/evidence") {
        return jsonResponse({ success: false, error: "second image failed" }, 500);
      }
      return jsonResponse({ success: true });
    });

    await A.submitEvidence();

    expect(A.activeCaseId()).toBe("");
    expect(A.st.step).toBe(1);
    expect(A.el.evidenceMsg.hidden).toBe(false);
    expect(A.el.evidenceMsg.textContent).toContain("second image failed");
    expect(document.querySelector(".wizard-nav button[data-next-step='2']").disabled).toBe(true);
    expect(A.st.images).toEqual([]);
    expect(A.st.artifacts).toEqual([]);
    expect(A.st.parsedSelections).toMatchObject({ caseId: "", runId: "", mode: "", images: {} });
    expect(document.getElementById("artifact-image-tabs").hidden).toBe(true);
    expect(document.getElementById("artifact-image-panels").children).toHaveLength(0);
    expect(document.getElementById("evidence-summaries-container").hidden).toBe(true);
    A.getImageForms().forEach((card) => {
      expect(card.querySelector(".image-metadata-card").hidden).toBe(true);
      expect(card.querySelector(".image-status-msg").hidden).toBe(true);
    });
  });

  test("does not clear state for a different active case when a failed intake loses ownership", async () => {
    seedUsableOldMultiImageState();
    configurePathImageForms(["E:\\evidence\\broken.E01"]);
    let currentImages = [];
    let currentParsedSelections = null;

    global.fetch = jest.fn((url) => {
      if (url === "/api/cases") {
        return jsonResponse({ success: true, case_id: "case-stale-failure", case_name: "Stale" }, 201);
      }
      if (url === "/api/cases/case-stale-failure/images") {
        return jsonResponse({ success: true, image_id: "img-1", label: "Image 1" }, 201);
      }
      if (url === "/api/cases/case-stale-failure/images/img-1/evidence") {
        A.setCaseId("other-active-case");
        setImagesAndBuildTabs(makeTwoWindowsImages());
        A.renderImageSummaries(A.st.images);
        A.st.parsedSelections = {
          caseId: "other-active-case",
          runId: "other-parse",
          mode: "multi",
          artifactOptions: [],
          artifacts: ["prefetch"],
          aiArtifacts: ["prefetch"],
          images: { "img-w2": { image_id: "img-w2", artifacts: ["prefetch"], aiArtifacts: ["prefetch"] } },
        };
        A.st.chat.allMessages = [{ role: "user", content: "current question" }];
        currentImages = A.st.images.slice();
        currentParsedSelections = A.st.parsedSelections;
        return jsonResponse({ success: false, error: "stale failure" }, 500);
      }
      return jsonResponse({ success: true });
    });

    await A.submitEvidence();

    expect(A.activeCaseId()).toBe("other-active-case");
    expect(A.st.step).toBe(1);
    expect(A.el.evidenceMsg.textContent).not.toContain("stale failure");
    expect(A.st.images).toEqual(currentImages);
    expect(A.st.parsedSelections).toBe(currentParsedSelections);
    expect(A.st.chat.allMessages).toEqual([{ role: "user", content: "current question" }]);
    expect(document.getElementById("artifact-image-tabs").hidden).toBe(false);
    expect(document.getElementById("artifact-image-panels").children.length).toBeGreaterThan(0);
  });

  test("retires the prior case's parse/analysis/chat state as soon as the new case is committed", async () => {
    /* Seed a fully completed prior case whose results are still on screen. */
    A.setCaseId("old-case");
    A.st.images = [{ image_id: "img-old", label: "Old", os_type: "windows", available_artifacts: [] }];
    A.st.selected = ["runkeys"];
    A.st.selectedAi = ["runkeys"];
    A.st.parse.done = true;
    A.st.analysis.done = true;
    A.st.parsedSelections = {
      caseId: "old-case",
      runId: "old-parse",
      mode: "single",
      artifactOptions: [{ artifact_key: "runkeys", mode: A.MODE_PARSE_AND_AI }],
      artifacts: ["runkeys"],
      aiArtifacts: ["runkeys"],
      images: {},
    };
    A.st.chat.allMessages = [{ role: "user", content: "old question" }];
    const staleResult = document.createElement("li");
    staleResult.textContent = "Old case finding";
    document.getElementById("analysis-results-list").appendChild(staleResult);
    A.updateNav();
    const step4Btn = document.querySelector(".wizard-nav button[data-next-step='4']");
    expect(step4Btn.disabled).toBe(false);

    configurePathImageForms(["E:\\evidence\\new.E01"]);
    const evidenceRequest = deferred();
    global.fetch = jest.fn((url) => {
      if (url === "/api/cases") {
        return jsonResponse({ success: true, case_id: "case-new", case_name: "New" }, 201);
      }
      if (url === "/api/cases/case-new/images") {
        return jsonResponse({ success: true, image_id: "img-1", label: "Image 1" }, 201);
      }
      if (url === "/api/cases/case-new/images/img-1/evidence") {
        return evidenceRequest.promise;
      }
      return jsonResponse({ success: true });
    });

    const submitPromise = A.submitEvidence();
    await flushMicrotasks(50);

    /* The intake is still mid-flight: the case is committed but the first
       image's evidence request has not resolved. The prior case's state
       must already be retired. */
    expect(A.activeCaseId()).toBe("case-new");
    expect(A.st.parse.done).toBe(false);
    expect(A.st.selected).toEqual([]);
    expect(A.st.selectedAi).toEqual([]);
    expect(A.st.analysis.done).toBe(false);
    expect(A.st.parsedSelections).toMatchObject({ caseId: "", runId: "", mode: "" });
    expect(A.st.chat.allMessages).toEqual([]);
    expect(step4Btn.disabled).toBe(true);
    expect(document.getElementById("analysis-results-list").textContent).not.toContain("Old case finding");
    /* The in-progress intake UI must stay live for the user. */
    expect(A.el.evidenceProgWrap.hidden).toBe(false);

    evidenceRequest.resolve({
      ok: true,
      status: 200,
      headers: { get: () => "application/json" },
      json: async () => ({
        success: true,
        metadata: { hostname: "NEW-PC", os_version: "Windows 11" },
        hashes: {},
        os_type: "windows",
        available_artifacts: [],
      }),
      text: async () => "{}",
    });
    await submitPromise;

    expect(A.activeCaseId()).toBe("case-new");
    expect(A.st.images.map((img) => img.image_id)).toEqual(["img-1"]);
  });

  test("cancels a running prior-case parse against the old case id and closes its stream", async () => {
    /* Seed a parse that is still running for the previous case, with an
       open progress EventSource. */
    A.setCaseId("old-case");
    A.st.images = [{ image_id: "img-old", label: "Old", os_type: "windows", available_artifacts: [] }];
    A.st.selected = ["evtx"];
    A.st.parse.owner = A.newRunOwner("old-case", "parse");
    A.st.parse.imageId = "img-old";
    A.st.parse.run = true;
    A.openSseStream("/api/cases/old-case/images/img-old/parse/progress", A.st.parse, { onEvent: () => {} });
    const oldParseEs = A.st.parse.es;
    expect(oldParseEs).toBeTruthy();

    configurePathImageForms(["E:\\evidence\\new.E01"]);
    global.fetch = jest.fn((url) => {
      if (url === "/api/cases/old-case/parse/cancel") {
        return jsonResponse({ success: true });
      }
      if (url === "/api/cases") {
        return jsonResponse({ success: true, case_id: "case-new", case_name: "New" }, 201);
      }
      if (url === "/api/cases/case-new/images") {
        return jsonResponse({ success: true, image_id: "img-1", label: "Image 1" }, 201);
      }
      if (url === "/api/cases/case-new/images/img-1/evidence") {
        return jsonResponse({
          success: true,
          metadata: { hostname: "NEW-PC", os_version: "Windows 11" },
          hashes: {},
          os_type: "windows",
          available_artifacts: [],
        });
      }
      return jsonResponse({ success: true });
    });

    await A.submitEvidence();

    const urls = global.fetch.mock.calls.map((call) => String(call[0]));
    const cancelIndex = urls.indexOf("/api/cases/old-case/parse/cancel");
    const createIndex = urls.indexOf("/api/cases");
    /* The cancel must target the OLD case and precede case creation. */
    expect(cancelIndex).toBeGreaterThanOrEqual(0);
    expect(global.fetch.mock.calls[cancelIndex][1].method).toBe("POST");
    expect(createIndex).toBeGreaterThan(cancelIndex);
    expect(oldParseEs.close).toHaveBeenCalled();
    expect(A.st.parse.es).toBeNull();
    expect(A.st.parse.run).toBe(false);
    expect(A.activeCaseId()).toBe("case-new");
    /* The stale "Parsing cancelled." message must not linger. */
    expect(A.el.parseErr.hidden).toBe(true);
  });
});

// ── isMultiImage ────────────────────────────────────────────────────────────

describe("case reset UI boundaries", () => {
  test("clears unsupported evidence state and restores artifact selection", () => {
    A.applyEvidence({
      os_type: "windows",
      metadata: { hostname: "UNKNOWN", os_version: "unknown" },
      hashes: {},
      available_artifacts: [],
    });

    const unsupported = document.getElementById("unsupported-evidence-error");
    const artifactContent = document.getElementById("artifact-selection-content");
    expect(unsupported.hidden).toBe(false);
    expect(artifactContent.hidden).toBe(true);

    A.resetCaseUi();

    expect(unsupported.hidden).toBe(true);
    expect(document.getElementById("unsupported-evidence-hint").hidden).toBe(true);
    expect(artifactContent.hidden).toBe(false);
    expect(document.getElementById("artifact-image-tabs").hidden).toBe(true);
    expect(document.getElementById("artifact-image-panels").children).toHaveLength(0);
    expect(A.el.chatThread.textContent).toContain("Chat history will appear here");
  });

  test("returns multi-image UI and state to a single empty image form", () => {
    A.addImageForm();
    const forms = A.getImageForms();
    forms[0].querySelector(".image-label-input").value = "First";
    forms[0].querySelector(".image-path-input").value = "E:\\evidence\\first.E01";
    forms[1].querySelector(".image-label-input").value = "Second";
    forms[1].querySelector(".image-path-input").value = "E:\\evidence\\second.E01";
    forms[0].querySelector(".image-status-msg").hidden = false;
    forms[0].querySelector(".image-status-msg").textContent = "Evidence loaded.";
    forms[0].querySelector(".image-status-msg").dataset.status = "success";

    setImagesAndBuildTabs(makeTwoWindowsImages());
    A.applyRecommendedToAllImages();
    A.st.parsedSelections = {
      caseId: "case-old",
      runId: "parse-old",
      mode: "multi",
      artifactOptions: [],
      artifacts: ["runkeys"],
      aiArtifacts: ["runkeys"],
      images: { "img-w1": { image_id: "img-w1", artifacts: ["runkeys"], aiArtifacts: ["runkeys"] } },
    };
    A.st.chat.allMessages = [{ role: "user", content: "old" }];
    A.st.chat.historyLoadedCaseId = "case-old";
    document.getElementById("evidence-intake-status").hidden = false;
    document.getElementById("evidence-intake-status").textContent = "Processing image 2 of 2...";
    A.el.evidenceProgWrap.hidden = false;
    A.el.evidenceProg.value = 67;

    A.resetCaseUi();

    const remainingForms = A.getImageForms();
    expect(remainingForms).toHaveLength(1);
    expect(remainingForms[0].querySelector(".image-form-title").textContent).toBe("Image 1");
    expect(remainingForms[0].querySelector(".image-label-input").value).toBe("");
    expect(remainingForms[0].querySelector(".image-path-input").value).toBe("");
    expect(remainingForms[0].querySelector(".image-mode-path").checked).toBe(true);
    expect(remainingForms[0].querySelector(".image-mode-upload").checked).toBe(false);
    expect(remainingForms[0].querySelector(".image-status-msg").hidden).toBe(true);
    expect(remainingForms[0].querySelector(".image-status-msg").textContent).toBe("");
    expect(document.getElementById("artifact-image-tabs").hidden).toBe(true);
    expect(document.getElementById("artifact-image-panels").children).toHaveLength(0);
    expect(A.el.artifactsForm.hidden).toBe(false);
    expect(A.el.applyRecommendedAllBtn.hidden).toBe(true);
    expect(A.el.applySelectionAllBtn.hidden).toBe(true);
    expect(A.st.images).toEqual([]);
    expect(A.st.parsedSelections).toMatchObject({ caseId: "", runId: "", mode: "" });
    expect(A.st.chat.historyLoadedCaseId).toBe("");
    expect(A.el.evidenceProgWrap.hidden).toBe(true);
    expect(A.el.evidenceProg.value).toBe(0);
    expect(document.getElementById("evidence-intake-status").hidden).toBe(true);
    expect(document.getElementById("evidence-intake-status").textContent).toBe("");
  });
});

describe("isMultiImage", () => {
  test("returns false when no images loaded", () => {
    A.st.images = [];
    expect(A.isMultiImage()).toBe(false);
  });

  test("returns false for a single image", () => {
    A.st.images = [{ image_id: "x" }];
    expect(A.isMultiImage()).toBe(false);
  });

  test("returns true for multiple images", () => {
    A.st.images = [{ image_id: "a" }, { image_id: "b" }];
    expect(A.isMultiImage()).toBe(true);
  });
});

// ── buildMultiImageArtifactTabs ─────────────────────────────────────────────

describe("buildMultiImageArtifactTabs", () => {
  test("hides tabs and shows main form for single image", () => {
    A.st.images = [{ image_id: "solo", label: "Solo", os_type: "windows", available_artifacts: [] }];
    A.buildMultiImageArtifactTabs();

    const tabContainer = document.getElementById("artifact-image-tabs");
    expect(tabContainer.hidden).toBe(true);
    if (A.el.artifactsForm) expect(A.el.artifactsForm.hidden).toBe(false);
  });

  test("shows tabs and hides main form for multiple images", () => {
    setImagesAndBuildTabs(makeTwoWindowsImages());

    const tabContainer = document.getElementById("artifact-image-tabs");
    expect(tabContainer.hidden).toBe(false);
    if (A.el.artifactsForm) expect(A.el.artifactsForm.hidden).toBe(true);
  });

  test("creates one tab button per image", () => {
    setImagesAndBuildTabs(makeTwoWindowsImages());

    const tabBar = document.querySelector(".artifact-tab-bar");
    const buttons = tabBar.querySelectorAll("button");
    expect(buttons.length).toBe(2);
    expect(buttons[0].textContent).toBe("Workstation 1");
    expect(buttons[1].textContent).toBe("Workstation 2");
  });

  test("creates one panel per image", () => {
    setImagesAndBuildTabs(makeTwoWindowsImages());

    const panels = document.querySelectorAll(".artifact-image-panel");
    expect(panels.length).toBe(2);
    expect(panels[0].dataset.imageId).toBe("img-w1");
    expect(panels[1].dataset.imageId).toBe("img-w2");
  });

  test("preserves grouped Advanced artifacts in each image panel", () => {
    const images = [
      {
        image_id: "img-w1",
        label: "Workstation 1",
        os_type: "windows",
        metadata: { hostname: "WS-01" },
        hashes: {},
        available_artifacts: [
          { key: "runkeys", name: "Run/RunOnce Keys", available: true },
          {
            key: "certlog",
            name: "AD CS Certificate Logs",
            category: "PKI",
            available: true,
          },
        ],
      },
      {
        image_id: "img-w2",
        label: "Workstation 2",
        os_type: "windows",
        metadata: { hostname: "WS-02" },
        hashes: {},
        available_artifacts: [
          { key: "runkeys", name: "Run/RunOnce Keys", available: true },
          {
            key: "mssql.errorlog",
            name: "MSSQL Error Log",
            category: "Database",
            available: true,
          },
        ],
      },
    ];
    const combinedArtifacts = [
      { key: "runkeys", name: "Run/RunOnce Keys", available: true },
      {
        key: "certlog",
        name: "AD CS Certificate Logs",
        category: "PKI",
        available: true,
      },
      {
        key: "mssql.errorlog",
        name: "MSSQL Error Log",
        category: "Database",
        available: true,
      },
    ];

    A.st.images = images;
    A.applyEvidence({
      os_type: "windows",
      metadata: { os_version: "Windows 11" },
      hashes: {},
      available_artifacts: combinedArtifacts,
    });

    const panel = document.querySelector(".artifact-image-panel[data-image-id='img-w1']");
    const advanced = panel.querySelector("details.artifact-advanced-section");
    expect(advanced).not.toBeNull();
    expect(advanced.id).toBe("");
    expect(advanced.querySelector("summary").textContent).toBe("Advanced");
    expect(
      advanced.querySelector("fieldset[data-category='pki'] input[data-artifact-key='certlog']")
    ).not.toBeNull();
    expect(
      advanced.querySelector("fieldset[data-category='database'] input[data-artifact-key='mssql.errorlog']")
    ).not.toBeNull();
    expect(document.querySelectorAll("#dynamic-artifact-category").length).toBe(1);
  });

  test("preserves OS-specific labels for duplicate artifact keys", () => {
    setImagesAndBuildTabs(makeWindowsLinuxServiceImages());

    const winPanel = document.querySelector(".artifact-image-panel[data-image-id='img-win']");
    const linuxPanel = document.querySelector(".artifact-image-panel[data-image-id='img-linux']");
    const winServices = winPanel.querySelector("input[data-artifact-key='services']");
    const linuxServices = linuxPanel.querySelector("input[data-artifact-key='services']");

    expect(A.artifactLabelText(winServices)).toBe("Services");
    expect(A.artifactLabelText(linuxServices)).toBe("Systemd Services");
  });

  test("first tab and panel are active by default", () => {
    setImagesAndBuildTabs(makeTwoWindowsImages());

    const buttons = document.querySelectorAll(".artifact-tab-bar button");
    expect(buttons[0].classList.contains("is-active")).toBe(true);
    expect(buttons[1].classList.contains("is-active")).toBe(false);

    const panels = document.querySelectorAll(".artifact-image-panel");
    expect(panels[0].classList.contains("is-active")).toBe(true);
    expect(panels[1].classList.contains("is-active")).toBe(false);
  });

  test("shows multi-image buttons when multiple images present", () => {
    setImagesAndBuildTabs(makeTwoWindowsImages());

    if (A.el.applyRecommendedAllBtn) {
      expect(A.el.applyRecommendedAllBtn.hidden).toBe(false);
    }
    if (A.el.applySelectionAllBtn) {
      expect(A.el.applySelectionAllBtn.hidden).toBe(false);
    }
  });

  test("hides multi-image buttons for single image", () => {
    A.st.images = [{ image_id: "solo", label: "Solo", os_type: "windows", available_artifacts: [] }];
    A.buildMultiImageArtifactTabs();

    if (A.el.applyRecommendedAllBtn) {
      expect(A.el.applyRecommendedAllBtn.hidden).toBe(true);
    }
    if (A.el.applySelectionAllBtn) {
      expect(A.el.applySelectionAllBtn.hidden).toBe(true);
    }
  });

  test("disables unavailable artifacts per image", () => {
    setImagesAndBuildTabs(makeTwoWindowsImages());

    const panels = document.querySelectorAll(".artifact-image-panel");
    /* Image 1: prefetch unavailable. */
    const panel1Prefetch = panels[0].querySelector("input[data-artifact-key='prefetch']");
    if (panel1Prefetch) {
      expect(panel1Prefetch.disabled).toBe(true);
    }
    /* Image 2: shimcache unavailable. */
    const panel2Shim = panels[1].querySelector("input[data-artifact-key='shimcache']");
    if (panel2Shim) {
      expect(panel2Shim.disabled).toBe(true);
    }
    /* Image 2: prefetch available. */
    const panel2Prefetch = panels[1].querySelector("input[data-artifact-key='prefetch']");
    if (panel2Prefetch) {
      expect(panel2Prefetch.disabled).toBe(false);
    }
  });
});

// ── OS-aware fieldset cloning ───────────────────────────────────────────────

describe("OS-aware fieldset cloning", () => {
  test("Windows image panel contains Windows fieldsets only", () => {
    setImagesAndBuildTabs(makeWindowsLinuxImages());

    const winPanel = document.querySelector(".artifact-image-panel[data-image-id='img-win']");
    expect(winPanel).not.toBeNull();

    /* Should have Windows artifacts (no data-os). */
    const winCheckbox = winPanel.querySelector("input[data-artifact-key='runkeys']");
    expect(winCheckbox).not.toBeNull();

    /* Should NOT have Linux artifacts. */
    const linuxCheckbox = winPanel.querySelector("input[data-artifact-key='cronjobs']");
    expect(linuxCheckbox).toBeNull();
  });

  test("Linux image panel contains Linux fieldsets only", () => {
    setImagesAndBuildTabs(makeWindowsLinuxImages());

    const linuxPanel = document.querySelector(".artifact-image-panel[data-image-id='img-linux']");
    expect(linuxPanel).not.toBeNull();

    /* Should have Linux artifacts. */
    const linuxCheckbox = linuxPanel.querySelector("input[data-artifact-key='cronjobs']");
    expect(linuxCheckbox).not.toBeNull();

    /* Should NOT have Windows-only artifacts. */
    const winCheckbox = linuxPanel.querySelector("input[data-artifact-key='runkeys']");
    expect(winCheckbox).toBeNull();
  });

  test("Linux fieldsets are visible (not hidden) in Linux panel", () => {
    setImagesAndBuildTabs(makeWindowsLinuxImages());

    const linuxPanel = document.querySelector(".artifact-image-panel[data-image-id='img-linux']");
    const fieldsets = linuxPanel.querySelectorAll("fieldset.artifact-category");
    fieldsets.forEach((fs) => {
      expect(fs.hidden).toBe(false);
    });
  });

  test("available Linux artifacts are enabled in Linux panel", () => {
    setImagesAndBuildTabs(makeWindowsLinuxImages());

    const linuxPanel = document.querySelector(".artifact-image-panel[data-image-id='img-linux']");
    const cronCb = linuxPanel.querySelector("input[data-artifact-key='cronjobs']");
    expect(cronCb).not.toBeNull();
    expect(cronCb.disabled).toBe(false);

    const bashCb = linuxPanel.querySelector("input[data-artifact-key='bash_history']");
    expect(bashCb).not.toBeNull();
    expect(bashCb.disabled).toBe(false);
  });

  test("two Windows images both get Windows fieldsets", () => {
    setImagesAndBuildTabs(makeTwoWindowsImages());

    const panels = document.querySelectorAll(".artifact-image-panel");
    panels.forEach((panel) => {
      const runkeys = panel.querySelector("input[data-artifact-key='runkeys']");
      expect(runkeys).not.toBeNull();

      /* Should not have Linux fieldsets. */
      const cronjobs = panel.querySelector("input[data-artifact-key='cronjobs']");
      expect(cronjobs).toBeNull();
    });
  });

  test("checkbox names are prefixed with image ID to avoid collisions", () => {
    setImagesAndBuildTabs(makeWindowsLinuxImages());

    const winPanel = document.querySelector(".artifact-image-panel[data-image-id='img-win']");
    const runkeys = winPanel.querySelector("input[data-artifact-key='runkeys']");
    if (runkeys) {
      expect(runkeys.name).toBe("img-win__runkeys");
      expect(runkeys.dataset.imageId).toBe("img-win");
    }

    const linuxPanel = document.querySelector(".artifact-image-panel[data-image-id='img-linux']");
    const cron = linuxPanel.querySelector("input[data-artifact-key='cronjobs']");
    if (cron) {
      expect(cron.name).toBe("img-linux__cronjobs");
      expect(cron.dataset.imageId).toBe("img-linux");
    }
  });

  test("dynamic OS-tagged fieldsets are cloned only into matching OS panels", () => {
    const images = makeWindowsLinuxImages();
    images[0].available_artifacts.push(
      { key: "iis_logs", name: "IIS Logs", category: "Web Servers", os: "windows", available: true },
    );
    images[1].available_artifacts.push(
      { key: "container_logs", name: "Container Logs", category: "Containers", os: "linux", available: true },
    );

    /* Mirror submitEvidence(): record the per-image entries, then apply
       the merged intake response, which builds the dynamic Advanced
       section in the main form and clones it into the per-image panels. */
    A.st.images = images;
    const merged = [];
    for (const img of images) {
      for (const a of img.available_artifacts) {
        if (!merged.find((x) => x.key === a.key)) merged.push(Object.assign({}, a));
      }
    }
    A.applyEvidence({
      os_type: images[0].os_type,
      metadata: { os_version: "Windows 10" },
      hashes: {},
      available_artifacts: merged,
    });

    const winPanel = mustQuery(document, ".artifact-image-panel[data-image-id='img-win']");
    const linuxPanel = mustQuery(document, ".artifact-image-panel[data-image-id='img-linux']");

    /* Windows panel gets the dynamic Windows artifact, not the Linux one. */
    expect(winPanel.querySelector("input[data-artifact-key='iis_logs']")).not.toBeNull();
    expect(winPanel.querySelector("input[data-artifact-key='container_logs']")).toBeNull();

    /* Linux panel gets the dynamic Linux artifact, visible and enabled. */
    const linuxCb = linuxPanel.querySelector("input[data-artifact-key='container_logs']");
    expect(linuxCb).not.toBeNull();
    expect(linuxCb.disabled).toBe(false);
    expect(linuxCb.closest("fieldset").hidden).toBe(false);
    expect(linuxPanel.querySelector("input[data-artifact-key='iis_logs']")).toBeNull();
  });
});

// ── switchArtifactTab ───────────────────────────────────────────────────────

describe("switchArtifactTab", () => {
  test("activates the selected tab and panel", () => {
    setImagesAndBuildTabs(makeTwoWindowsImages());

    A.switchArtifactTab("img-w2");

    const buttons = document.querySelectorAll(".artifact-tab-bar button");
    expect(buttons[0].classList.contains("is-active")).toBe(false);
    expect(buttons[1].classList.contains("is-active")).toBe(true);

    const panels = document.querySelectorAll(".artifact-image-panel");
    expect(panels[0].classList.contains("is-active")).toBe(false);
    expect(panels[1].classList.contains("is-active")).toBe(true);
  });

  test("switching back to first tab restores activation", () => {
    setImagesAndBuildTabs(makeTwoWindowsImages());

    A.switchArtifactTab("img-w2");
    A.switchArtifactTab("img-w1");

    const buttons = document.querySelectorAll(".artifact-tab-bar button");
    expect(buttons[0].classList.contains("is-active")).toBe(true);
    expect(buttons[1].classList.contains("is-active")).toBe(false);
  });
});

// ── activeArtifactTabImageId ────────────────────────────────────────────────

describe("activeArtifactTabImageId", () => {
  test("returns null when tabs are hidden (single image)", () => {
    A.st.images = [{ image_id: "solo", os_type: "windows", available_artifacts: [] }];
    A.buildMultiImageArtifactTabs();
    expect(A.activeArtifactTabImageId()).toBeNull();
  });

  test("returns first image ID by default in multi-image mode", () => {
    setImagesAndBuildTabs(makeTwoWindowsImages());
    expect(A.activeArtifactTabImageId()).toBe("img-w1");
  });

  test("returns switched tab's image ID", () => {
    setImagesAndBuildTabs(makeTwoWindowsImages());
    A.switchArtifactTab("img-w2");
    expect(A.activeArtifactTabImageId()).toBe("img-w2");
  });
});

// ── selectedArtifactOptionsForImage ─────────────────────────────────────────

describe("selectedArtifactOptionsForImage", () => {
  test("returns empty array when no artifacts checked", () => {
    setImagesAndBuildTabs(makeTwoWindowsImages());
    expect(A.selectedArtifactOptionsForImage("img-w1")).toEqual([]);
  });

  test("returns checked artifacts for a specific image", () => {
    setImagesAndBuildTabs(makeTwoWindowsImages());

    /* Check runkeys on image 1. */
    const panel = document.querySelector(".artifact-image-panel[data-image-id='img-w1']");
    const cb = panel.querySelector("input[data-artifact-key='runkeys']");
    if (cb) {
      cb.checked = true;
      const result = A.selectedArtifactOptionsForImage("img-w1");
      expect(result.length).toBe(1);
      expect(result[0].artifact_key).toBe("runkeys");
    }
  });

  test("does not include disabled (unavailable) checked artifacts", () => {
    setImagesAndBuildTabs(makeTwoWindowsImages());

    /* Image 1: prefetch is unavailable. Force-check it. */
    const panel = document.querySelector(".artifact-image-panel[data-image-id='img-w1']");
    const cb = panel.querySelector("input[data-artifact-key='prefetch']");
    if (cb) {
      cb.checked = true; /* Still disabled. */
      const result = A.selectedArtifactOptionsForImage("img-w1");
      const prefetchEntry = result.find((r) => r.artifact_key === "prefetch");
      expect(prefetchEntry).toBeUndefined();
    }
  });

  test("returns empty array for null/empty imageId", () => {
    expect(A.selectedArtifactOptionsForImage(null)).toEqual([]);
    expect(A.selectedArtifactOptionsForImage("")).toEqual([]);
  });

  test("returns Linux artifacts for Linux image", () => {
    setImagesAndBuildTabs(makeWindowsLinuxImages());

    const panel = document.querySelector(".artifact-image-panel[data-image-id='img-linux']");
    const cb = panel.querySelector("input[data-artifact-key='cronjobs']");
    if (cb) {
      cb.checked = true;
      const result = A.selectedArtifactOptionsForImage("img-linux");
      expect(result.length).toBe(1);
      expect(result[0].artifact_key).toBe("cronjobs");
    }
  });
});

// ── allImageArtifactSelections ──────────────────────────────────────────────

describe("allImageArtifactSelections", () => {
  test("returns empty array for single image", () => {
    A.st.images = [{ image_id: "solo", os_type: "windows", available_artifacts: [] }];
    A.buildMultiImageArtifactTabs();
    expect(A.allImageArtifactSelections()).toEqual([]);
  });

  test("returns per-image entries for multiple images", () => {
    setImagesAndBuildTabs(makeTwoWindowsImages());

    const result = A.allImageArtifactSelections();
    expect(result.length).toBe(2);
    expect(result[0].image_id).toBe("img-w1");
    expect(result[0].label).toBe("Workstation 1");
    expect(result[1].image_id).toBe("img-w2");
    expect(result[1].label).toBe("Workstation 2");
  });

  test("includes checked artifacts in each image entry", () => {
    setImagesAndBuildTabs(makeTwoWindowsImages());

    /* Check runkeys on image 1. */
    const panel1 = document.querySelector(".artifact-image-panel[data-image-id='img-w1']");
    const cb1 = panel1.querySelector("input[data-artifact-key='runkeys']");
    if (cb1) cb1.checked = true;

    const result = A.allImageArtifactSelections();
    expect(result[0].artifact_options.length).toBe(1);
    expect(result[0].artifact_options[0].artifact_key).toBe("runkeys");
    /* Image 2 has nothing checked. */
    expect(result[1].artifact_options.length).toBe(0);
  });
});

// ── applyRecommendedToAllImages ─────────────────────────────────────────────

describe("applyRecommendedToAllImages", () => {
  test("checks available non-excluded artifacts on all Windows panels", () => {
    setImagesAndBuildTabs(makeTwoWindowsImages());
    A.applyRecommendedToAllImages();

    const panels = document.querySelectorAll(".artifact-image-panel");
    panels.forEach((panel) => {
      panel.querySelectorAll("input[type='checkbox'][data-artifact-key]").forEach((cb) => {
        const key = String(cb.dataset.artifactKey || "").toLowerCase();
        if (cb.disabled) {
          expect(cb.checked).toBe(false);
        } else if (A.RECOMMENDED_PRESET_EXCLUDED_ARTIFACTS.has(key)) {
          expect(cb.checked).toBe(false);
        } else {
          expect(cb.checked).toBe(true);
        }
      });
    });
  });

  test("checks available Linux artifacts in Linux panel", () => {
    setImagesAndBuildTabs(makeWindowsLinuxImages());
    A.applyRecommendedToAllImages();

    const linuxPanel = document.querySelector(".artifact-image-panel[data-image-id='img-linux']");
    const cronCb = linuxPanel.querySelector("input[data-artifact-key='cronjobs']");
    if (cronCb) {
      /* cronjobs is available and not in the excluded set. */
      expect(cronCb.disabled).toBe(false);
      expect(cronCb.checked).toBe(true);
    }

    const bashCb = linuxPanel.querySelector("input[data-artifact-key='bash_history']");
    if (bashCb) {
      expect(bashCb.disabled).toBe(false);
      expect(bashCb.checked).toBe(true);
    }
  });

  test("uses loaded recommended profile modes for all image panels", () => {
    A.st.profiles = [
      {
        name: "recommended",
        builtin: true,
        artifact_options: [
          { artifact_key: "runkeys", mode: A.MODE_PARSE_AND_AI },
          { artifact_key: "evtx", mode: A.MODE_PARSE_ONLY },
        ],
      },
    ];
    setImagesAndBuildTabs(makeWindowsLinuxImages());
    A.applyRecommendedToAllImages();

    const winPanel = document.querySelector(".artifact-image-panel[data-image-id='img-win']");
    const evtxCb = winPanel.querySelector("input[data-artifact-key='evtx']");
    const mftCb = winPanel.querySelector("input[data-artifact-key='mft']");
    const evtxMode = evtxCb.closest("li").querySelector(".artifact-mode-select");
    const mftMode = mftCb.closest("li").querySelector(".artifact-mode-select");

    expect(evtxCb.checked).toBe(true);
    expect(mftCb.checked).toBe(false);
    expect(evtxMode.value).toBe(A.MODE_PARSE_ONLY);
    expect(mftMode.value).toBe(A.MODE_PARSE_AND_AI);
  });

  test("fallback recommended excludes EVTX and MFT", () => {
    const images = makeWindowsLinuxImages();
    images[0].available_artifacts.push({ key: "defender.evtx", name: "Defender Logs", available: true });
    setImagesAndBuildTabs(images);
    A.applyRecommendedToAllImages();

    const winPanel = document.querySelector(".artifact-image-panel[data-image-id='img-win']");
    const defenderCb = winPanel.querySelector("input[data-artifact-key='defender.evtx']");
    const evtxCb = winPanel.querySelector("input[data-artifact-key='evtx']");
    const mftCb = winPanel.querySelector("input[data-artifact-key='mft']");
    const evtxMode = evtxCb.closest("li").querySelector(".artifact-mode-select");
    const mftMode = mftCb.closest("li").querySelector(".artifact-mode-select");

    expect(defenderCb.checked).toBe(true);
    expect(evtxCb.checked).toBe(false);
    expect(mftCb.checked).toBe(false);
    expect(evtxMode.value).toBe(A.MODE_PARSE_AND_AI);
    expect(mftMode.value).toBe(A.MODE_PARSE_AND_AI);
  });

  test("does nothing when not in multi-image mode", () => {
    A.st.images = [{ image_id: "solo", os_type: "windows", available_artifacts: [] }];
    A.buildMultiImageArtifactTabs();
    /* Should not throw. */
    expect(() => A.applyRecommendedToAllImages()).not.toThrow();
  });
});

// ── applyCurrentSelectionToAllImages ────────────────────────────────────────

describe("applyCurrentSelectionToAllImages", () => {
  test("mirrors active tab selection to other panels", () => {
    setImagesAndBuildTabs(makeTwoWindowsImages());

    /* Check runkeys on image 1 (active tab). */
    const panel1 = document.querySelector(".artifact-image-panel[data-image-id='img-w1']");
    const cb1 = panel1.querySelector("input[data-artifact-key='runkeys']");
    if (cb1) cb1.checked = true;

    A.applyCurrentSelectionToAllImages();

    /* Image 2 should now have runkeys checked too. */
    const panel2 = document.querySelector(".artifact-image-panel[data-image-id='img-w2']");
    const cb2 = panel2.querySelector("input[data-artifact-key='runkeys']");
    if (cb2 && !cb2.disabled) {
      expect(cb2.checked).toBe(true);
    }
  });

  test("does not enable disabled artifacts on target panel", () => {
    setImagesAndBuildTabs(makeTwoWindowsImages());

    /* Check shimcache on image 1 (available). */
    const panel1 = document.querySelector(".artifact-image-panel[data-image-id='img-w1']");
    const cb1 = panel1.querySelector("input[data-artifact-key='shimcache']");
    if (cb1) cb1.checked = true;

    A.applyCurrentSelectionToAllImages();

    /* Image 2: shimcache is unavailable (disabled). Should not be checked. */
    const panel2 = document.querySelector(".artifact-image-panel[data-image-id='img-w2']");
    const cb2 = panel2.querySelector("input[data-artifact-key='shimcache']");
    if (cb2 && cb2.disabled) {
      expect(cb2.checked).toBe(false);
    }
  });

  test("leaves OS-specific artifacts untouched when not in source panel", () => {
    setImagesAndBuildTabs(makeWindowsLinuxImages());

    /* Active tab is Windows (img-win). Check runkeys. */
    const winPanel = document.querySelector(".artifact-image-panel[data-image-id='img-win']");
    const runkeys = winPanel.querySelector("input[data-artifact-key='runkeys']");
    if (runkeys) runkeys.checked = true;

    /* Pre-check cronjobs on Linux panel. */
    const linuxPanel = document.querySelector(".artifact-image-panel[data-image-id='img-linux']");
    const cronCb = linuxPanel.querySelector("input[data-artifact-key='cronjobs']");
    if (cronCb) cronCb.checked = true;

    A.applyCurrentSelectionToAllImages();

    /* runkeys does not exist in Linux panel — cronjobs should remain checked. */
    if (cronCb) {
      expect(cronCb.checked).toBe(true);
    }
  });

  test("skips cross-OS duplicate-key artifacts when applying current selection to all", () => {
    setImagesAndBuildTabs(makeWindowsLinuxServiceImages());

    const winPanel = document.querySelector(".artifact-image-panel[data-image-id='img-win']");
    const winServices = winPanel.querySelector("input[data-artifact-key='services']");
    winServices.checked = true;
    const winMode = winServices.closest("li").querySelector("select.artifact-mode-select");
    winMode.value = A.MODE_PARSE_ONLY;

    const linuxPanel = document.querySelector(".artifact-image-panel[data-image-id='img-linux']");
    const linuxServices = linuxPanel.querySelector("input[data-artifact-key='services']");
    const linuxMode = linuxServices.closest("li").querySelector("select.artifact-mode-select");
    linuxServices.checked = false;
    linuxMode.value = A.MODE_PARSE_AND_AI;

    A.applyCurrentSelectionToAllImages();

    expect(linuxServices.checked).toBe(false);
    expect(linuxMode.value).toBe(A.MODE_PARSE_AND_AI);
  });
});

// ── applyPresetMultiAware ───────────────────────────────────────────────────

describe("applyPresetMultiAware", () => {
  test("applies recommended preset to active multi-image panel", () => {
    setImagesAndBuildTabs(makeTwoWindowsImages());
    A.applyPresetMultiAware("recommended");

    /* Only the active panel (img-w1) should be affected. */
    const panel1 = document.querySelector(".artifact-image-panel[data-image-id='img-w1']");
    const available1 = panel1.querySelectorAll("input[type='checkbox'][data-artifact-key]:not(:disabled)");
    let anyChecked = false;
    available1.forEach((cb) => {
      const key = String(cb.dataset.artifactKey || "").toLowerCase();
      if (!A.RECOMMENDED_PRESET_EXCLUDED_ARTIFACTS.has(key)) {
        if (cb.checked) anyChecked = true;
      }
    });
    /* At least some available non-excluded artifacts should be checked. */
    if (available1.length > 0) {
      expect(anyChecked).toBe(true);
    }

    /* Panel 2 should remain unchecked (not affected by per-tab preset). */
    const panel2 = document.querySelector(".artifact-image-panel[data-image-id='img-w2']");
    const checked2 = panel2.querySelectorAll("input[type='checkbox'][data-artifact-key]:checked");
    expect(checked2.length).toBe(0);
  });

  test("clear preset unchecks all in active panel", () => {
    setImagesAndBuildTabs(makeTwoWindowsImages());

    /* First apply recommended, then clear. */
    A.applyPresetMultiAware("recommended");
    A.applyPresetMultiAware("clear");

    const panel1 = document.querySelector(".artifact-image-panel[data-image-id='img-w1']");
    const checked = panel1.querySelectorAll("input[type='checkbox'][data-artifact-key]:checked");
    expect(checked.length).toBe(0);
  });
});

// ── renderImageSummaries ────────────────────────────────────────────────────

describe("renderImageSummaries", () => {
  test("hides multi-image summaries for single image", () => {
    A.renderImageSummaries([{ image_id: "solo", metadata: {}, hashes: {} }]);

    const container = document.getElementById("evidence-summaries-container");
    if (container) {
      expect(container.hidden).toBe(true);
    }
  });

  test("shows summary cards for multiple images", () => {
    const images = makeWindowsLinuxImages();
    A.renderImageSummaries(images);

    const container = document.getElementById("evidence-summaries-container");
    const list = document.getElementById("evidence-summaries-list");
    if (container && list) {
      expect(container.hidden).toBe(false);
      const cards = list.querySelectorAll(".summary-card");
      expect(cards.length).toBe(2);
    }
  });

  test("displays image labels and metadata", () => {
    const images = makeWindowsLinuxImages();
    A.renderImageSummaries(images);

    const list = document.getElementById("evidence-summaries-list");
    if (list) {
      const cards = list.querySelectorAll(".summary-card");
      expect(cards[0].querySelector("h4").textContent).toBe("Windows PC");
      expect(cards[1].querySelector("h4").textContent).toBe("Linux Server");
      expect(cards[0].textContent).toContain("WIN-PC");
      expect(cards[1].textContent).toContain("SRV-01");
    }
  });
});

describe("profile actions with image tabs", () => {
  test("selection maps can apply to every image tab", () => {
    setImagesAndBuildTabs(makeTwoWindowsImages());

    A.applyArtifactSelectionMap({ runkeys: A.MODE_PARSE_ONLY }, "all");

    const panel1 = document.querySelector(".artifact-image-panel[data-image-id='img-w1']");
    const panel2 = document.querySelector(".artifact-image-panel[data-image-id='img-w2']");
    const firstRunkeys = panel1.querySelector("input[data-artifact-key='runkeys']");
    const secondRunkeys = panel2.querySelector("input[data-artifact-key='runkeys']");
    const firstMode = firstRunkeys.closest("li").querySelector(".artifact-mode-select");
    const secondMode = secondRunkeys.closest("li").querySelector(".artifact-mode-select");

    expect(firstRunkeys.checked).toBe(true);
    expect(secondRunkeys.checked).toBe(true);
    expect(firstMode.value).toBe(A.MODE_PARSE_ONLY);
    expect(secondMode.value).toBe(A.MODE_PARSE_ONLY);
  });

  test("single selection serialization reads the single root even while tabs exist", () => {
    setImagesAndBuildTabs(makeTwoWindowsImages());
    const hiddenRunkeys = A.el.artifactsForm.querySelector("input[data-artifact-key='runkeys']");
    hiddenRunkeys.disabled = false;
    hiddenRunkeys.checked = true;

    const activePanel = document.querySelector(".artifact-image-panel[data-image-id='img-w1']");
    const activeShimcache = activePanel.querySelector("input[data-artifact-key='shimcache']");
    activeShimcache.checked = true;

    expect(A.serializeArtifactSelections("single")).toEqual([
      { artifact_key: "runkeys", mode: A.MODE_PARSE_AND_AI },
    ]);
    expect(A.serializeArtifactSelections("active")).toEqual([
      { artifact_key: "shimcache", mode: A.MODE_PARSE_AND_AI },
    ]);
  });

  test("loads a profile into the active image tab only", () => {
    setImagesAndBuildTabs(makeTwoWindowsImages());
    A.st.profiles = [
      {
        name: "focused",
        builtin: false,
        artifact_options: [{ artifact_key: "runkeys", mode: A.MODE_PARSE_ONLY }],
      },
    ];
    A.el.profileSelect.innerHTML = '<option value="focused">focused</option>';
    A.el.profileSelect.value = "focused";
    A.clearMsg(A.el.profileMsg);
    A.clearMsg(A.el.artifactsMsg);

    A.el.profileLoadBtn.dispatchEvent(new Event("click"));

    const panel1 = document.querySelector(".artifact-image-panel[data-image-id='img-w1']");
    const panel2 = document.querySelector(".artifact-image-panel[data-image-id='img-w2']");
    const firstRunkeys = panel1.querySelector("input[data-artifact-key='runkeys']");
    const secondRunkeys = panel2.querySelector("input[data-artifact-key='runkeys']");
    const firstMode = firstRunkeys.closest("li").querySelector(".artifact-mode-select");

    expect(firstRunkeys.checked).toBe(true);
    expect(firstMode.value).toBe(A.MODE_PARSE_ONLY);
    expect(secondRunkeys.checked).toBe(false);
    expect(A.el.profileMsg.textContent).toBe("Loaded profile: focused");
    expect(A.el.profileMsg.dataset.status).toBe("success");
    expect(A.el.artifactsMsg.hidden).toBe(true);
  });

  test("saves profile options from the active image tab instead of the hidden form", async () => {
    setImagesAndBuildTabs(makeTwoWindowsImages());
    A.switchArtifactTab("img-w2");
    const hiddenMain = A.el.artifactsForm.querySelector("input[data-artifact-key='runkeys']");
    hiddenMain.disabled = false;
    hiddenMain.checked = true;

    const activePanel = document.querySelector(".artifact-image-panel[data-image-id='img-w2']");
    const prefetch = activePanel.querySelector("input[data-artifact-key='prefetch']");
    prefetch.checked = true;
    const mode = prefetch.closest("li").querySelector(".artifact-mode-select");
    mode.value = A.MODE_PARSE_ONLY;
    A.el.profileName.value = "active-tab";

    let savedBody = null;
    global.fetch = jest.fn((_url, init = {}) => {
      if (init.body) savedBody = JSON.parse(init.body);
      return Promise.resolve({
        ok: true,
        status: 200,
        headers: { get: () => "application/json" },
        json: async () => ({
          success: true,
          profiles: [
            { name: "recommended", builtin: true, artifact_options: [] },
            { name: "active-tab", builtin: false, artifact_options: [{ artifact_key: "prefetch", mode: A.MODE_PARSE_ONLY }] },
          ],
        }),
        text: async () => "{}",
      });
    });

    A.el.profileSaveBtn.dispatchEvent(new Event("click"));
    await Promise.resolve();

    expect(savedBody.name).toBe("active-tab");
    expect(savedBody.artifact_options).toEqual([
      { artifact_key: "prefetch", mode: A.MODE_PARSE_ONLY },
    ]);
  });

  test("reverting from image tabs clears stale hidden single-image selections", () => {
    setImagesAndBuildTabs(makeTwoWindowsImages());
    const hiddenMain = A.el.artifactsForm.querySelector("input[data-artifact-key='runkeys']");
    hiddenMain.disabled = false;
    hiddenMain.checked = true;

    A.st.images = [{ image_id: "solo", label: "Solo", os_type: "windows", available_artifacts: [] }];
    A.buildMultiImageArtifactTabs();

    expect(A.el.artifactsForm.hidden).toBe(false);
    expect(A.selectedArtifactOptions()).toEqual([]);
    expect(hiddenMain.checked).toBe(false);
  });
});
