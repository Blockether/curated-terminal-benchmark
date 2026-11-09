// Filters, sorting, URL state and Pele diagrams for the CTB report. The pages read fine without this file.
(function () {
  "use strict";
  document.documentElement.classList.add("js");

  function cellValue(row, index, type) {
    var cell = row.children[index];
    var raw = cell ? (cell.getAttribute("data-v") || cell.textContent) : "";
    if (type === "num") {
      var n = parseFloat(raw);
      return isNaN(n) ? -Infinity : n;
    }
    return raw.toLowerCase();
  }

  function sortTable(table, index, direction) {
    var head = table.tHead.rows[0].children;
    var type = head[index].getAttribute("data-type") || "text";
    var body = table.tBodies[0];
    var rows = Array.prototype.slice.call(body.rows);
    var factor = direction === "ascending" ? 1 : -1;
    rows.sort(function (a, b) {
      var x = cellValue(a, index, type), y = cellValue(b, index, type);
      return x < y ? -factor : x > y ? factor : 0;
    });
    rows.forEach(function (row) { body.appendChild(row); });
    Array.prototype.forEach.call(head, function (cell, i) {
      if (i === index) cell.setAttribute("aria-sort", direction);
      else cell.removeAttribute("aria-sort");
    });
  }

  function setParams(changes) {
    var url = new URL(window.location.href);
    Object.keys(changes).forEach(function (key) {
      if (changes[key]) url.searchParams.set(key, changes[key]);
      else url.searchParams.delete(key);
    });
    window.history.replaceState(null, "", url);
  }

  function setupSorting() {
    var tables = document.querySelectorAll("table.sortable");
    var params = new URLSearchParams(window.location.search);
    Array.prototype.forEach.call(tables, function (table, tableIndex) {
      var key = table.hasAttribute("data-trials") ? "sort" : "sort" + tableIndex;
      var buttons = table.tHead.querySelectorAll("button.sort");
      Array.prototype.forEach.call(buttons, function (button) {
        var cell = button.parentElement;
        var index = Array.prototype.indexOf.call(cell.parentElement.children, cell);
        button.addEventListener("click", function () {
          var type = cell.getAttribute("data-type");
          var current = cell.getAttribute("aria-sort");
          var direction = current ? (current === "ascending" ? "descending" : "ascending")
            : (type === "num" ? "descending" : "ascending");
          sortTable(table, index, direction);
          setParams(Object.fromEntries([[key, index + (direction === "ascending" ? "a" : "d")]]));
        });
      });
      var saved = params.get(key);
      if (saved && /^\d+[ad]$/.test(saved)) {
        var column = parseInt(saved, 10);
        if (column < table.tHead.rows[0].children.length) {
          sortTable(table, column, saved.slice(-1) === "a" ? "ascending" : "descending");
        }
      }
    });
  }

  function setupFilters() {
    var form = document.querySelector("[data-filters]");
    var table = document.querySelector("[data-trials]");
    if (!form || !table) return;
    var count = document.querySelector("[data-count]");
    var empty = document.querySelector("[data-empty]");
    var names = ["q", "agent", "round", "outcome"];
    var params = new URLSearchParams(window.location.search);
    names.forEach(function (name) {
      var value = params.get(name);
      if (value !== null && form.elements[name]) form.elements[name].value = value;
    });

    function apply() {
      var q = form.elements.q.value.trim().toLowerCase();
      var agent = form.elements.agent.value;
      var round = form.elements.round.value;
      var outcome = form.elements.outcome.value;
      var rows = table.tBodies[0].rows, shown = 0;
      Array.prototype.forEach.call(rows, function (row) {
        var match = (!q || row.getAttribute("data-text").indexOf(q) !== -1) &&
          (!agent || row.getAttribute("data-agent") === agent) &&
          (!round || row.getAttribute("data-round") === round) &&
          (!outcome || row.getAttribute("data-outcome") === outcome);
        row.hidden = !match;
        if (match) shown += 1;
      });
      count.textContent = shown === rows.length ? rows.length + " trials"
        : "Showing " + shown + " of " + rows.length + " trials";
      empty.hidden = shown !== 0;
      table.parentElement.hidden = shown === 0;
      var clear = document.querySelector("[data-clear]");
      if (clear) clear.hidden = !(q || agent || round || outcome);
      setParams({ q: q, agent: agent, round: round, outcome: outcome });
    }

    form.addEventListener("submit", function (event) { event.preventDefault(); apply(); });
    form.addEventListener("input", apply);
    form.addEventListener("change", apply);
    apply();
  }

  function setupTranscript() {
    var list = document.querySelector("[data-events]");
    if (!list) return;
    var links = document.querySelectorAll("[data-show]");
    var count = document.querySelector("[data-event-count]");
    var empty = document.querySelector("[data-empty]");
    var items = list.querySelectorAll("li.ev");
    if (!items.length) return;

    function show(key) {
      var shown = 0;
      Array.prototype.forEach.call(items, function (item) {
        var match = key === "all" || (key === "errors" ? item.getAttribute("data-error") === "1"
          : item.getAttribute("data-group") === key);
        item.hidden = !match;
        if (match) shown += 1;
      });
      Array.prototype.forEach.call(links, function (link) {
        if (link.getAttribute("data-show") === key) link.setAttribute("aria-current", "true");
        else link.removeAttribute("aria-current");
      });
      count.textContent = key === "all" ? items.length.toLocaleString("en") + " events"
        : "Showing " + shown.toLocaleString("en") + " of " + items.length.toLocaleString("en") + " events";
      empty.hidden = shown !== 0 || items.length === 0;
    }

    Array.prototype.forEach.call(links, function (link) {
      link.addEventListener("click", function (event) {
        event.preventDefault();
        var key = link.getAttribute("data-show");
        show(key);
        setParams({ show: key === "all" ? "" : key });
      });
    });
    var key = new URLSearchParams(window.location.search).get("show") || "all";
    var known = Array.prototype.some.call(links, function (l) { return l.getAttribute("data-show") === key; });
    show(known ? key : "all");
    var target = window.location.hash && document.getElementById(window.location.hash.slice(1));
    if (target && target.hidden) { show("all"); target.scrollIntoView(); }
  }

  function setupTabs() {
    var tabs = Array.prototype.slice.call(document.querySelectorAll("[data-tab]"));
    var panels = Array.prototype.slice.call(document.querySelectorAll("[data-tab-panel]"));
    if (!tabs.length) return;

    function select(key, focus) {
      tabs.forEach(function (tab) {
        var on = tab.getAttribute("data-tab") === key;
        tab.setAttribute("aria-selected", on ? "true" : "false");
        tab.tabIndex = on ? 0 : -1;
        if (on && focus) tab.focus();
      });
      panels.forEach(function (panel) { panel.hidden = panel.getAttribute("data-tab-panel") !== key; });
    }

    tabs.forEach(function (tab, i) {
      tab.addEventListener("click", function () {
        var key = tab.getAttribute("data-tab");
        select(key, false);
        setParams({ tab: key === "implementation" ? "" : key });
      });
      tab.addEventListener("keydown", function (event) {
        var step = event.key === "ArrowRight" ? 1 : event.key === "ArrowLeft" ? -1 : 0;
        if (!step) return;
        event.preventDefault();
        var next = tabs[(i + step + tabs.length) % tabs.length];
        select(next.getAttribute("data-tab"), true);
        setParams({ tab: next.getAttribute("data-tab") === "implementation" ? "" : next.getAttribute("data-tab") });
      });
    });
    var key = new URLSearchParams(window.location.search).get("tab");
    var target = window.location.hash && document.getElementById(window.location.hash.slice(1));
    var holder = target && target.closest && target.closest("[data-tab-panel]");
    if (holder) key = holder.getAttribute("data-tab-panel");
    var known = tabs.some(function (t) { return t.getAttribute("data-tab") === key; });
    select(known ? key : "implementation", false);
    if (holder) target.scrollIntoView();
  }

  function setupComparison() {
    var picker = document.querySelector("[data-compare-picker]");
    if (!picker) return;
    var inputs = Array.from(picker.querySelectorAll("input[name=compare]"));
    var panels = Array.from(document.querySelectorAll("[data-comparison-panel]"));
    var status = document.querySelector("[data-comparison-status]");
    var mode = document.querySelector("[data-comparison-mode]");
    var params = new URLSearchParams(location.search);
    var requested = params.has("compare") ? params.get("compare").split(",")
      : panels.length ? JSON.parse(panels[0].dataset.comparisonPanel) : [];
    inputs.forEach(function (input) { input.checked = requested.includes(input.value); });
    mode.value = params.get("mode") === "normalized" || params.get("basis") === "solved" ? "normalized" : "general";
    function apply() {
      var selected = inputs.filter(function (input) { return input.checked; }).map(function (input) { return input.value; }).sort();
      var key = JSON.stringify(selected), shown = 0;
      panels.forEach(function (panel) {
        panel.hidden = JSON.stringify(JSON.parse(panel.dataset.comparisonPanel)) !== key || panel.dataset.mode !== mode.value;
        if (!panel.hidden) shown += 1;
      });
      status.textContent = selected.length < 2 ? "Choose at least two agents to compare."
        : !shown ? "No shared successful tasks with matching task content and model for this selection."
        : "Comparing " + selected.length + " agents · " + (mode.value === "general"
          ? "General: all attempts, including failures and extra tasks."
          : "Normalized: only tasks every selected agent passed.");
      status.dataset.state = selected.length < 2 ? "idle" : shown ? "ready" : "unmatched";
      var url = new URL(location.href);
      url.searchParams.set("compare", selected.join(","));
      url.searchParams.set("mode", mode.value);
      url.searchParams.delete("basis");
      history.replaceState(null, "", url);
      setupDiagrams();
    }
    picker.addEventListener("change", apply);
    mode.addEventListener("change", apply);
    window.addEventListener("popstate", function () {
      var values = (new URLSearchParams(location.search).get("compare") || "").split(",");
      inputs.forEach(function (input) { input.checked = values.includes(input.value); });
      var restored = new URLSearchParams(location.search);
      mode.value = restored.get("mode") === "normalized" || restored.get("basis") === "solved" ? "normalized" : "general";
      apply();
    });
    apply();
  }

  function setupDiagrams() {
    var blocks = document.querySelectorAll("pre.mermaid");
    if (!window.pele || !blocks.length) return;
    Array.prototype.forEach.call(blocks, function (block) {
      if (block.hasAttribute("data-drawn") || block.closest("[hidden]")) return;
      var source = block.textContent;
      try {
        var width = Math.max(320, block.clientWidth);
        window.pele.mount(block, source, { maxWidth: width, config: { gantt: { useWidth: width } } });
        block.setAttribute("data-drawn", "");
      } catch (error) {
        block.textContent = source;
        block.setAttribute("data-failed", "");
      }
    });
  }

  setupComparison();
  setupSorting();
  setupFilters();
  setupTranscript();
  setupTabs();
  document.addEventListener("ctb:transcript-loaded", setupTranscript);
  setupDiagrams();
})();
