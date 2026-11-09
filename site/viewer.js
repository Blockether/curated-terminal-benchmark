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
    const response = await fetch(new URL("transcripts.json", root), {credentials: "same-origin"});
    if (!response.ok) throw Error("Manifest unavailable");
    const manifest = JSON.parse(new TextDecoder("utf-8", {fatal: true}).decode(await limited(response.body, 65536)));
    const record = manifest.transcripts?.[id];
    const bounded = (n, max) => Number.isInteger(n) && n > 0 && n <= max;
    if (manifest.schema !== 3 || !record || !bounded(record.bytes, 2097152) ||
        !bounded(record.expanded_bytes, 16777216) || !bounded(record.events, 20000) ||
        !(Number.isInteger(record.checks) && record.checks >= 0 && record.checks <= 20000) ||
        typeof record.sha256 !== "string" || !/^[0-9a-f]{64}$/.test(record.sha256)) throw Error("Invalid manifest");
    const trialResponse = await fetch(new URL(`transcripts/${id}.json.gz`, root), {credentials: "same-origin"});
    if (!trialResponse.ok) throw Error("Transcript unavailable");
    const compressed = await limited(trialResponse.body, Math.min(record.bytes, 2097152));
    const hash = Array.from(new Uint8Array(await crypto.subtle.digest("SHA-256", compressed)), b => b.toString(16).padStart(2, "0")).join("");
    if (hash !== record.sha256 || compressed.length !== record.bytes) throw Error("Transcript hash mismatch");
    const bytes = await limited(new Blob([compressed]).stream().pipeThrough(new DecompressionStream("gzip")), Math.min(record.expanded_bytes, 16777216));
    if (bytes.length !== record.expanded_bytes) throw Error("Transcript size mismatch");
    const data = JSON.parse(new TextDecoder("utf-8", {fatal: true}).decode(bytes));
    if (data.schema !== 3 || data.result_sha256 !== id || !Array.isArray(data.events) || data.events.length !== record.events ||
        !Array.isArray(data.verification) || data.verification.length !== record.checks) throw Error("Invalid transcript");
    const fragment = document.createDocumentFragment();
    data.events.forEach((event, i) => {
      if (!event || !["user", "assistant", "reasoning", "call", "tool", "command", "result"].includes(event.kind)) throw Error("Invalid event");
      const text = value => value === null || (typeof value === "string" && value.length <= 1000000);
      if (!["name", "text", "input", "output"].every(key => text(event[key]))) throw Error("Invalid event text");
      if (!Array.isArray(event.commands) || event.commands.length > 1000 ||
          !event.commands.every(c => c && ["name", "input", "output"].every(key => text(c[key])))) throw Error("Invalid event commands");
      const item = document.createElement("li");
      item.id = `e-${i}`;
      item.className = `ev ev-${event.kind}`;
      item.dataset.group = ["tool", "command"].includes(event.kind) ? "tools" : event.kind === "call" ? "calls" : "messages";
      const head = document.createElement("div"); head.className = "ev-head";
      const link = document.createElement("a"); link.className = "ev-kind"; link.href = `#e-${i}`;
      // One label: the tool or record name when there is one, else the kind.
      const kind = {user: "Prompt", assistant: "Assistant", reasoning: "Reasoning", call: "Model call", tool: "Tool", command: "Command", result: "Run result"}[event.kind];
      link.textContent = event.name || kind; link.title = kind;
      if (event.name && ["tool", "command"].includes(event.kind)) link.classList.add("mono");
      head.append(link);
      item.append(head);
      const body = document.createElement("section"); body.className = "ev-body";
      const block = (label, value, open) => {
        const detail = document.createElement("details"); detail.open = open && value.length <= 1200;
        const summary = document.createElement("summary"); summary.textContent = label;
        const pre = document.createElement("pre"); pre.textContent = value;
        detail.append(summary, pre); return detail;
      };
      // Commands that this tool call ran come first; the script and its raw result stay closed.
      const nested = event.commands.length > 0;
      if (nested) {
        const steps = document.createElement("ol"); steps.className = "ev-steps";
        for (const c of event.commands) {
          const step = document.createElement("li"); step.className = "ev-step";
          const stepHead = document.createElement("div"); stepHead.className = "ev-head";
          const name = document.createElement("span"); name.className = "ev-kind mono"; name.textContent = c.name || "Command";
          stepHead.append(name); step.append(stepHead);
          if (c.input !== null && c.input !== "") step.append(block("Command", c.input, true));
          if (c.output !== null && c.output !== "") step.append(block("Output", c.output, true));
          steps.append(step);
        }
        body.append(steps);
      }
      for (const key of ["text", "input", "output"]) {
        if (event[key] !== null) {
          const pre = document.createElement("pre"); pre.textContent = event[key];
          if (key === "text") {
            // Markdown **bold**, as model messages and summaries write it; the rest stays plain text.
            pre.className = "ev-text"; pre.textContent = ""; let last = 0;
            for (const match of event.text.matchAll(/\*\*([^\n]+?)\*\*/g)) {
              const strong = document.createElement("strong"); strong.textContent = match[1];
              pre.append(event.text.slice(last, match.index), strong); last = match.index + match[0].length;
            }
            pre.append(event.text.slice(last)); body.append(pre);
          } else {
            const label = key === "input" ? (nested ? "Script" : "Input") : (nested ? "Script output" : "Output");
            body.append(block(label, event[key], !nested));
          }
        }
      }
      if (body.childElementCount) item.append(body);
      fragment.append(item);
    });
    document.getElementById("events").replaceChildren(fragment);
    const statuses = ["passed", "failed", "skipped", "pending", "other"];
    const duration = ms => ms < 1000 ? `${ms.toFixed(0)} ms` : ms < 60000 ? `${(ms / 1000).toFixed(1)} s`
      : (t => t < 3600 ? `${Math.floor(t / 60)} min ${String(t % 60).padStart(2, "0")} s`
        : `${Math.floor(t / 3600)} h ${String(Math.floor(t % 3600 / 60)).padStart(2, "0")} min`)(Math.round(ms / 1000));
    const chip = status => {
      const span = document.createElement("span");
      span.className = `chip ${status === "passed" ? "chip-passed" : status === "failed" ? "chip-error" : "chip-failed"}`;
      span.textContent = status.charAt(0).toUpperCase() + status.slice(1); return span;
    };
    const time = ms => { const span = document.createElement("span"); span.className = "mono duration"; span.textContent = duration(ms); return span; };
    const checks = document.createDocumentFragment();
    let quiet = [];
    const flush = () => {
      if (!quiet.length) return;
      const item = document.createElement("li"); item.className = "ev ev-tests";
      const detail = document.createElement("details"); const summary = document.createElement("summary");
      summary.textContent = `${quiet.length.toLocaleString("en")} tests without a failure`;
      const list = document.createElement("ul"); list.className = "test-names";
      for (const test of quiet) {
        const row = document.createElement("li"); const name = document.createElement("span");
        name.className = "mono"; name.textContent = test.name; row.append(name, " ", chip(test.status));
        if (test.ms !== null) row.append(" ", time(test.ms));
        list.append(row);
      }
      detail.append(summary, list); item.append(detail); checks.append(item); quiet = [];
    };
    data.verification.forEach((entry, i) => {
      if (!entry || !["suite", "test", "log"].includes(entry.kind)) throw Error("Invalid check");
      if (typeof entry.name !== "string" || !entry.name || entry.name.length > 10000) throw Error("Invalid check name");
      if (entry.text !== null && (typeof entry.text !== "string" || entry.text.length > 1000000)) throw Error("Invalid check text");
      if (entry.kind === "log" ? entry.status !== null : !statuses.includes(entry.status)) throw Error("Invalid check status");
      if (entry.ms !== null && !(Number.isInteger(entry.ms) && entry.ms >= 0 && entry.ms <= 1e9)) throw Error("Invalid check time");
      if (entry.kind === "test" && entry.status !== "failed") { quiet.push(entry); return; }
      if (entry.kind !== "test") flush();
      const failed = entry.status === "failed";
      const item = document.createElement("li");
      item.id = `v-${i}`; item.className = `ev ev-${entry.kind}${failed ? " ev-error" : ""}`;
      const head = document.createElement("div"); head.className = "ev-head";
      const link = document.createElement("a"); link.className = "ev-kind"; link.href = `#v-${i}`;
      link.textContent = entry.name; link.title = {suite: "Suite", test: "Test", log: "Log"}[entry.kind];
      if (entry.kind !== "suite") link.classList.add("mono");
      const meta = document.createElement("span"); meta.className = "ev-meta";
      if (entry.status !== null) meta.append(chip(entry.status));
      if (entry.ms !== null) {
        const took = document.createElement("span"); took.className = "fact duration"; took.title = "How long this ran, recorded";
        const label = document.createElement("span"); label.className = "fact-k"; label.textContent = "Took";
        const value = time(entry.ms); value.className = "mono"; took.append(label, " ", value); meta.append(took);
      }
      head.append(link, meta); item.append(head);
      if (entry.text !== null) {
        const body = document.createElement("section"); body.className = "ev-body";
        const pre = document.createElement("pre"); pre.textContent = entry.text;
        if (entry.kind === "suite") { pre.className = "ev-text"; body.append(pre); }
        else {
          const detail = document.createElement("details"); detail.open = failed || entry.text.length <= 1200;
          if (failed) detail.className = "is-error";
          const summary = document.createElement("summary");
          summary.textContent = entry.kind === "log" ? "Output" : failed ? "Failure" : "Details";
          detail.append(summary, pre); body.append(detail);
        }
        item.append(body);
      }
      checks.append(item);
    });
    flush();
    if (!data.verification.length) {
      const empty = document.createElement("p"); empty.className = "empty"; empty.textContent = "No verifier output was recorded.";
      checks.append(empty);
    }
    document.getElementById("checks").replaceChildren(checks);
    document.dispatchEvent(new Event("ctb:transcript-loaded"));
    status.textContent = `${data.events.length} events · ${data.verification.length} checks · ${(compressed.length / 1024).toFixed(0)} KB downloaded`;
  } catch (_) { status.textContent = "Transcript could not be verified or loaded. Use a current browser with gzip streaming support."; }
})();
