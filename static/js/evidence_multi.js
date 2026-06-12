/**
 * Multi-image evidence intake, submission, and per-image artifact tab management.
 *
 * Handles adding/removing image form cards, multi-image evidence submission,
 * per-image metadata rendering, and per-image artifact selection tabs.
 *
 * Depends on: AIFT (utils.js), evidence.js (must be loaded first)
 */
"use strict";

(() => {
  const A = window.AIFT;
  const { st, el, q } = A;

  // ── Multi-image form management ─────────────────────────────────────────

  /** Counter for generating unique image form indices. */
  let imageFormCounter = 0;
  const discoveryDescriptorsByCard = new WeakMap();

  /** Return a descriptor payload with only fields the backend understands. */
  function discoveryDescriptorPayload(entry) {
    if (!A.isObj(entry)) return null;
    const payload = {};
    [
      "dissect_path",
      "source_path",
      "label",
      "source_mode",
      "files_to_hash",
      "extracted_from",
      "extraction_root",
    ].forEach((key) => {
      if (Object.prototype.hasOwnProperty.call(entry, key)) payload[key] = entry[key];
    });
    if (!payload.dissect_path && entry.path) payload.dissect_path = entry.path;
    return payload.dissect_path || payload.source_path ? payload : null;
  }

  /** Clear any discovery descriptor associated with a card. */
  function clearDiscoveryDescriptor(card) {
    if (!card) return;
    discoveryDescriptorsByCard.delete(card);
    delete card.dataset.discoveryPath;
  }

  /** Add a new image intake form to the container. */
  function addImageForm() {
    const container = q("image-forms-container");
    if (!container) return null;
    imageFormCounter += 1;
    const idx = imageFormCounter;
    const card = A.createImageFormCard(idx, A.getImageForms().length + 1);
    container.appendChild(card);
    A.initImageForm(card);
    renumberImageForms();
    return card;
  }

  /**
   * Remove an image form card from the container.
   *
   * @param {HTMLElement} card - The .image-form-card element to remove.
   */
  function removeImageForm(card) {
    if (!card) return;
    const forms = A.getImageForms();
    /* Don't allow removing the last remaining image form. */
    if (forms.length <= 1) return;
    clearDiscoveryDescriptor(card);
    A.clearDroppedFilesForCard(card);
    card.remove();
    renumberImageForms();
  }

  /** Re-number image form titles after add/remove. */
  function renumberImageForms() {
    const forms = A.getImageForms();
    const totalForms = forms.length;
    forms.forEach((card, i) => {
      const title = card.querySelector(".image-form-title");
      if (title) title.textContent = `Image ${i + 1}`;
      /* Show remove button only when there are multiple forms. */
      const removeBtn = card.querySelector(".image-remove-btn");
      if (removeBtn) removeBtn.hidden = totalForms <= 1;
    });
  }

  /**
   * Gather the evidence data from a single image form card.
   *
   * @param {HTMLElement} card - The .image-form-card element.
   * @returns {{uploadMode: boolean, files: File[], path: string, label: string}|null}
   *     Null if validation fails (sets error message on card).
   */
  function gatherImageFormData(card) {
    const modeUpload = card.querySelector(".image-mode-upload");
    const uploadMode = !!(modeUpload && modeUpload.checked);
    const labelInput = card.querySelector(".image-label-input");
    const label = labelInput ? String(labelInput.value || "").trim() : "";
    const statusMsg = card.querySelector(".image-status-msg");

    if (uploadMode) {
      const files = A.imageUploadFilesForCard(card);
      if (files.length === 0) {
        setImageStatusMsg(statusMsg, "Choose one or more evidence files first.", "error");
        return null;
      }
      clearDiscoveryDescriptor(card);
      return { uploadMode: true, files, path: "", label };
    }

    const pathInput = card.querySelector(".image-path-input");
    const path = A.sanitizeEvidencePath(pathInput ? pathInput.value : "");
    if (!path) {
      setImageStatusMsg(statusMsg, "Enter a local evidence path.", "error");
      return null;
    }
    const descriptor = discoveryDescriptorsByCard.get(card);
    const descriptorPath = A.sanitizeEvidencePath(
      descriptor && (descriptor.dissect_path || descriptor.path),
    );
    const evidenceDescriptor = descriptor && descriptorPath === path
      ? discoveryDescriptorPayload(descriptor)
      : null;
    return { uploadMode: false, files: [], path, label, evidenceDescriptor };
  }

  /**
   * Set the status message on an image card.
   *
   * @param {HTMLElement|null} node - The .image-status-msg element.
   * @param {string} text - Message text.
   * @param {string} [kind="info"] - "info", "error", or "success".
   */
  function setImageStatusMsg(node, text, kind) {
    if (!node) return;
    if (!text) {
      node.hidden = true;
      node.textContent = "";
      delete node.dataset.status;
      return;
    }
    node.hidden = false;
    node.textContent = text;
    node.dataset.status = kind === "error" ? "failed" : kind === "success" ? "success" : "in-progress";
  }

  /** Build a readable fallback label from a discovered path. */
  function labelFromPath(path) {
    const trimmed = String(path || "").replace(/[\\/]+$/, "");
    const name = trimmed.split(/[\\/]/).pop() || "Image";
    return (name.replace(/\.[^/.]+$/, "") || name || "Image").trim();
  }

  /** Normalize one backend discovery entry into a path/label object. */
  function normalizeDiscoveredEvidence(entry) {
    if (typeof entry === "string") {
      const path = A.sanitizeEvidencePath(entry);
      return path ? { path, label: labelFromPath(path) } : null;
    }
    if (!A.isObj(entry)) return null;
    const path = A.sanitizeEvidencePath(entry.path);
    if (!path) return null;
    const label = String(entry.label || "").trim() || labelFromPath(path);
    return Object.assign({}, entry, { path, label });
  }

  /** Fill a form card with a discovered local-path evidence entry. */
  function setCardToDiscoveredEvidence(card, entry, index) {
    const labelInput = card.querySelector(".image-label-input");
    if (labelInput) labelInput.value = entry.label || `Image ${index + 1}`;

    const modeUpload = card.querySelector(".image-mode-upload");
    const modePath = card.querySelector(".image-mode-path");
    if (modeUpload) modeUpload.checked = false;
    if (modePath) modePath.checked = true;

    const pathInput = card.querySelector(".image-path-input");
    if (pathInput) pathInput.value = entry.path;
    const descriptor = discoveryDescriptorPayload(entry);
    if (descriptor) {
      discoveryDescriptorsByCard.set(card, descriptor);
      card.dataset.discoveryPath = entry.path;
    } else {
      clearDiscoveryDescriptor(card);
    }

    const fileInput = card.querySelector(".image-file-input");
    if (fileInput) fileInput.value = "";
    A.clearDroppedFilesForCard(card);
    A.applyEvidenceFormatMetadata(card);

    const metaCard = card.querySelector(".image-metadata-card");
    if (metaCard) metaCard.hidden = true;

    const statusMsg = card.querySelector(".image-status-msg");
    setImageStatusMsg(statusMsg, "Discovered. Ready to submit.", "success");
  }

  /** Replace the current image cards with discovered evidence paths. */
  function populateImageFormsFromDiscovery(entries) {
    const container = q("image-forms-container");
    if (!container || !entries.length) return;

    const existing = A.getImageForms();
    existing.forEach((card, i) => { if (i > 0) card.remove(); });

    let firstCard = A.getImageForms()[0];
    if (!firstCard) firstCard = addImageForm();

    entries.forEach((entry, i) => {
      const card = i === 0 ? firstCard : addImageForm();
      if (!card) return;
      setCardToDiscoveredEvidence(card, entry, i);
    });

    renumberImageForms();
    A.syncMode();
  }

  /** Render the discovered target list in the directory scan panel. */
  function renderScanDirectoryResults(entries) {
    if (!el.scanDirectoryResults) return;
    el.scanDirectoryResults.innerHTML = "";
    entries.forEach((entry) => {
      const item = document.createElement("li");
      const label = entry.label ? `${entry.label}: ` : "";
      item.textContent = `${label}${entry.path}`;
      el.scanDirectoryResults.appendChild(item);
    });
    el.scanDirectoryResults.hidden = !entries.length;
  }

  /** Scan an absolute local path for evidence targets and add them as image cards. */
  async function scanEvidenceDirectory() {
    A.clearMsg(el.evidenceMsg);
    A.clearMsg(el.scanDirectoryMsg);
    if (el.scanDirectoryResults) {
      el.scanDirectoryResults.hidden = true;
      el.scanDirectoryResults.innerHTML = "";
    }
    const scanPath = A.sanitizeEvidencePath(el.scanDirectoryPath ? el.scanDirectoryPath.value : "");
    if (!scanPath) {
      return A.setMsg(el.scanDirectoryMsg || el.evidenceMsg, "Enter a local directory path before scanning.", "error");
    }

    const timeoutMs = A.num(
      A.obj(A.obj(st.settings).evidence).intake_timeout_seconds,
      7200,
    ) * 1000;

    const token = A.beginEvidenceOperation("scan");
    setEvidenceBusy(true, false);
    A.setMsg(el.scanDirectoryMsg || el.evidenceMsg, "Scanning for supported evidence targets...", "info");

    try {
      const response = await A.apiJson(
        "/api/evidence/discover",
        { method: "POST", json: { path: scanPath }, timeout: timeoutMs, signal: token.signal },
      );
      if (!A.isEvidenceOperationCurrent(token)) return;
      const entries = (Array.isArray(response.evidence) ? response.evidence : [])
        .map(normalizeDiscoveredEvidence)
        .filter(Boolean);
      /* Non-fatal backend warnings, e.g. corrupt archives skipped while
         scanning a directory, are appended to the scan messages. */
      const warnings = (Array.isArray(response.warnings) ? response.warnings : [])
        .filter((warning) => typeof warning === "string" && warning.trim())
        .map((warning) => warning.trim());
      const warningSuffix = warnings.length ? ` ${warnings.join(" ")}` : "";

      if (!entries.length) {
        return A.setMsg(
          el.scanDirectoryMsg || el.evidenceMsg,
          `No supported evidence targets were found at that path.${warningSuffix}`,
          "error",
        );
      }

      populateImageFormsFromDiscovery(entries);
      renderScanDirectoryResults(entries);
      const noun = entries.length === 1 ? "target" : "targets";
      const msgKind = warnings.length ? "warning" : "success";
      A.setMsg(
        el.scanDirectoryMsg || el.evidenceMsg,
        `Found ${entries.length} evidence ${noun}. Image cards were added below.${warningSuffix}`,
        msgKind,
      );
      A.setMsg(
        el.evidenceMsg,
        `Found ${entries.length} evidence ${noun}. Review the paths, then submit.${warningSuffix}`,
        msgKind,
      );
    } catch (e) {
      if (!A.isEvidenceOperationCurrent(token) || e.name === "AbortError") return;
      A.setMsg(el.scanDirectoryMsg || el.evidenceMsg, `Directory scan failed: ${e.message}`, "error");
    } finally {
      if (A.finishEvidenceOperation(token)) {
        setEvidenceBusy(false, false);
        A.updateNav();
      }
    }
  }

  // ── Multi-image submission ──────────────────────────────────────────────

  /**
   * Submit evidence to the backend: create a case, then for each image form
   * call the multi-image endpoints sequentially.
   *
   * Any parse or analysis still running for the previous case is cancelled
   * before the new case is created, and the previous case's wizard state is
   * retired as soon as the new case ID is committed — not only after the
   * whole intake finishes — so stale results cannot be acted on mid-intake.
   */
  async function submitEvidence() {
    A.clearMsg(el.evidenceMsg);
    A.clearMsg(el.artifactsMsg);
    A.clearMsg(el.profileMsg);
    A.clearMsg(el.parseErr);

    const imageForms = A.getImageForms();
    if (!imageForms.length) return A.setMsg(el.evidenceMsg, "No image forms found.", "error");

    /* Gather and validate all image form data upfront. */
    const imageDataList = [];
    for (const card of imageForms) {
      const statusMsg = card.querySelector(".image-status-msg");
      setImageStatusMsg(statusMsg, "", "info");
      const data = gatherImageFormData(card);
      if (!data) return; /* Validation error already shown on the card. */
      imageDataList.push({ card, data });
    }

    /* Check upload size thresholds. */
    const threshMb = A.num(A.obj(A.obj(st.settings).evidence).large_file_threshold_mb, 0);
    if (threshMb > 0) {
      for (const { data } of imageDataList) {
        if (!data.uploadMode) continue;
        const totalBytes = data.files.reduce(function(sum, f) { return sum + (f.size || 0); }, 0);
        const threshBytes = threshMb * 1024 * 1024;
        if (totalBytes > threshBytes) {
          const limitGb = (threshMb / 1024).toFixed(1);
          const sizeGb = (totalBytes / (1024 * 1024 * 1024)).toFixed(1);
          return A.setMsg(el.evidenceMsg,
            "File size (" + sizeGb + " GB) exceeds the Evidence Size Threshold (" + limitGb + " GB). " +
            "Use path mode instead, or increase the threshold in Settings \u2192 Advanced.",
            "error");
        }
      }
    }

    /* Cancel any prior-case parse/analysis while the previous case is still
       the active case, so the cancel requests target the case that owns the
       running work. Both cancel helpers post to the active case ID. */
    if (st.parse.run) {
      A.cancelParse();
      await st.parse.cancelPending;
      A.clearMsg(el.parseErr);
    }
    if (st.analysis.run) {
      A.cancelAnalysis();
      await st.analysis.cancelPending;
      A.clearMsg(el.analysisMsg);
    }

    const token = A.beginEvidenceOperation("submit");
    setEvidenceBusy(true);
    const intakeProgress = createIntakeProgressTracker();
    const intakeStatusEl = q("evidence-intake-status");
    let intakeCaseId = "";

    try {
      /* Step 1: Create the case. */
      const c = await A.apiJson("/api/cases", { method: "POST", json: { case_name: A.val(el.caseName) }, signal: token.signal });
      if (!A.isEvidenceOperationCurrent(token)) return;
      const caseId = String(c.case_id || "").trim();
      intakeCaseId = caseId;
      st.caseName = String(c.case_name || "");
      if (!caseId) throw new Error("Case ID missing from create response.");
      intakeProgress.setPhase("case-created");
      A.setCaseId(caseId);

      /* The new case is now the active case: retire the previous case's
         parse/analysis/results/chat state and stale Step-2 UI immediately,
         instead of waiting for the whole intake to finish. The evidence
         forms, intake progress bar, and intake status message stay live. */
      clearStaleCaseUiState();

      const intakeTimeoutMs = A.num(A.obj(A.obj(st.settings).evidence).intake_timeout_seconds, 7200) * 1000;
      const skipHashing = !A.boolSetting(A.obj(A.obj(st.settings).evidence).compute_hashes, true);

      /* Step 2: Process each image sequentially. */
      st.images = [];
      const allArtifacts = [];
      let firstOsType = "";
      const totalImages = imageDataList.length;

      for (let i = 0; i < totalImages; i++) {
        const { card, data } = imageDataList[i];
        const statusMsg = card.querySelector(".image-status-msg");

        if (intakeStatusEl) {
          intakeStatusEl.hidden = false;
          intakeStatusEl.textContent = `Processing image ${i + 1} of ${totalImages}...`;
        }
        setImageStatusMsg(statusMsg, "Processing...", "info");

        /* Create image slot. */
        const imgResp = await A.apiJson(
          `/api/cases/${encodeURIComponent(caseId)}/images`,
          { method: "POST", json: { label: data.label || `Image ${i + 1}` }, signal: token.signal },
        );
        if (!A.isEvidenceOperationCurrent(token)) return;
        const imageId = String(imgResp.image_id || "").trim();
        if (!imageId) throw new Error(`Image ID missing from response for image ${i + 1}.`);

        /* Upload/link evidence for this image. */
        let ev;
        if (data.uploadMode) {
          const fd = new FormData();
          data.files.forEach((file, index) => {
            fd.append("evidence_file", file, file.name || `evidence_${index + 1}.bin`);
          });
          if (skipHashing) fd.append("skip_hashing", "1");
          ev = await A.apiJson(
            `/api/cases/${encodeURIComponent(caseId)}/images/${encodeURIComponent(imageId)}/evidence`,
            { method: "POST", body: fd, timeout: intakeTimeoutMs, signal: token.signal },
          );
        } else {
          const jsonPayload = { path: data.path, skip_hashing: skipHashing };
          if (data.evidenceDescriptor) jsonPayload.evidence_descriptor = data.evidenceDescriptor;
          ev = await A.apiJson(
            `/api/cases/${encodeURIComponent(caseId)}/images/${encodeURIComponent(imageId)}/evidence`,
            { method: "POST", json: jsonPayload, timeout: intakeTimeoutMs, signal: token.signal },
          );
        }
        if (!A.isEvidenceOperationCurrent(token)) return;

        /* Show metadata on this card. */
        renderImageMetadataCard(card, ev.metadata || {}, ev.hashes || {}, ev.os_type || "");
        setImageStatusMsg(statusMsg, "Evidence loaded.", "success");

        /* Track this image. */
        const imageEntry = {
          image_id: imageId,
          label: data.label || imgResp.label || `Image ${i + 1}`,
          metadata: ev.metadata || {},
          hashes: ev.hashes || {},
          os_type: ev.os_type || "",
          available_artifacts: Array.isArray(ev.available_artifacts) ? ev.available_artifacts : [],
        };
        st.images.push(imageEntry);

        /* Merge parseable artifacts. */
        if (Array.isArray(ev.available_artifacts)) {
          ev.available_artifacts.forEach((a) => {
            if (!a || !a.key) return;
            const existing = allArtifacts.find((x) => x.key === a.key);
            if (!existing) allArtifacts.push(Object.assign({}, a));
            else if (a.available && !existing.available) {
              existing.available = true;
              if (a.name) existing.name = a.name;
            }
          });
        }

        if (i === 0) firstOsType = ev.os_type || "";
      }

      intakeProgress.complete();
      if (intakeStatusEl) intakeStatusEl.hidden = true;

      /* Build a combined evidence response for applyEvidence. */
      const combinedEv = {
        available_artifacts: allArtifacts,
        os_type: firstOsType,
        metadata: st.images.length === 1 ? st.images[0].metadata : buildCombinedMetadata(st.images),
        hashes: st.images.length === 1 ? st.images[0].hashes : {},
      };
      A.applyEvidence(combinedEv);

      /* Build per-image summaries in Step 2. */
      renderImageSummaries(st.images);

      const imageCountLabel = totalImages === 1 ? "1 image" : `${totalImages} images`;
      A.setMsg(el.evidenceMsg, `Evidence intake complete (${imageCountLabel}).`, "success");
      A.showStep(2);
    } catch (e) {
      if (!A.isEvidenceOperationCurrent(token) || e.name === "AbortError") return;
      if (!clearFailedEvidenceIntakeState(intakeCaseId)) return;
      A.setMsg(el.evidenceMsg, `Evidence intake failed: ${e.message}`, "error");
      if (intakeStatusEl) intakeStatusEl.hidden = true;
    } finally {
      intakeProgress.stop();
      if (A.finishEvidenceOperation(token)) {
        setEvidenceBusy(false);
        A.updateNav();
      }
    }
  }

  /**
   * Clear wizard state and Step-2+ UI that belongs to a previous case.
   *
   * Shared by the post-case-creation reset in submitEvidence() and by
   * clearFailedEvidenceIntakeState() so the two cleanup paths cannot drift.
   * Resets parse/analysis/chat state, artifact selections, the evidence
   * summary cards, and the multi-image artifact tabs. Deliberately leaves
   * the evidence intake forms, intake progress bar, in-progress intake
   * status message, active case ID, and current wizard step untouched —
   * callers decide those.
   */
  function clearStaleCaseUiState() {
    st.artifacts = [];
    st.artifactNames = {};
    st.selected = [];
    st.selectedAi = [];
    st.detectedOs = "";

    A.resetParseState();
    A.resetAnalysisState();
    A.resetChatState();

    if (el.summaryCard) el.summaryCard.hidden = true;
    if (el.sumHost) el.sumHost.textContent = "-";
    if (el.sumOs) el.sumOs.textContent = "-";
    if (el.sumDomain) el.sumDomain.textContent = "-";
    if (el.sumIps) el.sumIps.textContent = "-";
    if (el.sumSha) el.sumSha.textContent = "-";

    const unsupportedBox = q("unsupported-evidence-error");
    if (unsupportedBox) unsupportedBox.hidden = true;
    const unsupportedHint = q("unsupported-evidence-hint");
    if (unsupportedHint) unsupportedHint.hidden = true;
    const artifactContent = q("artifact-selection-content");
    if (artifactContent) artifactContent.hidden = false;

    const tabContainer = q("artifact-image-tabs");
    if (tabContainer) {
      tabContainer.hidden = true;
      const tabBar = tabContainer.querySelector(".artifact-tab-bar");
      if (tabBar) tabBar.innerHTML = "";
    }
    const panelsContainer = q("artifact-image-panels");
    if (panelsContainer) panelsContainer.innerHTML = "";
    if (el.artifactsForm) el.artifactsForm.hidden = false;
    if (el.applyRecommendedAllBtn) el.applyRecommendedAllBtn.hidden = true;
    if (el.applySelectionAllBtn) el.applySelectionAllBtn.hidden = true;

    const summariesContainer = q("evidence-summaries-container");
    if (summariesContainer) summariesContainer.hidden = true;
    const summariesList = q("evidence-summaries-list");
    if (summariesList) summariesList.innerHTML = "";

    if (typeof A.clearDynamicArtifacts === "function") A.clearDynamicArtifacts();
    A.artifactBoxes().forEach((cb) => {
      cb.checked = false;
      cb.disabled = true;
      const select = A.ensureArtifactModeControl(cb, A.MODE_PARSE_AND_AI);
      if (select) select.value = A.MODE_PARSE_AND_AI;
      A.syncArtifactModeControl(cb, select);
      const li = cb.closest("li");
      if (li) {
        li.classList.add("artifact-unavailable");
        li.dataset.available = "false";
        li.title = "Load evidence to detect parseable artifacts";
      }
    });
    if (el.parseBtn) el.parseBtn.disabled = true;
    A.updateNav();
  }

  /**
   * Clear state that is only valid after applyEvidence() has consumed a full
   * intake response. This preserves the user's evidence forms and failure
   * message while making the incomplete case unusable from later steps.
   *
   * @param {string} failedCaseId - Case ID allocated for the failed intake.
   * @returns {boolean} True when this failed intake still owns the UI.
   */
  function clearFailedEvidenceIntakeState(failedCaseId) {
    const currentCaseId = A.activeCaseId();
    if (failedCaseId && currentCaseId && currentCaseId !== failedCaseId) return false;
    if (!failedCaseId || currentCaseId === failedCaseId) A.setCaseId("");
    st.caseName = "";
    st.images = [];

    clearStaleCaseUiState();

    A.getImageForms().forEach((card) => {
      const metaCard = card.querySelector(".image-metadata-card");
      if (metaCard) metaCard.hidden = true;
      const statusMsg = card.querySelector(".image-status-msg");
      if (statusMsg) {
        statusMsg.hidden = true;
        statusMsg.textContent = "";
        delete statusMsg.dataset.status;
      }
    });

    if (typeof A.showStep === "function") A.showStep(1);
    else A.updateNav();
    return true;
  }

  // ── Multi-image metadata helpers ────────────────────────────────────────

  /**
   * Build combined metadata from multiple images for display.
   *
   * @param {Object[]} images - Array of image entry objects.
   * @returns {Object} Combined metadata.
   */
  function buildCombinedMetadata(images) {
    if (!images.length) return { hostname: "-", os_version: "-", domain: "-" };
    if (images.length === 1) return images[0].metadata;
    const hostnames = images.map((img) => String((img.metadata || {}).hostname || "Unknown")).join(", ");
    /* Collect unique OS versions and domains across all images so
       multi-image cases do not silently drop info from images 2+. */
    const osVersions = Array.from(new Set(
      images.map((img) => String((img.metadata || {}).os_version || "")).filter(Boolean)
    ));
    const domains = Array.from(new Set(
      images.map((img) => String((img.metadata || {}).domain || "")).filter(Boolean)
    ));
    return {
      hostname: hostnames,
      os_version: osVersions.length ? osVersions.join(", ") : "-",
      domain: domains.length ? domains.join(", ") : "-",
    };
  }

  /**
   * Render per-image metadata on a card in Step 1.
   *
   * @param {HTMLElement} card - The .image-form-card element.
   * @param {Object} metadata - Evidence metadata.
   * @param {Object} hashes - Hash information.
   * @param {string} osType - Detected OS type.
   */
  function renderImageMetadataCard(card, metadata, hashes, osType) {
    const metaCard = card.querySelector(".image-metadata-card");
    if (!metaCard) return;
    const setText = (cls, val) => {
      const el = metaCard.querySelector(`.${cls}`);
      if (el) el.textContent = val;
    };
    setText("image-sum-hostname", String(metadata.hostname || "-"));
    setText("image-sum-os", A.formatOsVersion(metadata.os_version, osType));
    setText("image-sum-domain", String(metadata.domain || "-"));
    setText("image-sum-ips", String(metadata.ips || "-"));
    setText("image-sum-sha256", String(hashes.sha256 || "-"));
    metaCard.hidden = false;
  }

  /**
   * Render per-image summaries in the Step 2 evidence summaries container.
   *
   * @param {Object[]} images - Array of image entry objects.
   */
  function renderImageSummaries(images) {
    const container = q("evidence-summaries-container");
    const list = q("evidence-summaries-list");
    if (!container || !list) return;

    /* For single image, use the legacy summary card instead. */
    if (images.length <= 1) {
      container.hidden = true;
      list.innerHTML = "";
      return;
    }

    /* Hide the legacy single summary card. */
    if (el.summaryCard) el.summaryCard.hidden = true;

    list.innerHTML = "";
    images.forEach((img) => {
      const m = img.metadata || {};
      const h = img.hashes || {};
      const osVersion = A.formatOsVersion(m.os_version, img.os_type);
      const article = A.createEvidenceSummaryCard({
        title: img.label || "Image",
        hostname: m.hostname || "-",
        os: osVersion,
        domain: m.domain || "-",
        ips: m.ips || "-",
        sha256: h.sha256 || "-",
      });
      list.appendChild(article);
    });
    container.hidden = false;
  }

  // ── Intake progress & busy state ────────────────────────────────────────

  /**
   * Create a progress tracker for the evidence intake operation.
   *
   * Returns an object with setPhase/complete/stop methods that drive the
   * progress bar and elapsed-time message during upload.
   *
   * @returns {{setPhase: function, complete: function, stop: function}}
   */
  function createIntakeProgressTracker() {
    if (!el.evidenceProg) return { setPhase: () => {}, complete: () => {}, stop: () => {} };
    let cap = 30;
    let barTicker = 0;
    let msgTicker = 0;
    const startedAt = Date.now();

    const updateMessage = () => {
      A.setMsg(el.evidenceMsg, `Intake in progress... (${A.fmtElapsed(startedAt)})`, "info");
    };

    /**
     * Tick the progress bar forward.
     *
     * Uses a time-based curve so the bar advances steadily over a long
     * period instead of racing to the cap and stalling.  The position is
     * interpolated as:  cap * (1 - 1/(1 + t/T))  where t is elapsed
     * seconds and T is a half-life constant (seconds to reach ~50% of cap).
     */
    const tickProgress = () => {
      const current = A.num(el.evidenceProg.value, 0);
      if (current >= cap) return;
      const elapsed = (Date.now() - startedAt) / 1000;
      /* Half-life: 30s means bar reaches ~50% of cap after 30s,
         ~75% after 90s, ~90% after 270s — stays well below cap. */
      const halfLife = 30;
      const target = cap * (1 - 1 / (1 + elapsed / halfLife));
      /* Only move forward, never backward, and cap at the limit. */
      el.evidenceProg.value = Math.min(cap, Math.max(current, target));
    };

    el.evidenceProg.value = 2;
    updateMessage();
    barTicker = window.setInterval(tickProgress, 500);
    msgTicker = window.setInterval(updateMessage, 1000);

    return {
      setPhase: (phase) => {
        if (phase === "case-created") {
          cap = 90;
          if (el.evidenceProg.value < 15) el.evidenceProg.value = 15;
        }
      },
      complete: () => { cap = 100; el.evidenceProg.value = 100; },
      stop: () => {
        if (barTicker) { window.clearInterval(barTicker); barTicker = 0; }
        if (msgTicker) { window.clearInterval(msgTicker); msgTicker = 0; }
      },
    };
  }

  /**
   * Toggle evidence intake controls and optional progress bar visibility.
   *
   * @param {boolean} on - Whether evidence intake/discovery is busy.
   * @param {boolean} [showProgress=true] - Whether to show the progress bar.
   */
  function setEvidenceBusy(on, showProgress = true) {
    if (el.submitEvidence) el.submitEvidence.disabled = on;
    if (el.scanDirectoryBtn) el.scanDirectoryBtn.disabled = on;
    if (el.scanDirectoryPath) el.scanDirectoryPath.disabled = on;
    if (el.scanDirectoryBtn) {
      el.scanDirectoryBtn.setAttribute("aria-busy", on ? "true" : "false");
      el.scanDirectoryBtn.textContent = on ? "Scanning..." : "Scan Directory";
    }
    if (el.addImageBtn) el.addImageBtn.disabled = on;
    if (el.evidenceProgWrap) el.evidenceProgWrap.hidden = !(on && showProgress);
  }

  // ── Multi-image artifact tabs ──────────────────────────────────────────

  /**
   * Build per-image artifact tabs when multiple images are present.
   *
   * Clones the main artifact form fieldsets into per-image panels, each with
   * its own checkboxes filtered to that image's parseable artifacts.  The
   * main form is hidden and the tab interface is shown instead.
   */
  /** AbortController used to remove prior change listeners from the panels container. */
  let _panelsChangeAC = null;

  function buildMultiImageArtifactTabs() {
    const tabContainer = q("artifact-image-tabs");
    const panelsContainer = q("artifact-image-panels");
    if (!tabContainer || !panelsContainer) return;

    /* Abort the previous change listener so we don't accumulate handlers. */
    if (_panelsChangeAC) _panelsChangeAC.abort();
    _panelsChangeAC = new AbortController();

    const hadMultiPanels = !!panelsContainer.querySelector(".artifact-image-panel");

    /* Clean up any prior tabs. */
    const tabBar = tabContainer.querySelector(".artifact-tab-bar");
    if (tabBar) tabBar.innerHTML = "";
    panelsContainer.innerHTML = "";

    if (st.images.length <= 1) {
      tabContainer.hidden = true;
      panelsContainer.innerHTML = "";
      /* Show the main artifact form for single-image. */
      if (el.artifactsForm) el.artifactsForm.hidden = false;
      if (hadMultiPanels) A.applyArtifactSelectionMap([], "single");
      /* Hide multi-image-only buttons. */
      if (el.applyRecommendedAllBtn) el.applyRecommendedAllBtn.hidden = true;
      if (el.applySelectionAllBtn) el.applySelectionAllBtn.hidden = true;
      return;
    }

    /* Hide the main artifact form — each tab has its own copy. */
    if (el.artifactsForm) el.artifactsForm.hidden = true;
    tabContainer.hidden = false;
    /* Show multi-image-only buttons. */
    if (el.applyRecommendedAllBtn) el.applyRecommendedAllBtn.hidden = false;
    if (el.applySelectionAllBtn) el.applySelectionAllBtn.hidden = false;

    st.images.forEach((img, idx) => {
      const imgId = img.image_id;
      const label = img.label || `Image ${idx + 1}`;
      const availSet = new Set(
        (img.available_artifacts || [])
          .filter((a) => a && a.available)
          .map((a) => String(a.key)),
      );
      const availMap = new Map();
      (img.available_artifacts || []).forEach((a) => {
        if (a && a.key) availMap.set(String(a.key), a);
      });

      /* Create tab button. */
      const tabBtn = document.createElement("button");
      tabBtn.type = "button";
      tabBtn.role = "tab";
      tabBtn.textContent = label;
      tabBtn.dataset.imageId = imgId;
      tabBtn.dataset.tabIndex = String(idx);
      if (idx === 0) tabBtn.classList.add("is-active");
      tabBtn.addEventListener("click", () => switchArtifactTab(imgId));
      if (tabBar) tabBar.appendChild(tabBtn);

      /* Create panel. */
      const panel = document.createElement("div");
      panel.className = "artifact-image-panel";
      panel.dataset.imageId = imgId;
      panel.role = "tabpanel";
      if (idx === 0) panel.classList.add("is-active");

      /* Clone artifact fieldsets from the main form into this panel.
         Use this image's own os_type to decide which OS-specific fieldsets
         to include, instead of relying on the main form's hidden state
         (which only reflects the first image's OS). */
      const imgIsLinux = String(img.os_type || "").trim().toLowerCase() === "linux";
      if (el.artifactsForm) {
        const fieldsetMatchesImage = (fs) => {
          const fsOs = String(fs.dataset.os || "").trim().toLowerCase();
          /* Include fieldsets that match this image's OS:
             - Linux fieldsets (data-os="linux") only for Linux images
             - Windows fieldsets (no data-os) only for non-Linux images */
          if (fsOs === "linux" && !imgIsLinux) return false;
          if (!fsOs && imgIsLinux) return false;
          return true;
        };

        const prepareFieldsetClone = (sourceFieldset, clone) => {
          /* Ensure the cloned fieldset is visible (the main form may
             have hidden it based on the first image's OS). */
          clone.hidden = false;
          /* Update checkboxes for this image's availability. */
          clone.querySelectorAll("input[type='checkbox'][data-artifact-key]").forEach((cb) => {
            const key = String(cb.dataset.artifactKey || "");
            /* Prefix with image ID to avoid name collisions. */
            cb.name = `${imgId}__${key}`;
            cb.dataset.imageId = imgId;
            const available = availSet.has(key);
            cb.disabled = !available;
            cb.checked = false;
            const li = cb.closest("li");
            if (li) {
              li.dataset.available = String(available);
              li.classList.toggle("artifact-unavailable", !available);
              li.title = available ? "" : "Not parseable in this image";
            }
            const descriptor = availMap.get(key);
            if (descriptor && descriptor.name && li && sourceFieldset.dataset.advancedCategory === "true") {
              const labelEl = cb.closest("label");
              const txt = labelEl ? Array.from(labelEl.childNodes).find((n) => n.nodeType === Node.TEXT_NODE) : null;
              if (txt) txt.textContent = ` ${descriptor.name}`;
            }
            /* Remove any existing mode select clones — we'll rebuild. */
            const existingSelect = li ? li.querySelector("select.artifact-mode-select") : null;
            if (existingSelect) existingSelect.remove();
          });
        };

        const appendFieldsetClone = (sourceFieldset, targetRoot) => {
          if (!fieldsetMatchesImage(sourceFieldset)) return false;
          const clone = sourceFieldset.cloneNode(true);
          prepareFieldsetClone(sourceFieldset, clone);
          targetRoot.appendChild(clone);
          return true;
        };

        Array.from(el.artifactsForm.children).forEach((node) => {
          if (node.matches && node.matches("fieldset.artifact-category")) {
            appendFieldsetClone(node, panel);
            return;
          }
          if (!(node.matches && node.matches("details.artifact-advanced-section"))) return;

          const sectionClone = document.createElement("details");
          sectionClone.className = node.className;
          sectionClone.open = node.open;

          const summary = node.querySelector("summary");
          if (summary) sectionClone.appendChild(summary.cloneNode(true));

          const sourceGrid = node.querySelector(".artifact-advanced-grid");
          const gridClone = document.createElement("div");
          gridClone.className = sourceGrid ? sourceGrid.className : "artifact-category-grid artifact-advanced-grid";

          let hasFieldsets = false;
          node.querySelectorAll("fieldset.artifact-category").forEach((sourceFieldset) => {
            hasFieldsets = appendFieldsetClone(sourceFieldset, gridClone) || hasFieldsets;
          });
          if (!hasFieldsets) return;

          sectionClone.appendChild(gridClone);
          panel.appendChild(sectionClone);
        });
      }

      if (typeof A.syncAdvancedArtifactSections === "function") A.syncAdvancedArtifactSections(panel);

      /* Ensure mode controls are created for all checkboxes in this panel. */
      panel.querySelectorAll("input[type='checkbox'][data-artifact-key]").forEach((cb) => {
        A.ensureArtifactModeControl(cb, A.MODE_PARSE_AND_AI);
      });

      panelsContainer.appendChild(panel);
    });

    /* Wire change events on the panels container (with abort signal to prevent accumulation). */
    panelsContainer.addEventListener("change", (e) => {
      const t = e.target;
      if (t instanceof HTMLInputElement && t.type === "checkbox" && t.dataset.artifactKey) {
        A.syncArtifactModeControl(t);
        A.markParsedSelectionStale();
        return A.updateParseButton();
      }
      if (t instanceof HTMLSelectElement && t.classList.contains("artifact-mode-select") && t.dataset.artifactKey) {
        t.value = A.artifactModeValue(t.value);
        A.markParsedSelectionStale();
        return A.updateParseButton();
      }
    }, { signal: _panelsChangeAC.signal });
  }

  /**
   * Switch the active artifact tab to the given image.
   *
   * @param {string} imageId - The image_id to activate.
   */
  function switchArtifactTab(imageId) {
    const tabContainer = q("artifact-image-tabs");
    const panelsContainer = q("artifact-image-panels");
    if (!tabContainer || !panelsContainer) return;

    tabContainer.querySelectorAll(".artifact-tab-bar button").forEach((btn) => {
      btn.classList.toggle("is-active", btn.dataset.imageId === imageId);
    });
    panelsContainer.querySelectorAll(".artifact-image-panel").forEach((panel) => {
      panel.classList.toggle("is-active", panel.dataset.imageId === imageId);
    });
  }

  /**
   * Return the image_id of the currently active artifact tab, or null.
   *
   * @returns {string|null}
   */
  function activeArtifactTabImageId() {
    const tabContainer = q("artifact-image-tabs");
    if (!tabContainer || tabContainer.hidden) return null;
    const active = tabContainer.querySelector(".artifact-tab-bar button.is-active");
    return active ? active.dataset.imageId || null : null;
  }

  /**
   * Collect selected artifact options for a specific image from its tab panel.
   *
   * @param {string} imageId - Image ID.
   * @returns {{artifact_key: string, mode: string}[]}
   */
  function selectedArtifactOptionsForImage(imageId) {
    if (!imageId) return [];
    const panelsContainer = q("artifact-image-panels");
    if (!panelsContainer) return [];
    const panel = panelsContainer.querySelector(`.artifact-image-panel[data-image-id="${A.cssEscape(imageId)}"]`);
    if (!panel) return [];
    return A.selectedArtifactOptionsIn(panel);
  }

  /** Return the display label for an artifact in a specific image panel. */
  function artifactNameForImage(imageId, artifactKey) {
    const key = String(artifactKey || "").trim();
    if (!imageId || !key) return "";
    const panelsContainer = q("artifact-image-panels");
    const panel = panelsContainer
      ? panelsContainer.querySelector(`.artifact-image-panel[data-image-id="${A.cssEscape(imageId)}"]`)
      : null;
    const cb = panel ? panel.querySelector(`input[type='checkbox'][data-artifact-key="${A.cssEscape(key)}"]`) : null;
    const panelLabel = cb ? A.artifactLabelText(cb) : "";
    if (panelLabel) return panelLabel;
    const image = st.images.find((img) => String(img.image_id || "") === String(imageId));
    const descriptor = image && Array.isArray(image.available_artifacts)
      ? image.available_artifacts.find((a) => a && String(a.key || "") === key)
      : null;
    return descriptor && descriptor.name ? String(descriptor.name) : A.artifactName(key);
  }

  /**
   * Collect per-image artifact selections for all images.
   *
   * @returns {{image_id: string, label: string, artifact_options: Object[]}[]}
   */
  function allImageArtifactSelections() {
    if (st.images.length <= 1) return [];
    return st.images.map((img) => ({
      image_id: img.image_id,
      label: img.label || img.image_id,
      artifact_options: selectedArtifactOptionsForImage(img.image_id),
    }));
  }

  /**
   * Check whether multi-image mode is active (more than one image loaded).
   *
   * @returns {boolean}
   */
  function isMultiImage() {
    return st.images.length > 1;
  }

  /**
   * Apply a preset to the active tab panel in multi-image mode,
   * or to the main form in single-image mode.
   *
   * @param {string} mode - "recommended" or "clear".
   */
  function applyPresetMultiAware(mode) {
    if (!isMultiImage()) return A.applyPreset(mode);
    A.applyArtifactPreset(mode, "active");
  }

  /**
   * Apply the recommended preset to every image tab panel.
   *
   * Iterates all per-image panels and applies the loaded recommended profile
   * when available. The fallback keeps only the frontend preset behavior:
   * MFT and USN Journal are excluded, while EVTX is selected as parse-only.
   */
  function applyRecommendedToAllImages() {
    if (!isMultiImage()) return;
    A.applyArtifactPreset("recommended", "all");
  }

  /**
   * Apply the current active tab's artifact selection to all other image tabs.
   *
   * Reads the checked state and mode of every artifact checkbox in the
   * currently active tab panel, then mirrors that state across all other
   * image panels.  Only artifacts that exist in the source panel are
   * synced — OS-specific artifacts that only appear in the target panel
   * (e.g. Linux artifacts on a Windows source) are left untouched so
   * that mixed-OS selections are not accidentally wiped out.
   *
   * Only visible in multi-image mode.
   */
  function applyCurrentSelectionToAllImages() {
    if (!isMultiImage()) return;
    const activeId = activeArtifactTabImageId();
    if (!activeId) return;
    const panelsContainer = q("artifact-image-panels");
    if (!panelsContainer) return;

    /* Build a list of source states keyed by display identity, not key alone. */
    const activePanel = panelsContainer.querySelector(
      `.artifact-image-panel[data-image-id="${A.cssEscape(activeId)}"]`,
    );
    if (!activePanel) return;
    const sourceStates = A.artifactBoxesIn(activePanel).map((cb) => {
      const li = cb.closest("li");
      const select = li ? li.querySelector("select.artifact-mode-select") : null;
      return {
        identity: A.artifactDisplayIdentity(cb),
        checked: cb.checked,
        mode: A.artifactModeValue(select ? select.value : A.MODE_PARSE_AND_AI),
      };
    });
    const identityMatches = (sourceIdentity, targetIdentity) => sourceIdentity.key === targetIdentity.key
      && sourceIdentity.os === targetIdentity.os
      && sourceIdentity.category === targetIdentity.category
      && sourceIdentity.label === targetIdentity.label;

    /* Apply to every other panel.  Only touch artifacts that exist in the
       source panel — OS-specific artifacts unique to the target are left
       as-is so mixed-OS configurations are preserved. */
    panelsContainer.querySelectorAll(".artifact-image-panel").forEach((panel) => {
      if (panel.dataset.imageId === activeId) return;
      A.artifactBoxesIn(panel).forEach((cb) => {
        const key = String(cb.dataset.artifactKey || "").trim();
        if (!key) return;
        const entry = sourceStates.find((source) => identityMatches(source.identity, A.artifactDisplayIdentity(cb)));
        /* Artifact not in source panel with matching display identity - leave target state untouched. */
        if (!entry) return;
        const select = A.ensureArtifactModeControl(cb, A.MODE_PARSE_AND_AI);
        if (cb.disabled) {
          cb.checked = false;
          if (select) select.value = A.MODE_PARSE_AND_AI;
          return A.syncArtifactModeControl(cb, select);
        }
        cb.checked = entry.checked;
        if (select) select.value = entry.mode;
        A.syncArtifactModeControl(cb, select);
      });
    });
    A.markParsedSelectionStale();
    A.updateParseButton();
  }

  // ── Public API ─────────────────────────────────────────────────────────
  A.submitEvidence = submitEvidence;
  A.scanEvidenceDirectory = scanEvidenceDirectory;
  A.clearDiscoveryDescriptor = clearDiscoveryDescriptor;
  A.addImageForm = addImageForm;
  A.removeImageForm = removeImageForm;
  A.renderImageSummaries = renderImageSummaries;
  A.buildMultiImageArtifactTabs = buildMultiImageArtifactTabs;
  A.switchArtifactTab = switchArtifactTab;
  A.activeArtifactTabImageId = activeArtifactTabImageId;
  A.selectedArtifactOptionsForImage = selectedArtifactOptionsForImage;
  A.artifactNameForImage = artifactNameForImage;
  A.allImageArtifactSelections = allImageArtifactSelections;
  A.isMultiImage = isMultiImage;
  A.applyPresetMultiAware = applyPresetMultiAware;
  A.applyRecommendedToAllImages = applyRecommendedToAllImages;
  A.applyCurrentSelectionToAllImages = applyCurrentSelectionToAllImages;
})();
