"use strict";

const { productionScripts, setupAift, setupUtilsOnly, mustGet } = require("./harness");

describe("shared frontend harness", () => {
  test("derives production script order from the template", () => {
    expect(productionScripts()).toEqual([
      "js/utils.js",
      "js/markdown.js",
      "js/evidence.js",
      "js/evidence_multi.js",
      "js/parsing.js",
      "js/analysis.js",
      "js/chat.js",
      "js/settings.js",
      "app.js",
    ]);
  });

  test("startup contract names missing module exports", () => {
    const A = setupAift({
      omitScripts: ["js/settings.js", "app.js"],
      dispatchDOMContentLoaded: false,
    });
    expect(() => A.requireModules(["setupSettings", "setupHelpTooltips"])).toThrow(
      /missing module exports: .*setupSettings.*setupHelpTooltips/
    );
  });

  test("required DOM helper fails clearly", () => {
    setupAift();
    document.getElementById("parse-progress-rows").remove();
    expect(() => mustGet("parse-progress-rows")).toThrow("Required DOM node #parse-progress-rows is missing");
  });

  test("utility-only setup loads utils without app initialization", () => {
    const A = setupUtilsOnly();
    expect(A.fmtBytes(1024)).toBe("1.0 KB");
    expect(A.setupEvidence).toBeUndefined();
  });

  test("cleanup closes pending SSE retry timers", () => {
    const A = setupAift();
    const closed = jest.fn();
    A.st.parse.es = { close: closed };
    A.st.parse.retry = setTimeout(() => {}, 10000);
    A.closeParseSse();
    expect(closed).toHaveBeenCalled();
    expect(A.st.parse.retry).toBeNull();
  });
});
