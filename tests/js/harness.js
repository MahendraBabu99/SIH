"use strict";

const fs = require("fs");
const path = require("path");

const ROOT = path.resolve(__dirname, "..", "..");
const STATIC = path.join(ROOT, "static");
const TEMPLATES = path.join(ROOT, "templates");
const INDEX_HTML_PATH = path.join(TEMPLATES, "index.html");

function readIndexHtml() {
  return fs.readFileSync(INDEX_HTML_PATH, "utf-8");
}

function productionScripts() {
  const html = readIndexHtml();
  const scripts = [];
  const pattern = /<script\b[^>]*\bsrc="{{\s*url_for\('static',\s*filename='([^']+)'\)\s*}}"[^>]*><\/script>/g;
  let match;
  while ((match = pattern.exec(html)) !== null) {
    scripts.push(match[1]);
  }
  if (!scripts.length) {
    throw new Error("Unable to derive frontend script order from templates/index.html");
  }
  return scripts;
}

function readStatic(relPath) {
  return fs.readFileSync(path.join(STATIC, relPath), "utf-8");
}

function installBrowserStubs() {
  global.fetch = jest.fn(() => Promise.reject(new Error("fetch not available in tests")));
  if (!global.CSS) global.CSS = {};
  if (!global.CSS.escape) {
    global.CSS.escape = (v) => String(v).replace(/([^\w-])/g, "\\$1");
  }

  const openSources = [];
  global.EventSource = class HarnessEventSource {
    constructor(url) {
      this.url = url;
      this.readyState = 0;
      this.close = jest.fn(() => {
        this.readyState = 2;
      });
      openSources.push(this);
    }
  };
  window.EventSource = global.EventSource;
  return { openSources };
}

function evalScript(relPath) {
  try {
    const fn = new Function(readStatic(relPath));
    fn.call(window);
  } catch (error) {
    throw new Error(`Failed to evaluate ${relPath}: ${error.message}`);
  }
}

function loadTemplate() {
  const html = readIndexHtml();
  const head = (html.match(/<head[^>]*>([\s\S]*?)<\/head>/i) || ["", ""])[1];
  const body = (html.match(/<body[^>]*>([\s\S]*?)<\/body>/i) || ["", html])[1];
  document.head.innerHTML = head;
  document.body.innerHTML = body;
}

function setupAift(options = {}) {
  loadTemplate();
  const stubs = installBrowserStubs();
  const omitted = new Set(options.omitScripts || []);
  const scripts = options.scripts || productionScripts();
  for (const script of scripts) {
    if (!omitted.has(script)) evalScript(script);
  }
  if (options.dispatchDOMContentLoaded !== false) {
    document.dispatchEvent(new Event("DOMContentLoaded"));
  }
  if (options.requireDomContracts !== false) {
    [
      "wizard",
      "parse-progress-rows",
      "analysis-results-list",
      "artifact-image-tabs",
      "chat-thread",
      "settings-panel",
    ].forEach(mustGet);
  }
  window.__AIFT_TEST_OPEN_EVENT_SOURCES__ = stubs.openSources;
  return window.AIFT;
}

function setupUtilsOnly() {
  loadTemplate();
  installBrowserStubs();
  evalScript("js/utils.js");
  return window.AIFT;
}

function mustGet(id) {
  const node = document.getElementById(id);
  if (!node) throw new Error(`Required DOM node #${id} is missing`);
  return node;
}

function mustQuery(root, selector) {
  const scope = root || document;
  const node = scope.querySelector(selector);
  if (!node) throw new Error(`Required DOM selector "${selector}" is missing`);
  return node;
}

function cleanupAift() {
  const A = window.AIFT;
  if (A) {
    if (typeof A.closeParseSse === "function") A.closeParseSse();
    if (typeof A.closeAnalysisSse === "function") A.closeAnalysisSse();
    if (typeof A.closeChatSse === "function") A.closeChatSse();
    if (typeof A.stopTimer === "function") {
      A.stopTimer("parse");
      A.stopTimer("analysis");
    }
    if (A.st && A.st.imageParse) {
      Object.keys(A.st.imageParse).forEach((imageId) => {
        const imageState = A.st.imageParse[imageId];
        if (imageState && imageState.sseState && typeof A.closeSseChannel === "function") {
          A.closeSseChannel(imageState.sseState);
        }
      });
    }
  }
  const openSources = window.__AIFT_TEST_OPEN_EVENT_SOURCES__ || [];
  openSources.forEach((source) => {
    if (source && typeof source.close === "function") source.close();
  });
  delete window.__AIFT_TEST_OPEN_EVENT_SOURCES__;
  delete window.AIFT;
  document.body.innerHTML = "";
  document.head.innerHTML = "";
  if (jest.isMockFunction(global.fetch)) global.fetch.mockReset();
  jest.restoreAllMocks();
  try {
    jest.clearAllTimers();
  } catch (_error) {
    // The current test may be using real timers.
  }
}

afterEach(cleanupAift);

module.exports = {
  productionScripts,
  setupAift,
  setupUtilsOnly,
  mustGet,
  mustQuery,
  cleanupAift,
};
