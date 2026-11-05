"use strict";
(async () => {
  const status = document.getElementById("status");
  const root = new URL(".", document.currentScript.src);
  try {
    const id = document.querySelector("[data-result-id]")?.dataset.resultId || new URLSearchParams(location.search).get("id");
    if (!/^[0-9a-f]{64}$/.test(id || "")) throw Error("Invalid trial");
    async function limited(stream, limit) {
      const reader = stream.getReader();
      const chunks = []; let size = 0;
      while (true) {
        const {done, value} = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > limit) { await reader.cancel(); throw Error("Size limit exceeded"); }
        chunks.push(value);
      }
      return new Uint8Array(await new Blob(chunks).arrayBuffer());
    }
    const response = await fetch(new URL("transcripts.json", root), {credentials: "omit"});
    if (!response.ok) throw Error("Manifest unavailable");
    const manifest = JSON.parse(new TextDecoder("utf-8", {fatal: true}).decode(await limited(response.body, 65536)));
    const record = manifest.transcripts?.[id];
    const bounded = (n, max) => Number.isInteger(n) && n > 0 && n <= max;
    if (manifest.schema !== 1 || !record || !bounded(record.bytes, 2097152) ||
        !bounded(record.expanded_bytes, 16777216) || !bounded(record.events, 20000) ||
        typeof record.sha256 !== "string" || !/^[0-9a-f]{64}$/.test(record.sha256)) throw Error("Invalid manifest");
    const trialResponse = await fetch(new URL(`transcripts/${id}.json.gz`, root), {credentials: "omit"});
    if (!trialResponse.ok) throw Error("Transcript unavailable");
    const compressed = await limited(trialResponse.body, Math.min(record.bytes, 2097152));
    const hash = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", compressed)), b => b.toString(16).padStart(2, "0")).join("");
    if (hash !== record.sha256 || compressed.length !== record.bytes) throw Error("Transcript hash mismatch");
    const bytes = await limited(new Blob([compressed]).stream().pipeThrough(new DecompressionStream("gzip")), Math.min(record.expanded_bytes, 16777216));
    if (bytes.length !== record.expanded_bytes) throw Error("Transcript size mismatch");
    const data = JSON.parse(new TextDecoder("utf-8", {fatal: true}).decode(bytes));
    if (data.schema !== 1 || data.result_sha256 !== id || !Array.isArray(data.events) || data.events.length !== record.events) throw Error("Invalid transcript");
    const fragment = document.createDocumentFragment();
    data.events.forEach((event, i) => {
      if (!event || !["user", "assistant", "reasoning", "call", "tool", "command", "result"].includes(event.kind)) throw Error("Invalid event");
      for (const key of ["name", "text", "input", "output"]) {
        if (event[key] !== null && (typeof event[key] !== "string" || event[key].length > 1000000)) throw Error("Invalid event text");
      }
      const item = document.createElement("li");
      item.id = `e-${i}`;
      item.className = `ev ev-${event.kind}`;
      item.dataset.group = ["tool", "command"].includes(event.kind) ? "tools" : event.kind === "call" ? "calls" : "messages";
      const head = document.createElement("div"); head.className = "ev-head";
      const link = document.createElement("a"); link.className = "ev-kind"; link.href = `#e-${i}`;
      link.textContent = {user: "Prompt", assistant: "Assistant", reasoning: "Reasoning", call: "Provider call", tool: "Tool", command: "Command", result: "Run result"}[event.kind];
      head.append(link);
      if (event.name) {
        const name = document.createElement("span"); name.className = "ev-name mono";
        name.textContent = event.name; head.append(name);
      }
      item.append(head);
      const body = document.createElement("section"); body.className = "ev-body";
      for (const key of ["text", "input", "output"]) {
        if (event[key] !== null) {
          const pre = document.createElement("pre"); pre.textContent = event[key];
          if (key === "text") { pre.className = "ev-text"; body.append(pre); }
          else {
            const detail = document.createElement("details"); detail.open = event[key].length <= 1200;
            const summary = document.createElement("summary"); summary.textContent = key === "input" ? "Input" : "Output";
            detail.append(summary, pre); body.append(detail);
          }
        }
      }
      item.append(body); fragment.append(item);
    });
    document.getElementById("events").replaceChildren(fragment);
    document.dispatchEvent(new Event("ctb:transcript-loaded"));
    status.textContent = `${data.events.length} events · ${(compressed.length / 1024).toFixed(0)} KB downloaded`;
  } catch (_) { status.textContent = "Transcript could not be verified or loaded. Use a current browser with gzip streaming support."; }
})();
