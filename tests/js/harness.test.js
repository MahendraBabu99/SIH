"use strict";

const fs = require("fs");
const path = require("path");

const { productionScripts, setupAift, setupUtilsOnly, mustGet, mustFindAll } = require("./harness");

function frontendTestFiles() {
  const testDir = __dirname;
  return fs.readdirSync(testDir)
    .filter((name) => name.endsWith(".test.js"))
    .map((name) => path.join(testDir, name));
}

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

  test("required DOM list helper fails clearly", () => {
    setupAift();
    expect(() => mustFindAll(document, ".definitely-missing-test-node")).toThrow(
      'Required DOM selector ".definitely-missing-test-node" matched 0 node(s), expected at least 1'
    );
  });

  test("Jest test bodies do not silently return before assertions", () => {
    const offenders = [];
    const earlyReturnPattern = new RegExp("\\breturn" + ";\\s*(?://.*)?$");
    for (const filePath of frontendTestFiles()) {
      const relPath = path.relative(path.join(__dirname, "..", ".."), filePath).replace(/\\/g, "/");
      fs.readFileSync(filePath, "utf-8").split(/\r?\n/).forEach((line, index) => {
        if (earlyReturnPattern.test(line)) {
          offenders.push(`${relPath}:${index + 1}: ${line.trim()}`);
        }
      });
    }
    expect(offenders).toEqual([]);
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
