/**
 * Browser-level frontend user-flow coverage with mocked network and SSE.
 *
 * These tests intentionally use the production template and production
 * scripts under jsdom.  Network responses and EventSource messages are
 * mocked so the flow stays fast and does not require Flask, AI services, or
 * a real browser.
 *
 * @jest-environment jsdom
 */

"use strict";

const { setupAift, mustQuery, flushMicrotasks } = require("./harness");

let A;

beforeEach(() => {
  A = setupAift();
});

function jsonResponse(payload, status = 200, extraHeaders = {}) {
  const headers = Object.assign({ "content-type": "application/json" }, extraHeaders);
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name) => headers[String(name || "").toLowerCase()] || "" },
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  });
}

function blobResponse(content, filename) {
  const blob = new Blob([content], { type: "text/html" });
  return Promise.resolve({
    ok: true,
    status: 200,
    headers: {
      get: (name) => {
        const key = String(name || "").toLowerCase();
        if (key === "content-disposition") return `attachment; filename="${filename}"`;
        if (key === "content-type") return "text/html";
        return "";
      },
    },
    blob: async () => blob,
    text: async () => content,
  });
}

function emit(source, payload) {
  source.onmessage({ data: JSON.stringify(payload) });
}

function nextTick() {
  return flushMicrotasks();
}

function requestBody(call) {
  return JSON.parse(call[1].body);
}

function selectPanelArtifact(imageId, artifactKey, mode) {
  const panel = mustQuery(
    document,
    `.artifact-image-panel[data-image-id="${imageId}"]`,
  );
  const checkbox = mustQuery(
    panel,
    `input[type="checkbox"][data-artifact-key="${artifactKey}"]`,
  );
  checkbox.checked = true;
  checkbox.dispatchEvent(new Event("change", { bubbles: true }));

  const select = checkbox.closest("li").querySelector("select.artifact-mode-select");
  select.value = mode;
  select.dispatchEvent(new Event("change", { bubbles: true }));
}

describe("mocked final browser flow", () => {
  afterEach(() => {
    jest.useRealTimers();
  });

  test("multi-image intake, parse-only filtering, analysis, and report download", async () => {
    jest.useFakeTimers();
    const calls = [];
    let imageIndex = 0;
    global.fetch = jest.fn((url, init = {}) => {
      calls.push([url, init]);

      if (url === "/api/cases") {
        return jsonResponse({ success: true, case_id: "case-final", case_name: "Final Flow" }, 201);
      }
      if (url === "/api/cases/case-final/images") {
        imageIndex += 1;
        return jsonResponse({
          success: true,
          image_id: `img-${imageIndex}`,
          label: imageIndex === 1 ? "Workstation" : "Server",
        }, 201);
      }
      if (url === "/api/cases/case-final/images/img-1/evidence") {
        return jsonResponse({
          success: true,
          os_type: "windows",
          metadata: { hostname: "WIN-01", os_version: "Windows 11", domain: "ACME" },
          hashes: { sha256: "hash-win" },
          available_artifacts: [
            { key: "runkeys", name: "Run/RunOnce Keys", available: true },
            { key: "prefetch", name: "Prefetch", available: true },
          ],
        });
      }
      if (url === "/api/cases/case-final/images/img-2/evidence") {
        return jsonResponse({
          success: true,
          os_type: "windows",
          metadata: { hostname: "WIN-02", os_version: "Windows Server 2022", domain: "ACME" },
          hashes: { sha256: "hash-server" },
          available_artifacts: [
            { key: "runkeys", name: "Run/RunOnce Keys", available: true },
            { key: "prefetch", name: "Prefetch", available: true },
          ],
        });
      }
      if (url.endsWith("/parse")) {
        return jsonResponse({ success: true });
      }
      if (url === "/api/cases/case-final/analyze") {
        return jsonResponse({ success: true });
      }
      if (url === "/api/cases/case-final/report") {
        return blobResponse("<html><body>AIFT report for runkeys</body></html>", "case-final_report.html");
      }
      if (url === "/api/cases/case-final/chat/history") {
        if (init.method === "DELETE") {
          return jsonResponse({ success: true });
        }
        return jsonResponse({
          success: true,
          messages: [
            { role: "user", content: "Previous question" },
            { role: "assistant", content: "Previous answer" },
          ],
        });
      }
      if (url === "/api/cases/case-final/chat") {
        return jsonResponse({ success: true });
      }
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    URL.createObjectURL = jest.fn(() => "blob:aift-report");
    URL.revokeObjectURL = jest.fn();

    const firstCard = A.getImageForms()[0];
    firstCard.querySelector(".image-label-input").value = "Workstation";
    firstCard.querySelector(".image-path-input").value = "E:\\evidence\\workstation.E01";
    A.addImageForm();
    const secondCard = A.getImageForms()[1];
    secondCard.querySelector(".image-label-input").value = "Server";
    secondCard.querySelector(".image-path-input").value = "E:\\evidence\\server.E01";

    await A.submitEvidence();

    expect(A.activeCaseId()).toBe("case-final");
    expect(A.st.images.map((img) => img.image_id)).toEqual(["img-1", "img-2"]);
    expect(document.getElementById("artifact-image-tabs").hidden).toBe(false);

    selectPanelArtifact("img-1", "prefetch", A.MODE_PARSE_ONLY);
    selectPanelArtifact("img-2", "runkeys", A.MODE_PARSE_AND_AI);

    await A.submitParse();
    const parseCalls = calls.filter(([url]) => String(url).endsWith("/parse"));
    expect(parseCalls).toHaveLength(2);
    expect(requestBody(parseCalls[0])).toEqual({
      artifact_options: [{ artifact_key: "prefetch", mode: A.MODE_PARSE_ONLY }],
    });
    expect(requestBody(parseCalls[1])).toEqual({
      artifact_options: [{ artifact_key: "runkeys", mode: A.MODE_PARSE_AND_AI }],
    });

    const parseSources = window.__AIFT_TEST_OPEN_EVENT_SOURCES__
      .filter((source) => source.url.includes("/parse/progress"));
    expect(parseSources.map((source) => source.url).sort()).toEqual([
      "/api/cases/case-final/images/img-1/parse/progress",
      "/api/cases/case-final/images/img-2/parse/progress",
    ]);
    emit(parseSources[0], { type: "parse_started", sequence: 1 });
    emit(parseSources[0], { type: "artifact_completed", artifact_key: "prefetch", record_count: 3, sequence: 2 });
    emit(parseSources[0], { type: "parse_completed", sequence: 3 });
    emit(parseSources[1], { type: "parse_started", sequence: 1 });
    emit(parseSources[1], { type: "artifact_completed", artifact_key: "runkeys", record_count: 2, sequence: 2 });
    emit(parseSources[1], { type: "parse_completed", sequence: 3 });
    jest.runOnlyPendingTimers();
    await nextTick();

    expect(A.st.parse.done).toBe(true);
    expect(A.st.selected).toEqual(expect.arrayContaining(["prefetch", "runkeys"]));
    expect(A.st.selectedAi).toEqual(["runkeys"]);
    expect(Object.keys(A.st.parsedSelections.images)).toEqual(["img-1", "img-2"]);
    expect(A.st.step).toBe(4);

    document.getElementById("investigation-context").value = "Investigate persistence.";
    await A.submitAnalysis();
    const analysisCall = calls.find(([url]) => url === "/api/cases/case-final/analyze");
    expect(requestBody(analysisCall)).toMatchObject({
      prompt: "Investigate persistence.",
      images: [{ image_id: "img-2", artifacts: ["runkeys"] }],
    });

    const analysisSource = window.__AIFT_TEST_OPEN_EVENT_SOURCES__
      .find((source) => source.url === "/api/cases/case-final/analyze/progress");
    expect(analysisSource).toBeTruthy();
    emit(analysisSource, { type: "analysis_started", analysis_artifact_count: 1, multi_image: true, sequence: 1 });
    emit(analysisSource, {
      type: "artifact_analysis_completed",
      image_id: "img-2",
      image_label: "Server",
      artifact_key: "runkeys",
      artifact_name: "Run/RunOnce Keys",
      analysis: "Persistence in Run keys.",
      sequence: 2,
    });
    emit(analysisSource, {
      type: "analysis_summary",
      summary: "Server persistence summary.",
      model_info: { provider: "local", model: "mock-model" },
      multi_image: true,
      cross_image_summary: "Only the AI-enabled image was analyzed.",
      images: { "img-2": { label: "Server", summary: "Run keys reviewed." } },
      sequence: 3,
    });
    emit(analysisSource, {
      type: "analysis_completed",
      multi_image: true,
      per_artifact: [{
        image_id: "img-2",
        image_label: "Server",
        artifact_key: "runkeys",
        artifact_name: "Run/RunOnce Keys",
        analysis: "Persistence in Run keys.",
      }],
      sequence: 4,
    });

    expect(A.st.step).toBe(5);
    expect(document.getElementById("analysis-results-list").textContent).toContain("Persistence in Run keys");
    expect(document.getElementById("artifact-findings").textContent).toContain("Server");

    document.getElementById("download-report").click();
    await nextTick();

    expect(global.fetch).toHaveBeenCalledWith(
      "/api/cases/case-final/report",
      expect.objectContaining({ method: "GET" }),
    );
    expect(URL.createObjectURL).toHaveBeenCalled();
    expect(document.getElementById("results-message").textContent).toContain("case-final_report.html");

    await A.loadChatHistory();
    await nextTick();
    expect(document.getElementById("chat-thread").textContent).toContain("Previous answer");

    A.el.chatInput.value = "What should I review next?";
    await A._sendChatMessage();
    await nextTick();
    const chatSource = window.__AIFT_TEST_OPEN_EVENT_SOURCES__
      .find((source) => source.url === "/api/cases/case-final/chat/stream");
    expect(chatSource).toBeTruthy();
    emit(chatSource, { type: "token", content: "Review Run keys first." });
    emit(chatSource, { type: "done", data_retrieved: ["runkeys.csv"] });
    expect(document.getElementById("chat-thread").textContent).toContain("Review Run keys first.");

    window.confirm = jest.fn(() => true);
    A.el.chatClear.click();
    await nextTick();
    expect(global.fetch).toHaveBeenCalledWith(
      "/api/cases/case-final/chat/history",
      expect.objectContaining({ method: "DELETE" }),
    );
    expect(document.getElementById("chat-thread").textContent).toContain("Chat history will appear here");

    A.openSettings();
    const settingsPanel = document.getElementById("settings-panel");
    expect(settingsPanel.hidden).toBe(false);
    expect(settingsPanel.contains(document.activeElement)).toBe(true);
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    expect(settingsPanel.hidden).toBe(true);
  });

  test("parse and analysis streams reconnect and cancel from the production template", async () => {
    jest.useFakeTimers();
    const calls = [];
    global.fetch = jest.fn((url, init = {}) => {
      calls.push([url, init]);
      if (url === "/api/cases/case-smoke/parse/cancel") {
        return jsonResponse({ success: true, status: "cancel_requested" });
      }
      if (url === "/api/cases/case-smoke/analyze") {
        return jsonResponse({ success: true });
      }
      if (url === "/api/cases/case-smoke/analyze/cancel") {
        return jsonResponse({ success: true, status: "cancel_requested" });
      }
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    A.setCaseId("case-smoke");
    A.st.artifacts = [{ key: "runkeys", name: "Run/RunOnce Keys", available: true }];
    A.st.selected = ["runkeys"];
    A.st.selectedAi = ["runkeys"];

    A.st.parse.run = true;
    A.st.parse.imageId = "img-smoke";
    A.st.parse.owner = A.newRunOwner("case-smoke", "parse");
    function startParseSmokeStream() {
      A.openSseStream(
        "/api/cases/case-smoke/images/img-smoke/parse/progress",
        A.st.parse,
        {
          onEvent: () => {},
          onError: () => A.retrySseStream(A.st.parse, { reconnect: startParseSmokeStream }),
        },
      );
    }
    startParseSmokeStream();
    const firstParseSource = window.__AIFT_TEST_OPEN_EVENT_SOURCES__
      .find((source) => source.url === "/api/cases/case-smoke/images/img-smoke/parse/progress");
    expect(firstParseSource).toBeTruthy();
    firstParseSource.onerror(new Event("error"));
    expect(A.st.parse.retryCount).toBe(1);
    jest.advanceTimersByTime(A.sseRetryDelayMs(1));
    await nextTick();
    const parseSources = window.__AIFT_TEST_OPEN_EVENT_SOURCES__
      .filter((source) => source.url === "/api/cases/case-smoke/images/img-smoke/parse/progress");
    expect(parseSources).toHaveLength(2);

    A.cancelParse();
    await nextTick();
    expect(global.fetch).toHaveBeenCalledWith(
      "/api/cases/case-smoke/parse/cancel",
      expect.objectContaining({ method: "POST" }),
    );
    expect(A.st.parse.run).toBe(false);

    A.st.analysis.run = true;
    A.st.analysis.owner = A.newRunOwner("case-smoke", "analysis");
    function startAnalysisSmokeStream() {
      A.openSseStream(
        "/api/cases/case-smoke/analyze/progress",
        A.st.analysis,
        {
          onEvent: () => {},
          onError: () => A.retrySseStream(A.st.analysis, { reconnect: startAnalysisSmokeStream }),
        },
      );
    }
    startAnalysisSmokeStream();
    const firstAnalysisSource = window.__AIFT_TEST_OPEN_EVENT_SOURCES__
      .find((source) => source.url === "/api/cases/case-smoke/analyze/progress");
    expect(firstAnalysisSource).toBeTruthy();
    firstAnalysisSource.onerror(new Event("error"));
    expect(A.st.analysis.retryCount).toBe(1);
    jest.advanceTimersByTime(A.sseRetryDelayMs(1));
    await nextTick();
    const analysisSources = window.__AIFT_TEST_OPEN_EVENT_SOURCES__
      .filter((source) => source.url === "/api/cases/case-smoke/analyze/progress");
    expect(analysisSources).toHaveLength(2);

    A.cancelAnalysis();
    await nextTick();
    expect(global.fetch).toHaveBeenCalledWith(
      "/api/cases/case-smoke/analyze/cancel",
      expect.objectContaining({ method: "POST" }),
    );
    expect(A.st.analysis.run).toBe(false);
    expect(calls.map(([url]) => url)).toEqual(expect.arrayContaining([
      "/api/cases/case-smoke/parse/cancel",
      "/api/cases/case-smoke/analyze/cancel",
    ]));
  });

  test("SSE control events do not unlock parse or analysis workflow steps", async () => {
    jest.useFakeTimers();
    global.fetch = jest.fn((url) => {
      if (url === "/api/cases/case-control/analyze") return jsonResponse({ success: true });
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    A.setCaseId("case-control");
    const parseOwner = A.newRunOwner("case-control", "parse");
    A.st.parse.owner = parseOwner;
    A.st.parse.run = true;
    A.st.selectedAi = ["evtx"];
    A.st.imageParse = {
      img1: {
        run: true,
        done: false,
        fail: false,
        owner: parseOwner,
        rows: {},
        status: {},
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

    A._startImageParseSse("case-control", "img1", parseOwner);
    const parseSource = window.__AIFT_TEST_OPEN_EVENT_SOURCES__
      .find((source) => source.url === "/api/cases/case-control/images/img1/parse/progress");
    expect(parseSource).toBeTruthy();
    emit(parseSource, { type: "idle", sequence: 1 });
    jest.runOnlyPendingTimers();

    expect(A.st.parse.done).toBe(false);
    expect(A.st.step).not.toBe(4);

    A.st.parse.done = true;
    A.st.parse.fail = false;
    A.st.selectedAi = ["evtx"];
    A.el.prompt.value = "Investigate.";
    await A.submitAnalysis();
    const analysisSource = window.__AIFT_TEST_OPEN_EVENT_SOURCES__
      .find((source) => source.url === "/api/cases/case-control/analyze/progress");
    expect(analysisSource).toBeTruthy();
    emit(analysisSource, { type: "complete", sequence: 1 });

    expect(A.st.analysis.done).toBe(false);
    expect(A.st.analysis.fail).toBe(true);
    expect(A.st.step).not.toBe(5);
  });
});
