/**
 * Evidence intake and artifact selection / profile management for AIFT.
 *
 * Handles file upload, dropzone, evidence submission, artifact checkboxes,
 * artifact mode controls (parse-only vs parse+AI), and artifact profiles.
 *
 * Depends on: AIFT (utils.js)
 */
"use strict";

(() => {
  const A = window.AIFT;
  const { st, el, q } = A;
  const droppedFilesByCard = new WeakMap();

  // ── Multi-image state ──────────────────────────────────────────────────────

  /** Array of image intake entries. Each entry: {index, image_id, label, metadata, available_artifacts} */
  st.images = [];

  // ── Evidence intake ────────────────────────────────────────────────────────

  /** Wire up the evidence form: mode toggle, file input, dropzone, and submit handler. */
  function setupEvidence() {
    if (!el.evidenceForm) return;
    /* Legacy single-image elements are no longer used directly.
       Mode toggling and dropzone init are handled per image card. */
    initImageForm(getImageForms()[0]);
    el.evidenceForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      await A.submitEvidence();
    });
    const addBtn = el.addImageBtn || q("add-image-btn");
    if (addBtn) addBtn.addEventListener("click", () => A.addImageForm());
    const scanBtn = el.scanDirectoryBtn || q("scan-directory-btn");
    if (scanBtn) scanBtn.addEventListener("click", () => A.scanEvidenceDirectory());
    if (el.scanDirectoryPath) {
      el.scanDirectoryPath.addEventListener("keydown", async (e) => {
        if (e.key !== "Enter") return;
        e.preventDefault();
        await A.scanEvidenceDirectory();
      });
    }
  }

  /**
   * Batch-sync upload/path panels for every image form card.
   *
   * Called from app.js when restoring wizard state.  Per-image sync is
   * handled by syncImageFormMode(); this is the convenience wrapper that
   * iterates all cards.
   */
  function syncMode() {
    getImageForms().forEach(syncImageFormMode);
  }

  /**
   * Sync upload/path panels for a single image form card.
   *
   * @param {HTMLElement} card - The .image-form-card element.
   */
  function syncImageFormMode(card) {
    if (!card) return;
    const pathRadio = card.querySelector(".image-mode-path");
    const pathMode = !!(pathRadio && pathRadio.checked);
    const uploadPanel = card.querySelector(".image-upload-panel");
    const pathPanel = card.querySelector(".image-path-panel");
    if (uploadPanel) uploadPanel.hidden = pathMode;
    if (pathPanel) pathPanel.hidden = !pathMode;
  }

  /**
   * Initialise event listeners for a single image form card.
   *
   * @param {HTMLElement|null} card - The .image-form-card element.
   */
  function initImageForm(card) {
    if (!card) return;
    A.applyEvidenceFormatMetadata(card);
    const modeUpload = card.querySelector(".image-mode-upload");
    const modePath = card.querySelector(".image-mode-path");
    if (modeUpload) modeUpload.addEventListener("change", () => syncImageFormMode(card));
    if (modePath) modePath.addEventListener("change", () => syncImageFormMode(card));

    const fileInput = card.querySelector(".image-file-input");
    const dropzoneHelp = card.querySelector(".image-dropzone-help");
    if (fileInput) {
      fileInput.addEventListener("change", () => {
        if (typeof A.clearDiscoveryDescriptor === "function") A.clearDiscoveryDescriptor(card);
        clearDroppedFilesForCard(card);
        updateDropzoneHelp(fileInput, dropzoneHelp);
      });
    }
    const pathInput = card.querySelector(".image-path-input");
    if (pathInput) {
      pathInput.addEventListener("input", () => {
        if (typeof A.clearDiscoveryDescriptor === "function") A.clearDiscoveryDescriptor(card);
      });
    }
    initImageDropzone(card);
    syncImageFormMode(card);

    const removeBtn = card.querySelector(".image-remove-btn");
    if (removeBtn) {
      removeBtn.addEventListener("click", () => A.removeImageForm(card));
    }
  }

  /**
   * Initialise drag-and-drop for an image form card's dropzone.
   *
   * @param {HTMLElement} card - The .image-form-card element.
   */
  function initImageDropzone(card) {
    const drop = card.querySelector(".image-dropzone");
    if (!drop) return;
    const prevent = (e) => { e.preventDefault(); e.stopPropagation(); };
    ["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => {
      prevent(e);
      drop.classList.add("is-dragover");
    }));
    ["dragleave", "dragend", "drop"].forEach((t) => drop.addEventListener(t, (e) => {
      prevent(e);
      drop.classList.remove("is-dragover");
    }));
    drop.addEventListener("drop", (e) => {
      const files = e.dataTransfer && e.dataTransfer.files ? e.dataTransfer.files : null;
      if (!files || !files.length) return;
      const dropped = Array.from(files);
      const fileInput = card.querySelector(".image-file-input");
      if (fileInput) {
        try {
          const dt = new DataTransfer();
          dropped.forEach((file) => dt.items.add(file));
          fileInput.files = dt.files;
        } catch (_err) { /* fallback */ }
      }
      setDroppedFilesForCard(card, dropped);
      const helpEl = card.querySelector(".image-dropzone-help");
      updateDropzoneHelp(fileInput, helpEl);
    });
  }

  /** Store dropped files for a card without attaching expando state to DOM. */
  function setDroppedFilesForCard(card, files) {
    if (!card) return [];
    const normalized = Array.isArray(files) ? files.filter(Boolean) : [];
    if (normalized.length) droppedFilesByCard.set(card, normalized);
    else droppedFilesByCard.delete(card);
    return normalized;
  }

  /** Return dropped files currently cached for a card. */
  function getDroppedFilesForCard(card) {
    return card ? (droppedFilesByCard.get(card) || []) : [];
  }

  /** Clear dropped-file cache for a card. */
  function clearDroppedFilesForCard(card) {
    if (card) droppedFilesByCard.delete(card);
  }

  /** Return the upload files selected for a card, preferring drag/drop cache. */
  function imageUploadFilesForCard(card) {
    if (!card) return [];
    const cachedFiles = getDroppedFilesForCard(card);
    if (cachedFiles.length) return cachedFiles;
    const fileInput = card.querySelector(".image-file-input");
    return fileInput && fileInput.files ? Array.from(fileInput.files) : [];
  }

  /**
   * Update a dropzone help text element based on selected files.
   *
   * @param {HTMLInputElement|null} fileInput - The file input element.
   * @param {HTMLElement|null} helpEl - The dropzone help text element.
   */
  function updateDropzoneHelp(fileInput, helpEl) {
    if (!helpEl) return;
    const card = helpEl.closest(".image-form-card");
    const cachedFiles = getDroppedFilesForCard(card);
    const inputFiles = fileInput && fileInput.files ? Array.from(fileInput.files) : [];
    const files = cachedFiles.length ? cachedFiles : inputFiles;
    if (!files.length) {
      helpEl.textContent = A.evidenceFormatMetadata().help;
      return;
    }
    if (files.length === 1) {
      const file = files[0];
      helpEl.textContent = `${file.name}${Number.isFinite(file.size) ? ` (${A.fmtBytes(file.size)})` : ""}`;
      return;
    }
    const totalSize = files.reduce((sum, file) => sum + (Number.isFinite(file.size) ? file.size : 0), 0);
    helpEl.textContent = `${files.length} files selected (${A.fmtBytes(totalSize)})`;
  }

  /** Strip curly/smart quotes and whitespace from a user-supplied evidence path. */
  function sanitizeEvidencePath(raw) {
    return String(raw || "").replace(/["\u201c\u201d]/g, "").trim();
  }

  // ── Image form management ─────────────────────────────────────────────────

  /** Return all image form card elements from the DOM. */
  function getImageForms() {
    const container = q("image-forms-container");
    return container ? Array.from(container.querySelectorAll(".image-form-card")) : [];
  }

  /**
   * Apply evidence intake response data to the UI.
   *
   * Populates artifact checkboxes, renders the evidence summary card, and
   * handles the unsupported-evidence error state.
   *
   * @param {Object} data - Backend response from the evidence endpoint.
   */
  function applyEvidence(data) {
    st.artifacts = Array.isArray(data.available_artifacts) ? data.available_artifacts : [];
    st.artifactNames = {};
    st.artifacts.forEach((a) => {
      if (a && a.key) st.artifactNames[String(a.key)] = String(a.name || a.key);
    });

    A.resetParseState();
    A.resetAnalysisState();
    A.resetChatState();
    st.selected = [];
    st.selectedAi = [];

    const detectedOs = String(data.os_type || "").trim().toLowerCase();
    st.detectedOs = detectedOs;

    renderSummary(data.metadata || {}, data.hashes || {}, data.os_type || "");

    /* Log OS detection warning when the backend could not determine the OS.
       The unsupported-evidence error box below handles the visual feedback. */
    if (data.os_warning) {
      console.warn("[AIFT] OS detection warning:", data.os_warning);
    }

    const osVersion = String((data.metadata || {}).os_version || "").trim().toLowerCase();
    const isUnsupported = !osVersion || osVersion === "unknown" || osVersion === "-";
    const errorBox = document.getElementById("unsupported-evidence-error");
    const hintEl = document.getElementById("unsupported-evidence-hint");
    const artifactContent = document.getElementById("artifact-selection-content");

    if (isUnsupported && errorBox) {
      errorBox.hidden = false;
      if (hintEl) {
        const firstCard = getImageForms()[0];
        const firstUploadRadio = firstCard ? firstCard.querySelector(".image-mode-upload") : null;
        const wasUpload = !!(firstUploadRadio && firstUploadRadio.checked);
        hintEl.hidden = !wasUpload;
      }
      if (artifactContent) artifactContent.hidden = true;
    } else {
      if (errorBox) errorBox.hidden = true;
      if (artifactContent) artifactContent.hidden = false;
      populateArtifacts(st.artifacts);
      showOsArtifactFieldsets(detectedOs);
    }

    A.renderParsePlaceholder();
    A.renderAnalysis();
    A.renderExecSummary();
    A.renderFindings();
    A.buildMultiImageArtifactTabs();
    updateParseButton();
  }

  /**
   * Show/hide OS-specific artifact fieldsets based on the detected OS.
   *
   * Windows fieldsets are shown for Windows images: static Windows
   * fieldsets carry no data-os attribute, while dynamically created
   * Advanced fieldsets are tagged data-os="windows" explicitly.
   * Linux fieldsets (data-os="linux") are shown for Linux images.
   *
   * @param {string} osType - Detected OS type ("windows", "linux", etc.).
   */
  function showOsArtifactFieldsets(osType) {
    if (!el.artifactsForm) return;
    const isLinux = osType === "linux";
    const fieldsets = el.artifactsForm.querySelectorAll("fieldset.artifact-category");
    fieldsets.forEach((fs) => {
      const fsOs = String(fs.dataset.os || "").trim().toLowerCase();
      let hide;
      if (fsOs === "linux") {
        hide = !isLinux;
      } else if (!fsOs || fsOs === "windows") {
        /* Static Windows fieldsets have no data-os attribute; dynamic
           Advanced fieldsets carry an explicit data-os="windows". */
        hide = isLinux;
      } else {
        return;
      }
      fs.hidden = hide;
      /* Force-disable checkboxes in hidden fieldsets so they cannot be
         selected by profiles or presets (prevents duplicate keys like
         'services' from being selected in the wrong OS context). */
      fs.querySelectorAll("input[type='checkbox']").forEach((cb) => {
        if (hide) {
          cb.disabled = true;
          cb.checked = false;
        }
      });
    });
    syncAdvancedArtifactSections(el.artifactsForm);
  }

  /**
   * Format an OS version string, prepending the OS type label when it is
   * not already contained in the version text.
   *
   * @param {string} rawVersion - Raw os_version value (may be empty/"-").
   * @param {string} osType - Detected OS type label (e.g. "windows").
   * @returns {string} Formatted OS version string.
   */
  function formatOsVersion(rawVersion, osType) {
    let osVersion = String(rawVersion || "-");
    const osLabel = String(osType || "").trim().toLowerCase();
    if (osLabel && osLabel !== "unknown" && osVersion !== "-") {
      if (osVersion.toLowerCase().indexOf(osLabel) === -1) {
        const capitalized = osLabel.charAt(0).toUpperCase() + osLabel.slice(1);
        osVersion = capitalized + " \u2014 " + osVersion;
      }
    }
    return osVersion;
  }

  /**
   * Populate the evidence summary card with metadata and hashes.
   *
   * @param {Object} m - Metadata object (hostname, os_version, domain, ips).
   * @param {Object} h - Hashes object (sha256).
   * @param {string} osType - Detected OS type ("windows", "linux", etc.).
   */
  function renderSummary(m, h, osType) {
    if (el.sumHost) el.sumHost.textContent = String(m.hostname || "-");

    if (el.sumOs) el.sumOs.textContent = A.formatOsVersion(m.os_version, osType);

    if (el.sumDomain) el.sumDomain.textContent = String(m.domain || "-");
    if (el.sumIps) el.sumIps.textContent = String(m.ips || "-");
    if (el.sumSha) el.sumSha.textContent = String(h.sha256 || "-");
    if (el.summaryCard) el.summaryCard.hidden = false;
  }

  // ── Artifact checkboxes & mode controls ────────────────────────────────────

  /** Normalise a mode string to MODE_PARSE_ONLY or MODE_PARSE_AND_AI. */
  function artifactModeValue(rawMode) {
    return String(rawMode || "").trim().toLowerCase() === A.MODE_PARSE_ONLY ? A.MODE_PARSE_ONLY : A.MODE_PARSE_AND_AI;
  }

  /** Return all artifact checkbox `<input>` elements under a DOM root. */
  function artifactBoxesIn(root) {
    return root
      ? Array.from(root.querySelectorAll("input[type='checkbox'][data-artifact-key]"))
      : [];
  }

  /** Return all artifact checkbox `<input>` elements in the artifacts form. */
  function artifactBoxes() {
    return artifactBoxesIn(el.artifactsForm);
  }

  /**
   * Synchronise a mode `<select>` with its parent checkbox's state.
   *
   * Disables the select when the checkbox is unchecked or disabled.
   *
   * @param {HTMLInputElement} cb - The artifact checkbox.
   * @param {HTMLSelectElement|null} [modeSelect] - Override for the select element.
   */
  function syncArtifactModeControl(cb, modeSelect = null) {
    if (!(cb instanceof HTMLInputElement)) return;
    const li = cb.closest("li");
    const select = modeSelect || (li ? li.querySelector("select.artifact-mode-select") : null);
    if (!select) return;
    select.disabled = cb.disabled || !cb.checked;
    if (!select.disabled) select.value = artifactModeValue(select.value);
  }

  /**
   * Ensure a mode `<select>` dropdown exists for an artifact checkbox,
   * creating one if necessary.
   *
   * @param {HTMLInputElement} cb - The artifact checkbox element.
   * @param {string} [preferredMode=MODE_PARSE_AND_AI] - Initial mode value.
   * @returns {HTMLSelectElement|null} The mode select element.
   */
  function ensureArtifactModeControl(cb, preferredMode = A.MODE_PARSE_AND_AI) {
    if (!(cb instanceof HTMLInputElement)) return null;
    const key = String(cb.dataset.artifactKey || "").trim();
    if (!key) return null;
    const li = cb.closest("li");
    if (!li) return null;

    /* Search within the parent <li> to avoid collisions when duplicate
       artifact keys exist across OS-specific fieldsets (e.g. "services"). */
    let select = li.querySelector("select.artifact-mode-select");
    if (!select) {
      select = A.createArtifactModeSelect(key, artifactModeValue(preferredMode));
      const artifactName = artifactLabelText(cb) || st.artifactNames[key] || key;
      select.setAttribute("aria-label", `Analysis mode for ${artifactName}`);
      li.appendChild(select);
    }
    select.value = artifactModeValue(preferredMode);
    syncArtifactModeControl(cb, select);
    return select;
  }

  /** Ensure all artifact checkboxes have an associated mode `<select>`. */
  function ensureArtifactModeControls() {
    artifactBoxesIn(el.artifactsForm).forEach((cb) => {
      const select = ensureArtifactModeControl(cb, A.MODE_PARSE_AND_AI);
      syncArtifactModeControl(cb, select);
    });
  }

  /** Synchronise every artifact mode select under a DOM root. */
  function syncArtifactModesIn(root) {
    artifactBoxesIn(root).forEach((cb) => {
      syncArtifactModeControl(cb);
    });
  }

  /** Update the visible text of a checkbox's parent `<label>`. */
  function setLabelText(cb, text) {
    const label = cb.closest("label");
    if (!label) return;
    const txt = Array.from(label.childNodes).find((n) => n.nodeType === Node.TEXT_NODE);
    if (txt) txt.textContent = ` ${text}`;
    else label.appendChild(document.createTextNode(` ${text}`));
  }

  /** Return the visible label text associated with an artifact checkbox. */
  function artifactLabelText(cb) {
    const label = cb && cb.closest ? cb.closest("label") : null;
    if (!label) return "";
    return Array.from(label.childNodes)
      .filter((n) => n.nodeType === Node.TEXT_NODE)
      .map((n) => String(n.textContent || "").trim())
      .filter(Boolean)
      .join(" ")
      .trim();
  }

  /** Return normalized display identity for an artifact checkbox. */
  function artifactDisplayIdentity(cb) {
    const fs = cb && cb.closest ? cb.closest("fieldset.artifact-category") : null;
    const key = String((cb && cb.dataset && cb.dataset.artifactKey) || "").trim();
    const os = fs && fs.dataset.os ? String(fs.dataset.os).trim().toLowerCase() : "windows";
    const category = fs ? String(fs.dataset.category || "").trim().toLowerCase() : "";
    const label = artifactLabelText(cb) || key;
    return { key, os, category, label };
  }

  /** Return true when a key appears in multiple static OS/category contexts. */
  function artifactKeyHasMultipleStaticIdentities(key) {
    const normalizedKey = String(key || "").trim();
    if (!normalizedKey || !el.artifactsForm) return false;
    const identities = new Set();
    artifactBoxes().forEach((cb) => {
      if (String(cb.dataset.artifactKey || "").trim() !== normalizedKey) return;
      const identity = artifactDisplayIdentity(cb);
      identities.add(`${identity.os}\n${identity.category}\n${identity.label}`);
    });
    return identities.size > 1;
  }

  /** Remove the dynamically-created Advanced artifact section from the DOM. */
  function clearDynamicArtifacts() {
    const d = q("dynamic-artifact-category");
    if (d) d.remove();
  }

  /** Convert a category label to a stable DOM token. */
  function artifactCategorySlug(rawCategory) {
    return String(rawCategory || "Other")
      .trim()
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "") || "other";
  }

  /** Return the display category for a dynamic artifact descriptor. */
  function artifactDescriptorCategory(rawArtifact) {
    const category = rawArtifact && rawArtifact.category ? String(rawArtifact.category).trim() : "";
    return category || "Other";
  }

  /**
   * Return the OS tag ("windows" or "linux") for a dynamic artifact descriptor.
   *
   * Prefers the descriptor's own `os` field (set by the backend from the
   * OS-specific registry that produced it) and falls back to the detected
   * OS of the current intake when the field is missing.
   *
   * @param {Object} rawArtifact - Artifact availability descriptor.
   * @returns {string} "windows" or "linux".
   */
  function artifactDescriptorOs(rawArtifact) {
    const os = rawArtifact && rawArtifact.os ? String(rawArtifact.os).trim().toLowerCase() : "";
    if (os === "windows" || os === "linux") return os;
    return st.detectedOs === "linux" ? "linux" : "windows";
  }

  /** Create one artifact checkbox row from an availability descriptor. */
  function createArtifactListItem(rawArtifact) {
    const key = String(rawArtifact.key || "");
    const avail = !!rawArtifact.available;
    const name = String(rawArtifact.name || key);
    st.artifactNames[key] = name;

    const li = document.createElement("li");
    li.dataset.available = String(avail);
    li.classList.toggle("artifact-unavailable", !avail);
    if (!avail) li.title = "Not parseable in this image";

    const label = document.createElement("label");
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.dataset.artifactKey = key;
    cb.disabled = !avail;
    label.appendChild(cb);
    label.appendChild(document.createTextNode(` ${name}`));
    li.appendChild(label);
    return li;
  }

  /** Hide Advanced sections that do not contain visible category groups. */
  function syncAdvancedArtifactSections(root = el.artifactsForm) {
    if (!root) return;
    root.querySelectorAll("details.artifact-advanced-section").forEach((section) => {
      const visibleFieldsets = Array.from(section.querySelectorAll("fieldset.artifact-category"))
        .filter((fs) => !fs.hidden);
      section.hidden = visibleFieldsets.length === 0;
    });
  }

  /**
   * Populate the artifact selection UI from the backend's parseable-artifact list.
   *
   * Updates existing checkboxes (enabling available ones) and creates a
   * foldable Advanced section for artifacts not present in the static HTML.
   * Dynamic Advanced fieldsets are grouped per (OS, category) and tagged
   * with a data-os attribute so the OS-visibility filters treat them like
   * their static counterparts.
   *
   * @param {Object[]} list - Array of artifact descriptors with key, name,
   *     available, and optional category/os fields.
   */
  function populateArtifacts(list) {
    clearDynamicArtifacts();
    const map = new Map();
    list.forEach((a) => a && a.key && map.set(String(a.key), a));

    const known = new Set();
    artifactBoxes().forEach((cb) => {
      const key = cb.dataset.artifactKey;
      if (!key) return;
      known.add(key);
      const info = map.get(key);
      const available = !!(info && info.available);
      cb.disabled = !available;
      cb.checked = false;
      const li = cb.closest("li");
      if (li) {
        li.dataset.available = String(available);
        li.classList.toggle("artifact-unavailable", !available);
        li.title = available ? "" : "Not parseable in this image";
      }
      if (info && info.name && !artifactKeyHasMultipleStaticIdentities(key)) setLabelText(cb, String(info.name));
      const modeSelect = ensureArtifactModeControl(cb, A.MODE_PARSE_AND_AI);
      if (modeSelect) modeSelect.value = A.MODE_PARSE_AND_AI;
      syncArtifactModeControl(cb, modeSelect);
    });

    const extra = list.filter((a) => a && a.key && !known.has(String(a.key)));
    if (extra.length && el.artifactsForm && el.parseBtn) {
      const advanced = document.createElement("details");
      advanced.className = "artifact-advanced-section";
      advanced.id = "dynamic-artifact-category";

      const summary = document.createElement("summary");
      summary.textContent = "Advanced";
      advanced.appendChild(summary);

      const grid = document.createElement("div");
      grid.className = "artifact-category-grid artifact-advanced-grid";

      const grouped = new Map();
      extra.forEach((a) => {
        const categoryLabel = artifactDescriptorCategory(a);
        const categoryKey = artifactCategorySlug(categoryLabel);
        const os = artifactDescriptorOs(a);
        /* Group per (OS, category) so every dynamic fieldset can carry a
           single data-os tag for the OS-visibility filters. */
        const groupKey = `${os}::${categoryKey}`;
        if (!grouped.has(groupKey)) grouped.set(groupKey, { label: categoryLabel, categoryKey, os, artifacts: [] });
        grouped.get(groupKey).artifacts.push(a);
      });

      grouped.forEach((group) => {
        const fs = document.createElement("fieldset");
        fs.className = "artifact-category artifact-category-advanced";
        fs.dataset.category = group.categoryKey;
        fs.dataset.advancedCategory = "true";
        /* Tag the fieldset with the artifacts' OS so single-image
           visibility (showOsArtifactFieldsets) and multi-image panel
           cloning (fieldsetMatchesImage) include or exclude it like the
           static OS-specific fieldsets. */
        fs.dataset.os = group.os;
        const lg = document.createElement("legend");
        lg.textContent = group.label;
        fs.appendChild(lg);
        const ul = document.createElement("ul");
        group.artifacts.forEach((a) => ul.appendChild(createArtifactListItem(a)));
        fs.appendChild(ul);
        grid.appendChild(fs);
      });

      advanced.appendChild(grid);
      el.artifactsForm.appendChild(advanced);
    }
    ensureArtifactModeControls();
    syncAdvancedArtifactSections();
    updateParseButton();
  }

  // ── Selection helpers ──────────────────────────────────────────────────────

  /**
   * Collect the selected artifact options (key + mode) from all checked checkboxes.
   *
   * @returns {{artifact_key: string, mode: string}[]}
   */
  function selectedArtifactOptionsIn(root) {
    return artifactBoxesIn(root)
      .filter((cb) => cb.checked && !cb.disabled && cb.dataset.artifactKey)
      .map((cb) => {
        const key = String(cb.dataset.artifactKey || "");
        const li = cb.closest("li");
        const select = li ? li.querySelector("select.artifact-mode-select") : null;
        return { artifact_key: key, mode: artifactModeValue(select ? select.value : A.MODE_PARSE_AND_AI) };
      });
  }

  /**
   * Collect the selected artifact options (key + mode) from all checked checkboxes.
   *
   * @returns {{artifact_key: string, mode: string}[]}
   */
  function selectedArtifactOptions() {
    return selectedArtifactOptionsIn(el.artifactsForm);
  }

  /** Return all artifact checkbox states under a root keyed by artifact key. */
  function artifactSelectionStateByKeyIn(root) {
    const selectionMap = new Map();
    artifactBoxesIn(root).forEach((cb) => {
      const key = String(cb.dataset.artifactKey || "").trim();
      if (!key) return;
      const li = cb.closest("li");
      const select = li ? li.querySelector("select.artifact-mode-select") : null;
      selectionMap.set(key, {
        checked: cb.checked,
        mode: artifactModeValue(select ? select.value : A.MODE_PARSE_AND_AI),
      });
    });
    return selectionMap;
  }

  /** Convert artifact options or state entries into a key -> state map. */
  function normalizeArtifactSelectionMap(source) {
    const selectionMap = new Map();
    if (Array.isArray(source)) {
      source.forEach((option) => {
        if (!A.isObj(option)) return;
        const key = String(option.artifact_key || "").trim();
        if (!key) return;
        selectionMap.set(key, { checked: true, mode: artifactModeValue(option.mode) });
      });
      return selectionMap;
    }
    const entries = source instanceof Map
      ? Array.from(source.entries())
      : (A.isObj(source) ? Object.entries(source) : []);
    entries.forEach(([rawKey, rawValue]) => {
      const key = String(rawKey || "").trim();
      if (!key) return;
      if (A.isObj(rawValue)) {
        selectionMap.set(key, {
          checked: !!rawValue.checked,
          mode: artifactModeValue(rawValue.mode),
        });
      } else {
        selectionMap.set(key, { checked: true, mode: artifactModeValue(rawValue) });
      }
    });
    return selectionMap;
  }

  /** Return artifact selection roots for single, active-tab, or all-tab scope. */
  function artifactSelectionRoots(scope = "active") {
    const wanted = String(scope || "active").toLowerCase();
    if (!A.isMultiImage() || wanted === "single") return el.artifactsForm ? [el.artifactsForm] : [];
    const panelsContainer = q("artifact-image-panels");
    if (!panelsContainer) return [];
    if (wanted === "all") return Array.from(panelsContainer.querySelectorAll(".artifact-image-panel"));
    const activeId = A.activeArtifactTabImageId();
    if (!activeId) return [];
    const activePanel = panelsContainer.querySelector(`.artifact-image-panel[data-image-id="${A.cssEscape(activeId)}"]`);
    return activePanel ? [activePanel] : [];
  }

  /** Apply a normalized option/state map under one artifact UI root. */
  function applyArtifactSelectionMapToRoot(root, sourceMap, opts = {}) {
    const selectionMap = normalizeArtifactSelectionMap(sourceMap);
    const preserveMissing = !!opts.preserveMissing;
    artifactBoxesIn(root).forEach((cb) => {
      const key = String(cb.dataset.artifactKey || "").trim();
      if (!key) return;
      const entry = selectionMap.get(key);
      if (!entry && preserveMissing) return;
      const mode = entry ? artifactModeValue(entry.mode) : A.MODE_PARSE_AND_AI;
      const select = ensureArtifactModeControl(cb, mode);
      if (cb.disabled || !entry) cb.checked = false;
      else cb.checked = !!entry.checked;
      if (select) select.value = mode;
      syncArtifactModeControl(cb, select);
    });
  }

  /** Apply an artifact option/state map to the requested selection scope. */
  function applyArtifactSelectionMap(sourceMap, scope = "active", opts = {}) {
    artifactSelectionRoots(scope).forEach((root) => {
      applyArtifactSelectionMapToRoot(root, sourceMap, opts);
    });
    A.markParsedSelectionStale();
    updateParseButton();
  }

  /** Serialize artifact selections from the requested scope. */
  function serializeArtifactSelections(scope = "active") {
    const wanted = String(scope || "active").toLowerCase();
    if (wanted === "single") return selectedArtifactOptions();
    if (A.isMultiImage()) {
      if (wanted === "all") return A.allImageArtifactSelections();
      const activeId = A.activeArtifactTabImageId();
      return activeId ? A.selectedArtifactOptionsForImage(activeId) : [];
    }
    return selectedArtifactOptions();
  }

  /** Return an array of selected artifact keys (all modes). */
  function selectedArtifacts() {
    return selectedArtifactOptions().map((option) => option.artifact_key);
  }

  /**
   * Return artifact keys that are set to "Parse and use in AI" mode.
   *
   * @param {Object[]|null} [options] - Pre-computed options array; defaults to
   *     selectedArtifactOptions().
   * @returns {string[]}
   */
  function selectedAiArtifacts(options = null) {
    const artifactOptions = Array.isArray(options) ? options : selectedArtifactOptions();
    return artifactOptions
      .filter((option) => artifactModeValue(option.mode) === A.MODE_PARSE_AND_AI)
      .map((option) => String(option.artifact_key || ""))
      .filter(Boolean);
  }

  /** Apply a checkbox preset ("recommended" or "clear") within a DOM root. */
  function applyArtifactPresetIn(root, mode) {
    if (mode === "recommended") {
      const recommendedProfile = findProfileByName(A.RECOMMENDED_PROFILE);
      const profileOptions = recommendedProfile && Array.isArray(recommendedProfile.artifact_options)
        ? recommendedProfile.artifact_options
        : [];
      if (profileOptions.length) {
        applyArtifactSelectionMapToRoot(root, profileOptions);
        return;
      }
    }
    artifactBoxesIn(root).forEach((cb) => {
      const select = ensureArtifactModeControl(cb, A.MODE_PARSE_AND_AI);
      if (cb.disabled) {
        cb.checked = false;
        if (select) select.value = A.MODE_PARSE_AND_AI;
        return syncArtifactModeControl(cb, select);
      }
      if (mode === "clear") cb.checked = false;
      else cb.checked = !A.RECOMMENDED_PRESET_EXCLUDED_ARTIFACTS.has(String(cb.dataset.artifactKey || "").trim().toLowerCase());
      if (select) {
        const key = String(cb.dataset.artifactKey || "").trim().toLowerCase();
        select.value = mode === "recommended" && A.RECOMMENDED_PRESET_PARSE_ONLY_ARTIFACTS.has(key)
          ? A.MODE_PARSE_ONLY
          : A.MODE_PARSE_AND_AI;
      }
      syncArtifactModeControl(cb, select);
    });
  }

  /** Apply a checkbox preset to a single, active-tab, or all-tab scope. */
  function applyArtifactPreset(mode, scope = "active") {
    artifactSelectionRoots(scope).forEach((root) => {
      applyArtifactPresetIn(root, mode);
    });
    A.markParsedSelectionStale();
    updateParseButton();
    if (mode === "recommended") {
      const recommendedProfile = findProfileByName(A.RECOMMENDED_PROFILE);
      const notice = recommendedProfile ? String(recommendedProfile.notice || "").trim() : "";
      if (notice) A.setMsg(el.profileMsg || el.artifactsMsg, notice, "warning");
    }
  }

  // ── Date range ─────────────────────────────────────────────────────────────

  /** Read start and end date strings from the analysis date-range inputs. */
  function readAnalysisDateRangeInputs() {
    return { start: A.val(el.analysisDateStart), end: A.val(el.analysisDateEnd) };
  }

  /**
   * Validate the analysis date-range inputs.
   *
   * @returns {{ok: boolean, message?: string, range?: {start_date: string, end_date: string}|null}}
   */
  function validateAnalysisDateRange() {
    const { start, end } = readAnalysisDateRangeInputs();
    if (!start && !end) return { ok: true, range: null };
    if (!start || !end) return { ok: false, message: "Provide both begin and end dates." };
    if (start > end) return { ok: false, message: "Begin date must be earlier than or equal to end date." };
    return { ok: true, range: { start_date: start, end_date: end } };
  }

  // ── Artifact profiles ──────────────────────────────────────────────────────

  /**
   * Normalise a raw profile object from the backend into a consistent shape.
   *
   * @param {Object} rawProfile - Raw profile descriptor.
   * @returns {{name: string, builtin: boolean, artifact_options: Object[]}|null}
   */
  function normalizeArtifactProfile(rawProfile) {
    if (!A.isObj(rawProfile)) return null;
    const name = String(rawProfile.name || "").trim();
    if (!name) return null;
    const options = Array.isArray(rawProfile.artifact_options) ? rawProfile.artifact_options : [];
    const artifactOptions = options
      .map((option) => (A.isObj(option) ? option : null))
      .filter(Boolean)
      .map((option) => ({
        artifact_key: String(option.artifact_key || "").trim(),
        mode: artifactModeValue(option.mode),
      }))
      .filter((option) => option.artifact_key);
    return {
      name,
      builtin: !!rawProfile.builtin,
      artifact_options: artifactOptions,
      notice: String(rawProfile.notice || "").trim(),
    };
  }

  /** Find a profile in st.profiles by case-insensitive name match. */
  function findProfileByName(name) {
    const wanted = String(name || "").trim().toLowerCase();
    if (!wanted) return null;
    return st.profiles.find((profile) => String(profile.name || "").trim().toLowerCase() === wanted) || null;
  }

  /**
   * Rebuild the profile `<select>` dropdown options from st.profiles.
   *
   * @param {string} [preferredName=""] - Profile name to select after render.
   */
  function renderArtifactProfileOptions(preferredName = "") {
    if (!el.profileSelect) return;
    const currentValue = String(preferredName || el.profileSelect.value || A.RECOMMENDED_PROFILE).trim().toLowerCase();
    el.profileSelect.innerHTML = "";
    st.profiles.forEach((profile) => {
      const name = String(profile.name || "").trim();
      if (!name) return;
      const opt = document.createElement("option");
      opt.value = name;
      opt.textContent = profile.builtin ? `${name} (built-in)` : name;
      el.profileSelect.appendChild(opt);
    });
    const fallback = findProfileByName(A.RECOMMENDED_PROFILE);
    const selected = findProfileByName(currentValue) || fallback || (st.profiles.length ? st.profiles[0] : null);
    if (selected) el.profileSelect.value = selected.name;
  }

  /**
   * Fetch artifact profiles from the backend and populate the dropdown.
   *
   * @param {string} [preferredName=""] - Profile name to select after loading.
   */
  async function loadArtifactProfiles(preferredName = "") {
    const response = await A.apiJson("/api/artifact-profiles", { method: "GET" });
    const profilesRaw = Array.isArray(response && response.profiles) ? response.profiles : [];
    st.profiles = profilesRaw.map((profile) => normalizeArtifactProfile(profile)).filter(Boolean);
    if (!st.profiles.length) {
      st.profiles = [{ name: A.RECOMMENDED_PROFILE, builtin: true, artifact_options: [], notice: "" }];
    }
    renderArtifactProfileOptions(preferredName);
  }

  /**
   * Apply a profile to the artifact checkboxes and mode selects.
   *
   * @param {Object} profile - Normalised profile object.
   * @param {Object} [opts={}] - Options.
   * @param {boolean} [opts.silent] - Suppress the success toast.
   * @param {string} [opts.scope="active"] - Selection scope to update.
   * @returns {boolean} True if the profile was applied.
   */
  function applyArtifactProfile(profile, opts = {}) {
    if (!profile) return false;
    const silent = !!opts.silent;
    const options = Array.isArray(profile.artifact_options) ? profile.artifact_options : [];
    applyArtifactSelectionMap(options, opts.scope || "active");
    if (!silent) {
      const notice = String(profile.notice || "").trim();
      if (notice) {
        A.setMsg(el.profileMsg || el.artifactsMsg, `Loaded profile: ${profile.name}. ${notice}`, "warning");
      } else {
        A.setMsg(el.profileMsg || el.artifactsMsg, `Loaded profile: ${profile.name}`, "success");
      }
    }
    return true;
  }

  /** Load and apply the currently selected profile from the dropdown. */
  function applySelectedProfile() {
    A.clearMsg(el.profileMsg || el.artifactsMsg);
    const selectedName = el.profileSelect ? el.profileSelect.value : A.RECOMMENDED_PROFILE;
    const profile = findProfileByName(selectedName);
    if (!profile) return A.setMsg(el.profileMsg || el.artifactsMsg, "Selected profile is not available.", "error");
    applyArtifactProfile(profile);
  }

  /** Save the current artifact selection as a named profile on the backend. */
  async function saveCurrentProfile() {
    A.clearMsg(el.profileMsg || el.artifactsMsg);
    const profileName = A.val(el.profileName);
    if (!profileName) return A.setMsg(el.profileMsg || el.artifactsMsg, "Enter a profile name before saving.", "error");
    const profileNameKey = profileName.toLowerCase();
    if (profileNameKey === A.RECOMMENDED_PROFILE || profileNameKey === "all") {
      return A.setMsg(el.profileMsg || el.artifactsMsg, `\`${profileNameKey}\` is reserved. Pick a different name.`, "error");
    }
    const options = serializeArtifactSelections("active");
    if (!options.length) return A.setMsg(el.profileMsg || el.artifactsMsg, "Select at least one artifact before saving a profile.", "error");
    try {
      const response = await A.apiJson("/api/artifact-profiles", { method: "POST", json: { name: profileName, artifact_options: options } });
      const profilesRaw = Array.isArray(response && response.profiles) ? response.profiles : [];
      st.profiles = profilesRaw.map((profile) => normalizeArtifactProfile(profile)).filter(Boolean);
      renderArtifactProfileOptions(profileName);
      if (el.profileName) el.profileName.value = "";
      A.setMsg(el.profileMsg || el.artifactsMsg, `Profile saved: ${profileName}`, "success");
    } catch (e) {
      A.setMsg(el.profileMsg || el.artifactsMsg, `Failed to save profile: ${e.message}`, "error");
    }
  }

  // ── Artifact step wiring ───────────────────────────────────────────────────

  /** Wire up event listeners for the artifact selection step (Step 2). */
  function setupArtifacts() {
    if (!el.artifactsForm) return;
    el.artifactsForm.addEventListener("change", (e) => {
      const t = e.target;
      if (t instanceof HTMLInputElement && t.type === "checkbox" && t.dataset.artifactKey) {
        syncArtifactModeControl(t);
        A.markParsedSelectionStale();
        return updateParseButton();
      }
      if (t instanceof HTMLSelectElement && t.classList.contains("artifact-mode-select") && t.dataset.artifactKey) {
        t.value = artifactModeValue(t.value);
        A.markParsedSelectionStale();
        return updateParseButton();
      }
    });
    if (el.analysisDateStart) el.analysisDateStart.addEventListener("change", updateParseButton);
    if (el.analysisDateEnd) el.analysisDateEnd.addEventListener("change", updateParseButton);
    if (el.quickBtn) {
      el.quickBtn.addEventListener("click", () => {
        if (A.isMultiImage()) return A.applyPresetMultiAware("recommended");
        const recommendedProfile = findProfileByName(A.RECOMMENDED_PROFILE);
        if (recommendedProfile) return applyArtifactProfile(recommendedProfile);
        return applyPreset("recommended");
      });
    }
    if (el.clearBtn) el.clearBtn.addEventListener("click", () => {
      if (A.isMultiImage()) return A.applyPresetMultiAware("clear");
      return applyPreset("clear");
    });
    if (el.applyRecommendedAllBtn) {
      el.applyRecommendedAllBtn.addEventListener("click", () => A.applyRecommendedToAllImages());
    }
    if (el.applySelectionAllBtn) {
      el.applySelectionAllBtn.addEventListener("click", () => A.applyCurrentSelectionToAllImages());
    }
    if (el.profileLoadBtn) el.profileLoadBtn.addEventListener("click", () => applySelectedProfile());
    if (el.profileSaveBtn) el.profileSaveBtn.addEventListener("click", async () => saveCurrentProfile());
    el.artifactsForm.addEventListener("submit", async (e) => {
      e.preventDefault();
      await A.submitParse();
    });
    if (el.parseBtn) el.parseBtn.addEventListener("click", async () => {
      await A.submitParse();
    });
    if (el.cancelParse) el.cancelParse.addEventListener("click", A.cancelParse);
  }

  /**
   * Apply a checkbox preset ("recommended" selects most, "clear" unchecks all).
   *
   * @param {string} mode - "recommended" or "clear".
   */
  function applyPreset(mode) {
    applyArtifactPreset(mode, "single");
  }

  /** Update the parse button's disabled state, label, and cancel button visibility. */
  function updateParseButton() {
    let hasArtifacts = false;
    if (A.isMultiImage()) {
      /* In multi-image mode, at least one image must have selections. */
      const selections = serializeArtifactSelections("all");
      hasArtifacts = selections.some((s) => s.artifact_options.length > 0);
    } else {
      /* Ensure the main artifact form is visible and its state is current
         before reading selections (avoids stale state when switching back
         from multi-image mode). */
      if (el.artifactsForm && el.artifactsForm.hidden) {
        el.artifactsForm.hidden = false;
      }
      const options = selectedArtifactOptions();
      hasArtifacts = options.length > 0;
    }
    const dateRangeValidation = validateAnalysisDateRange();
    const disabled = !A.activeCaseId() || !hasArtifacts || !dateRangeValidation.ok;
    if (el.parseBtn) {
      el.parseBtn.disabled = disabled;
      el.parseBtn.textContent = (st.parse.run || st.parse.done) ? "Restart Parsing" : "Parse Selected";
    }
    if (el.cancelParse) el.cancelParse.hidden = !st.parse.run;
    A.updateNav();
  }

  // ── Version update check ───────────────────────────────────────────────────

  /**
   * Query the backend for the latest GitHub release and show an update banner
   * in Step 1 when the running version differs, or a warning when offline.
   */
  async function checkForUpdate() {
    const banner = q("update-banner");
    const text = q("update-banner-text");
    const closeBtn = q("update-banner-close");
    if (!banner || !text) return;

    if (closeBtn) {
      closeBtn.addEventListener("click", () => { banner.hidden = true; });
    }

    try {
      const r = await A.fetchWithTimeout("/api/version/check", { method: "GET" }, 8000);
      if (r.ok) {
        const data = await r.json();
        if (data.update_available) {
          text.textContent = `A new version of AIFT is available (v${data.latest}). You are running v${data.current}.`;
          banner.dataset.kind = "update";
          banner.hidden = false;
        }
      } else {
        text.textContent = "Unable to check for updates. Please verify manually that you are running the latest version of AIFT.";
        banner.dataset.kind = "warning";
        banner.hidden = false;
      }
    } catch (_e) {
      text.textContent = "Unable to check for updates. Please verify manually that you are running the latest version of AIFT.";
      banner.dataset.kind = "warning";
      banner.hidden = false;
    }
  }

  // ── Public API ─────────────────────────────────────────────────────────────
  A.formatOsVersion = formatOsVersion;
  A.setupEvidence = setupEvidence;
  A.setupArtifacts = setupArtifacts;
  A.loadArtifactProfiles = loadArtifactProfiles;

  A.updateParseButton = updateParseButton;
  A.artifactBoxesIn = artifactBoxesIn;
  A.artifactLabelText = artifactLabelText;
  A.artifactDisplayIdentity = artifactDisplayIdentity;
  A.selectedArtifactOptions = selectedArtifactOptions;
  A.selectedArtifactOptionsIn = selectedArtifactOptionsIn;
  A.selectedArtifacts = selectedArtifacts;
  A.selectedAiArtifacts = selectedAiArtifacts;
  A.applyArtifactPresetIn = applyArtifactPresetIn;
  A.applyArtifactPreset = applyArtifactPreset;
  A.applyArtifactSelectionMap = applyArtifactSelectionMap;
  A.applyArtifactSelectionMapToRoot = applyArtifactSelectionMapToRoot;
  A.serializeArtifactSelections = serializeArtifactSelections;
  A.artifactSelectionRoots = artifactSelectionRoots;
  A.syncArtifactModesIn = syncArtifactModesIn;
  A.artifactSelectionStateByKeyIn = artifactSelectionStateByKeyIn;
  A.normalizeArtifactSelectionMap = normalizeArtifactSelectionMap;
  A.validateAnalysisDateRange = validateAnalysisDateRange;
  A.artifactBoxes = artifactBoxes;
  A.ensureArtifactModeControl = ensureArtifactModeControl;
  A.syncArtifactModeControl = syncArtifactModeControl;
  A.clearDynamicArtifacts = clearDynamicArtifacts;
  A.syncAdvancedArtifactSections = syncAdvancedArtifactSections;
  A.syncMode = syncMode;
  A.checkForUpdate = checkForUpdate;
  A.getImageForms = getImageForms;
  A.initImageForm = initImageForm;
  A.sanitizeEvidencePath = sanitizeEvidencePath;
  A.applyEvidence = applyEvidence;
  A.applyPreset = applyPreset;
  A.applyArtifactProfile = applyArtifactProfile;
  A.artifactModeValue = artifactModeValue;
  A.setDroppedFilesForCard = setDroppedFilesForCard;
  A.getDroppedFilesForCard = getDroppedFilesForCard;
  A.clearDroppedFilesForCard = clearDroppedFilesForCard;
  A.imageUploadFilesForCard = imageUploadFilesForCard;
})();
