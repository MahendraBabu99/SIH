/**
 * Unit tests for AIFT parse submission and progress tracking (parsing.js).
 *
 * Covers:
 *  - resetParseState clears all parse and analysis state
 *  - renderParsePlaceholder creates placeholder row
 *  - closeParseSse closes the SSE channel
 *  - Parse state lifecycle (run, done, fail flags)
 *  - Parse progress bar updates
 *  - Parse row creation and status updates
 *
 * @jest-environment jsdom
 */

"use strict";

const { setupAift, mustGet, mustQuery, mustFindAll, cleanupAift } = require("./harness");

let A;

beforeEach(() => {
  A = setupAift();
});

// ── resetParseState ─────────────────────────────────────────────────────────

describe("resetParseState", () => {
  test("resets all parse flags to initial state", () => {
    A.st.parse.run = true;
    A.st.parse.done = true;
    A.st.parse.fail = true;
    A.st.parse.retryCount = 5;
    A.st.parse.seq = 42;

    A.resetParseState();

    expect(A.st.parse.run).toBe(false);
    expect(A.st.parse.done).toBe(false);
    expect(A.st.parse.fail).toBe(false);
    expect(A.st.parse.retryCount).toBe(0);
    expect(A.st.parse.seq).toBe(-1);
  });

  test("clears parse rows and status", () => {
    A.st.parse.rows = { evtx: {} };
    A.st.parse.status = { evtx: "completed" };

    A.resetParseState();

    expect(A.st.parse.rows).toEqual({});
    expect(A.st.parse.status).toEqual({});
  });

  test("cascades reset to analysis state", () => {
    A.st.analysis.done = true;
    A.st.analysis.run = true;
    A.st.analysis.order = ["evtx"];
    A.st.analysis.byKey = { evtx: { text: "result" } };
    A.st.analysis.summary = "summary";

    A.resetParseState();

    expect(A.st.analysis.done).toBe(false);
    expect(A.st.analysis.run).toBe(false);
    expect(A.st.analysis.order).toEqual([]);
    expect(A.st.analysis.byKey).toEqual({});
    expect(A.st.analysis.summary).toBe("");
  });

  test("resets parse button to 'Parse Selected'", () => {
    A.setCaseId("test-case");
    A.st.parse.done = true;
    A.updateParseButton();
    expect(document.getElementById("parse-selected").textContent).toBe("Restart Parsing");

    A.resetParseState();
    expect(document.getElementById("parse-selected").textContent).toBe("Parse Selected");
  });

  test("updates navigation so analysis step becomes blocked", () => {
    A.setCaseId("test-case");
    A.st.selected = ["evtx"];
    A.st.selectedAi = ["evtx"];
    A.st.parse.done = true;

    A.updateNav();
    expect(A.el.indicators[3].classList.contains("is-disabled")).toBe(false);

    A.resetParseState();
    expect(A.el.indicators[3].classList.contains("is-disabled")).toBe(true);
  });

  test("clears parse error message", () => {
    const parseErr = mustGet("parse-error-message");
    A.setMsg(parseErr, "Some error", "error");
    expect(parseErr.hidden).toBe(false);

    A.resetParseState();
    expect(parseErr.hidden).toBe(true);
  });
});

// ── renderParsePlaceholder ──────────────────────────────────────────────────

describe("renderParsePlaceholder", () => {
  test("creates placeholder row in parse table", () => {
    A.renderParsePlaceholder();
    const rows = mustGet("parse-progress-rows").querySelectorAll("tr");
    expect(rows).toHaveLength(1);
    expect(rows[0].textContent).toContain("Awaiting selection");
  });

  test("resets progress bar to 0", () => {
    const progress = document.createElement("progress");
    A.el.parseProgress = progress;
    progress.value = 75;
    A.renderParsePlaceholder();
    expect(progress.value).toBe(0);
  });

  test("clears rows and status state", () => {
    A.st.parse.rows = { evtx: {} };
    A.st.parse.status = { evtx: "completed" };
    A.renderParsePlaceholder();
    expect(A.st.parse.rows).toEqual({});
    expect(A.st.parse.status).toEqual({});
  });
});

// ── closeParseSse ───────────────────────────────────────────────────────────

describe("closeParseSse", () => {
  test("closes the parse SSE channel", () => {
    const mockEs = { close: jest.fn() };
    A.st.parse.es = mockEs;
    A.st.parse.retry = setTimeout(() => {}, 10000);

    A.closeParseSse();

    expect(mockEs.close).toHaveBeenCalled();
    expect(A.st.parse.es).toBeNull();
    expect(A.st.parse.retry).toBeNull();
  });

  test("handles already-closed channel gracefully", () => {
    A.st.parse.es = null;
    A.st.parse.retry = null;
    expect(() => A.closeParseSse()).not.toThrow();
  });
});

// ── Parse state lifecycle ───────────────────────────────────────────────────

describe("parse state lifecycle", () => {
  test("initial parse state has all flags false", () => {
    A.resetParseState();
    expect(A.st.parse.run).toBe(false);
    expect(A.st.parse.done).toBe(false);
    expect(A.st.parse.fail).toBe(false);
  });

  test("parse completion enables analysis step navigation", () => {
    A.setCaseId("test-case");
    A.st.selected = ["evtx"];
    A.st.selectedAi = ["evtx"];
    A.st.parse.done = true;
    A.updateNav();
    expect(A.el.indicators[3].classList.contains("is-disabled")).toBe(false);
  });

  test("parse running blocks analysis step", () => {
    A.setCaseId("test-case");
    A.st.selected = ["evtx"];
    A.st.selectedAi = ["evtx"];
    A.st.parse.run = true;
    A.st.parse.done = false;
    A.updateNav();
    expect(A.el.indicators[3].classList.contains("is-disabled")).toBe(true);
  });

  test("parse failure blocks analysis step", () => {
    A.setCaseId("test-case");
    A.st.selected = ["evtx"];
    A.st.selectedAi = ["evtx"];
    A.st.parse.fail = true;
    A.st.parse.done = false;
    A.updateNav();
    expect(A.el.indicators[3].classList.contains("is-disabled")).toBe(true);
  });

  test("parse-only completion stays on parsing and blocks analysis", () => {
    A.setCaseId("test-case");
    A.showStep(3);
    A.st.parse.run = true;
    A.st.selected = ["evtx"];
    A.st.selectedAi = [];

    A._onParseEvent({ type: "parse_completed", sequence: 1 });

    expect(A.st.step).toBe(3);
    expect(A.st.parse.done).toBe(true);
    expect(A.el.indicators[3].classList.contains("is-disabled")).toBe(true);
    expect(A.el.parseErr.textContent).toContain("No artifacts were set");
  });

  test("AI-enabled parse completion advances to analysis", () => {
    A.setCaseId("test-case");
    A.showStep(3);
    A.st.parse.run = true;
    A.st.selected = ["evtx"];
    A.st.selectedAi = ["evtx"];

    A._onParseEvent({ type: "parse_completed", sequence: 1 });

    expect(A.st.step).toBe(4);
    expect(A.st.parse.done).toBe(true);
    expect(A.el.indicators[3].classList.contains("is-disabled")).toBe(false);
  });

  test("zero-record parse completion stays on parsing without failure", () => {
    A.setCaseId("test-case");
    A.showStep(3);
    A.st.parse.run = true;
    A.st.parse.done = false;
    A.st.parse.fail = false;
    A.st.selected = ["runkeys"];
    A.st.selectedAi = ["runkeys"];

    A._onParseEvent({
      type: "parse_completed",
      has_usable_csvs: false,
      outcome: "no_usable_output",
      message: "Parsing completed, but the selected artifacts produced no records to analyze.",
      sequence: 1,
    });

    expect(A.st.step).toBe(3);
    expect(A.st.parse.done).toBe(false);
    expect(A.st.parse.fail).toBe(false);
    expect(A.st.parsedSelections.caseId).toBe("");
    expect(A.el.indicators[3].classList.contains("is-disabled")).toBe(true);
    expect(A.el.parseErr.textContent).toContain("no records");
  });

  test("recoverable parser warning is visible without failing single-image parse", () => {
    A.setCaseId("warning-case");
    const owner = A.newRunOwner("warning-case", "parse");
    A.st.parse.owner = owner;
    A.st.parse.run = true;
    A.st.selected = ["jumplist"];
    A.st.selectedAi = ["jumplist"];

    A._onParseEvent({
      type: "parse_started",
      artifacts: ["jumplist"],
      analysis_artifacts: ["jumplist"],
      sequence: 1,
    }, owner);
    A._onParseEvent({
      type: "parse_warning",
      artifact_key: "jumplist",
      level: "ERROR",
      message: "Error parsing response headers: 'NoneType' object has no attribute 'decode'",
      sequence: 2,
    }, owner);

    expect(A.st.parse.run).toBe(true);
    expect(A.st.parse.fail).toBe(false);
    expect(A.st.parse.rows.jumplist.tdS.textContent).toBe("warning");
    expect(A.el.parseErr.dataset.status).toBe("warning");
    expect(A.el.parseErr.textContent).toContain("recoverable error");

    A._onParseEvent({
      type: "artifact_completed",
      artifact_key: "jumplist",
      record_count: 3,
      sequence: 3,
    }, owner);

    expect(A.st.parse.rows.jumplist.tdS.textContent).toBe("completed");
  });

  test("terminal single-image parse failure replaces warning row state", () => {
    A.setCaseId("warning-failure-case");
    const owner = A.newRunOwner("warning-failure-case", "parse");
    A.st.parse.owner = owner;
    A.st.parse.run = true;
    A.st.selected = ["jumplist"];

    A._onParseEvent({
      type: "parse_started",
      artifacts: ["jumplist"],
      analysis_artifacts: [],
      sequence: 1,
    }, owner);
    A._onParseEvent({
      type: "parse_warning",
      artifact_key: "jumplist",
      level: "WARNING",
      message: "Recoverable parser warning",
      sequence: 2,
    }, owner);
    A._onParseEvent({
      type: "parse_failed",
      error: "Parsing failed before completion: parser exploded",
      sequence: 3,
    }, owner);

    expect(A.st.parse.fail).toBe(true);
    expect(A.st.parse.rows.jumplist.tdS.textContent).toBe("failed");
    expect(A.el.parseProgress.value).toBe(100);
  });
});

// ── Parse button states ─────────────────────────────────────────────────────

describe("parse button states", () => {
  test("button says 'Parse Selected' initially", () => {
    A.setCaseId("test-case");
    A.st.parse.run = false;
    A.st.parse.done = false;
    A.updateParseButton();
    const btn = document.getElementById("parse-selected");
    expect(btn.textContent).toBe("Parse Selected");
  });

  test("button says 'Restart Parsing' when running", () => {
    A.setCaseId("test-case");
    A.st.parse.run = true;
    A.updateParseButton();
    const btn = document.getElementById("parse-selected");
    expect(btn.textContent).toBe("Restart Parsing");
  });

  test("button says 'Restart Parsing' when done", () => {
    A.setCaseId("test-case");
    A.st.parse.done = true;
    A.updateParseButton();
    const btn = document.getElementById("parse-selected");
    expect(btn.textContent).toBe("Restart Parsing");
  });
});

// ── Multi-image parse state ────────────────────────────────────────────────

describe("multi-image parse state", () => {
  test("st.imageParse is initialised as empty object", () => {
    expect(A.st.imageParse).toBeDefined();
    expect(typeof A.st.imageParse).toBe("object");
  });

  test("resetParseState clears imageParse", () => {
    A.st.imageParse = { img1: { run: true, done: false } };
    A.resetParseState();
    expect(A.st.imageParse).toEqual({});
  });

  test("isMultiImage returns false for zero or one image", () => {
    A.st.images = [];
    expect(A.isMultiImage()).toBe(false);
    A.st.images = [{ image_id: "img1" }];
    expect(A.isMultiImage()).toBe(false);
  });

  test("isMultiImage returns true for multiple images", () => {
    A.st.images = [{ image_id: "img1" }, { image_id: "img2" }];
    expect(A.isMultiImage()).toBe(true);
    A.st.images = [];
  });
});

describe("parse SSE ownership and retry state", () => {
  function installImageParseState(A, owner) {
    const tr = document.createElement("tr");
    const tdS = document.createElement("td");
    const tdR = document.createElement("td");
    tr.appendChild(tdS);
    tr.appendChild(tdR);
    A.st.parse.owner = owner;
    A.st.parse.run = true;
    A.st.imageParse = {
      img1: {
        run: true,
        done: false,
        fail: false,
        owner,
        rows: { evtx: { tr, tdS, tdR } },
        status: { evtx: "waiting" },
        sseState: { es: null, retry: null, retryCount: 0, seq: -1 },
        snapshot: {
          image_id: "img1",
          label: "Image 1",
          artifacts: ["evtx"],
          aiArtifacts: ["evtx"],
          artifactOptions: [{ artifact_key: "evtx", mode: A.MODE_PARSE_AND_AI }],
        },
      },
    };
    return { tdS, tdR };
  }

  test("multi-image SSE retry attempts persist across reconnects", () => {
    jest.useFakeTimers();
    A.setCaseId("case-retry");
    const owner = A.newRunOwner("case-retry", "parse");
    installImageParseState(A, owner);

    A._startImageParseSse("case-retry", "img1", owner);

    for (let attempt = 1; attempt <= A.SSE_MAX_RETRIES; attempt += 1) {
      const source = window.__AIFT_TEST_OPEN_EVENT_SOURCES__.at(-1);
      source.onerror();
      expect(A.st.imageParse.img1.sseState.retryCount).toBe(attempt);
      jest.advanceTimersByTime(A.sseRetryDelayMs(attempt));
    }

    const finalSource = window.__AIFT_TEST_OPEN_EVENT_SOURCES__.at(-1);
    finalSource.onerror();

    expect(A.st.imageParse.img1.run).toBe(false);
    expect(A.st.imageParse.img1.fail).toBe(true);
  });

  test("multi-image sequence dedupe survives reconnect", () => {
    jest.useFakeTimers();
    A.setCaseId("case-seq");
    const owner = A.newRunOwner("case-seq", "parse");
    const { tdR } = installImageParseState(A, owner);

    A._startImageParseSse("case-seq", "img1", owner);
    const firstSource = window.__AIFT_TEST_OPEN_EVENT_SOURCES__.at(-1);
    firstSource.onmessage({ data: JSON.stringify({ type: "artifact_progress", artifact_key: "evtx", record_count: 5, sequence: 5 }) });
    expect(tdR.textContent).toBe("5");

    firstSource.onerror();
    jest.advanceTimersByTime(A.sseRetryDelayMs(1));
    const secondSource = window.__AIFT_TEST_OPEN_EVENT_SOURCES__.at(-1);
    secondSource.onmessage({ data: JSON.stringify({ type: "artifact_progress", artifact_key: "evtx", record_count: 99, sequence: 4 }) });

    expect(tdR.textContent).toBe("5");
    expect(A.st.imageParse.img1.sseState.seq).toBe(5);
  });

  test("recoverable parser warning is visible without failing multi-image parse", () => {
    A.setCaseId("case-warning");
    const owner = A.newRunOwner("case-warning", "parse");
    const { tdS } = installImageParseState(A, owner);
    mustGet("parse-image-sections").innerHTML = `
      <div class="parse-image-section" data-image-id="img1">
        <span class="parse-image-status">Parsing...</span>
        <p class="parse-image-error" hidden></p>
      </div>
    `;

    A._onImageParseEvent("img1", {
      type: "parse_warning",
      artifact_key: "evtx",
      level: "WARNING",
      message: "Failed to parse LNK file from directory a",
      sequence: 1,
    }, owner);

    const section = mustQuery(document, '.parse-image-section[data-image-id="img1"]');
    expect(A.st.imageParse.img1.run).toBe(true);
    expect(A.st.imageParse.img1.fail).toBe(false);
    expect(tdS.textContent).toBe("warning");
    expect(section.querySelector(".parse-image-status").textContent).toBe("warning");
    expect(section.querySelector(".parse-image-error").hidden).toBe(false);
    expect(section.querySelector(".parse-image-error").dataset.status).toBe("warning");
  });

  test("terminal multi-image parse failure replaces warning row state", () => {
    jest.useFakeTimers();
    A.setCaseId("case-warning-fail");
    const owner = A.newRunOwner("case-warning-fail", "parse");
    const { tdS } = installImageParseState(A, owner);
    mustGet("parse-image-sections").innerHTML = `
      <div class="parse-image-section" data-image-id="img1">
        <span class="parse-image-status">Parsing...</span>
        <p class="parse-image-error" hidden></p>
      </div>
    `;

    A._onImageParseEvent("img1", {
      type: "parse_warning",
      artifact_key: "evtx",
      level: "WARNING",
      message: "Recoverable parser warning",
      sequence: 1,
    }, owner);
    A._onImageParseEvent("img1", {
      type: "parse_failed",
      error: "Parsing failed before completion: parser exploded",
      sequence: 2,
    }, owner);
    jest.runOnlyPendingTimers();

    const section = mustQuery(document, '.parse-image-section[data-image-id="img1"]');
    expect(A.st.imageParse.img1.fail).toBe(true);
    expect(tdS.textContent).toBe("failed");
    expect(section.querySelector(".parse-image-status").textContent).toBe("failed");
    expect(A.el.parseProgress.value).toBe(100);
  });

  test("old parse run events do not mutate the current case", () => {
    A.setCaseId("old-case");
    const owner = A.newRunOwner("old-case", "parse");
    A.st.parse.owner = owner;
    A.st.parse.run = true;

    A.setCaseId("new-case");
    A.resetParseState();
    A._onParseEvent({ type: "parse_completed", sequence: 1 }, owner);

    expect(A.st.parse.done).toBe(false);
    expect(A.st.step).not.toBe(4);
  });

  test("idle does not complete an active single-image parse", () => {
    A.setCaseId("case-idle");
    const owner = A.newRunOwner("case-idle", "parse");
    A.st.parse.owner = owner;
    A.st.parse.run = true;
    A.st.selectedAi = ["evtx"];

    A._onParseEvent({ type: "idle", sequence: 1 }, owner);

    expect(A.st.parse.done).toBe(false);
    expect(A.st.parse.fail).toBe(true);
    expect(A.st.step).not.toBe(4);
    expect(A.st.parsedSelections.caseId).toBe("");
  });

  test("synthetic complete does not complete an active single-image parse", () => {
    A.setCaseId("case-complete");
    const owner = A.newRunOwner("case-complete", "parse");
    A.st.parse.owner = owner;
    A.st.parse.run = true;
    A.st.selectedAi = ["evtx"];

    A._onParseEvent({ type: "complete", sequence: 1 }, owner);

    expect(A.st.parse.done).toBe(false);
    expect(A.st.parse.fail).toBe(true);
    expect(A.st.step).not.toBe(4);
    expect(A.st.parsedSelections.caseId).toBe("");
  });

  test("multi-image completion debounce cannot finish after cancellation", () => {
    jest.useFakeTimers();
    A.setCaseId("case-cancel-debounce");
    const owner = A.newRunOwner("case-cancel-debounce", "parse");
    A.st.parse.owner = owner;
    A.st.parse.run = true;
    A.st.selectedAi = ["evtx", "mft"];
    A.st.imageParse = {
      img1: {
        run: true,
        done: false,
        fail: false,
        owner,
        rows: {},
        status: {},
        snapshot: { image_id: "img1", artifacts: ["evtx"], aiArtifacts: ["evtx"], artifactOptions: [{ artifact_key: "evtx", mode: A.MODE_PARSE_AND_AI }] },
      },
      img2: {
        run: true,
        done: false,
        fail: false,
        owner,
        rows: {},
        status: {},
        snapshot: { image_id: "img2", artifacts: ["mft"], aiArtifacts: ["mft"], artifactOptions: [{ artifact_key: "mft", mode: A.MODE_PARSE_AND_AI }] },
      },
    };

    A._onImageParseEvent("img1", { type: "parse_completed", sequence: 1 }, owner);
    A._onImageParseEvent("img2", { type: "parse_completed", sequence: 1 }, owner);
    A.cancelParse();
    jest.runOnlyPendingTimers();

    expect(A.st.parse.done).toBe(false);
    expect(A.st.step).not.toBe(4);
    expect(A.st.parsedSelections.caseId).toBe("");
    jest.useRealTimers();
  });

  test("stale multi-image debounce cannot mutate a new case", () => {
    jest.useFakeTimers();
    A.setCaseId("case-old-debounce");
    const owner = A.newRunOwner("case-old-debounce", "parse");
    A.st.parse.owner = owner;
    A.st.parse.run = true;
    installImageParseState(A, owner);

    A._onImageParseEvent("img1", { type: "parse_completed", sequence: 1 }, owner);
    A.setCaseId("case-new-debounce");
    A.resetParseState();
    jest.runOnlyPendingTimers();

    expect(A.st.parse.done).toBe(false);
    expect(A.st.parsedSelections.caseId).toBe("");
    jest.useRealTimers();
  });

  test("multi-image completion snapshots only successful parsed images", () => {
    jest.useFakeTimers();
    A.setCaseId("case-snapshot");
    const owner = A.newRunOwner("case-snapshot", "parse");
    A.st.parse.owner = owner;
    A.st.parse.run = true;
    A.st.imageParse = {
      img1: {
        run: true,
        done: false,
        fail: false,
        owner,
        rows: {},
        status: {},
        snapshot: {
          image_id: "img1",
          label: "Image 1",
          artifacts: ["evtx"],
          aiArtifacts: ["evtx"],
          artifactOptions: [{ artifact_key: "evtx", mode: A.MODE_PARSE_AND_AI }],
        },
      },
      img2: {
        run: true,
        done: false,
        fail: false,
        owner,
        rows: {},
        status: {},
        snapshot: {
          image_id: "img2",
          label: "Image 2",
          artifacts: ["mft"],
          aiArtifacts: ["mft"],
          artifactOptions: [{ artifact_key: "mft", mode: A.MODE_PARSE_AND_AI }],
        },
      },
    };

    A._onImageParseEvent("img1", { type: "parse_completed", sequence: 1 }, owner);
    A._onImageParseEvent("img2", { type: "parse_failed", error: "boom", sequence: 1 }, owner);
    jest.runOnlyPendingTimers();

    expect(A.st.parse.done).toBe(true);
    expect(A.st.selectedAi).toEqual(["evtx"]);
    expect(Object.keys(A.st.parsedSelections.images)).toEqual(["img1"]);
  });

  test("multi-image zero-record completion does not unlock analysis", () => {
    jest.useFakeTimers();
    A.setCaseId("case-no-records");
    const owner = A.newRunOwner("case-no-records", "parse");
    A.st.parse.owner = owner;
    A.st.parse.run = true;
    A.st.selectedAi = ["evtx"];
    installImageParseState(A, owner);

    A._onImageParseEvent("img1", {
      type: "parse_completed",
      has_usable_csvs: false,
      outcome: "no_usable_output",
      sequence: 1,
    }, owner);
    jest.runOnlyPendingTimers();

    expect(A.st.imageParse.img1.noUsable).toBe(true);
    expect(A.st.parse.done).toBe(false);
    expect(A.st.parse.fail).toBe(false);
    expect(A.st.step).not.toBe(4);
    expect(A.st.parsedSelections.caseId).toBe("");
    expect(A.el.parseErr.textContent).toContain("no records");
    jest.useRealTimers();
  });

  test("multi-image cancelled terminal event blocks partial success unlock", () => {
    jest.useFakeTimers();
    A.setCaseId("case-cancelled-image");
    const owner = A.newRunOwner("case-cancelled-image", "parse");
    A.st.parse.owner = owner;
    A.st.parse.run = true;
    A.st.imageParse = {
      img1: {
        run: true,
        done: false,
        fail: false,
        owner,
        rows: {},
        status: {},
        snapshot: {
          image_id: "img1",
          label: "Image 1",
          artifacts: ["evtx"],
          aiArtifacts: ["evtx"],
          artifactOptions: [{ artifact_key: "evtx", mode: A.MODE_PARSE_AND_AI }],
        },
      },
      img2: {
        run: true,
        done: false,
        fail: false,
        owner,
        rows: {},
        status: {},
        snapshot: {
          image_id: "img2",
          label: "Image 2",
          artifacts: ["mft"],
          aiArtifacts: ["mft"],
          artifactOptions: [{ artifact_key: "mft", mode: A.MODE_PARSE_AND_AI }],
        },
      },
    };

    A._onImageParseEvent("img1", { type: "parse_completed", sequence: 1 }, owner);
    A._onImageParseEvent("img2", { type: "parse_cancelled", sequence: 1 }, owner);
    jest.runOnlyPendingTimers();

    expect(A.st.parse.done).toBe(false);
    expect(A.st.step).not.toBe(4);
    expect(A.st.parsedSelections.caseId).toBe("");
    expect(A.el.parseErr.textContent).toContain("cancelled");
    jest.useRealTimers();
  });

  test("multi-image control terminal event blocks partial success unlock", () => {
    jest.useFakeTimers();
    A.setCaseId("case-control-image");
    const owner = A.newRunOwner("case-control-image", "parse");
    A.st.parse.owner = owner;
    A.st.parse.run = true;
    A.st.imageParse = {
      img1: {
        run: true,
        done: false,
        fail: false,
        owner,
        rows: {},
        status: {},
        snapshot: {
          image_id: "img1",
          label: "Image 1",
          artifacts: ["evtx"],
          aiArtifacts: ["evtx"],
          artifactOptions: [{ artifact_key: "evtx", mode: A.MODE_PARSE_AND_AI }],
        },
      },
      img2: {
        run: true,
        done: false,
        fail: false,
        owner,
        rows: {},
        status: {},
        snapshot: {
          image_id: "img2",
          label: "Image 2",
          artifacts: ["mft"],
          aiArtifacts: ["mft"],
          artifactOptions: [{ artifact_key: "mft", mode: A.MODE_PARSE_AND_AI }],
        },
      },
    };

    A._onImageParseEvent("img1", { type: "parse_completed", sequence: 1 }, owner);
    A._onImageParseEvent("img2", { type: "complete", sequence: 1 }, owner);
    jest.runOnlyPendingTimers();

    expect(A.st.parse.done).toBe(false);
    expect(A.st.parse.fail).toBe(true);
    expect(A.st.step).not.toBe(4);
    expect(A.st.parsedSelections.caseId).toBe("");
    expect(A.el.parseErr.textContent).toContain("before all images completed");
    jest.useRealTimers();
  });

  test("idle marks an image parse failed instead of successful", () => {
    jest.useFakeTimers();
    A.setCaseId("case-image-idle");
    const owner = A.newRunOwner("case-image-idle", "parse");
    installImageParseState(A, owner);

    A._onImageParseEvent("img1", { type: "idle", sequence: 1 }, owner);
    jest.runOnlyPendingTimers();

    expect(A.st.imageParse.img1.done).toBe(false);
    expect(A.st.imageParse.img1.fail).toBe(true);
    expect(A.st.parse.done).toBe(false);
    expect(A.st.parsedSelections.caseId).toBe("");
    jest.useRealTimers();
  });
});

// ── Multi-image: showSingleImageParseTable ─────────────────────────────────

describe("showSingleImageParseTable", () => {
  test("parse-single-table is visible by default after reset", () => {
    A.renderParsePlaceholder();
    expect(mustGet("parse-single-table").hidden).toBe(false);
  });
});

// ── Multi-image: allImageArtifactSelections ────────────────────────────────

describe("allImageArtifactSelections", () => {
  test("returns empty array when one or zero images loaded", () => {
    A.st.images = [];
    expect(A.allImageArtifactSelections()).toEqual([]);
    A.st.images = [{ image_id: "img1" }];
    expect(A.allImageArtifactSelections()).toEqual([]);
    A.st.images = [];
  });

  test("returns per-image entries for multiple images", () => {
    A.st.images = [
      { image_id: "img1", label: "Image 1", available_artifacts: [] },
      { image_id: "img2", label: "Image 2", available_artifacts: [] },
    ];
    const selections = A.allImageArtifactSelections();
    expect(selections).toHaveLength(2);
    expect(selections[0].image_id).toBe("img1");
    expect(selections[1].image_id).toBe("img2");
    expect(Array.isArray(selections[0].artifact_options)).toBe(true);
    A.st.images = [];
  });
});

// ── Multi-image: selectedArtifactOptionsForImage ──────────────────────────

describe("selectedArtifactOptionsForImage", () => {
  test("returns empty array for nonexistent image panel", () => {
    expect(A.selectedArtifactOptionsForImage("nonexistent")).toEqual([]);
  });

  test("returns empty array for null/empty imageId", () => {
    expect(A.selectedArtifactOptionsForImage("")).toEqual([]);
    expect(A.selectedArtifactOptionsForImage(null)).toEqual([]);
  });
});

describe("browser selector fallback", () => {
  test("per-image parse status lookup works without native CSS.escape", () => {
    cleanupAift();
    A = setupAift({ withoutCssEscape: true });
    A.setCaseId("case-css-fallback");
    const owner = A.newRunOwner("case-css-fallback", "parse");
    A.st.parse.owner = owner;
    A.st.images = [
      { image_id: "img.1:win", label: "Image 1", available_artifacts: [{ key: "evtx", available: true }] },
      { image_id: "img.2:linux", label: "Image 2", available_artifacts: [{ key: "evtx", available: true }] },
    ];
    A.st.imageParse = {
      "img.1:win": { owner, status: { evtx: "waiting" }, rows: {} },
      "img.2:linux": { owner, status: { evtx: "waiting" }, rows: {} },
    };
    mustGet("parse-image-sections").innerHTML = `
      <div class="parse-image-section" data-image-id="img.2:linux">
        <span class="parse-image-status">Starting...</span>
        <p class="parse-image-error" hidden></p>
      </div>
    `;
    A._onImageParseEvent("img.2:linux", { type: "parse_started", sequence: 1 }, owner);

    const section = mustQuery(document, '.parse-image-section[data-image-id="img.2:linux"]');
    expect(section.querySelector(".parse-image-status").textContent).toBe("Parsing...");
  });
});

// ── Multi-image: activeArtifactTabImageId ─────────────────────────────────

describe("activeArtifactTabImageId", () => {
  test("returns null when tab container is hidden", () => {
    const tabContainer = document.getElementById("artifact-image-tabs");
    if (tabContainer) tabContainer.hidden = true;
    expect(A.activeArtifactTabImageId()).toBeNull();
  });

  test("returns null when no tabs exist", () => {
    expect(A.activeArtifactTabImageId()).toBeNull();
  });
});

// ── Multi-image: buildMultiImageArtifactTabs ──────────────────────────────

describe("buildMultiImageArtifactTabs", () => {
  test("hides tab container for single image", () => {
    A.st.images = [{ image_id: "img1", label: "Image 1" }];
    A.buildMultiImageArtifactTabs();
    expect(mustGet("artifact-image-tabs").hidden).toBe(true);
    A.st.images = [];
  });

  test("shows tab container for multiple images", () => {
    A.st.images = [
      { image_id: "img1", label: "Image 1", available_artifacts: [{ key: "evtx", available: true }] },
      { image_id: "img2", label: "Image 2", available_artifacts: [{ key: "evtx", available: true }] },
    ];
    A.buildMultiImageArtifactTabs();
    const tabContainer = mustGet("artifact-image-tabs");
    expect(tabContainer.hidden).toBe(false);
    const buttons = mustFindAll(tabContainer, ".artifact-tab-bar button", 2);
    expect(buttons.length).toBe(2);
    expect(buttons[0].textContent).toBe("Image 1");
    expect(buttons[1].textContent).toBe("Image 2");
    expect(buttons[0].classList.contains("is-active")).toBe(true);
    expect(buttons[1].classList.contains("is-active")).toBe(false);
    A.st.images = [];
  });

  test("creates per-image panels with correct data-image-id", () => {
    A.st.images = [
      { image_id: "img1", label: "Image 1", available_artifacts: [] },
      { image_id: "img2", label: "Image 2", available_artifacts: [] },
    ];
    A.buildMultiImageArtifactTabs();
    const panels = mustGet("artifact-image-panels");
    const panelDivs = mustFindAll(panels, ".artifact-image-panel", 2);
    expect(panelDivs.length).toBe(2);
    expect(panelDivs[0].dataset.imageId).toBe("img1");
    expect(panelDivs[1].dataset.imageId).toBe("img2");
    expect(panelDivs[0].classList.contains("is-active")).toBe(true);
    expect(panelDivs[1].classList.contains("is-active")).toBe(false);
    A.st.images = [];
  });

  test("hides main artifact form for multi-image", () => {
    A.st.images = [
      { image_id: "img1", label: "Image 1", available_artifacts: [] },
      { image_id: "img2", label: "Image 2", available_artifacts: [] },
    ];
    A.buildMultiImageArtifactTabs();
    expect(mustGet("artifacts-form").hidden).toBe(true);
    A.st.images = [];
  });
});

// ── Multi-image: switchArtifactTab ────────────────────────────────────────

describe("switchArtifactTab", () => {
  beforeEach(() => {
    A.st.images = [
      { image_id: "img1", label: "Image 1", available_artifacts: [] },
      { image_id: "img2", label: "Image 2", available_artifacts: [] },
    ];
    A.buildMultiImageArtifactTabs();
  });

  afterEach(() => {
    A.st.images = [];
  });

  test("switches active tab button", () => {
    A.switchArtifactTab("img2");
    const tabContainer = mustGet("artifact-image-tabs");
    const buttons = mustFindAll(tabContainer, ".artifact-tab-bar button", 2);
    expect(buttons[0].classList.contains("is-active")).toBe(false);
    expect(buttons[1].classList.contains("is-active")).toBe(true);
  });

  test("switches active panel", () => {
    A.switchArtifactTab("img2");
    const panels = mustGet("artifact-image-panels");
    const panelDivs = mustFindAll(panels, ".artifact-image-panel", 2);
    expect(panelDivs[0].classList.contains("is-active")).toBe(false);
    expect(panelDivs[1].classList.contains("is-active")).toBe(true);
  });

  test("activeArtifactTabImageId returns switched tab id", () => {
    A.switchArtifactTab("img2");
    expect(A.activeArtifactTabImageId()).toBe("img2");
  });
});
