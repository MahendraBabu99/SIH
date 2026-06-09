/**
 * Parse submission and SSE progress tracking for AIFT.
 *
 * Manages the parse lifecycle: submit, track progress via SSE,
 * handle retries, cancellation, and state reset.  Supports both
 * single-image and multi-image parsing.
 *
 * Depends on: AIFT (utils.js), evidence.js
 */
"use strict";

(() => {
  const A = window.AIFT;
  const { st, el, q } = A;

  // ── Multi-image parse state ───────────────────────────────────────────────

  /**
   * Per-image parse tracking.  Keyed by image_id, each value holds:
   * {run, done, fail, rows, status, sse, abort, retryCount, seq, cancelPending}.
   */
  st.imageParse = {};

  /** Clone artifact option payloads into a stable frontend snapshot. */
  function cloneArtifactOptions(options) {
    return (Array.isArray(options) ? options : [])
      .map((option) => ({
        artifact_key: String(option.artifact_key || "").trim(),
        mode: A.artifactModeValue(option.mode),
      }))
      .filter((option) => option.artifact_key);
  }

  /** Return the loaded image entry used by the single-image UI. */
  function singleImageEntry() {
    const images = Array.isArray(st.images) ? st.images : [];
    if (!images.length) return null;
    const entry = images[0] || {};
    const imageId = String(entry.image_id || "").trim();
    return imageId ? entry : null;
  }

  /** Build a stable parse-selection snapshot for a single-image parse. */
  function buildSingleParseSnapshot(caseId, owner, imageEntry, artifactOptions, artifacts, aiArtifacts) {
    const imageId = String((imageEntry || {}).image_id || "").trim();
    const imageLabel = String((imageEntry || {}).label || imageId || "");
    const normalizedOptions = cloneArtifactOptions(artifactOptions);
    const normalizedArtifacts = (Array.isArray(artifacts) ? artifacts : []).map(String).filter(Boolean);
    const normalizedAiArtifacts = (Array.isArray(aiArtifacts) ? aiArtifacts : []).map(String).filter(Boolean);
    return {
      caseId,
      runId: owner ? owner.runId : "",
      mode: "single",
      image_id: imageId,
      artifactOptions: normalizedOptions,
      artifacts: normalizedArtifacts,
      aiArtifacts: normalizedAiArtifacts,
      images: imageId ? {
        [imageId]: {
          caseId,
          runId: owner ? owner.runId : "",
          image_id: imageId,
          label: imageLabel,
          artifactOptions: normalizedOptions.slice(),
          artifacts: normalizedArtifacts.slice(),
          aiArtifacts: normalizedAiArtifacts.slice(),
        },
      } : {},
    };
  }

  /** Build a stable parse-selection snapshot for one image in a multi-image parse. */
  function buildImageParseSnapshot(caseId, owner, sel, artifacts, aiArtifacts) {
    return {
      caseId,
      runId: owner ? owner.runId : "",
      image_id: String(sel.image_id || ""),
      label: String(sel.label || sel.image_id || ""),
      artifactOptions: cloneArtifactOptions(sel.artifact_options),
      artifacts: (Array.isArray(artifacts) ? artifacts : []).map(String).filter(Boolean),
      aiArtifacts: (Array.isArray(aiArtifacts) ? aiArtifacts : []).map(String).filter(Boolean),
    };
  }

  /** Commit the successful single-image parse snapshot for later analysis. */
  function commitSingleParsedSelection(owner, payload = {}) {
    if (owner && !A.isRunOwnerCurrent(st.parse, owner)) return;
    const pending = st.parse.pendingSelectionSnapshot || buildSingleParseSnapshot(
      A.activeCaseId(),
      owner || st.parse.owner,
      singleImageEntry(),
      [],
      st.selected,
      st.selectedAi,
    );
    const artifacts = Array.isArray(payload.artifacts) ? payload.artifacts.map(String) : pending.artifacts;
    const aiArtifacts = Array.isArray(payload.analysis_artifacts)
      ? payload.analysis_artifacts.map(String)
      : pending.aiArtifacts;
    st.selected = artifacts;
    st.selectedAi = aiArtifacts;
    const images = Object.assign({}, pending.images || {});
    const pendingImageId = String(pending.image_id || "").trim();
    if (pendingImageId && images[pendingImageId]) {
      images[pendingImageId] = Object.assign({}, images[pendingImageId], {
        artifacts: artifacts.slice(),
        aiArtifacts: aiArtifacts.slice(),
        artifactOptions: pending.artifactOptions.filter((option) => artifacts.includes(option.artifact_key)),
      });
    }
    st.parsedSelections = Object.assign({}, pending, {
      artifacts,
      aiArtifacts,
      artifactOptions: pending.artifactOptions.filter((option) => artifacts.includes(option.artifact_key)),
      caseId: pending.caseId || A.activeCaseId(),
      runId: pending.runId || (owner && owner.runId) || "",
      mode: "single",
      images,
    });
    st.parse.selectionStale = false;
  }

  /** Return whether a parse completion produced record-bearing CSVs. */
  function parseEventHasUsableCsvs(payload = {}) {
    return payload.has_usable_csvs !== false && payload.outcome !== "no_usable_output";
  }

  /** Build a concise user-facing message for recoverable parser warnings. */
  function parseWarningText(payload = {}, artifactLabel = "") {
    const level = String(payload.level || "WARNING").toUpperCase();
    const prefix = level === "ERROR" || level === "CRITICAL" ? "Dissect reported a recoverable error" : "Dissect warning";
    const scope = artifactLabel ? ` while parsing ${artifactLabel}` : "";
    const message = String(payload.message || "The parser reported a recoverable issue and is continuing.");
    return `${prefix}${scope}: ${message}`;
  }

  /** Mark a parse as finished without analysis-ready records. */
  function finishParseWithNoUsableRecords(payload = {}) {
    st.parse.run = false;
    st.parse.done = false;
    st.parse.fail = false;
    st.parse.selectionStale = false;
    A.clearParsedSelections();
    updateParseProgress(true);
    A.stopTimer("parse");
    A.clearMsg(el.analysisMsg);
    A.setMsg(
      el.parseErr,
      String(payload.message || payload.error || "Parsing completed, but selected artifacts produced no records to analyze."),
      "info",
    );
    A.updateParseButton();
    A.updateNav();
    return A.showStep(3);
  }

  /** Commit successful multi-image parse snapshots and exclude failed images. */
  function commitMultiParsedSelections(owner) {
    if (!owner || !A.isRunOwnerCurrent(st.parse, owner)) return false;
    const images = {};
    const allArtifacts = new Set();
    const allAiArtifacts = new Set();
    Object.keys(st.imageParse).forEach((imageId) => {
      const imgState = st.imageParse[imageId];
      if (!imgState || !imgState.done || imgState.fail || imgState.noUsable || !imgState.snapshot) return;
      images[imageId] = Object.assign({}, imgState.snapshot);
      images[imageId].artifactOptions = cloneArtifactOptions(imgState.snapshot.artifactOptions);
      images[imageId].artifacts = (imgState.snapshot.artifacts || []).map(String).filter(Boolean);
      images[imageId].aiArtifacts = (imgState.snapshot.aiArtifacts || []).map(String).filter(Boolean);
      images[imageId].artifacts.forEach((artifact) => allArtifacts.add(artifact));
      images[imageId].aiArtifacts.forEach((artifact) => allAiArtifacts.add(artifact));
    });
    st.selected = Array.from(allArtifacts);
    st.selectedAi = Array.from(allAiArtifacts);
    st.parsedSelections = {
      caseId: owner.caseId,
      runId: owner.runId,
      mode: "multi",
      artifactOptions: [],
      artifacts: st.selected.slice(),
      aiArtifacts: st.selectedAi.slice(),
      images,
    };
    st.parse.selectionStale = false;
    return true;
  }

  /** Return true when a captured per-image parse owner is still current. */
  function isImageParseOwnerCurrent(imageId, owner) {
    if (!owner) return false;
    const imgState = st.imageParse[imageId];
    return !!(
      imgState
      && imgState.owner
      && imgState.owner.caseId === owner.caseId
      && imgState.owner.runId === owner.runId
      && A.isRunOwnerCurrent(st.parse, owner)
    );
  }

  // ── Parse submission ───────────────────────────────────────────────────────

  /**
   * Submit selected artifacts for parsing.
   *
   * Single-image cases use the same image-scoped parse endpoint as
   * multi-image cases while keeping the compact single-image progress table.
   */
  async function submitParse() {
    A.clearMsg(el.artifactsMsg);
    A.clearMsg(el.parseErr);
    const caseId = A.activeCaseId();
    if (!caseId) {
      A.setMsg(el.artifactsMsg, "Create and intake a case first.", "error");
      A.showStep(1);
      return;
    }

    if (A.isMultiImage()) {
      return submitMultiImageParse(caseId);
    }
    return submitSingleImageParse(caseId);
  }

  /**
   * Submit parse for a single-image case.
   *
   * @param {string} caseId - Active case ID.
   */
  async function submitSingleImageParse(caseId) {
    const artifactOptions = A.serializeArtifactSelections("single");
    const arts = artifactOptions.map((option) => option.artifact_key);
    const aiArtifacts = A.selectedAiArtifacts(artifactOptions);
    if (!arts.length) return A.setMsg(el.artifactsMsg, "Select at least one artifact.", "error");
    const imageEntry = singleImageEntry();
    const imageId = imageEntry ? String(imageEntry.image_id || "").trim() : "";
    if (!imageId) return A.setMsg(el.artifactsMsg, "No evidence image is available for parsing. Intake evidence first.", "error");
    const dateRangeValidation = A.validateAnalysisDateRange();
    if (!dateRangeValidation.ok) return A.setMsg(el.artifactsMsg, dateRangeValidation.message, "error");

    if (st.parse.run) cancelParse();
    if (st.parse.cancelPending) {
      await st.parse.cancelPending;
    }
    resetParseState();
    const owner = A.newRunOwner(caseId, "parse");
    st.selected = arts;
    st.selectedAi = aiArtifacts;
    st.parse.owner = owner;
    st.parse.imageId = imageId;
    st.parse.pendingSelectionSnapshot = buildSingleParseSnapshot(caseId, owner, imageEntry, artifactOptions, arts, aiArtifacts);
    st.parse.selectionStale = false;
    st.parse.run = true;
    const abortCtrl = new AbortController();
    st.parse.abort = abortCtrl;

    /* Ensure single-image table is visible. */
    showSingleImageParseTable(true);

    initParseRows(arts);
    updateParseProgress();
    A.updateParseButton();

    try {
      const parsePayload = { artifact_options: artifactOptions };
      if (dateRangeValidation.range) parsePayload.analysis_date_range = dateRangeValidation.range;
      A.startTimer("parse");
      await A.apiJson(
        `/api/cases/${encodeURIComponent(caseId)}/images/${encodeURIComponent(imageId)}/parse`,
        { method: "POST", json: parsePayload, signal: abortCtrl.signal },
      );
      if (!A.isRunOwnerCurrent(st.parse, owner)) return;
      startParseSse(owner, imageId);
      A.showStep(3);
    } catch (e) {
      if (e.name === "AbortError") return;
      if (!A.isRunOwnerCurrent(st.parse, owner)) return;
      st.parse.abort = null;
      st.parse.run = false;
      st.parse.owner = null;
      st.parse.imageId = "";
      st.parse.pendingSelectionSnapshot = null;
      A.stopTimer("parse");
      A.setMsg(el.artifactsMsg, `Failed to start parsing: ${e.message}`, "error");
      A.updateParseButton();
    } finally {
      A.updateNav();
    }
  }

  /**
   * Submit parse for a multi-image case.
   *
   * Iterates each image, calls POST /api/cases/<id>/images/<image_id>/parse,
   * and opens a separate SSE stream per image.
   *
   * @param {string} caseId - Active case ID.
   */
  async function submitMultiImageParse(caseId) {
    const selections = A.serializeArtifactSelections("all");
    const activeSelections = selections.filter((s) => s.artifact_options.length > 0);
    if (!activeSelections.length) return A.setMsg(el.artifactsMsg, "Select at least one artifact for at least one image.", "error");

    const dateRangeValidation = A.validateAnalysisDateRange();
    if (!dateRangeValidation.ok) return A.setMsg(el.artifactsMsg, dateRangeValidation.message, "error");

    if (st.parse.run) cancelParse();
    if (st.parse.cancelPending) {
      await st.parse.cancelPending;
    }

    /* Aggregate all artifact keys across images for st.selected/selectedAi. */
    const allArts = new Set();
    const allAiArts = new Set();
    activeSelections.forEach((s) => {
      s.artifact_options.forEach((opt) => {
        allArts.add(opt.artifact_key);
        if (A.selectedAiArtifacts([opt]).length > 0) allAiArts.add(opt.artifact_key);
      });
    });
    resetParseState();
    const owner = A.newRunOwner(caseId, "parse");
    st.selected = Array.from(allArts);
    st.selectedAi = Array.from(allAiArts);
    st.parse.owner = owner;
    st.parse.selectionStale = false;
    st.parse.run = true;
    st.parse.abort = new AbortController();

    /* Hide single-image table, show multi-image sections. */
    showSingleImageParseTable(false);
    buildMultiImageParseSections(activeSelections);
    updateParseProgress();
    A.updateParseButton();
    A.startTimer("parse");
    A.showStep(3);

    /* Launch parse requests concurrently. */
    const promises = activeSelections.map((sel) => startImageParse(caseId, sel, dateRangeValidation.range, owner));
    await Promise.allSettled(promises);

    /* Check if all failed immediately. */
    checkMultiImageCompletion(owner);
  }

  /**
   * Start parsing for a single image within a multi-image parse.
   *
   * @param {string} caseId - Case ID.
   * @param {Object} sel - Selection: {image_id, label, artifact_options}.
   * @param {Object|null} dateRange - Date range filter or null.
   * @param {{caseId: string, runId: string}} owner - Parse run owner.
   */
  async function startImageParse(caseId, sel, dateRange, owner) {
    const imageId = sel.image_id;
    const arts = sel.artifact_options.map((o) => o.artifact_key);
    const aiArts = A.selectedAiArtifacts(sel.artifact_options);

    const imgState = st.imageParse[imageId] || {};
    const sseState = imgState.sseState || { es: null, retry: null, retryCount: 0, seq: -1 };
    sseState.retryCount = 0;
    sseState.seq = -1;
    imgState.run = true;
    imgState.done = false;
    imgState.fail = false;
    imgState.cancelled = false;
    imgState.controlEnded = false;
    imgState.transportFailed = false;
    imgState.owner = owner;
    imgState.sseState = sseState;
    imgState.arts = arts;
    imgState.aiArts = aiArts;
    imgState.snapshot = buildImageParseSnapshot(caseId, owner, sel, arts, aiArts);
    st.imageParse[imageId] = imgState;

    try {
      const payload = { artifact_options: sel.artifact_options };
      if (dateRange) payload.analysis_date_range = dateRange;

      await A.apiJson(
        `/api/cases/${encodeURIComponent(caseId)}/images/${encodeURIComponent(imageId)}/parse`,
        { method: "POST", json: payload, signal: st.parse.abort ? st.parse.abort.signal : undefined },
      );

      if (!isImageParseOwnerCurrent(imageId, owner)) return;
      startImageParseSse(caseId, imageId, owner);
    } catch (e) {
      if (e.name === "AbortError") return;
      if (!isImageParseOwnerCurrent(imageId, owner)) return;
      imgState.run = false;
      imgState.fail = true;
      setImageParseSectionStatus(imageId, "failed");
      setImageParseSectionError(imageId, `Failed to start: ${e.message}`);
      A.setMsg(el.parseErr, `Parse failed for ${sel.label}: ${e.message}`, "error");
      checkMultiImageCompletion(owner);
    }
  }

  // ── Parse progress rows (single-image) ────────────────────────────────────

  /**
   * Create the initial progress table rows for each artifact being parsed.
   *
   * @param {string[]} keys - Artifact keys to track.
   */
  function initParseRows(keys) {
    if (!el.parseRows) return;
    st.parse.rows = {};
    st.parse.status = {};
    el.parseRows.innerHTML = "";
    keys.forEach((k) => {
      const { tr, tdS, tdR } = A.createParseProgressRow(k, A.artifactName(k));
      el.parseRows.appendChild(tr);
      st.parse.rows[k] = { tr, tdS, tdR };
      st.parse.status[k] = "waiting";
    });
  }

  /** Reset the parse table to a single "Awaiting selection" placeholder row. */
  function renderParsePlaceholder() {
    if (!el.parseRows) return;
    el.parseRows.innerHTML = "";
    const { tr } = A.createParseProgressRow("", "Awaiting selection");
    el.parseRows.appendChild(tr);
    st.parse.rows = {};
    st.parse.status = {};
    if (el.parseProgress) el.parseProgress.value = 0;
    /* Clear multi-image sections. */
    const sectionsContainer = q("parse-image-sections");
    if (sectionsContainer) sectionsContainer.innerHTML = "";
    showSingleImageParseTable(true);
  }

  /**
   * Update (or create) a parse progress row for the given artifact.
   *
   * @param {string} key - Artifact key.
   * @param {string} status - Status label.
   * @param {number|null} count - Record count to display.
   * @param {string} [err] - Optional error message shown as a tooltip.
   */
  function setParseRow(key, status, count, err) {
    if (!key) return;
    let row = st.parse.rows[key];
    if (!row && el.parseRows) {
      const { tr, tdS, tdR } = A.createParseProgressRow(key, A.artifactName(key));
      el.parseRows.appendChild(tr);
      row = { tr, tdS, tdR };
      st.parse.rows[key] = row;
    }
    if (!row) return;
    row.tdS.textContent = status;
    row.tdS.dataset.status = status;
    if (typeof count === "number" && Number.isFinite(count)) row.tdR.textContent = String(Math.max(0, Math.floor(count)));
    if (err) row.tr.title = err;
    st.parse.status[key] = status;
  }

  /**
   * Recompute and set the overall parse progress bar value (0-100).
   *
   * For multi-image mode, tracks completion across all images.
   *
   * @param {boolean} [force=false] - When true, immediately set to 100%.
   */
  function updateParseProgress(force) {
    if (!el.parseProgress) return;
    if (force) return (el.parseProgress.value = 100);

    if (A.isMultiImage()) {
      return updateMultiImageParseProgress();
    }

    const keys = st.selected.length ? st.selected : Object.keys(st.parse.status);
    if (!keys.length) return (el.parseProgress.value = 0);
    const done = keys.filter((k) => st.parse.status[k] === "completed" || st.parse.status[k] === "failed").length;
    el.parseProgress.value = Math.max(0, Math.min(100, Math.round((done / keys.length) * 100)));
  }

  /** Return whether a parse row is already in a terminal state. */
  function isTerminalParseStatus(status) {
    return status === "completed" || status === "failed";
  }

  /** Mark any unfinished single-image parse rows as failed. */
  function failPendingParseRows() {
    Object.keys(st.parse.status || {}).forEach((key) => {
      if (!isTerminalParseStatus(st.parse.status[key])) setParseRow(key, "failed", null);
    });
  }

  /** Mark any unfinished per-image parse rows as failed. */
  function failPendingImageParseRows(imageId) {
    const imgState = st.imageParse[imageId];
    if (!imgState || !imgState.status) return;
    Object.keys(imgState.status).forEach((key) => {
      if (!isTerminalParseStatus(imgState.status[key])) setImageParseRow(imageId, key, "failed", null);
    });
  }

  // ── Multi-image parse progress UI ─────────────────────────────────────────

  /**
   * Show or hide the single-image parse table.
   *
   * @param {boolean} show - Whether to show the table.
   */
  function showSingleImageParseTable(show) {
    const table = q("parse-single-table");
    if (table) table.hidden = !show;
  }

  /**
   * Build per-image parse progress sections in the DOM.
   *
   * @param {Object[]} selections - Array of {image_id, label, artifact_options}.
   */
  function buildMultiImageParseSections(selections) {
    const container = q("parse-image-sections");
    if (!container) return;
    container.innerHTML = "";

    selections.forEach((sel) => {
      const section = document.createElement("div");
      section.className = "parse-image-section";
      section.dataset.imageId = sel.image_id;

      const header = document.createElement("div");
      header.className = "parse-image-section-header";
      const h4 = document.createElement("h4");
      h4.textContent = sel.label;
      header.appendChild(h4);
      const statusSpan = document.createElement("span");
      statusSpan.className = "parse-image-status";
      statusSpan.textContent = "Starting...";
      header.appendChild(statusSpan);
      section.appendChild(header);

      const table = document.createElement("table");
      table.appendChild(A.createParseProgressTableHead());
      const tbody = document.createElement("tbody");
      tbody.dataset.imageId = sel.image_id;

      /* Initialize rows for this image's artifacts. */
      const imgState = st.imageParse[sel.image_id] || {};
      imgState.rows = {};
      imgState.status = {};
      st.imageParse[sel.image_id] = imgState;

      const arts = sel.artifact_options.map((o) => o.artifact_key);
      arts.forEach((k) => {
        const { tr, tdS, tdR } = A.createParseProgressRow(k, A.artifactNameForImage(sel.image_id, k));
        tbody.appendChild(tr);
        imgState.rows[k] = { tr, tdS, tdR };
        imgState.status[k] = "waiting";
      });
      table.appendChild(tbody);
      section.appendChild(table);

      /* Error message area. */
      const errP = document.createElement("p");
      errP.className = "parse-image-error";
      errP.hidden = true;
      section.appendChild(errP);

      container.appendChild(section);
    });
  }

  /**
   * Update a parse progress row for a specific image.
   *
   * @param {string} imageId - Image ID.
   * @param {string} key - Artifact key.
   * @param {string} status - Status label.
   * @param {number|null} count - Record count.
   * @param {string} [err] - Optional error message.
   */
  function setImageParseRow(imageId, key, status, count, err) {
    const imgState = st.imageParse[imageId];
    if (!imgState || !imgState.rows) return;
    let row = imgState.rows[key];
    if (!row) return;
    row.tdS.textContent = status;
    row.tdS.dataset.status = status;
    if (typeof count === "number" && Number.isFinite(count)) row.tdR.textContent = String(Math.max(0, Math.floor(count)));
    if (err) row.tr.title = err;
    imgState.status[key] = status;
  }

  /**
   * Set the status text in a per-image parse section header.
   *
   * @param {string} imageId - Image ID.
   * @param {string} status - Status text.
   */
  function setImageParseSectionStatus(imageId, status) {
    const container = q("parse-image-sections");
    if (!container) return;
    const section = container.querySelector(`.parse-image-section[data-image-id="${A.cssEscape(imageId)}"]`);
    if (!section) return;
    const statusEl = section.querySelector(".parse-image-status");
    if (statusEl) {
      statusEl.textContent = status;
      statusEl.dataset.status = status;
    }
  }

  /**
   * Show an error message within a per-image parse section.
   *
   * @param {string} imageId - Image ID.
   * @param {string} msg - Error message.
   */
  function setImageParseSectionError(imageId, msg, kind = "error") {
    const container = q("parse-image-sections");
    if (!container) return;
    const section = container.querySelector(`.parse-image-section[data-image-id="${A.cssEscape(imageId)}"]`);
    if (!section) return;
    const errP = section.querySelector(".parse-image-error");
    if (errP) {
      errP.textContent = msg;
      errP.hidden = false;
      errP.dataset.status = kind === "warning" ? "warning" : "failed";
    }
  }

  /** Recompute the overall progress bar for multi-image parsing. */
  function updateMultiImageParseProgress() {
    if (!el.parseProgress) return;
    let totalArts = 0;
    let doneArts = 0;
    Object.values(st.imageParse).forEach((imgState) => {
      if (!imgState.status) return;
      const keys = Object.keys(imgState.status);
      totalArts += keys.length;
      doneArts += keys.filter((k) => imgState.status[k] === "completed" || imgState.status[k] === "failed").length;
    });
    if (!totalArts) return (el.parseProgress.value = 0);
    el.parseProgress.value = Math.max(0, Math.min(100, Math.round((doneArts / totalArts) * 100)));
  }

  /**
   * Debounce timer ID for {@link checkMultiImageCompletionImpl}.
   * @type {number|null}
   */
  let _multiImageCompleteTimer = null;

  /** Clear any pending debounced multi-image completion check. */
  function clearMultiImageCompletionTimer() {
    if (_multiImageCompleteTimer != null) {
      clearTimeout(_multiImageCompleteTimer);
      _multiImageCompleteTimer = null;
    }
  }

  /**
   * Schedule a debounced check for multi-image parse completion.
   *
   * Multiple SSE streams can fire completion events near-simultaneously.
   * This wrapper coalesces those calls so the actual finalization logic
   * runs only once on the next event-loop tick via setTimeout(…, 0).
   */
  function checkMultiImageCompletion(owner = st.parse.owner) {
    if (!owner || !A.isRunOwnerCurrent(st.parse, owner)) return;
    clearMultiImageCompletionTimer();
    _multiImageCompleteTimer = setTimeout(() => {
      _multiImageCompleteTimer = null;
      if (!A.isRunOwnerCurrent(st.parse, owner)) return;
      checkMultiImageCompletionImpl(owner);
    }, 0);
  }

  /**
   * Check if all images have finished parsing in multi-image mode.
   *
   * If all images are done (or failed), finalize the overall parse state.
   * Guarded: if the parse is already finalized (done or fail while not
   * running), this is a no-op so concurrent/duplicate calls are harmless.
   */
  function checkMultiImageCompletionImpl(owner = st.parse.owner) {
    if (!owner || !A.isRunOwnerCurrent(st.parse, owner)) return;
    /* Guard: already finalized — nothing to do. */
    if (!st.parse.run && (st.parse.done || st.parse.fail)) return;

    const imageIds = Object.keys(st.imageParse).filter((id) => isImageParseOwnerCurrent(id, owner));
    if (!imageIds.length) return;

    const allDone = imageIds.every((id) => {
      const s = st.imageParse[id];
      return s.done || s.fail;
    });
    if (!allDone) return;

    const allCancelled = imageIds.every((id) => st.imageParse[id].cancelled);
    const anyCancelled = imageIds.some((id) => st.imageParse[id].cancelled);
    const anyControlEnded = imageIds.some((id) => st.imageParse[id].controlEnded || st.imageParse[id].transportFailed);
    const allFailed = imageIds.every((id) => st.imageParse[id].fail);
    const anyFailed = imageIds.some((id) => st.imageParse[id].fail);
    const anyUsable = imageIds.some((id) => {
      const s = st.imageParse[id];
      return s.done && !s.fail && !s.noUsable;
    });

    updateMultiImageParseProgress();

    if (allCancelled) {
      st.parse.run = false;
      st.parse.done = false;
      st.parse.fail = false;
      st.parse.selectionStale = false;
      A.clearParsedSelections();
      A.stopTimer("parse");
      A.setMsg(el.parseErr, "Parsing cancelled.", "info");
      A.updateParseButton();
      A.updateNav();
      return;
    }

    if (anyCancelled || anyControlEnded) {
      st.parse.run = false;
      st.parse.done = false;
      st.parse.fail = !anyCancelled;
      st.parse.selectionStale = false;
      A.clearParsedSelections();
      A.stopTimer("parse");
      const message = anyCancelled
        ? "Parsing cancelled before all images completed."
        : "Parse progress ended before all images completed. Start parsing again.";
      A.setMsg(el.parseErr, message, anyCancelled ? "info" : "error");
      A.updateParseButton();
      A.updateNav();
      return;
    }

    if (allFailed) {
      st.parse.run = false;
      st.parse.done = false;
      st.parse.fail = true;
      st.parse.selectionStale = false;
      A.clearParsedSelections();
      A.stopTimer("parse");
      A.setMsg(el.parseErr, "All image parsing failed.", "error");
      A.updateParseButton();
      A.updateNav();
      return;
    }

    if (!anyUsable) {
      st.parse.run = false;
      st.parse.done = false;
      st.parse.fail = anyFailed;
      st.parse.selectionStale = false;
      A.clearParsedSelections();
      A.stopTimer("parse");
      const message = anyFailed
        ? "Parsing finished, but no selected artifacts produced records to analyze. Some parsing errors occurred; check the image details."
        : "Parsing completed, but selected artifacts produced no records to analyze.";
      A.setMsg(el.parseErr, message, anyFailed ? "error" : "info");
      A.updateParseButton();
      A.updateNav();
      return;
    }

    /* At least one image succeeded. */
    if (!commitMultiParsedSelections(owner)) return;
    st.parse.run = false;
    st.parse.done = true;
    st.parse.fail = false;
    if (el.parseProgress) el.parseProgress.value = 100;
    A.stopTimer("parse");
    A.clearMsg(el.parseErr);
    A.updateParseButton();
    A.updateNav();

    if (st.selectedAi.length > 0) return A.showStep(4);
    A.setMsg(el.parseErr, "Parsing complete. No artifacts were set to \u201cParse and use in AI,\u201d so analysis is not available.", "success");
  }

  // ── Parse SSE (single-image) ──────────────────────────────────────────────

  /** Open the image-scoped parse-progress SSE stream for the active case. */
  function startParseSse(owner = st.parse.owner, imageId = st.parse.imageId) {
    A.clearMsg(el.parseErr);
    const caseId = owner ? owner.caseId : A.activeCaseId();
    if (!caseId) return A.setMsg(el.parseErr, "No active case for parse stream.", "error");
    const parsedImageId = String(imageId || "").trim();
    if (!parsedImageId) return A.setMsg(el.parseErr, "No evidence image for parse stream.", "error");
    if (!A.isRunOwnerCurrent(st.parse, owner)) return;
    A.openSseStream(
      `/api/cases/${encodeURIComponent(caseId)}/images/${encodeURIComponent(parsedImageId)}/parse/progress`,
      st.parse,
      {
        onEvent: (p) => onParseEvent(p, owner),
        onError: () => {
          if (A.isRunOwnerCurrent(st.parse, owner) && !st.parse.done && !st.parse.fail && st.parse.run) retryParseSse(owner, parsedImageId);
        },
      },
    );
  }

  /** Dispatch a single parse SSE event to the appropriate UI handler. */
  function onParseEvent(p, owner = null) {
    if (!A.isRunOwnerCurrent(st.parse, owner)) return;
    const t = String(p.type || "");
    if (t === "parse_started") {
      const arts = Array.isArray(p.artifacts) ? p.artifacts.map(String) : st.selected;
      const aiArtifacts = Array.isArray(p.analysis_artifacts) ? p.analysis_artifacts.map(String) : st.selectedAi;
      st.selected = arts;
      st.selectedAi = aiArtifacts;
      if (st.parse.pendingSelectionSnapshot) {
        st.parse.pendingSelectionSnapshot.artifacts = arts.slice();
        st.parse.pendingSelectionSnapshot.aiArtifacts = aiArtifacts.slice();
      }
      if (arts.length && !Object.keys(st.parse.rows).length) initParseRows(arts);
      updateParseProgress();
      return;
    }
    if (t === "artifact_started") return (setParseRow(String(p.artifact_key || ""), "parsing", A.num(p.record_count, null)), updateParseProgress());
    if (t === "artifact_progress") return setParseRow(String(p.artifact_key || ""), "parsing", A.num(p.record_count, 0));
    if (t === "artifact_completed") return (setParseRow(String(p.artifact_key || ""), "completed", A.num(p.record_count, 0)), updateParseProgress());
    if (t === "parse_warning") {
      const key = String(p.artifact_key || "");
      const message = parseWarningText(p, key ? A.artifactName(key) : "");
      if (key) setParseRow(key, "warning", A.num(p.record_count, null), message);
      A.setMsg(el.parseErr, message, "warning");
      return;
    }
    if (t === "artifact_failed") {
      const key = String(p.artifact_key || "");
      setParseRow(key, "failed", A.num(p.record_count, 0), String(p.error || "Unknown parser error."));
      updateParseProgress();
      return A.setMsg(el.parseErr, `Parse failed for ${A.artifactName(key)}: ${String(p.error || "Unknown parser error.")}`, "error");
    }
    if (t === "parse_completed") {
      if (!parseEventHasUsableCsvs(p)) {
        st.parse.pendingSelectionSnapshot = null;
        closeParseSse();
        return finishParseWithNoUsableRecords(p);
      }
      commitSingleParsedSelection(owner, p);
      st.parse.run = false;
      st.parse.done = true;
      st.parse.fail = false;
      updateParseProgress(true);
      A.stopTimer("parse");
      closeParseSse();
      A.clearMsg(el.parseErr);
      A.updateParseButton();
      A.updateNav();
      if (st.selectedAi.length > 0) return A.showStep(4);
      A.setMsg(el.parseErr, "Parsing complete. No artifacts were set to \u201cParse and use in AI,\u201d so analysis is not available. You can review parsed CSVs in the case folder or go back and re-parse with AI-enabled artifacts.", "success");
      return;
    }
    if (t === "parse_failed") {
      failPendingParseRows();
      st.parse.run = false;
      st.parse.done = false;
      st.parse.fail = true;
      st.parse.selectionStale = false;
      A.clearParsedSelections();
      updateParseProgress();
      A.stopTimer("parse");
      closeParseSse();
      A.setMsg(el.parseErr, String(p.error || "Parsing failed."), "error");
      A.updateParseButton();
      A.updateNav();
      return A.showStep(3);
    }
    if (t === "parse_cancelled" || t === "cancelled") {
      st.parse.run = false;
      st.parse.done = false;
      st.parse.fail = false;
      st.parse.selectionStale = false;
      A.clearParsedSelections();
      A.stopTimer("parse");
      closeParseSse();
      A.setMsg(el.parseErr, "Parsing cancelled.", "info");
      A.updateParseButton();
      A.updateNav();
      return;
    }
    if (t === "complete" || t === "idle") {
      closeParseSse();
      if (st.parse.done || st.parse.fail || !st.parse.run) {
        A.updateParseButton();
        A.updateNav();
        return;
      }
      failPendingParseRows();
      st.parse.run = false;
      st.parse.done = false;
      st.parse.fail = true;
      st.parse.selectionStale = false;
      A.clearParsedSelections();
      updateParseProgress();
      A.stopTimer("parse");
      const message = t === "idle"
        ? "No active parse progress stream. Start parsing again."
        : "Parse progress ended before a completion event. Start parsing again.";
      A.setMsg(el.parseErr, message, "error");
      A.updateParseButton();
      A.updateNav();
      return A.showStep(3);
    }
    if (t === "error") A.setMsg(el.parseErr, String(p.message || "Parse stream error."), "error");
  }

  // ── Parse SSE (multi-image) ───────────────────────────────────────────────

  /**
   * Open an SSE stream for a specific image's parse progress.
   *
   * @param {string} caseId - Case ID.
   * @param {string} imageId - Image ID.
   * @param {{caseId: string, runId: string}|null} owner - Parse run owner.
   */
  function startImageParseSse(caseId, imageId, owner = null) {
    const imgState = st.imageParse[imageId];
    if (!imgState) return;
    const runOwner = owner || imgState.owner || st.parse.owner;
    if (!isImageParseOwnerCurrent(imageId, runOwner)) return;

    const url = `/api/cases/${encodeURIComponent(caseId)}/images/${encodeURIComponent(imageId)}/parse/progress`;

    /* Keep one SSE-compatible channel object for the life of this image run. */
    const sseState = imgState.sseState || { es: null, retry: null, retryCount: 0, seq: -1 };
    sseState.owner = runOwner;
    imgState.sseState = sseState;

    A.openSseStream(url, sseState, {
      onEvent: (p) => onImageParseEvent(imageId, p, runOwner),
      onError: () => {
        if (isImageParseOwnerCurrent(imageId, runOwner) && !imgState.done && !imgState.fail && imgState.run) {
          retryImageParseSse(caseId, imageId, runOwner);
        }
      },
    });
  }

  /**
   * Handle a parse SSE event for a specific image.
   *
   * @param {string} imageId - Image ID.
   * @param {Object} p - SSE event payload.
   * @param {{caseId: string, runId: string}|null} owner - Parse run owner.
   */
  function onImageParseEvent(imageId, p, owner = null) {
    if (!isImageParseOwnerCurrent(imageId, owner)) return;
    const imgState = st.imageParse[imageId];
    if (!imgState) return;
    const t = String(p.type || "");

    if (t === "parse_started") {
      setImageParseSectionStatus(imageId, "Parsing...");
      updateMultiImageParseProgress();
      return;
    }
    if (t === "artifact_started") {
      setImageParseRow(imageId, String(p.artifact_key || ""), "parsing", A.num(p.record_count, null));
      updateMultiImageParseProgress();
      return;
    }
    if (t === "artifact_progress") {
      setImageParseRow(imageId, String(p.artifact_key || ""), "parsing", A.num(p.record_count, 0));
      return;
    }
    if (t === "artifact_completed") {
      setImageParseRow(imageId, String(p.artifact_key || ""), "completed", A.num(p.record_count, 0));
      updateMultiImageParseProgress();
      return;
    }
    if (t === "parse_warning") {
      const key = String(p.artifact_key || "");
      const message = parseWarningText(p, key ? A.artifactNameForImage(imageId, key) : "");
      if (key) setImageParseRow(imageId, key, "warning", A.num(p.record_count, null), message);
      setImageParseSectionStatus(imageId, "warning");
      setImageParseSectionError(imageId, message, "warning");
      return;
    }
    if (t === "artifact_failed") {
      const key = String(p.artifact_key || "");
      setImageParseRow(imageId, key, "failed", A.num(p.record_count, 0), String(p.error || "Unknown parser error."));
      setImageParseSectionError(imageId, `${A.artifactNameForImage(imageId, key)}: ${String(p.error || "Unknown parser error.")}`);
      updateMultiImageParseProgress();
      return;
    }
    if (t === "parse_completed") {
      const hasUsableCsvs = parseEventHasUsableCsvs(p);
      imgState.run = false;
      imgState.done = true;
      imgState.fail = false;
      imgState.cancelled = false;
      imgState.noUsable = !hasUsableCsvs;
      imgState.controlEnded = false;
      imgState.transportFailed = false;
      imgState.parsedSnapshot = hasUsableCsvs ? (imgState.snapshot || null) : null;
      setImageParseSectionStatus(imageId, hasUsableCsvs ? "completed" : "no records");
      closeImageParseSse(imageId);
      checkMultiImageCompletion(owner);
      return;
    }
    if (t === "parse_failed") {
      failPendingImageParseRows(imageId);
      imgState.run = false;
      imgState.done = false;
      imgState.fail = true;
      imgState.cancelled = false;
      imgState.controlEnded = false;
      imgState.transportFailed = false;
      setImageParseSectionStatus(imageId, "failed");
      setImageParseSectionError(imageId, String(p.error || "Parsing failed."));
      closeImageParseSse(imageId);
      checkMultiImageCompletion(owner);
      return;
    }
    if (t === "parse_cancelled" || t === "cancelled") {
      imgState.run = false;
      imgState.done = false;
      imgState.fail = true;
      imgState.cancelled = true;
      imgState.controlEnded = false;
      imgState.transportFailed = false;
      setImageParseSectionStatus(imageId, "cancelled");
      setImageParseSectionError(imageId, "Parsing cancelled.");
      closeImageParseSse(imageId);
      checkMultiImageCompletion(owner);
      return;
    }
    if (t === "complete" || t === "idle") {
      closeImageParseSse(imageId);
      if (imgState.done || imgState.fail || !imgState.run) {
        checkMultiImageCompletion(owner);
        return;
      }
      failPendingImageParseRows(imageId);
      imgState.run = false;
      imgState.done = false;
      imgState.fail = true;
      imgState.cancelled = false;
      imgState.controlEnded = true;
      const message = t === "idle"
        ? "No active parse progress stream."
        : "Parse progress ended before completion.";
      setImageParseSectionStatus(imageId, "failed");
      setImageParseSectionError(imageId, message);
      checkMultiImageCompletion(owner);
      return;
    }
    if (t === "error") {
      setImageParseSectionError(imageId, String(p.message || "Parse stream error."));
    }
  }

  /**
   * Retry the SSE connection for a specific image's parse stream.
   *
   * @param {string} caseId - Case ID.
   * @param {string} imageId - Image ID.
   * @param {{caseId: string, runId: string}|null} owner - Parse run owner.
   */
  function retryImageParseSse(caseId, imageId, owner = null) {
    const imgState = st.imageParse[imageId];
    if (!imgState || imgState.done || imgState.fail || !imgState.run) return;
    const runOwner = owner || imgState.owner || st.parse.owner;
    if (!isImageParseOwnerCurrent(imageId, runOwner)) return;
    const sseState = imgState.sseState;
    if (!sseState) return;

    A.retrySseStream(sseState, {
      reconnect: () => {
        if (isImageParseOwnerCurrent(imageId, runOwner) && !imgState.done && !imgState.fail && imgState.run) {
          startImageParseSse(caseId, imageId, runOwner);
        }
      },
      onRetryScheduled: (attempt, delaySec) => {
        setImageParseSectionError(imageId, `Connection dropped. Reconnecting (${attempt}/${A.SSE_MAX_RETRIES}) in ${delaySec}s...`);
      },
      onMaxRetries: () => {
        if (!isImageParseOwnerCurrent(imageId, runOwner)) return;
        imgState.run = false;
        imgState.fail = true;
        imgState.transportFailed = true;
        failPendingImageParseRows(imageId);
        closeImageParseSse(imageId);
        setImageParseSectionStatus(imageId, "failed");
        setImageParseSectionError(imageId, `Connection lost after ${A.SSE_MAX_RETRIES} retries.`);
        checkMultiImageCompletion(runOwner);
      },
    });
  }

  /**
   * Close the SSE stream for a specific image.
   *
   * @param {string} imageId - Image ID.
   */
  function closeImageParseSse(imageId) {
    const imgState = st.imageParse[imageId];
    if (!imgState || !imgState.sseState) return;
    A.closeSseChannel(imgState.sseState);
  }

  // ── SSE retry / close / cancel (single-image table) ──────────────────────

  /** Attempt to reconnect the parse SSE stream with exponential backoff. */
  function retryParseSse(owner = st.parse.owner, imageId = st.parse.imageId) {
    if (st.parse.done || st.parse.fail || !st.parse.run) return;
    if (!A.isRunOwnerCurrent(st.parse, owner)) return;
    const parsedImageId = String(imageId || "").trim();
    if (!parsedImageId) return;
    A.retrySseStream(st.parse, {
      reconnect: () => {
        if (A.isRunOwnerCurrent(st.parse, owner) && !st.parse.done && !st.parse.fail && st.parse.run) startParseSse(owner, parsedImageId);
      },
      onRetryScheduled: (attempt, delaySec) => {
        A.setMsg(el.parseErr, `Parse progress connection dropped. Reconnecting (${attempt}/${A.SSE_MAX_RETRIES}) in ${delaySec}s...`, "error");
      },
      onMaxRetries: () => {
        if (!A.isRunOwnerCurrent(st.parse, owner)) return;
        st.parse.run = false;
        st.parse.done = false;
        st.parse.fail = true;
        st.parse.selectionStale = false;
        A.clearParsedSelections();
        failPendingParseRows();
        st.parse.retryCount = 0;
        updateParseProgress();
        A.stopTimer("parse");
        closeParseSse();
        A.setMsg(el.parseErr, `Parse progress connection lost after ${A.SSE_MAX_RETRIES} retries. Start parsing again.`, "error");
        A.updateParseButton();
        A.updateNav();
      },
    });
  }

  /** Close the parse SSE EventSource and clear pending retries. */
  function closeParseSse() {
    A.closeSseChannel(st.parse);
  }

  /** Cancel any in-progress parse: abort HTTP, close SSE, notify backend. */
  function cancelParse() {
    const caseId = A.activeCaseId();
    const wasRunning = st.parse.run;
    clearMultiImageCompletionTimer();
    st.parse.owner = null;
    st.parse.imageId = "";
    st.parse.pendingSelectionSnapshot = null;
    Object.keys(st.imageParse).forEach((imageId) => {
      if (st.imageParse[imageId]) {
        st.imageParse[imageId].owner = null;
        st.imageParse[imageId].run = false;
        st.imageParse[imageId].done = false;
        st.imageParse[imageId].fail = true;
        st.imageParse[imageId].cancelled = true;
        st.imageParse[imageId].controlEnded = false;
        st.imageParse[imageId].transportFailed = false;
        st.imageParse[imageId].snapshot = null;
        setImageParseSectionStatus(imageId, "cancelled");
      }
    });
    if (st.parse.abort) {
      st.parse.abort.abort();
      st.parse.abort = null;
    }
    closeParseSse();

    /* Close all per-image SSE streams. */
    Object.keys(st.imageParse).forEach((imageId) => {
      closeImageParseSse(imageId);
    });

    if (!wasRunning) return;
    st.parse.run = false;
    st.parse.done = false;
    st.parse.fail = false;
    st.parse.selectionStale = false;
    A.clearParsedSelections();
    A.stopTimer("parse");
    A.setMsg(el.parseErr, "Parsing cancelled.", "info");
    A.updateParseButton();
    A.updateNav();
    if (caseId) {
      const cancelPromise = A.apiJson(`/api/cases/${encodeURIComponent(caseId)}/parse/cancel`, { method: "POST" })
        .catch(() => {});
      st.parse.cancelPending = cancelPromise;
      cancelPromise.finally(() => {
        if (st.parse.cancelPending === cancelPromise) st.parse.cancelPending = null;
      });
    }
  }

  /** Reset all parse state, close SSE, clear UI, and cascade to analysis reset. */
  function resetParseState() {
    clearMultiImageCompletionTimer();
    st.parse.owner = null;
    st.parse.imageId = "";
    st.parse.pendingSelectionSnapshot = null;
    Object.keys(st.imageParse).forEach((imageId) => {
      if (st.imageParse[imageId]) st.imageParse[imageId].owner = null;
    });
    closeParseSse();
    /* Close all per-image SSE streams. */
    Object.keys(st.imageParse).forEach((imageId) => {
      closeImageParseSse(imageId);
    });
    st.imageParse = {};

    A.stopTimer("parse");
    st.parse.run = false;
    st.parse.done = false;
    st.parse.fail = false;
    st.parse.selectionStale = false;
    st.parse.retryCount = 0;
    st.parse.seq = -1;
    st.parse.rows = {};
    st.parse.status = {};
    A.clearParsedSelections();
    A.clearMsg(el.parseErr);
    renderParsePlaceholder();
    // Old analysis results depend on old parsed data -- clear them.
    A.resetAnalysisState();
    A.updateParseButton();
    A.updateNav();
  }

  // ── Public API ─────────────────────────────────────────────────────────────
  A.submitParse = submitParse;
  A.cancelParse = cancelParse;
  A.closeParseSse = closeParseSse;
  A.resetParseState = resetParseState;
  A.renderParsePlaceholder = renderParsePlaceholder;
  A._onParseEvent = onParseEvent;
  A._onImageParseEvent = onImageParseEvent;
  A._startImageParseSse = startImageParseSse;
  A._retryImageParseSse = retryImageParseSse;
})();
