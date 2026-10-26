// Served as a separate file so the Content-Security-Policy can forbid inline scripts.
const log = document.getElementById("log");
const form = document.getElementById("form");
const input = document.getElementById("input");
const send = document.getElementById("send");

// The server keeps the conversation; the browser only remembers which session it belongs to.
let sessionId = null;

function addMessage(role, text, extraClass) {
  const el = document.createElement("div");
  el.className = `msg ${role}${extraClass ? " " + extraClass : ""}`;
  el.textContent = text; // never innerHTML: replies are model output
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
  return el;
}

// CloudFront signs requests to the Lambda Function URL (Origin Access Control), and Lambda only accepts
// signed POSTs whose body hash the viewer supplies in x-amz-content-sha256.
async function sha256Hex(text) {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

// Reads an application/x-ndjson response line by line as it streams in.
async function* readLines(response) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let newline;
    while ((newline = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, newline).trim();
      buffer = buffer.slice(newline + 1);
      if (line) yield JSON.parse(line);
    }
  }
  if (buffer.trim()) yield JSON.parse(buffer);
}

async function ask(text) {
  addMessage("user", text);
  const bubble = addMessage("assistant", "Thinking…", "pending");
  send.disabled = true;
  let reply = "";
  let ended = false;
  try {
    const body = JSON.stringify({ sessionId, message: text });
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "content-type": "application/json", "x-amz-content-sha256": await sha256Hex(body) },
      body,
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      const busy = res.status === 429 || res.status === 403;
      throw new Error(busy ? "Too many requests, please wait a few minutes." : data.error || `HTTP ${res.status}`);
    }
    for await (const event of readLines(res)) {
      if (event.sessionId) sessionId = event.sessionId;
      if (event.delta) {
        reply += event.delta;
        bubble.textContent = reply;
        bubble.classList.remove("pending");
        log.scrollTop = log.scrollHeight;
      }
      if (event.done) ended = Boolean(event.sessionEnded);
    }
    if (!reply) throw new Error("empty reply");
  } catch (err) {
    bubble.classList.remove("pending");
    bubble.textContent = reply ? `${reply}\n\n(Connection lost: ${err.message})` : `Couldn't get a reply: ${err.message}`;
  }
  if (ended) {
    input.disabled = true;
    return;
  }
  send.disabled = false;
  input.focus();
}

form.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text || send.disabled) return;
  input.value = "";
  ask(text);
});

input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    form.requestSubmit();
  }
});
