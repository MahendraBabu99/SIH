/**
 * Unit tests for AIFT Markdown rendering (markdown.js).
 *
 * Covers:
 *  - renderMarkdownInto container population
 *  - Headings (H1–H6)
 *  - Unordered and ordered lists
 *  - Fenced code blocks
 *  - Markdown tables (header, separator, body, padding)
 *  - Inline bold, italic, code spans
 *  - Confidence token highlighting (CRITICAL, HIGH, MEDIUM, LOW)
 *  - Empty / blank input handling
 *  - Mixed content rendering
 *
 * @jest-environment jsdom
 */

"use strict";

const { setupAift, mustGet, mustQuery } = require("./harness");

let A;

beforeEach(() => {
  A = setupAift();
});

/** Helper: render markdown into a fresh div and return its innerHTML. */
function renderMd(text, emptyText = "") {
  const container = document.createElement("div");
  A.renderMarkdownInto(container, text, emptyText);
  return container;
}

// ── renderMarkdownInto basics ───────────────────────────────────────────────

describe("renderMarkdownInto basics", () => {
  test("renders empty text with placeholder", () => {
    const c = renderMd("", "Nothing here.");
    expect(c.querySelector("p").textContent).toBe("Nothing here.");
  });

  test("renders whitespace-only text with placeholder", () => {
    const c = renderMd("   \n  ", "Empty.");
    expect(c.querySelector("p").textContent).toBe("Empty.");
  });

  test("clears previous content before rendering", () => {
    const container = document.createElement("div");
    container.innerHTML = "<p>Old content</p>";
    A.renderMarkdownInto(container, "New content", "");
    expect(container.textContent).not.toContain("Old content");
    expect(container.textContent).toContain("New content");
  });

  test("does nothing when container is null", () => {
    expect(() => A.renderMarkdownInto(null, "text", "empty")).not.toThrow();
  });
});

// ── Headings ────────────────────────────────────────────────────────────────

describe("headings", () => {
  test("renders H1 through H6", () => {
    for (let level = 1; level <= 6; level++) {
      const prefix = "#".repeat(level);
      const c = renderMd(`${prefix} Heading ${level}`);
      const heading = c.querySelector(`h${level}`);
      expect(heading).not.toBeNull();
      expect(heading.textContent).toContain(`Heading ${level}`);
    }
  });

  test("renders heading with inline formatting", () => {
    const c = renderMd("## **Bold** heading");
    const h2 = c.querySelector("h2");
    expect(h2).not.toBeNull();
    expect(h2.querySelector("strong")).not.toBeNull();
  });
});

// ── Lists ───────────────────────────────────────────────────────────────────

describe("lists", () => {
  test("renders unordered list with - markers", () => {
    const c = renderMd("- Item A\n- Item B\n- Item C");
    const ul = c.querySelector("ul");
    expect(ul).not.toBeNull();
    const items = ul.querySelectorAll("li");
    expect(items).toHaveLength(3);
    expect(items[0].textContent).toContain("Item A");
  });

  test("renders unordered list with * markers", () => {
    const c = renderMd("* Alpha\n* Beta");
    const ul = c.querySelector("ul");
    expect(ul).not.toBeNull();
    expect(ul.querySelectorAll("li")).toHaveLength(2);
  });

  test("renders ordered list", () => {
    const c = renderMd("1. First\n2. Second\n3. Third");
    const ol = c.querySelector("ol");
    expect(ol).not.toBeNull();
    expect(ol.querySelectorAll("li")).toHaveLength(3);
  });

  test("separates different list types with blank line", () => {
    const c = renderMd("- Unordered\n\n1. Ordered");
    expect(c.querySelector("ul")).not.toBeNull();
    expect(c.querySelector("ol")).not.toBeNull();
  });
});

// ── Code blocks ─────────────────────────────────────────────────────────────

describe("fenced code blocks", () => {
  test("renders code fence as pre>code", () => {
    const c = renderMd("```\nconst x = 1;\nconsole.log(x);\n```");
    const pre = c.querySelector("pre");
    expect(pre).not.toBeNull();
    const code = pre.querySelector("code");
    expect(code).not.toBeNull();
    expect(code.textContent).toContain("const x = 1;");
  });

  test("preserves code content without markdown processing", () => {
    const c = renderMd("```\n**not bold** _not italic_\n```");
    const code = c.querySelector("code");
    expect(code.textContent).toContain("**not bold**");
    expect(code.querySelector("strong")).toBeNull();
  });

  test("handles unclosed code fence", () => {
    const c = renderMd("```\nno closing fence");
    const code = c.querySelector("code");
    expect(code).not.toBeNull();
    expect(code.textContent).toContain("no closing fence");
  });
});

// ── Tables ──────────────────────────────────────────────────────────────────

describe("tables", () => {
  test("renders a basic table", () => {
    const md = "| Name | Value |\n| --- | --- |\n| A | 1 |\n| B | 2 |";
    const c = renderMd(md);
    const table = c.querySelector("table");
    expect(table).not.toBeNull();
    const headers = table.querySelectorAll("thead th");
    expect(headers).toHaveLength(2);
    expect(headers[0].textContent).toBe("Name");
    const rows = table.querySelectorAll("tbody tr");
    expect(rows).toHaveLength(2);
  });

  test("handles tables with alignment markers", () => {
    const md = "| Left | Center | Right |\n| :--- | :---: | ---: |\n| a | b | c |";
    const c = renderMd(md);
    expect(c.querySelector("table")).not.toBeNull();
  });

  test("pads rows with fewer cells", () => {
    const md = "| A | B | C |\n| --- | --- | --- |\n| only-one |";
    const c = renderMd(md);
    const tds = c.querySelectorAll("tbody td");
    expect(tds.length).toBeGreaterThanOrEqual(1);
  });

  test("does not render a table without separator row", () => {
    const md = "| Not | A | Table |\n| Just | Pipe | Text |";
    const c = renderMd(md);
    expect(c.querySelector("table")).toBeNull();
  });
});

// ── Inline formatting ───────────────────────────────────────────────────────

describe("inline formatting", () => {
  test("renders bold with ** markers", () => {
    const c = renderMd("This is **bold** text.");
    expect(c.querySelector("strong")).not.toBeNull();
    expect(c.querySelector("strong").textContent).toBe("bold");
  });

  test("renders bold with __ markers", () => {
    const c = renderMd("This is __bold__ text.");
    expect(c.querySelector("strong")).not.toBeNull();
  });

  test("does not render __ markers inside identifiers as bold", () => {
    const c = renderMd("Identifier: foo__bar__baz");
    expect(c.querySelector("strong")).toBeNull();
    expect(c.textContent).toContain("foo__bar__baz");
  });

  test("renders italic with * markers", () => {
    const c = renderMd("This is *italic* text.");
    expect(c.querySelector("em")).not.toBeNull();
    expect(c.querySelector("em").textContent).toBe("italic");
  });

  test("renders italic with _ markers", () => {
    const c = renderMd("This is _italic_ text.");
    expect(c.querySelector("em")).not.toBeNull();
  });

  test("renders italic with _ markers before punctuation", () => {
    const c = renderMd("Finding: _italic_.");
    expect(c.querySelector("em")).not.toBeNull();
    expect(c.querySelector("em").textContent).toBe("italic");
  });

  test("does not render underscores inside identifiers as italic", () => {
    const identifiers = [
      "source_ip",
      "destination_ip",
      "image_id",
      "csv_output_dir",
      "artifact_csv_paths",
      "foo_bar_baz",
    ];
    const c = renderMd(`Columns: ${identifiers.join(" ")}`);
    expect(c.querySelector("em")).toBeNull();
    identifiers.forEach((identifier) => {
      expect(c.textContent).toContain(identifier);
    });
  });

  test("does not render underscores inside paths as italic", () => {
    const path = "C:/Users/test_user/AppData/file_name.exe";
    const c = renderMd(`Path: ${path}`);
    expect(c.querySelector("em")).toBeNull();
    expect(c.textContent).toContain(path);
  });

  test("renders inline code with backticks", () => {
    const c = renderMd("Use `console.log()` here.");
    const code = c.querySelector("code");
    expect(code).not.toBeNull();
    expect(code.textContent).toBe("console.log()");
  });

  test("inline code does not process bold/italic inside", () => {
    const c = renderMd("`**not bold**`");
    const code = c.querySelector("code");
    expect(code.textContent).toBe("**not bold**");
    expect(code.querySelector("strong")).toBeNull();
  });
});

// ── Python renderer parity guards ───────────────────────────────────────────

describe("python renderer parity guards", () => {
  test("does not pair a stray star with double-star markers", () => {
    const c = renderMd("a * b ** c");
    expect(c.querySelector("em")).toBeNull();
    expect(c.textContent).toContain("a * b ** c");
  });

  test("stray star after bold stays literal", () => {
    const c = renderMd("**bold** * stray");
    expect(c.querySelector("strong")).not.toBeNull();
    expect(c.querySelector("em")).toBeNull();
    expect(c.textContent).toContain("* stray");
  });

  test("italic still renders around embedded bold", () => {
    const c = renderMd("*a **b** c*");
    const em = c.querySelector("em");
    expect(em).not.toBeNull();
    expect(em.querySelector("strong")).not.toBeNull();
  });

  test("a lone backtick stays literal text", () => {
    const c = renderMd("`");
    expect(c.querySelector("code")).toBeNull();
    expect(c.textContent).toContain("`");
  });

  test("an empty code span still renders as code", () => {
    const c = renderMd("a `` b");
    const code = c.querySelector("code");
    expect(code).not.toBeNull();
    expect(code.textContent).toBe("");
  });
});

// ── Confidence tokens ───────────────────────────────────────────────────────

describe("confidence token highlighting", () => {
  test("wraps CRITICAL confidence token in span", () => {
    const c = renderMd("Confidence: CRITICAL");
    const span = c.querySelector(".confidence-inline.confidence-critical");
    expect(span).not.toBeNull();
    expect(span.textContent).toBe("CRITICAL");
  });

  test("wraps HIGH confidence token in span", () => {
    const c = renderMd("Confidence HIGH");
    const span = c.querySelector(".confidence-inline.confidence-high");
    expect(span).not.toBeNull();
  });

  test("wraps MEDIUM confidence token in span", () => {
    const c = renderMd("confidence level: medium");
    const span = c.querySelector(".confidence-inline.confidence-medium");
    expect(span).not.toBeNull();
  });

  test("wraps LOW confidence token in span", () => {
    const c = renderMd("confidence: LOW");
    const span = c.querySelector(".confidence-inline.confidence-low");
    expect(span).not.toBeNull();
  });

  test("highlights case-insensitive tokens", () => {
    const c = renderMd("confidence: critical");
    const span = c.querySelector(".confidence-inline.confidence-critical");
    expect(span).not.toBeNull();
    expect(span.textContent).toBe("CRITICAL");
  });

  test("wraps markdown-emphasized explicit confidence token", () => {
    const c = renderMd("Confidence: **HIGH**");
    const strong = c.querySelector("strong");
    const span = c.querySelector(".confidence-inline.confidence-high");
    expect(strong).not.toBeNull();
    expect(span).not.toBeNull();
    expect(strong.contains(span)).toBe(true);
  });

  test("does not highlight ordinary prose tokens", () => {
    const c = renderMd("a high number of events. LOW-value rows. HIGH CPU usage.");
    const spans = c.querySelectorAll(".confidence-inline");
    expect(spans.length).toBe(0);
  });
});

// ── Mixed content ───────────────────────────────────────────────────────────

describe("mixed content", () => {
  test("renders heading followed by list followed by paragraph", () => {
    const md = "# Title\n\n- Item 1\n- Item 2\n\nSome paragraph text.";
    const c = renderMd(md);
    expect(c.querySelector("h1")).not.toBeNull();
    expect(c.querySelector("ul")).not.toBeNull();
    const paragraphs = c.querySelectorAll("p");
    const hasParagraphText = Array.from(paragraphs).some((p) => p.textContent.includes("Some paragraph text."));
    expect(hasParagraphText).toBe(true);
  });

  test("renders paragraph between two code blocks", () => {
    const md = "```\ncode1\n```\n\nMiddle text.\n\n```\ncode2\n```";
    const c = renderMd(md);
    const pres = c.querySelectorAll("pre");
    expect(pres).toHaveLength(2);
    const paragraphs = c.querySelectorAll("p");
    const hasMiddle = Array.from(paragraphs).some((p) => p.textContent.includes("Middle text."));
    expect(hasMiddle).toBe(true);
  });
});

// ── Paragraphs ──────────────────────────────────────────────────────────────

describe("paragraphs", () => {
  test("wraps plain text in a paragraph", () => {
    const c = renderMd("Hello world");
    const p = c.querySelector("p");
    expect(p).not.toBeNull();
    expect(p.textContent).toBe("Hello world");
  });

  test("consecutive lines become one paragraph", () => {
    const c = renderMd("Line one\nLine two");
    const paragraphs = c.querySelectorAll("p");
    expect(paragraphs).toHaveLength(1);
  });

  test("blank line separates paragraphs", () => {
    const c = renderMd("First paragraph.\n\nSecond paragraph.");
    const paragraphs = c.querySelectorAll("p");
    expect(paragraphs).toHaveLength(2);
  });
});

describe("escape-first rendering", () => {
  test("does not turn malicious links into anchors", () => {
    const c = renderMd("[open](javascript:alert(1))");
    expect(c.querySelector("a")).toBeNull();
    expect(c.textContent).toContain("[open](javascript:alert(1))");
  });

  test("does not turn malicious images into image elements", () => {
    const c = renderMd("![alt](x onerror=alert(1))");
    expect(c.querySelector("img")).toBeNull();
    expect(c.textContent).toContain("![alt](x onerror=alert(1))");
  });

  test("escapes raw HTML elements", () => {
    const c = renderMd("<script>alert(1)</script><b>bold</b>");
    expect(c.querySelector("script")).toBeNull();
    expect(c.querySelector("b")).toBeNull();
    expect(c.textContent).toContain("<script>alert(1)</script>");
  });

  test("keeps event attributes as text, not DOM attributes", () => {
    const c = renderMd("<img src=x onerror=alert(1)>");
    expect(c.querySelector("img")).toBeNull();
    expect(c.querySelector("[onerror]")).toBeNull();
    expect(c.textContent).toContain("onerror=alert(1)");
  });

  test("escapes HTML inside table cells", () => {
    const c = renderMd("| Name | Value |\n| --- | --- |\n| A | <svg onload=alert(1)> |");
    expect(c.querySelector("table")).not.toBeNull();
    expect(c.querySelector("svg")).toBeNull();
    expect(c.querySelector("[onload]")).toBeNull();
    expect(c.textContent).toContain("<svg onload=alert(1)>");
  });

  test("keeps code spans literal", () => {
    const c = renderMd("Use `<img src=x onerror=alert(1)>` here.");
    const code = c.querySelector("code");
    expect(code).not.toBeNull();
    expect(code.textContent).toBe("<img src=x onerror=alert(1)>");
    expect(code.querySelector("img")).toBeNull();
  });
});
