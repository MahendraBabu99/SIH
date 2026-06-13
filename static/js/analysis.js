/**
 * Analysis SSE, result rendering, and findings display for AIFT.
 *
 * Manages the analysis lifecycle: submit, track via SSE, render
 * per-artifact results, executive summary, collapsible findings, and
 * visible notes for images that were skipped during multi-image analysis.
 * Supports both single-image and multi-image analysis flows.
 *
 * Depends on: AIFT (utils.js, markdown.js)
 */
"use strict";

(() => {
  const A = window.AIFT;
  const { st, el } = A;

  /** Build multi-image analysis payload from the successful parse snapshot. */
  function parsedMultiImageSelectionsForAnalysis() {
    const snapshot = st.parsedSelections || {};
    const images = A.isObj(snapshot.images) ? snapshot.images : {};
    const fromSnapshot = Object.keys(images).map((imageId) => {
      const entry = images[imageId] || {};
      const artifacts = (Array.isArray(entry.aiArtifacts) ? entry.aiArtifacts : [])
        .map(String)
        .filter(Boolean);
      return { image_id: String(entry.image_id || imageId), artifacts };
    }).filter((entry) => entry.image_id && entry.artifacts.length > 0);
    if (fromSnapshot.length) return fromSnapshot;

    return Object.keys(st.imageParse || {}).map((imageId) => {
      const imgState = st.imageParse[imageId];
      if (!imgState || !imgState.done || imgState.fail) return null;
      const artifacts = (Array.isArray(imgState.aiArts) ? imgState.aiArts : [])
        .map(String)
        .filter(Boolean);
      return { image_id: imageId, artifacts };
    }).filter((entry) => entry && entry.artifacts.length > 0);
  }

  // ── Analysis submission ────────────────────────────────────────────────────

  /** Wire up the analysis form: submit, cancel, and settings link handlers. */
  function setupAnalysis() {
    if (!el.analysisForm) return;
    el.analysisForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      await submitAnalysis();
    });
    if (el.cancelAnalysis) el.cancelAnalysis.addEventListener("click", cancelAnalysis);
    if (el.settingsLink) {
      el.settingsLink.addEventListener("click", (e) => {
        e.preventDefault();
        A.openSettings();
      });
    }
  }

  /**
   * Submit the analysis request.
   *
   * Validates preconditions (case exists, parse complete, AI artifacts selected),
   * posts to the analyze endpoint, and opens the SSE progress stream.
   * For multi-image cases, sends per-image artifact selections.
   */
  async function submitAnalysis() {
    A.clearMsg(el.analysisMsg);
    const caseId = A.activeCaseId();
    if (!caseId) {
      A.setMsg(el.analysisMsg, "No active case. Intake evidence first.", "error");
      A.showStep(1);
      return;
    }
    if (!st.parse.done) {
      A.setMsg(el.analysisMsg, "Parsing must complete before analysis.", "error");
      A.showStep(3);
      return;
    }
    if (st.parse.selectionStale) {
      A.setMsg(el.analysisMsg, "Artifact selections changed after parsing. Re-parse before analysis.", "error");
      A.showStep(3);
      return;
    }
    if (!st.selectedAi.length) {
      A.setMsg(el.analysisMsg, "No artifacts are set to `Parse and use in AI`. Update artifact options and parse again.", "error");
      A.showStep(2);
      return;
    }
    if (st.analysis.run) return A.setMsg(el.analysisMsg, "Analysis is already running.", "error");

    if (st.analysis.cancelPending) {
      await st.analysis.cancelPending;
    }
    resetAnalysisState();
    const owner = A.newRunOwner(caseId, "analysis");
    st.analysis.owner = owner;
    st.analysis.run = true;
    const abortCtrl = new AbortController();
    st.analysis.abort = abortCtrl;
    A.clearMsg(el.resultsMsg);
    if (el.runBtn) el.runBtn.disabled = true;
    if (el.cancelAnalysis) el.cancelAnalysis.hidden = false;

    // Build the request body.
    const body = { prompt: A.val(el.prompt) };
    const isMulti = A.isMultiImage && A.isMultiImage();
    if (isMulti) {
      const selections = parsedMultiImageSelectionsForAnalysis();
      if (selections && selections.length) {
        // Backend expects body.images as an array of {image_id: string,
        // artifacts: string[]} where each artifact entry is a key string
        // (e.g. "evtx", "prefetch").  See analysis.py start_analysis().
        body.images = selections;
        st.analysis.multiImage = true;
        st.analysis.imageResults = {};
      }
    }

    try {
      A.startTimer("analysis");
      await A.apiJson(`/api/cases/${encodeURIComponent(caseId)}/analyze`, { method: "POST", json: body, signal: abortCtrl.signal });
      if (!A.isRunOwnerCurrent(st.analysis, owner)) return;
      startAnalysisSse(owner);
      A.showStep(4);
    } catch (e) {
      if (e.name === "AbortError") return;
      if (!A.isRunOwnerCurrent(st.analysis, owner)) return;
      st.analysis.abort = null;
      st.analysis.run = false;
      st.analysis.owner = null;
      A.stopTimer("analysis");
      if (el.runBtn) el.runBtn.disabled = false;
      if (el.cancelAnalysis) el.cancelAnalysis.hidden = true;
      A.setMsg(el.analysisMsg, `Failed to start analysis: ${e.message}`, "error");
    } finally {
      A.updateNav();
    }
  }

  // ── Analysis SSE ───────────────────────────────────────────────────────────

  /** Open the analysis-progress SSE stream for the active case. */
  function startAnalysisSse(owner = st.analysis.owner) {
    const caseId = owner ? owner.caseId : A.activeCaseId();
    if (!caseId) return A.setMsg(el.analysisMsg, "No case ID for analysis stream.", "error");
    if (!A.isRunOwnerCurrent(st.analysis, owner)) return;
    A.openSseStream(
      `/api/cases/${encodeURIComponent(caseId)}/analyze/progress`,
      st.analysis,
      {
        onEvent: (p) => onAnalysisEvent(p, owner),
        onError: () => {
          if (A.isRunOwnerCurrent(st.analysis, owner) && !st.analysis.done && !st.analysis.fail && st.analysis.run) retryAnalysisSse(owner);
        },
      },
    );
  }

  /**
   * Compute the expected number of AI prompts for the status banner total.
   *
   * When more than one image is analysed, the backend forwards per-image
   * summary prompts (artifact_key "summary_<image_id>") and one
   * cross-image correlation prompt as artifact-style events. Those events
   * are tracked in st.analysis.order alongside real artifact prompts, so
   * the banner total must include them or the "Analysing (i/total)" index
   * overshoots the artifact-only total during the summary phase.
   *
   * The gate is the backend-reported image count (summary events and the
   * cross-image prompt are streamed only when image_count > 1), NOT the
   * frontend multiImage flag: submitAnalysis pre-sets that flag for any
   * multi-image case, and it stays stale when the backend then treats the
   * run as single-image (e.g. only one image had AI-enabled artifacts),
   * which would inflate the total and make the banner undershoot.
   *
   * @returns {number} Expected prompt count, or 0 when not yet known.
   */
  function expectedAnalysisPromptCount() {
    const artifacts = st.analysis.totalArtifacts || 0;
    const images = st.analysis.imageCount || 0;
    if (!artifacts || images <= 1) return artifacts;
    return artifacts + images + 1;
  }

  /** Dispatch a single analysis SSE event to the appropriate UI handler. */
  function onAnalysisEvent(p, owner = null) {
    if (!A.isRunOwnerCurrent(st.analysis, owner)) return;
    const t = String(p.type || "");
    if (t === "analysis_started") {
      A.clearMsg(el.analysisMsg);
      st.analysis.totalArtifacts = Number(p.analysis_artifact_count) || 0;
      st.analysis.imageCount = Number(p.image_count) || 0;
      if (p.multi_image) st.analysis.multiImage = true;
      setAnalysisStatus("Preparing analysis\u2026");
      renderAnalysis();
      renderFindings();
      return;
    }
    if (t === "artifact_analysis_started") {
      const r = A.isObj(p.result) ? p.result : p;
      // Carry over top-level fields that the result dict may lack.
      if (!r.artifact_key && p.artifact_key) r.artifact_key = p.artifact_key;
      if (!r.image_id && p.image_id) r.image_id = p.image_id;
      if (!r.image_label && p.image_label) r.image_label = p.image_label;
      upsertAnalysisStarted(r);
      const name = String(r.artifact_name || A.artifactName(String(r.artifact_key || "")));
      const idx = st.analysis.order.length;
      const total = expectedAnalysisPromptCount() || idx;
      const imageLabel = String(r.image_label || "");
      const statusPrefix = imageLabel ? `[${imageLabel}] ` : "";
      setAnalysisStatus(`${statusPrefix}Analysing (${idx}/${total}): ${name}`);
      renderAnalysis();
      renderFindings();
      return;
    }
    if (t === "artifact_analysis_thinking") {
      const rt = A.isObj(p.result) ? p.result : p;
      if (!rt.artifact_key && p.artifact_key) rt.artifact_key = p.artifact_key;
      if (!rt.image_id && p.image_id) rt.image_id = p.image_id;
      if (!rt.image_label && p.image_label) rt.image_label = p.image_label;
      upsertAnalysisThinking(rt);
      renderAnalysis();
      renderFindings();
      return;
    }
    if (t === "artifact_analysis_completed") {
      const rc = A.isObj(p.result) ? p.result : p;
      if (!rc.artifact_key && p.artifact_key) rc.artifact_key = p.artifact_key;
      if (!rc.image_id && p.image_id) rc.image_id = p.image_id;
      if (!rc.image_label && p.image_label) rc.image_label = p.image_label;
      upsertAnalysis(rc);
      renderAnalysis();
      renderFindings();
      return;
    }
    if (t === "image_skipped") {
      // A whole image was excluded from AI analysis (e.g. no parsed CSV
      // output for its requested artifacts). Surface it as a visible note.
      if (recordSkippedImage(p)) {
        renderAnalysis();
        renderFindings();
      }
      return;
    }
    if (t === "analysis_summary") {
      st.analysis.summary = String(p.summary || "");
      st.analysis.model = A.isObj(p.model_info) ? p.model_info : {};
      // Store canonical image-scoped summary data when present.
      if (A.isObj(p.images)) {
        st.analysis.multiImage = Boolean(p.multi_image || Object.keys(p.images).length > 1);
        st.analysis.imageResults = p.images;
        st.analysis.crossImageSummary = String(p.cross_image_summary || "");
      }
      // Merge the payload's skipped-image list so SSE reconnects that
      // missed the live image_skipped events still surface every skip.
      if (mergeSkippedImages(p.skipped_images)) {
        renderAnalysis();
        renderFindings();
      }
      renderExecSummary();
      if (st.analysis.model.provider || st.analysis.model.model) {
        const display = st.analysis.model.model
          ? `${A.prettyProvider(String(st.analysis.model.provider || ""))} (${String(st.analysis.model.model || "")})`
          : A.prettyProvider(String(st.analysis.model.provider || ""));
        setProvider(display || "Not configured");
      }
      return;
    }
    if (t === "analysis_completed") {
      // Handle canonical image-scoped completed payload.
      if (A.isObj(p.images)) {
        st.analysis.multiImage = Boolean(p.multi_image || Object.keys(p.images).length > 1);
        st.analysis.imageResults = p.images;
        st.analysis.crossImageSummary = String(p.cross_image_summary || "");
      }
      // Merge skipped images from the terminal payload (covers reconnects
      // that missed the live image_skipped events); the renders below pick
      // up any newly recorded entries.
      mergeSkippedImages(p.skipped_images);
      const finalArtifacts = flattenImageScopedArtifacts(p.images);
      finalArtifacts.forEach((entry) => {
        if (!A.isObj(entry)) return;
        /* Single-image runs stream per-artifact SSE rows WITHOUT image
           context (the backend strips image_id/image_label when only one
           image is analyzed), so streamed rows are keyed by the bare
           artifact key. st.analysis.multiImage was recomputed above from
           this event's multi_image flag and images key count; when it is
           false, strip the image context injected by the flatten so this
           final upsert merges into the streamed row (preserving its
           reasoning text) instead of appending a duplicate composite-key
           row. With no streamed rows (e.g. SSE reconnect), recovery
           entries then land on the same bare keys. Multi-image entries
           keep their composite keys untouched. */
        if (!st.analysis.multiImage) {
          delete entry.image_id;
          delete entry.image_label;
        }
        upsertAnalysis(entry);
      });
      finalizeAnyThinkingArtifacts();
      renderAnalysis();
      renderFindings();
      st.analysis.run = false;
      st.analysis.done = true;
      st.analysis.fail = false;
      A.stopTimer("analysis");
      closeAnalysisSse();
      setAnalysisStatus(null);
      if (el.runBtn) el.runBtn.disabled = false;
      if (el.cancelAnalysis) el.cancelAnalysis.hidden = true;
      A.clearMsg(el.analysisMsg);
      A.updateNav();
      return A.showStep(5);
    }
    if (t === "analysis_failed") {
      st.analysis.run = false;
      st.analysis.done = false;
      st.analysis.fail = true;
      A.stopTimer("analysis");
      closeAnalysisSse();
      setAnalysisStatus(null);
      if (el.runBtn) el.runBtn.disabled = false;
      if (el.cancelAnalysis) el.cancelAnalysis.hidden = true;
      A.setMsg(el.analysisMsg, String(p.error || "Analysis failed."), "error");
      A.updateNav();
      return;
    }
    if (t === "analysis_cancelled" || t === "cancelled") {
      st.analysis.run = false;
      st.analysis.done = false;
      st.analysis.fail = false;
      A.stopTimer("analysis");
      closeAnalysisSse();
      setAnalysisStatus(null);
      if (el.runBtn) el.runBtn.disabled = false;
      if (el.cancelAnalysis) el.cancelAnalysis.hidden = true;
      A.setMsg(el.analysisMsg, "Analysis cancelled.", "info");
      A.updateNav();
      return;
    }
    if (t === "complete" || t === "idle") {
      closeAnalysisSse();
      setAnalysisStatus(null);
      if (el.runBtn) el.runBtn.disabled = false;
      if (el.cancelAnalysis) el.cancelAnalysis.hidden = true;
      if (st.analysis.done || st.analysis.fail || !st.analysis.run) {
        A.updateNav();
        return;
      }
      st.analysis.run = false;
      st.analysis.done = false;
      st.analysis.fail = true;
      A.stopTimer("analysis");
      const message = t === "idle"
        ? "No active analysis progress stream. Run analysis again."
        : "Analysis progress ended before a completion event. Run analysis again.";
      A.setMsg(el.analysisMsg, message, "error");
      A.updateNav();
      renderAnalysis();
      renderExecSummary();
      renderFindings();
      return;
    }
    if (t === "error") A.setMsg(el.analysisMsg, String(p.message || "Analysis stream error."), "error");
  }

  // ── Analysis data upserts ──────────────────────────────────────────────────

  /**
   * Flatten the canonical image-scoped `images` mapping into a flat list
   * of per-artifact entries, injecting image_id/image_label from the
   * mapping key into each copied entry.
   *
   * @param {Object} images - Mapping of image_id to {label, per_artifact}.
   * @returns {Array<Object>} Per-artifact entry copies with image context.
   */
  function flattenImageScopedArtifacts(images) {
    if (!A.isObj(images)) return [];
    const rows = [];
    Object.keys(images).forEach(function(imageId) {
      const imgData = images[imageId];
      if (!A.isObj(imgData) || !Array.isArray(imgData.per_artifact)) return;
      const imageLabel = String(imgData.label || imageId);
      imgData.per_artifact.forEach(function(item) {
        if (!A.isObj(item)) return;
        rows.push(Object.assign({}, item, {
          image_id: item.image_id || imageId,
          image_label: item.image_label || imageLabel,
        }));
      });
    });
    return rows;
  }

  // ── Skipped image tracking ─────────────────────────────────────────────────

  /**
   * Record one image that was skipped during analysis, deduplicated by
   * image id (falling back to the label when the id is missing).
   *
   * @param {Object} entry - Skip descriptor with image_id, label, and
   *     reason fields (snake_case as emitted by the backend).
   * @returns {boolean} True when a new entry was recorded.
   */
  function recordSkippedImage(entry) {
    if (!A.isObj(entry)) return false;
    const imageId = String(entry.image_id || "");
    const label = String(entry.label || "");
    const reason = String(entry.reason || "");
    const key = imageId || label;
    if (!key) return false;
    if (!A.isObj(st.analysis.skippedImages)) st.analysis.skippedImages = {};
    if (st.analysis.skippedImages[key]) return false;
    st.analysis.skippedImages[key] = { imageId, label: label || imageId, reason };
    return true;
  }

  /**
   * Merge a skipped_images payload array into the per-run skip store.
   *
   * Covers SSE reconnects that missed the live image_skipped events: the
   * analysis_summary and analysis_completed payloads repeat the full list.
   *
   * @param {Array<Object>} entries - skipped_images array from an SSE payload.
   * @returns {boolean} True when at least one new entry was recorded.
   */
  function mergeSkippedImages(entries) {
    if (!Array.isArray(entries)) return false;
    let changed = false;
    entries.forEach((entry) => {
      if (recordSkippedImage(entry)) changed = true;
    });
    return changed;
  }

  /**
   * Return the recorded skipped-image entries in insertion order.
   *
   * @returns {Array<Object>} Entries of {imageId, label, reason}.
   */
  function skippedImageList() {
    const store = A.isObj(st.analysis.skippedImages) ? st.analysis.skippedImages : {};
    return Object.keys(store).map((key) => store[key]).filter(A.isObj);
  }

  /**
   * Build the visible note element for one skipped image.
   *
   * The wording must make clear the image was NOT analyzed by the AI,
   * mirroring how the HTML report lists skipped images as processing notes.
   *
   * @param {Object} entry - Skip entry of {imageId, label, reason}.
   * @returns {HTMLElement} The note element.
   */
  function buildSkippedImageNote(entry) {
    const label = String(entry.label || entry.imageId || "Unknown image");
    const reason = String(entry.reason || "No reason provided.");
    return A.createDomElement("div", {
      className: "analysis-skipped-note",
      attrs: { role: "note" },
      dataset: { imageId: String(entry.imageId || "") },
    }, [
      A.createDomElement("strong", { text: `Image skipped: ${label}` }),
      A.createDomElement("p", { text: `${reason} This image was not analyzed by the AI.` }),
    ]);
  }

  /**
   * Append one note element per recorded skipped image to a container.
   *
   * @param {HTMLElement} container - Target list/section element.
   */
  function appendSkippedImageNotes(container) {
    if (!container) return;
    skippedImageList().forEach((entry) => {
      container.appendChild(buildSkippedImageNote(entry));
    });
  }

  /**
   * Extract the common key, name, and model from an analysis SSE payload,
   * and ensure the key is tracked in st.analysis.order.
   *
   * @param {Object} r - Raw event payload.
   * @returns {{key: string, name: string, model: string, current: Object, imageId: string, imageLabel: string}}
   */
  function extractAnalysisIdentifiers(r) {
    const rawKey = String(r.artifact_key || r.key || `artifact_${st.analysis.order.length + 1}`);
    const name = String(r.artifact_name || A.artifactName(rawKey));
    const model = String(r.model || "");
    const imageId = String(r.image_id || "");
    const imageLabel = String(r.image_label || "");
    /* Use a composite key when image_id is present so that the same
       artifact from different images does not collide in byKey/order. */
    const key = imageId ? `${imageId}::${rawKey}` : rawKey;
    if (!st.analysis.byKey[key]) st.analysis.order.push(key);
    const current = st.analysis.byKey[key] || {};
    return { key, name, model, current, imageId, imageLabel };
  }

  /** Record a completed artifact analysis result. */
  function upsertAnalysis(r) {
    const { key, name, model, current, imageId, imageLabel } = extractAnalysisIdentifiers(r);
    const rawText = String(r.analysis || r.result || r.summary || "");
    const text = A.stripLeadingReasoningBlocks(rawText) || rawText;
    st.analysis.byKey[key] = {
      key, name, text, model, imageId, imageLabel,
      thinkingText: String(current.thinkingText || ""),
      partialText: "",
      isThinking: false,
    };
  }

  /** Record that artifact analysis has started (sets thinking state). */
  function upsertAnalysisStarted(r) {
    const { key, name, model, current, imageId, imageLabel } = extractAnalysisIdentifiers(r);
    st.analysis.byKey[key] = {
      key, name, imageId, imageLabel,
      text: String(current.text || ""),
      model: model || String(current.model || ""),
      thinkingText: String(current.thinkingText || ""),
      partialText: String(current.partialText || ""),
      isThinking: true,
    };
  }

  /** Update thinking/partial text for an in-progress artifact analysis. */
  function upsertAnalysisThinking(r) {
    const { key, name, model, current, imageId, imageLabel } = extractAnalysisIdentifiers(r);
    st.analysis.byKey[key] = {
      key, name, imageId, imageLabel,
      text: String(current.text || ""),
      model: model || String(current.model || ""),
      thinkingText: String(r.thinking_text || current.thinkingText || ""),
      partialText: String(r.partial_text || current.partialText || ""),
      isThinking: true,
    };
  }

  /** Resolve all still-thinking artifacts to their best available text. */
  function finalizeAnyThinkingArtifacts() {
    st.analysis.order.forEach((key) => {
      const current = st.analysis.byKey[key];
      if (!current || !current.isThinking) return;
      const rawResolvedText = String(current.text || current.partialText || "");
      const resolvedText = A.stripLeadingReasoningBlocks(rawResolvedText) || rawResolvedText.trim();
      st.analysis.byKey[key] = { ...current, text: resolvedText, isThinking: false };
    });
  }

  // ── Status banner ─────────────────────────────────────────────────────

  /**
   * Show or update the analysis status banner with the given message.
   * Pass null/empty to hide.
   */
  function setAnalysisStatus(msg) {
    if (!el.analysisStatusBanner) return;
    if (!msg) {
      el.analysisStatusBanner.hidden = true;
      return;
    }
    el.analysisStatusBanner.hidden = false;
    if (el.analysisStatusText) el.analysisStatusText.textContent = msg;
  }

  // ── Rendering helpers ──────────────────────────────────────────────────────

  /** Return the best display text for an analysis entry (thinking placeholder or final). */
  function resolveAnalysisText(r) {
    if (r.isThinking && !String(r.text || "").trim()) {
      return String(r.partialText || "Model is thinking...");
    }
    return r.text;
  }

  /**
   * Capture the open/closed state of keyed `<details>` elements inside a
   * container before a re-render replaces them.
   *
   * Streaming SSE events rebuild the analysis and findings lists from
   * scratch, which would otherwise reset every `<details>` the user has
   * toggled (e.g. an opened reasoning panel snapping shut mid-stream).
   *
   * @param {HTMLElement} container - Root element to scan.
   * @returns {Map<string, boolean>} Mapping of data-state-key to open state.
   */
  function snapshotDetailsState(container) {
    const state = new Map();
    if (!container) return state;
    container.querySelectorAll("details[data-state-key]").forEach((d) => {
      state.set(d.dataset.stateKey, d.open);
    });
    return state;
  }

  /**
   * Re-apply a captured open/closed snapshot to freshly rebuilt `<details>`
   * elements so user toggles survive re-renders. Elements whose key is not
   * in the snapshot (newly appeared) keep their default state.
   *
   * @param {HTMLElement} container - Root element holding the new elements.
   * @param {Map<string, boolean>} state - Snapshot from snapshotDetailsState.
   */
  function restoreDetailsState(container, state) {
    if (!container || !state.size) return;
    container.querySelectorAll("details[data-state-key]").forEach((d) => {
      if (state.has(d.dataset.stateKey)) d.open = state.get(d.dataset.stateKey);
    });
  }

  /**
   * Render all per-artifact analysis cards into the analysis results list,
   * followed by one visible note per image skipped during analysis.
   */
  function renderAnalysis() {
    if (!el.analysisList) return;
    const openState = snapshotDetailsState(el.analysisList);
    el.analysisList.innerHTML = "";
    if (!st.analysis.order.length && !skippedImageList().length) {
      const p = document.createElement("p");
      p.textContent = "No analysis output yet.";
      el.analysisList.appendChild(p);
      return;
    }

    // In multi-image mode, group artifacts by image.
    if (st.analysis.multiImage) {
      renderMultiImageAnalysis();
    } else {
      st.analysis.order.forEach((k) => {
        const r = st.analysis.byKey[k];
        if (!r) return;
        el.analysisList.appendChild(buildAnalysisCard(r));
      });
    }
    appendSkippedImageNotes(el.analysisList);
    restoreDetailsState(el.analysisList, openState);
  }

  /**
   * Render multi-image analysis cards grouped by image.
   * Each image gets a collapsible section with its artifacts inside.
   */
  function renderMultiImageAnalysis() {
    if (!el.analysisList) return;
    // Group artifacts by imageId.
    const groups = {};
    const groupOrder = [];
    st.analysis.order.forEach(function(k) {
      const r = st.analysis.byKey[k];
      if (!r) return;
      const imgId = r.imageId || "__single__";
      if (!groups[imgId]) {
        groups[imgId] = [];
        groupOrder.push(imgId);
      }
      groups[imgId].push(r);
    });

    groupOrder.forEach(function(imgId) {
      const items = groups[imgId];
      if (!items || !items.length) return;
      const label = items[0].imageLabel || (imgId === "__single__" ? "Analysis" : imgId);

      const section = document.createElement("div");
      section.className = "analysis-image-group";

      // Only show the image group header when there are multiple groups
      // (skip the header for __single__ fallback in single-image mode).
      if (groupOrder.length > 1 || imgId !== "__single__") {
        const header = document.createElement("h4");
        header.className = "analysis-image-group-header";
        header.textContent = label;
        section.appendChild(header);
      }

      items.forEach(function(r) {
        section.appendChild(buildAnalysisCard(r));
      });
      el.analysisList.appendChild(section);
    });
  }

  /**
   * Build a single analysis card DOM element.
   *
   * The embedded reasoning panel is tagged with a stable data-state-key so
   * its open/closed state survives streaming re-renders.
   *
   * @param {Object} r - Analysis entry from st.analysis.byKey.
   * @returns {HTMLElement} The article element.
   */
  function buildAnalysisCard(r) {
    const metaParts = [r.key];
    if (r.model) metaParts.push("model: " + r.model);
    if (r.imageLabel) metaParts.push("image: " + r.imageLabel);
    const displayText = resolveAnalysisText(r);
    const emptyLabel = r.isThinking ? "Model is thinking..." : "(No analysis text returned.)";
    const card = A.createAnalysisResultCard({
      title: r.name,
      metaText: metaParts.join(" | "),
      text: displayText,
      emptyText: emptyLabel,
      reasoningText: r.thinkingText,
    });
    const reasoning = card.querySelector("details.analysis-reasoning-panel");
    if (reasoning) reasoning.dataset.stateKey = "card-reasoning:" + r.key;
    return card;
  }

  /** Render the executive summary markdown into the results page. */
  function renderExecSummary() {
    if (!el.summaryOut) return;

    // Multi-image: show cross-image summary and per-image summaries.
    if (st.analysis.multiImage && st.analysis.crossImageSummary) {
      renderMultiImageExecSummary();
      return;
    }

    A.renderMarkdownInto(el.summaryOut, st.analysis.summary, "Summary is generated after analysis completes.");
  }

  /**
   * Render multi-image executive summary: cross-image summary at top,
   * then per-image summaries in collapsible sections.
   */
  function renderMultiImageExecSummary() {
    if (!el.summaryOut) return;
    el.summaryOut.innerHTML = "";

    // Remove any previously appended per-image summary containers so
    // repeated calls (e.g. SSE reconnects) do not create duplicates.
    document.querySelectorAll(".per-image-summaries").forEach(function(node) { node.remove(); });

    // Cross-image summary section.
    const crossSection = document.getElementById("cross-system-analysis");
    if (crossSection) {
      const crossContent = crossSection.querySelector(".cross-system-content");
      if (crossContent) {
        A.renderMarkdownInto(crossContent, st.analysis.crossImageSummary, "No cross-system analysis available.");
      }
      crossSection.hidden = false;
    }

    // Overall summary.
    A.renderMarkdownInto(el.summaryOut, st.analysis.summary, "Summary is generated after analysis completes.");

    // Per-image summaries below the main summary.
    const imageResults = st.analysis.imageResults || {};
    const imageIds = Object.keys(imageResults);
    if (imageIds.length > 0) {
      const perImageContainer = document.createElement("div");
      perImageContainer.className = "per-image-summaries";

      imageIds.forEach(function(imgId) {
        const imgData = imageResults[imgId];
        if (!imgData) return;
        const label = String(imgData.label || imgId);
        const summary = String(imgData.summary || "");

        const details = document.createElement("details");
        details.className = "per-image-summary-section";
        details.open = true;
        const summaryEl = document.createElement("summary");
        summaryEl.className = "per-image-summary-header";
        summaryEl.textContent = label;
        const bodyDiv = document.createElement("div");
        bodyDiv.className = "markdown-output per-image-summary-body";
        A.renderMarkdownInto(bodyDiv, summary, "(No summary for this image.)");
        details.appendChild(summaryEl);
        details.appendChild(bodyDiv);
        perImageContainer.appendChild(details);
      });

      el.summaryOut.appendChild(perImageContainer);
    }
  }

  /**
   * Render collapsible per-artifact findings `<details>` elements, followed
   * by one non-collapsible note per image skipped during analysis.
   */
  function renderFindings() {
    if (!el.findings) return;
    const openState = snapshotDetailsState(el.findings);
    Array.from(el.findings.children).forEach((c) => {
      if (c.id !== "artifact-findings-title") c.remove();
    });
    if (!st.analysis.order.length && !skippedImageList().length) {
      const p = document.createElement("p");
      p.textContent = "Findings will appear here.";
      el.findings.appendChild(p);
      return;
    }

    // Multi-image: group findings by image.
    if (st.analysis.multiImage) {
      renderMultiImageFindings();
    } else {
      st.analysis.order.forEach((k, i) => {
        const r = st.analysis.byKey[k];
        if (!r) return;
        el.findings.appendChild(buildFindingsDetails(r, i === 0));
      });
    }
    appendSkippedImageNotes(el.findings);
    restoreDetailsState(el.findings, openState);
  }

  /**
   * Render multi-image findings grouped by image in collapsible sections.
   */
  function renderMultiImageFindings() {
    if (!el.findings) return;

    // Group by image.
    const groups = {};
    const groupOrder = [];
    st.analysis.order.forEach(function(k) {
      const r = st.analysis.byKey[k];
      if (!r) return;
      const imgId = r.imageId || "__single__";
      if (!groups[imgId]) {
        groups[imgId] = [];
        groupOrder.push(imgId);
      }
      groups[imgId].push(r);
    });

    groupOrder.forEach(function(imgId, gi) {
      const items = groups[imgId];
      if (!items || !items.length) return;
      const label = items[0].imageLabel || (imgId === "__single__" ? "Analysis" : imgId);

      const imageSection = document.createElement("details");
      imageSection.className = "findings-image-group";
      imageSection.open = gi === 0;
      imageSection.dataset.stateKey = "findings-image-group:" + imgId;
      // For single-group __single__ fallback, render as a plain div
      // instead of a collapsible details to avoid showing the label.
      if (groupOrder.length === 1 && imgId === "__single__") {
        items.forEach(function(r, i) {
          el.findings.appendChild(buildFindingsDetails(r, gi === 0 && i === 0));
        });
        return;
      }
      const imageSummary = document.createElement("summary");
      imageSummary.className = "findings-image-group-header";
      imageSummary.textContent = label;
      imageSection.appendChild(imageSummary);

      items.forEach(function(r, i) {
        imageSection.appendChild(buildFindingsDetails(r, gi === 0 && i === 0));
      });

      el.findings.appendChild(imageSection);
    });
  }

  /**
   * Build a collapsible findings details element.
   *
   * The outer details and embedded reasoning panel are tagged with stable
   * data-state-keys so their open/closed state survives streaming re-renders.
   *
   * @param {Object} r - Analysis entry.
   * @param {boolean} isOpen - Whether to start open.
   * @returns {HTMLDetailsElement}
   */
  function buildFindingsDetails(r, isOpen) {
    const displayText = resolveAnalysisText(r);
    const emptyLabel = r.isThinking ? "Model is thinking..." : "(No analysis text returned.)";
    const details = A.createFindingsDetails({
      title: r.name,
      text: displayText,
      emptyText: emptyLabel,
      reasoningText: r.thinkingText,
      open: isOpen,
    });
    details.dataset.stateKey = "finding:" + r.key;
    const reasoning = details.querySelector("details.analysis-reasoning-panel");
    if (reasoning) reasoning.dataset.stateKey = "finding-reasoning:" + r.key;
    return details;
  }

  /** Update the provider name display in the analysis step header. */
  function setProvider(text) {
    if (el.providerName) el.providerName.textContent = text || "Not configured";
  }

  // ── SSE retry / close / cancel ─────────────────────────────────────────────

  /** Attempt to reconnect the analysis SSE stream with exponential backoff. */
  function retryAnalysisSse(owner = st.analysis.owner) {
    if (st.analysis.done || st.analysis.fail || !st.analysis.run) return;
    if (!A.isRunOwnerCurrent(st.analysis, owner)) return;
    A.retrySseStream(st.analysis, {
      reconnect: () => {
        if (A.isRunOwnerCurrent(st.analysis, owner) && !st.analysis.done && !st.analysis.fail && st.analysis.run) startAnalysisSse(owner);
      },
      onRetryScheduled: (attempt, delaySec) => {
        A.setMsg(el.analysisMsg, `Analysis progress connection dropped. Reconnecting (${attempt}/${A.SSE_MAX_RETRIES}) in ${delaySec}s...`, "error");
      },
      onMaxRetries: () => {
        if (!A.isRunOwnerCurrent(st.analysis, owner)) return;
        st.analysis.run = false;
        st.analysis.done = false;
        st.analysis.fail = true;
        st.analysis.retryCount = 0;
        A.stopTimer("analysis");
        closeAnalysisSse();
        if (el.runBtn) el.runBtn.disabled = false;
        A.setMsg(el.analysisMsg, `Analysis progress connection lost after ${A.SSE_MAX_RETRIES} retries. Run analysis again.`, "error");
        A.updateNav();
      },
    });
  }

  /** Close the analysis SSE EventSource and clear pending retries. */
  function closeAnalysisSse() {
    A.closeSseChannel(st.analysis);
  }

  /** Cancel any in-progress analysis: abort HTTP, close SSE, notify backend. */
  function cancelAnalysis() {
    const caseId = A.activeCaseId();
    st.analysis.owner = null;
    if (st.analysis.abort) {
      st.analysis.abort.abort();
      st.analysis.abort = null;
    }
    closeAnalysisSse();
    const wasRunning = st.analysis.run;
    if (!wasRunning) return;
    st.analysis.run = false;
    st.analysis.done = false;
    st.analysis.fail = false;
    A.stopTimer("analysis");
    if (el.runBtn) el.runBtn.disabled = false;
    if (el.cancelAnalysis) el.cancelAnalysis.hidden = true;
    setAnalysisStatus(null);
    A.setMsg(el.analysisMsg, "Analysis cancelled.", "info");
    A.updateNav();
    if (caseId) {
      const cancelPromise = A.apiJson(`/api/cases/${encodeURIComponent(caseId)}/analyze/cancel`, { method: "POST" })
        .catch(() => {});
      st.analysis.cancelPending = cancelPromise;
      cancelPromise.finally(() => {
        if (st.analysis.cancelPending === cancelPromise) st.analysis.cancelPending = null;
      });
    }
  }

  /** Reset all analysis state, close SSE, and clear rendered results. */
  function resetAnalysisState() {
    st.analysis.owner = null;
    closeAnalysisSse();
    A.stopTimer("analysis");
    st.analysis.run = false;
    st.analysis.done = false;
    st.analysis.fail = false;
    st.analysis.retryCount = 0;
    st.analysis.seq = -1;
    st.analysis.order = [];
    st.analysis.byKey = {};
    st.analysis.totalArtifacts = 0;
    st.analysis.imageCount = 0;
    st.analysis.summary = "";
    st.analysis.model = {};
    st.analysis.multiImage = false;
    st.analysis.imageResults = {};
    st.analysis.crossImageSummary = "";
    st.analysis.skippedImages = {};
    A.clearMsg(el.analysisMsg);
    setAnalysisStatus(null);
    if (el.runBtn) el.runBtn.disabled = false;
    if (el.cancelAnalysis) el.cancelAnalysis.hidden = true;

    // Hide and clear cross-system analysis section so stale content from a
    // prior multi-image run does not persist into a subsequent single-image run.
    const crossSection = document.getElementById("cross-system-analysis");
    if (crossSection) {
      crossSection.hidden = true;
      const crossContent = crossSection.querySelector(".cross-system-content");
      if (crossContent) crossContent.innerHTML = "";
    }

    // Remove any per-image summary sections from previous runs.
    document.querySelectorAll(".per-image-summaries").forEach(function(node) { node.remove(); });

    renderAnalysis();
    renderExecSummary();
    renderFindings();
    A.updateNav();
  }

  // ── Public API ─────────────────────────────────────────────────────────────
  A.setupAnalysis = setupAnalysis;
  A.submitAnalysis = submitAnalysis;
  A.cancelAnalysis = cancelAnalysis;
  A.closeAnalysisSse = closeAnalysisSse;
  A.resetAnalysisState = resetAnalysisState;
  A.renderAnalysis = renderAnalysis;
  A.renderExecSummary = renderExecSummary;
  A.renderFindings = renderFindings;
  A.setProvider = setProvider;
  A._onAnalysisEvent = onAnalysisEvent;
  A._parsedMultiImageSelectionsForAnalysis = parsedMultiImageSelectionsForAnalysis;
})();
