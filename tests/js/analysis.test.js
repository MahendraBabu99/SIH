/**
 * Unit tests for AIFT analysis SSE and results rendering (analysis.js).
 *
 * Covers:
 *  - resetAnalysisState clears all analysis state
 *  - renderAnalysis renders placeholder and artifact cards
 *  - renderExecSummary renders summary markdown
 *  - renderFindings renders collapsible details elements
 *  - setProvider updates provider display text
 *  - closeAnalysisSse closes the SSE channel
 *  - Analysis state lifecycle flags
 *  - Analysis navigation prerequisites
 *
 * @jest-environment jsdom
 */

"use strict";

const { setupAift, mustGet, mustQuery } = require("./harness");

let A;

beforeEach(() => {
  A = setupAift();
});

// ── resetAnalysisState ──────────────────────────────────────────────────────

describe("resetAnalysisState", () => {
  test("resets all analysis flags", () => {
    A.st.analysis.run = true;
    A.st.analysis.done = true;
    A.st.analysis.fail = true;
    A.st.analysis.retryCount = 5;
    A.st.analysis.seq = 10;

    A.resetAnalysisState();

    expect(A.st.analysis.run).toBe(false);
    expect(A.st.analysis.done).toBe(false);
    expect(A.st.analysis.fail).toBe(false);
    expect(A.st.analysis.retryCount).toBe(0);
    expect(A.st.analysis.seq).toBe(-1);
  });

  test("clears analysis order and byKey data", () => {
    A.st.analysis.order = ["evtx", "mft"];
    A.st.analysis.byKey = {
      evtx: { key: "evtx", name: "Event Logs", text: "analysis" },
      mft: { key: "mft", name: "MFT", text: "analysis" },
    };
    A.st.analysis.summary = "Executive summary";
    A.st.analysis.model = { provider: "claude" };

    A.resetAnalysisState();

    expect(A.st.analysis.order).toEqual([]);
    expect(A.st.analysis.byKey).toEqual({});
    expect(A.st.analysis.summary).toBe("");
    expect(A.st.analysis.model).toEqual({});
  });

  test("re-enables run button", () => {
    if (A.el.runBtn) {
      A.el.runBtn.disabled = true;
      A.resetAnalysisState();
      expect(A.el.runBtn.disabled).toBe(false);
    }
  });

  test("hides cancel button", () => {
    if (A.el.cancelAnalysis) {
      A.el.cancelAnalysis.hidden = false;
      A.resetAnalysisState();
      expect(A.el.cancelAnalysis.hidden).toBe(true);
    }
  });
});

// ── renderAnalysis ──────────────────────────────────────────────────────────

describe("renderAnalysis", () => {
  test("renders empty placeholder when no analysis results exist", () => {
    A.st.analysis.order = [];
    A.st.analysis.byKey = {};
    A.renderAnalysis();
    if (A.el.analysisList) {
      expect(A.el.analysisList.textContent).toContain("No analysis output yet");
    }
  });

  test("renders analysis cards for completed artifacts", () => {
    A.st.analysis.order = ["evtx"];
    A.st.analysis.byKey = {
      evtx: { key: "evtx", name: "Event Logs", text: "Found suspicious events.", model: "claude-3", isThinking: false },
    };
    A.renderAnalysis();
    if (A.el.analysisList) {
      const cards = A.el.analysisList.querySelectorAll(".analysis-card");
      expect(cards).toHaveLength(1);
      expect(cards[0].querySelector("h4").textContent).toBe("Event Logs");
    }
  });

  test("renders multiple analysis cards in order", () => {
    A.st.analysis.order = ["evtx", "mft"];
    A.st.analysis.byKey = {
      evtx: { key: "evtx", name: "Event Logs", text: "Events analysis.", model: "", isThinking: false },
      mft: { key: "mft", name: "MFT", text: "MFT analysis.", model: "gpt-4", isThinking: false },
    };
    A.renderAnalysis();
    if (A.el.analysisList) {
      const cards = A.el.analysisList.querySelectorAll(".analysis-card");
      expect(cards).toHaveLength(2);
      expect(cards[0].querySelector("h4").textContent).toBe("Event Logs");
      expect(cards[1].querySelector("h4").textContent).toBe("MFT");
    }
  });

  test("renders thinking placeholder for in-progress analysis", () => {
    A.st.analysis.order = ["evtx"];
    A.st.analysis.byKey = {
      evtx: { key: "evtx", name: "Event Logs", text: "", model: "", isThinking: true, thinkingText: "Model is thinking..." },
    };
    A.renderAnalysis();
    if (A.el.analysisList) {
      expect(A.el.analysisList.textContent).toContain("Model is thinking");
    }
  });

  test("shows model info when available", () => {
    A.st.analysis.order = ["evtx"];
    A.st.analysis.byKey = {
      evtx: { key: "evtx", name: "Event Logs", text: "Result.", model: "claude-3-opus", isThinking: false },
    };
    A.renderAnalysis();
    if (A.el.analysisList) {
      const mono = A.el.analysisList.querySelector(".mono");
      expect(mono.textContent).toContain("claude-3-opus");
    }
  });

  test("renders thinking text in a separate collapsible panel", () => {
    A.st.analysis.order = ["evtx"];
    A.st.analysis.byKey = {
      evtx: {
        key: "evtx",
        name: "Event Logs",
        text: "",
        partialText: "Partial visible answer.",
        thinkingText: "hidden model reasoning",
        model: "",
        isThinking: true,
      },
    };
    A.renderAnalysis();

    const card = mustQuery(A.el.analysisList, ".analysis-card");
    const answer = mustQuery(card, ".markdown-output");
    const panel = mustQuery(card, ".analysis-reasoning-panel");
    const reasoning = mustQuery(panel, ".analysis-reasoning-text");

    expect(answer.textContent).toContain("Partial visible answer.");
    expect(answer.textContent).not.toContain("hidden model reasoning");
    expect(panel.open).toBe(false);
    expect(reasoning.textContent).toBe("hidden model reasoning");
  });

  test("preserves reasoning panel when streamed analysis completes", () => {
    A._onAnalysisEvent({ type: "analysis_started", analysis_artifact_count: 1, sequence: 0 });
    A._onAnalysisEvent({
      type: "artifact_analysis_started",
      result: { artifact_key: "evtx", artifact_name: "Event Logs", model: "m" },
      sequence: 1,
    });
    A._onAnalysisEvent({
      type: "artifact_analysis_thinking",
      result: {
        artifact_key: "evtx",
        artifact_name: "Event Logs",
        thinking_text: "hidden model reasoning",
        partial_text: "Partial visible answer.",
      },
      sequence: 2,
    });
    A._onAnalysisEvent({
      type: "artifact_analysis_completed",
      result: {
        artifact_key: "evtx",
        artifact_name: "Event Logs",
        analysis: "Final visible answer.",
      },
      sequence: 3,
    });

    const card = mustQuery(A.el.analysisList, ".analysis-card");
    const answer = mustQuery(card, ".markdown-output");
    const panel = mustQuery(card, ".analysis-reasoning-panel");
    const reasoning = mustQuery(panel, ".analysis-reasoning-text");

    expect(answer.textContent).toContain("Final visible answer.");
    expect(answer.textContent).not.toContain("hidden model reasoning");
    expect(panel.open).toBe(false);
    expect(reasoning.textContent).toBe("hidden model reasoning");
  });
});

// ── renderExecSummary ───────────────────────────────────────────────────────

describe("renderExecSummary", () => {
  test("renders summary text as markdown", () => {
    A.st.analysis.summary = "## Summary\n\nKey finding: **malware detected**.";
    A.renderExecSummary();
    if (A.el.summaryOut) {
      expect(A.el.summaryOut.querySelector("h2")).not.toBeNull();
      expect(A.el.summaryOut.querySelector("strong")).not.toBeNull();
    }
  });

  test("renders placeholder when summary is empty", () => {
    A.st.analysis.summary = "";
    A.renderExecSummary();
    if (A.el.summaryOut) {
      expect(A.el.summaryOut.textContent).toContain("Summary is generated after analysis completes");
    }
  });
});

// ── renderFindings ──────────────────────────────────────────────────────────

describe("renderFindings", () => {
  test("renders placeholder when no findings exist", () => {
    A.st.analysis.order = [];
    A.renderFindings();
    if (A.el.findings) {
      expect(A.el.findings.textContent).toContain("Findings will appear here");
    }
  });

  test("renders collapsible details elements for each artifact", () => {
    A.st.analysis.order = ["evtx", "mft"];
    A.st.analysis.byKey = {
      evtx: { key: "evtx", name: "Event Logs", text: "Events finding.", isThinking: false },
      mft: { key: "mft", name: "MFT", text: "MFT finding.", isThinking: false },
    };
    A.renderFindings();
    if (A.el.findings) {
      const details = A.el.findings.querySelectorAll("details");
      expect(details).toHaveLength(2);
      expect(details[0].querySelector("summary").textContent).toBe("Event Logs");
      expect(details[1].querySelector("summary").textContent).toBe("MFT");
    }
  });

  test("first finding is open by default", () => {
    A.st.analysis.order = ["evtx"];
    A.st.analysis.byKey = {
      evtx: { key: "evtx", name: "Event Logs", text: "Finding.", isThinking: false },
    };
    A.renderFindings();
    if (A.el.findings) {
      const details = A.el.findings.querySelector("details");
      expect(details.open).toBe(true);
    }
  });

  test("subsequent findings are closed by default", () => {
    A.st.analysis.order = ["evtx", "mft"];
    A.st.analysis.byKey = {
      evtx: { key: "evtx", name: "Event Logs", text: "Finding 1.", isThinking: false },
      mft: { key: "mft", name: "MFT", text: "Finding 2.", isThinking: false },
    };
    A.renderFindings();
    if (A.el.findings) {
      const details = A.el.findings.querySelectorAll("details");
      expect(details[0].open).toBe(true);
      expect(details[1].open).toBe(false);
    }
  });
});

// ── setProvider ─────────────────────────────────────────────────────────────

describe("setProvider", () => {
  test("sets provider name text", () => {
    A.setProvider("Claude (claude-3-opus)");
    if (A.el.providerName) {
      expect(A.el.providerName.textContent).toBe("Claude (claude-3-opus)");
    }
  });

  test("shows 'Not configured' for empty input", () => {
    A.setProvider("");
    if (A.el.providerName) {
      expect(A.el.providerName.textContent).toBe("Not configured");
    }
  });

  test("shows 'Not configured' for null input", () => {
    A.setProvider(null);
    if (A.el.providerName) {
      expect(A.el.providerName.textContent).toBe("Not configured");
    }
  });
});

// ── closeAnalysisSse ────────────────────────────────────────────────────────

describe("closeAnalysisSse", () => {
  test("closes the analysis SSE channel", () => {
    const mockEs = { close: jest.fn() };
    A.st.analysis.es = mockEs;
    A.st.analysis.retry = setTimeout(() => {}, 10000);

    A.closeAnalysisSse();

    expect(mockEs.close).toHaveBeenCalled();
    expect(A.st.analysis.es).toBeNull();
    expect(A.st.analysis.retry).toBeNull();
  });
});

// ── Analysis status banner ──────────────────────────────────────────────────

describe("analysis status banner", () => {
  test("banner is hidden initially", () => {
    expect(A.el.analysisStatusBanner).not.toBeNull();
    expect(A.el.analysisStatusBanner.hidden).toBe(true);
  });

  test("analysis_started event shows 'Preparing analysis' banner", () => {
    A._onAnalysisEvent({ type: "analysis_started", analysis_artifact_count: 3, sequence: 0 });
    expect(A.el.analysisStatusBanner.hidden).toBe(false);
    expect(A.el.analysisStatusText.textContent).toContain("Preparing analysis");
  });

  test("analysis_started stores totalArtifacts count", () => {
    A._onAnalysisEvent({ type: "analysis_started", analysis_artifact_count: 5, sequence: 0 });
    expect(A.st.analysis.totalArtifacts).toBe(5);
  });

  test("artifact_analysis_started event shows artifact name and progress", () => {
    A._onAnalysisEvent({ type: "analysis_started", analysis_artifact_count: 3, sequence: 0 });
    A._onAnalysisEvent({
      type: "artifact_analysis_started",
      result: { artifact_key: "evtx", artifact_name: "Event Logs", model: "claude-3" },
      sequence: 1,
    });
    expect(A.el.analysisStatusBanner.hidden).toBe(false);
    expect(A.el.analysisStatusText.textContent).toContain("Event Logs");
    expect(A.el.analysisStatusText.textContent).toContain("1/3");
  });

  test("second artifact_analysis_started updates progress counter", () => {
    A._onAnalysisEvent({ type: "analysis_started", analysis_artifact_count: 3, sequence: 0 });
    A._onAnalysisEvent({
      type: "artifact_analysis_started",
      result: { artifact_key: "evtx", artifact_name: "Event Logs", model: "claude-3" },
      sequence: 1,
    });
    A._onAnalysisEvent({
      type: "artifact_analysis_completed",
      result: { artifact_key: "evtx", analysis: "done", artifact_name: "Event Logs" },
      sequence: 2,
    });
    A._onAnalysisEvent({
      type: "artifact_analysis_started",
      result: { artifact_key: "mft", artifact_name: "MFT", model: "claude-3" },
      sequence: 3,
    });
    expect(A.el.analysisStatusText.textContent).toContain("MFT");
    expect(A.el.analysisStatusText.textContent).toContain("2/3");
  });

  test("artifact_analysis_started falls back to artifactName lookup when artifact_name missing", () => {
    A.st.artifactNames["shimcache"] = "Shimcache";
    A._onAnalysisEvent({ type: "analysis_started", analysis_artifact_count: 2, sequence: 0 });
    A._onAnalysisEvent({
      type: "artifact_analysis_started",
      result: { artifact_key: "shimcache", model: "gpt-4" },
      sequence: 1,
    });
    expect(A.el.analysisStatusText.textContent).toContain("Shimcache");
  });

  test("analysis_completed hides the banner", () => {
    A._onAnalysisEvent({ type: "analysis_started", analysis_artifact_count: 1, sequence: 0 });
    A._onAnalysisEvent({
      type: "artifact_analysis_started",
      result: { artifact_key: "evtx", artifact_name: "Event Logs", model: "m" },
      sequence: 1,
    });
    expect(A.el.analysisStatusBanner.hidden).toBe(false);

    // Mark analysis as running so the completed handler can transition state
    A.st.analysis.run = true;
    A._onAnalysisEvent({
      type: "analysis_completed",
      artifact_count: 1,
      images: {
        img1: {
          label: "Image 1",
          per_artifact: [{ artifact_key: "evtx", analysis: "result" }],
        },
      },
      sequence: 2,
    });
    expect(A.el.analysisStatusBanner.hidden).toBe(true);
  });

  test("analysis_failed hides the banner", () => {
    A._onAnalysisEvent({ type: "analysis_started", analysis_artifact_count: 1, sequence: 0 });
    expect(A.el.analysisStatusBanner.hidden).toBe(false);

    A._onAnalysisEvent({ type: "analysis_failed", error: "Provider error", sequence: 1 });
    expect(A.el.analysisStatusBanner.hidden).toBe(true);
  });

  test("resetAnalysisState hides the banner and clears totalArtifacts", () => {
    A._onAnalysisEvent({ type: "analysis_started", analysis_artifact_count: 3, sequence: 0 });
    expect(A.el.analysisStatusBanner.hidden).toBe(false);
    expect(A.st.analysis.totalArtifacts).toBe(3);

    A.resetAnalysisState();
    expect(A.el.analysisStatusBanner.hidden).toBe(true);
    expect(A.st.analysis.totalArtifacts).toBe(0);
  });

  test("cancelAnalysis hides the banner", () => {
    A.st.analysis.run = true;
    A._onAnalysisEvent({ type: "analysis_started", analysis_artifact_count: 2, sequence: 0 });
    expect(A.el.analysisStatusBanner.hidden).toBe(false);

    A.cancelAnalysis();
    expect(A.el.analysisStatusBanner.hidden).toBe(true);
  });

  test("thinking event keeps the banner visible with current artifact name", () => {
    A._onAnalysisEvent({ type: "analysis_started", analysis_artifact_count: 2, sequence: 0 });
    A._onAnalysisEvent({
      type: "artifact_analysis_started",
      result: { artifact_key: "evtx", artifact_name: "Event Logs", model: "m" },
      sequence: 1,
    });
    const textAfterStart = A.el.analysisStatusText.textContent;

    A._onAnalysisEvent({
      type: "artifact_analysis_thinking",
      result: { artifact_key: "evtx", thinking_text: "Analyzing patterns..." },
      sequence: 2,
    });
    // Banner text should still show the artifact name from the started event
    expect(A.el.analysisStatusBanner.hidden).toBe(false);
    expect(A.el.analysisStatusText.textContent).toBe(textAfterStart);
  });
});

// ── upsertAnalysis: summary field support ──────────────────────────────────

describe("upsertAnalysis picks up summary field from per-image summary events", () => {
  test("artifact_analysis_completed with 'analysis' key populates text", () => {
    A._onAnalysisEvent({ type: "analysis_started", analysis_artifact_count: 1, sequence: 0 });
    A._onAnalysisEvent({
      type: "artifact_analysis_completed",
      artifact_key: "evtx",
      result: { artifact_key: "evtx", artifact_name: "Event Logs", analysis: "Found lateral movement." },
      sequence: 1,
    });

    const entry = A.st.analysis.byKey["evtx"];
    expect(entry).toBeDefined();
    expect(entry.text).toBe("Found lateral movement.");
  });

  test("artifact_analysis_completed with 'result' key populates text", () => {
    A._onAnalysisEvent({ type: "analysis_started", analysis_artifact_count: 1, sequence: 0 });
    A._onAnalysisEvent({
      type: "artifact_analysis_completed",
      artifact_key: "mft",
      result: { artifact_key: "mft", artifact_name: "MFT", result: "File system anomaly detected." },
      sequence: 1,
    });

    const entry = A.st.analysis.byKey["mft"];
    expect(entry).toBeDefined();
    expect(entry.text).toBe("File system anomaly detected.");
  });

  test("artifact_analysis_completed with 'summary' key populates text (per-image summary fix)", () => {
    A._onAnalysisEvent({ type: "analysis_started", analysis_artifact_count: 1, sequence: 0 });
    A._onAnalysisEvent({
      type: "artifact_analysis_completed",
      artifact_key: "summary_img1",
      result: {
        artifact_key: "summary_img1",
        artifact_name: "Summary: Workstation-PC01",
        image_id: "img1",
        image_label: "Workstation-PC01",
        summary: "This system shows evidence of compromise via phishing.",
      },
      sequence: 1,
    });

    const entry = A.st.analysis.byKey["img1::summary_img1"];
    expect(entry).toBeDefined();
    expect(entry.text).toBe("This system shows evidence of compromise via phishing.");
    expect(entry.text).not.toBe("");
  });

  test("per-image summary card renders text instead of empty placeholder", () => {
    A._onAnalysisEvent({ type: "analysis_started", analysis_artifact_count: 1, sequence: 0 });
    A._onAnalysisEvent({
      type: "artifact_analysis_completed",
      artifact_key: "summary_img2",
      result: {
        artifact_key: "summary_img2",
        artifact_name: "Summary: Server-DC01",
        image_id: "img2",
        image_label: "Server-DC01",
        summary: "No suspicious activity found on this server.",
      },
      sequence: 1,
    });

    A.st.analysis.multiImage = true;
    A.renderAnalysis();

    if (A.el.analysisList) {
      const text = A.el.analysisList.textContent;
      expect(text).toContain("No suspicious activity found on this server.");
      expect(text).not.toContain("No analysis text returned");
    }
  });

  test("summary field is not used when analysis field is present", () => {
    A._onAnalysisEvent({ type: "analysis_started", analysis_artifact_count: 1, sequence: 0 });
    A._onAnalysisEvent({
      type: "artifact_analysis_completed",
      artifact_key: "evtx",
      result: {
        artifact_key: "evtx",
        artifact_name: "Event Logs",
        analysis: "Primary analysis text.",
        summary: "Should not be used.",
      },
      sequence: 1,
    });

    const entry = A.st.analysis.byKey["evtx"];
    expect(entry.text).toBe("Primary analysis text.");
  });

  test("empty analysis and result fields fall through to summary", () => {
    A._onAnalysisEvent({ type: "analysis_started", analysis_artifact_count: 1, sequence: 0 });
    A._onAnalysisEvent({
      type: "artifact_analysis_completed",
      artifact_key: "summary_img3",
      result: {
        artifact_key: "summary_img3",
        artifact_name: "Summary: Image 3",
        image_id: "img3",
        image_label: "Image 3",
        analysis: "",
        result: "",
        summary: "Fallback summary text.",
      },
      sequence: 1,
    });

    const entry = A.st.analysis.byKey["img3::summary_img3"];
    expect(entry).toBeDefined();
    expect(entry.text).toBe("Fallback summary text.");
  });

  test("all three fields empty produces empty text", () => {
    A._onAnalysisEvent({ type: "analysis_started", analysis_artifact_count: 1, sequence: 0 });
    A._onAnalysisEvent({
      type: "artifact_analysis_completed",
      artifact_key: "empty_test",
      result: { artifact_key: "empty_test", artifact_name: "Empty" },
      sequence: 1,
    });

    const entry = A.st.analysis.byKey["empty_test"];
    expect(entry).toBeDefined();
    expect(entry.text).toBe("");
  });

  test("per-image summary finding renders in findings section", () => {
    A._onAnalysisEvent({ type: "analysis_started", analysis_artifact_count: 1, sequence: 0 });
    A._onAnalysisEvent({
      type: "artifact_analysis_completed",
      artifact_key: "summary_imgA",
      result: {
        artifact_key: "summary_imgA",
        artifact_name: "Summary: Laptop-01",
        image_id: "imgA",
        image_label: "Laptop-01",
        summary: "Malware indicators found in prefetch data.",
      },
      sequence: 1,
    });

    A.renderFindings();

    if (A.el.findings) {
      const text = A.el.findings.textContent;
      expect(text).toContain("Malware indicators found in prefetch data.");
      expect(text).not.toContain("No analysis text returned");
    }
  });
});

// ── Analysis navigation prerequisites ───────────────────────────────────────

describe("multi-image analysis rendering behavior", () => {
  test("groups artifact cards by image label and renders cross-image summary", () => {
    A._onAnalysisEvent({ type: "analysis_started", analysis_artifact_count: 2, sequence: 0 });
    A._onAnalysisEvent({
      type: "artifact_analysis_completed",
      artifact_key: "evtx",
      image_id: "img1",
      image_label: "Workstation",
      result: { artifact_name: "Event Logs", analysis: "Suspicious service install." },
      sequence: 1,
    });
    A._onAnalysisEvent({
      type: "artifact_analysis_completed",
      artifact_key: "mft",
      image_id: "img2",
      image_label: "Server",
      result: { artifact_name: "MFT", analysis: "Unexpected executable." },
      sequence: 2,
    });
    A._onAnalysisEvent({
      type: "analysis_summary",
      multi_image: true,
      images: {
        img1: { label: "Workstation", summary: "Workstation summary" },
        img2: { label: "Server", summary: "Server summary" },
      },
      cross_image_summary: "Activity links the workstation and server.",
      sequence: 3,
    });
    A.renderAnalysis();

    const groups = A.el.analysisList.querySelectorAll(".analysis-image-group");
    expect(groups).toHaveLength(2);
    expect(groups[0].textContent).toContain("Workstation");
    expect(groups[0].textContent).toContain("Suspicious service install.");
    expect(groups[1].textContent).toContain("Server");
    expect(groups[1].textContent).toContain("Unexpected executable.");
    expect(document.getElementById("cross-system-analysis").hidden).toBe(false);
    expect(document.getElementById("cross-system-analysis").textContent).toContain(
      "Activity links the workstation and server."
    );
  });

  test("single fallback does not expose __single__ as a visible header", () => {
    A.st.analysis.multiImage = true;
    A.st.analysis.order = ["evtx"];
    A.st.analysis.byKey = {
      evtx: { key: "evtx", name: "Event Logs", text: "Single image result.", isThinking: false },
    };

    A.renderAnalysis();
    A.renderFindings();

    expect(A.el.analysisList.textContent).toContain("Single image result.");
    expect(A.el.analysisList.textContent).not.toContain("__single__");
    expect(A.el.findings.textContent).not.toContain("__single__");
  });
});

// ── Single-image duplicate rendering regression (P4-F1 / P8-F1) ─────────────

describe("single-image analysis_completed does not duplicate streamed rows", () => {
  /** Count direct-child <details> only: reasoning panels nest a second
      <details> inside each findings entry, so querySelectorAll("details")
      would over-count. */
  function topLevelFindingsDetails() {
    return Array.from(A.el.findings.children).filter((c) => c.tagName === "DETAILS");
  }

  test("canonical images mapping merges into bare streamed keys and keeps reasoning", () => {
    // Replay the exact single-image backend sequence: streamed result
    // payloads carry NO image context (app/routes/tasks.py strips
    // image_id/image_label when include_image_context is False), then the
    // terminal analysis_completed carries the canonical one-entry
    // image-scoped images mapping with multi_image:false.
    A.st.analysis.run = true;
    A._onAnalysisEvent({ type: "analysis_started", analysis_artifact_count: 1, multi_image: false, sequence: 0 });
    A._onAnalysisEvent({
      type: "artifact_analysis_started",
      artifact_key: "runkeys",
      result: { artifact_key: "runkeys", artifact_name: "Run/RunOnce Keys", model: "m" },
      sequence: 1,
    });
    A._onAnalysisEvent({
      type: "artifact_analysis_thinking",
      artifact_key: "runkeys",
      result: { artifact_key: "runkeys", artifact_name: "Run/RunOnce Keys", thinking_text: "hidden model reasoning" },
      sequence: 2,
    });
    A._onAnalysisEvent({
      type: "artifact_analysis_completed",
      artifact_key: "runkeys",
      status: "complete",
      result: { artifact_key: "runkeys", artifact_name: "Run/RunOnce Keys", analysis: "Persistence found." },
      sequence: 3,
    });
    A._onAnalysisEvent({
      type: "analysis_summary",
      summary: "S",
      model_info: {},
      multi_image: false,
      image_scoped: true,
      images: { "img-uuid-1": { label: "Image 1", summary: "S" } },
      cross_image_summary: "",
      skipped_images: [],
      sequence: 4,
    });
    A._onAnalysisEvent({
      type: "analysis_completed",
      artifact_count: 1,
      multi_image: false,
      image_scoped: true,
      images: {
        "img-uuid-1": {
          label: "Image 1",
          per_artifact: [{
            artifact_key: "runkeys",
            artifact_name: "Run/RunOnce Keys",
            analysis: "Persistence found.",
            analysis_text: "Persistence found.",
          }],
          summary: "S",
        },
      },
      cross_image_summary: "",
      skipped_images: [],
      sequence: 5,
    });

    // Exactly one tracked entry per artifact: no duplicate composite key.
    expect(A.st.analysis.order).toEqual(["runkeys"]);
    expect(Object.keys(A.st.analysis.byKey)).toEqual(["runkeys"]);

    // Exactly one analysis card in Step 4 live results.
    const cards = A.el.analysisList.querySelectorAll(".analysis-card");
    expect(cards).toHaveLength(1);
    expect(cards[0].textContent).toContain("Persistence found.");

    // Exactly one top-level findings <details> in Step 5.
    const findingsEntries = topLevelFindingsDetails();
    expect(findingsEntries).toHaveLength(1);
    expect(findingsEntries[0].textContent).toContain("Persistence found.");

    // Streamed reasoning text (artifact_analysis_thinking) survives completion.
    expect(A.st.analysis.byKey["runkeys"].thinkingText).toBe("hidden model reasoning");
    const cardReasoning = mustQuery(cards[0], ".analysis-reasoning-panel .analysis-reasoning-text");
    expect(cardReasoning.textContent).toBe("hidden model reasoning");
    const findingsReasoning = mustQuery(findingsEntries[0], ".analysis-reasoning-panel .analysis-reasoning-text");
    expect(findingsReasoning.textContent).toBe("hidden model reasoning");
  });

  test("single-image completion with no streamed events recovers onto bare keys", () => {
    // Recovery path (e.g. SSE reconnect missed the streamed events): the
    // analysis_completed flatten must still create rows, keyed bare so they
    // are consistent with streamed single-image keys.
    A.st.analysis.run = true;
    A._onAnalysisEvent({
      type: "analysis_completed",
      artifact_count: 1,
      multi_image: false,
      image_scoped: true,
      images: {
        "img-uuid-1": {
          label: "Image 1",
          per_artifact: [{ artifact_key: "runkeys", artifact_name: "Run/RunOnce Keys", analysis: "Persistence found." }],
          summary: "S",
        },
      },
      cross_image_summary: "",
      skipped_images: [],
      sequence: 1,
    });

    expect(A.st.analysis.order).toEqual(["runkeys"]);
    expect(A.el.analysisList.querySelectorAll(".analysis-card")).toHaveLength(1);
    expect(A.el.analysisList.textContent).toContain("Persistence found.");
    expect(topLevelFindingsDetails()).toHaveLength(1);
  });

  test("multi-image completion keeps composite keys and per-image grouping unchanged", () => {
    // Control: streamed multi-image events carry image context, so the
    // canonical completion payload must keep matching composite keys with
    // one card per image even when images share the same artifact_key.
    A.st.analysis.run = true;
    A._onAnalysisEvent({ type: "analysis_started", analysis_artifact_count: 2, multi_image: true, sequence: 0 });
    A._onAnalysisEvent({
      type: "artifact_analysis_completed",
      artifact_key: "evtx",
      image_id: "img1",
      image_label: "Workstation",
      result: { artifact_key: "evtx", artifact_name: "Event Logs", image_id: "img1", image_label: "Workstation", analysis: "Suspicious service install." },
      sequence: 1,
    });
    A._onAnalysisEvent({
      type: "artifact_analysis_completed",
      artifact_key: "evtx",
      image_id: "img2",
      image_label: "Server",
      result: { artifact_key: "evtx", artifact_name: "Event Logs", image_id: "img2", image_label: "Server", analysis: "Unexpected executable." },
      sequence: 2,
    });
    A._onAnalysisEvent({
      type: "analysis_completed",
      artifact_count: 2,
      multi_image: true,
      image_scoped: true,
      images: {
        img1: {
          label: "Workstation",
          per_artifact: [{ artifact_key: "evtx", artifact_name: "Event Logs", analysis: "Suspicious service install." }],
          summary: "W",
        },
        img2: {
          label: "Server",
          per_artifact: [{ artifact_key: "evtx", artifact_name: "Event Logs", analysis: "Unexpected executable." }],
          summary: "S",
        },
      },
      cross_image_summary: "Linked activity.",
      skipped_images: [],
      sequence: 3,
    });

    expect(A.st.analysis.order).toEqual(["img1::evtx", "img2::evtx"]);
    const groups = A.el.analysisList.querySelectorAll(".analysis-image-group");
    expect(groups).toHaveLength(2);
    expect(groups[0].querySelectorAll(".analysis-card")).toHaveLength(1);
    expect(groups[1].querySelectorAll(".analysis-card")).toHaveLength(1);
    expect(groups[0].textContent).toContain("Workstation");
    expect(groups[0].textContent).toContain("Suspicious service install.");
    expect(groups[1].textContent).toContain("Server");
    expect(groups[1].textContent).toContain("Unexpected executable.");
  });
});

describe("analysis navigation prerequisites", () => {
  test("step 5 is blocked when analysis not done", () => {
    A.setCaseId("test-case");
    A.st.selected = ["evtx"];
    A.st.selectedAi = ["evtx"];
    A.st.parse.done = true;
    A.st.analysis.done = false;
    A.updateNav();
    expect(A.el.indicators[4].classList.contains("is-disabled")).toBe(true);
  });

  test("step 5 is accessible when analysis is done", () => {
    A.setCaseId("test-case");
    A.st.selected = ["evtx"];
    A.st.selectedAi = ["evtx"];
    A.st.parse.done = true;
    A.st.analysis.done = true;
    A.updateNav();
    expect(A.el.indicators[4].classList.contains("is-disabled")).toBe(false);
  });

  test("step 4 blocked when parse done but no AI artifacts", () => {
    A.setCaseId("test-case");
    A.st.selected = ["evtx"];
    A.st.selectedAi = [];
    A.st.parse.done = true;
    A.updateNav();
    expect(A.el.indicators[3].classList.contains("is-disabled")).toBe(true);
    expect(A.el.indicators[3].title).toContain("Parse and use in AI");
  });
});

describe("analysis ownership and parsed-selection snapshots", () => {
  function mockJsonFetch(payload = { success: true }) {
    global.fetch = jest.fn(() => Promise.resolve({
      ok: true,
      status: 202,
      headers: { get: () => "application/json" },
      json: async () => payload,
      text: async () => JSON.stringify(payload),
    }));
  }

  test("old analysis run events do not render into the current case", () => {
    A.setCaseId("old-case");
    const owner = A.newRunOwner("old-case", "analysis");
    A.st.analysis.owner = owner;
    A.st.analysis.run = true;

    A.setCaseId("new-case");
    A.resetAnalysisState();
    A._onAnalysisEvent({
      type: "artifact_analysis_completed",
      artifact_key: "evtx",
      result: { artifact_key: "evtx", analysis: "stale result" },
      sequence: 1,
    }, owner);

    expect(A.st.analysis.order).toEqual([]);
    expect(A.el.analysisList.textContent).not.toContain("stale result");
  });

  test("idle does not complete an active analysis", () => {
    A.setCaseId("case-analysis-idle");
    const owner = A.newRunOwner("case-analysis-idle", "analysis");
    A.st.analysis.owner = owner;
    A.st.analysis.run = true;

    A._onAnalysisEvent({ type: "idle", sequence: 1 }, owner);

    expect(A.st.analysis.done).toBe(false);
    expect(A.st.analysis.fail).toBe(true);
    expect(A.st.step).not.toBe(5);
    expect(A.el.analysisMsg.textContent).toContain("No active analysis progress stream");
  });

  test("synthetic complete does not complete an active analysis", () => {
    A.setCaseId("case-analysis-complete");
    const owner = A.newRunOwner("case-analysis-complete", "analysis");
    A.st.analysis.owner = owner;
    A.st.analysis.run = true;

    A._onAnalysisEvent({ type: "complete", sequence: 1 }, owner);

    expect(A.st.analysis.done).toBe(false);
    expect(A.st.analysis.fail).toBe(true);
    expect(A.st.step).not.toBe(5);
    expect(A.el.analysisMsg.textContent).toContain("before a completion event");
  });

  test("analysis_cancelled closes without marking analysis complete", () => {
    A.setCaseId("case-analysis-cancelled");
    const owner = A.newRunOwner("case-analysis-cancelled", "analysis");
    A.st.analysis.owner = owner;
    A.st.analysis.run = true;

    A._onAnalysisEvent({ type: "analysis_cancelled", sequence: 1 }, owner);

    expect(A.st.analysis.run).toBe(false);
    expect(A.st.analysis.done).toBe(false);
    expect(A.st.analysis.fail).toBe(false);
    expect(A.el.analysisMsg.textContent).toContain("cancelled");
  });

  test("multi-image analysis payload uses parsed snapshot, not live tab state", async () => {
    A.setCaseId("case-analysis");
    A.st.parse.done = true;
    A.st.selectedAi = ["evtx"];
    A.st.images = [
      { image_id: "img1", label: "Image 1", available_artifacts: [{ key: "evtx", available: true }, { key: "mft", available: true }] },
      { image_id: "img2", label: "Image 2", available_artifacts: [{ key: "mft", available: true }] },
    ];
    A.st.parsedSelections = {
      caseId: "case-analysis",
      runId: "parse-1",
      mode: "multi",
      artifactOptions: [],
      artifacts: ["evtx"],
      aiArtifacts: ["evtx"],
      images: {
        img1: { image_id: "img1", label: "Image 1", artifacts: ["evtx"], aiArtifacts: ["evtx"], artifactOptions: [{ artifact_key: "evtx", mode: A.MODE_PARSE_AND_AI }] },
      },
    };
    A.buildMultiImageArtifactTabs();

    const panel = document.querySelector(".artifact-image-panel[data-image-id='img1']");
    panel.querySelector("input[data-artifact-key='evtx']").checked = false;
    panel.querySelector("input[data-artifact-key='mft']").checked = true;

    mockJsonFetch();
    await A.submitAnalysis();

    const analyzeCall = global.fetch.mock.calls.find((call) => String(call[0]).includes("/analyze"));
    expect(analyzeCall).toBeTruthy();
    const body = JSON.parse(analyzeCall[1].body);
    expect(body.images).toEqual([{ image_id: "img1", artifacts: ["evtx"] }]);
  });

  test("parse-only snapshot does not enable analysis even if DOM is later AI-enabled", async () => {
    A.setCaseId("case-parse-only");
    A.st.parse.done = true;
    A.st.selected = ["evtx"];
    A.st.selectedAi = [];
    const box = A.artifactBoxes()[0];
    box.disabled = false;
    box.checked = true;
    const select = A.ensureArtifactModeControl(box, A.MODE_PARSE_AND_AI);
    select.value = A.MODE_PARSE_AND_AI;
    mockJsonFetch();

    await A.submitAnalysis();

    expect(global.fetch).not.toHaveBeenCalled();
    expect(A.el.analysisMsg.textContent).toContain("No artifacts");
  });

  test("changed selections after parse require re-parse before analysis", async () => {
    A.setCaseId("case-stale");
    A.st.parse.done = true;
    A.st.parse.selectionStale = true;
    A.st.selectedAi = ["evtx"];
    mockJsonFetch();

    await A.submitAnalysis();

    expect(global.fetch).not.toHaveBeenCalled();
    expect(A.el.analysisMsg.textContent).toContain("Re-parse");
  });

  test("snapshot payload excludes failed or unparsed image entries", () => {
    A.st.parsedSelections = {
      mode: "multi",
      images: {
        img1: { image_id: "img1", aiArtifacts: ["evtx"] },
      },
    };
    A.st.imageParse = {
      img2: { done: false, fail: false, aiArts: ["mft"] },
      img3: { done: true, fail: true, aiArts: ["runkeys"] },
    };

    expect(A._parsedMultiImageSelectionsForAnalysis()).toEqual([{ image_id: "img1", artifacts: ["evtx"] }]);
  });
});
