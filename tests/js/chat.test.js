/**
 * Unit tests for AIFT chat panel and history management (chat.js).
 *
 * Covers:
 *  - resetChatState clears all chat state and UI
 *  - toggleChat opens/closes the chat panel
 *  - closeChatSse closes the SSE channel
 *  - Chat panel visibility and aria attributes
 *  - Chat controls disabled state
 *  - Chat empty state rendering
 *
 * @jest-environment jsdom
 */

"use strict";

const { setupAift, mustGet, mustQuery, flushMicrotasks } = require("./harness");

let A;

beforeEach(() => {
  A = setupAift();
});

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function jsonResponse(payload, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name) => (String(name || "").toLowerCase() === "content-type" ? "application/json" : "") },
    json: async () => payload,
    text: async () => JSON.stringify(payload),
  });
}

function flushPromises() {
  return flushMicrotasks();
}

async function submitChat(message) {
  A.el.chatInput.value = message;
  await A._sendChatMessage();
  await flushPromises();
}

function startChat(message) {
  A.el.chatInput.value = message;
  return A._sendChatMessage();
}

function emit(source, payload) {
  source.onmessage({ data: JSON.stringify(payload) });
}

// ── resetChatState ──────────────────────────────────────────────────────────

describe("resetChatState", () => {
  test("resets all chat flags to initial state", () => {
    A.st.chat.run = true;
    A.st.chat.retryCount = 5;
    A.st.chat.seq = 10;
    A.st.chat.pending = { bubble: {}, contentNode: {}, typingNode: {} };
    A.st.chat.historyLoadedCaseId = "old-case";

    A.resetChatState();

    expect(A.st.chat.run).toBe(false);
    expect(A.st.chat.retryCount).toBe(0);
    expect(A.st.chat.seq).toBe(-1);
    expect(A.st.chat.pending).toBeNull();
    expect(A.st.chat.historyLoadedCaseId).toBe("");
  });

  test("resets chat input to enabled and empty", () => {
    const input = mustGet("chat-input");
    input.disabled = true;
    input.value = "old message";

    A.resetChatState();

    expect(input.disabled).toBe(false);
    expect(input.value).toBe("");
  });

  test("re-enables chat send button", () => {
    const send = mustGet("chat-send");
    send.disabled = true;
    A.resetChatState();
    expect(send.disabled).toBe(false);
  });

  test("shows chat panel after reset", () => {
    const panel = mustGet("chat-panel");
    panel.hidden = true;
    A.resetChatState();
    // resetChatState opens the chat panel (hidden = false).
    expect(panel.hidden).toBe(false);
  });

  test("resets chat toggle to open state", () => {
    const toggle = mustGet("chat-toggle");
    toggle.textContent = "Show Chat";
    toggle.setAttribute("aria-expanded", "false");

    A.resetChatState();

    // resetChatState sets chat panel to open/visible.
    expect(toggle.textContent).toBe("Hide Chat");
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
  });

  test("renders empty state in chat thread", () => {
    A.resetChatState();
    const empty = mustQuery(mustGet("chat-thread"), "#chat-empty-state");
    expect(empty.textContent).toContain("Chat history will appear here");
  });
});

// ── toggleChat ──────────────────────────────────────────────────────────────

describe("toggleChat", () => {
  test("opens chat panel when forced open", () => {
    const panel = mustGet("chat-panel");
    const toggle = mustGet("chat-toggle");
    panel.hidden = true;
    A.toggleChat(true);
    expect(panel.hidden).toBe(false);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    expect(toggle.textContent).toBe("Hide Chat");
  });

  test("closes chat panel when forced closed", () => {
    const panel = mustGet("chat-panel");
    const toggle = mustGet("chat-toggle");
    panel.hidden = false;
    A.toggleChat(false);
    expect(panel.hidden).toBe(true);
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    expect(toggle.textContent).toBe("Show Chat");
  });

  test("toggles chat panel when no force argument", () => {
    const panel = mustGet("chat-panel");
    mustGet("chat-toggle");
    panel.hidden = true;
    A.toggleChat();
    expect(panel.hidden).toBe(false);

    A.toggleChat();
    expect(panel.hidden).toBe(true);
  });

  test("does nothing when elements are missing", () => {
    const savedPanel = A.el.chatPanel;
    A.el.chatPanel = null;
    expect(() => A.toggleChat(true)).not.toThrow();
    A.el.chatPanel = savedPanel;
  });
});

// ── closeChatSse ────────────────────────────────────────────────────────────

describe("closeChatSse", () => {
  test("closes the chat SSE channel", () => {
    const mockEs = { close: jest.fn() };
    A.st.chat.es = mockEs;
    A.st.chat.retry = setTimeout(() => {}, 10000);

    A.closeChatSse();

    expect(mockEs.close).toHaveBeenCalled();
    expect(A.st.chat.es).toBeNull();
    expect(A.st.chat.retry).toBeNull();
  });

  test("handles already-closed channel gracefully", () => {
    A.st.chat.es = null;
    A.st.chat.retry = null;
    expect(() => A.closeChatSse()).not.toThrow();
  });
});

// ── Chat panel initial state ────────────────────────────────────────────────

describe("chat panel initial state", () => {
  test("chat panel is visible on initial load", () => {
    // The HTML template renders the chat panel open by default.
    expect(mustGet("chat-panel").hidden).toBe(false);
  });

  test("chat toggle shows 'Hide Chat' initially", () => {
    // The HTML template renders with aria-expanded="true" and "Hide Chat".
    expect(mustGet("chat-toggle").textContent).toBe("Hide Chat");
  });

  test("chat is not running initially", () => {
    expect(A.st.chat.run).toBe(false);
  });

  test("chat input is enabled initially", () => {
    expect(mustGet("chat-input").disabled).toBe(false);
  });
});

// ── Chat allMessages / displayedCount state ─────────────────────────────────

describe("chat message state", () => {
  test("resetChatState clears allMessages and displayedCount", () => {
    A.st.chat.allMessages = [{ role: "user", content: "hi" }];
    A.st.chat.displayedCount = 1;

    A.resetChatState();

    expect(A.st.chat.allMessages).toEqual([]);
    expect(A.st.chat.displayedCount).toBe(0);
  });
});

describe("chat reasoning stream rendering", () => {
  test("renders reasoning in a collapsible panel outside answer text", () => {
    A.setCaseId("case-chat-reasoning");
    A.resetChatState();

    A._onChatEvent("case-chat-reasoning", { type: "reasoning", content: "hidden model reasoning" });
    A._onChatEvent("case-chat-reasoning", { type: "token", content: "Visible answer." });

    const bubble = mustQuery(A.el.chatThread, ".chat-bubble-ai");
    const answer = mustQuery(bubble, ".chat-message-content");
    const panel = mustQuery(bubble, ".chat-reasoning-panel");
    const reasoningText = mustQuery(panel, ".chat-reasoning-text");

    expect(answer.textContent).toContain("Visible answer.");
    expect(answer.textContent).not.toContain("hidden model reasoning");
    expect(panel.open).toBe(false);
    expect(reasoningText.textContent).toBe("hidden model reasoning");
  });
});

describe("chat async ownership", () => {
  test("ignores a chat POST response that resolves after reset", async () => {
    A.setCaseId("case-post-reset");
    const post = deferred();
    global.fetch = jest.fn(() => post.promise);

    const sendPromise = startChat("Will be reset");
    expect(A.st.chat.run).toBe(true);

    A.resetChatState();
    post.resolve(await jsonResponse({ success: true }));
    await sendPromise;
    await flushPromises();

    const chatSources = window.__AIFT_TEST_OPEN_EVENT_SOURCES__
      .filter((source) => source.url.includes("/chat/stream"));
    expect(chatSources).toHaveLength(0);
    expect(A.el.chatThread.textContent).toContain("Chat history will appear here");
    expect(A.st.chat.run).toBe(false);
  });

  test("does not reconnect or render stale SSE after reset", async () => {
    jest.useFakeTimers();
    A.setCaseId("case-sse-reset");
    global.fetch = jest.fn(() => jsonResponse({ success: true }));

    A.el.chatInput.value = "Retry then reset";
    await A._sendChatMessage();

    const source = window.__AIFT_TEST_OPEN_EVENT_SOURCES__
      .find((entry) => entry.url === "/api/cases/case-sse-reset/chat/stream");
    expect(source).toBeTruthy();
    source.onerror();
    expect(A.st.chat.retry).not.toBeNull();

    A.resetChatState();
    jest.runOnlyPendingTimers();

    emit(source, { type: "token", content: "late token", sequence: 1 });
    const chatSources = window.__AIFT_TEST_OPEN_EVENT_SOURCES__
      .filter((entry) => entry.url.includes("/chat/stream"));
    expect(chatSources).toHaveLength(1);
    expect(A.el.chatThread.textContent).not.toContain("late token");
    jest.useRealTimers();
  });

  test("ignores old-case SSE events after a case switch", async () => {
    A.setCaseId("case-before-switch");
    global.fetch = jest.fn(() => jsonResponse({ success: true }));

    await submitChat("Switch cases");
    const source = window.__AIFT_TEST_OPEN_EVENT_SOURCES__
      .find((entry) => entry.url === "/api/cases/case-before-switch/chat/stream");
    expect(source).toBeTruthy();

    A.setCaseId("case-after-switch");
    emit(source, { type: "token", content: "old case text", sequence: 1 });
    emit(source, { type: "error", message: "old case error", sequence: 2 });

    expect(A.el.chatThread.textContent).toContain("Switch cases");
    expect(A.el.chatThread.textContent).not.toContain("old case text");
    expect(A.el.resultsMsg.textContent).not.toContain("old case error");
  });

  test("does not let stale history overwrite freshly streamed chat", async () => {
    A.setCaseId("case-history-owner");
    const history = deferred();
    global.fetch = jest.fn((url) => {
      if (String(url).endsWith("/chat/history")) return history.promise;
      if (String(url).endsWith("/chat")) return jsonResponse({ success: true });
      return Promise.reject(new Error(`Unexpected URL: ${url}`));
    });

    const historyPromise = A.loadChatHistory();
    await flushPromises();
    await submitChat("Fresh question");
    const source = window.__AIFT_TEST_OPEN_EVENT_SOURCES__
      .find((entry) => entry.url === "/api/cases/case-history-owner/chat/stream");
    expect(source).toBeTruthy();

    emit(source, { type: "token", content: "Fresh answer", sequence: 1 });
    emit(source, { type: "done", sequence: 2 });
    history.resolve(await jsonResponse({
      messages: [
        { role: "user", content: "Old question" },
        { role: "assistant", content: "Old answer" },
      ],
    }));
    await historyPromise;
    await flushPromises();

    expect(A.el.chatThread.textContent).toContain("Fresh question");
    expect(A.el.chatThread.textContent).toContain("Fresh answer");
    expect(A.el.chatThread.textContent).not.toContain("Old answer");
  });

  test("does not let stale clear-history response overwrite a new case", async () => {
    A.setCaseId("case-clear-old");
    A.el.chatThread.innerHTML = '<div class="chat-message-row"><div>Old message</div></div>';
    A.el.chatClear.disabled = false;
    window.confirm = jest.fn(() => true);
    const clearRequest = deferred();
    global.fetch = jest.fn(() => clearRequest.promise);

    A.el.chatClear.click();
    await flushPromises();
    expect(global.fetch).toHaveBeenCalledWith(
      "/api/cases/case-clear-old/chat/history",
      expect.objectContaining({ method: "DELETE" }),
    );

    A.setCaseId("case-clear-new");
    A.el.chatThread.innerHTML = '<div class="chat-message-row"><div>New case message</div></div>';
    expect(A.el.chatThread.textContent).toContain("New case message");
    expect(A.activeCaseId()).toBe("case-clear-new");
    expect(A.st.chat.clearOwner).toBeNull();
    clearRequest.resolve(await jsonResponse({ success: true }));
    await flushPromises();

    expect(A.st.chat.historyLoadedCaseId).not.toBe("case-clear-old");
  });

  test("chat_cancelled closes without retrying", async () => {
    jest.useFakeTimers();
    A.setCaseId("case-chat-cancelled");
    global.fetch = jest.fn(() => jsonResponse({ success: true }));

    await submitChat("Cancel stream");
    const source = window.__AIFT_TEST_OPEN_EVENT_SOURCES__
      .find((entry) => entry.url === "/api/cases/case-chat-cancelled/chat/stream");
    expect(source).toBeTruthy();

    emit(source, { type: "chat_cancelled", sequence: 1 });
    source.onerror();
    jest.runOnlyPendingTimers();

    const chatSources = window.__AIFT_TEST_OPEN_EVENT_SOURCES__
      .filter((entry) => entry.url.includes("/chat/stream"));
    expect(A.st.chat.run).toBe(false);
    expect(A.st.chat.retry).toBeNull();
    expect(chatSources).toHaveLength(1);
    jest.useRealTimers();
  });

  test("ignores directly dispatched events for a non-active case", () => {
    A.setCaseId("case-current");
    A.resetChatState();

    A._onChatEvent("case-old", { type: "token", content: "wrong case" });
    A._onChatEvent("case-old", { type: "error", message: "wrong error" });

    expect(A.el.chatThread.textContent).not.toContain("wrong case");
    expect(A.el.resultsMsg.textContent).not.toContain("wrong error");
  });
});
