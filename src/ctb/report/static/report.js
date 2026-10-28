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

  function setupDiagrams() {
    var blocks = document.querySelectorAll("pre.mermaid");
    if (!window.pele || !blocks.length) return;
    Array.prototype.forEach.call(blocks, function (block) {
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

  setupSorting();
  setupFilters();
  setupTranscript();
  setupDiagrams();
})();
