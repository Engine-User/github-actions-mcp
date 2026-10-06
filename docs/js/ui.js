/* MCP UI — page behaviour: scrollspy, live filter, copy buttons, 3D pin wiring.
   Classic script (no ES module) so the page works from file:// without a server. */

(function () {
  "use strict";

  var PINS = [
    { sel: ".pin--host", at: [0, 1.72, 0], dx: 0, dy: -14 },
    { sel: ".pin--stdio", at: [-0.95, 1.44, 0.62], dx: -64, dy: -12 },
    { sel: ".pin--http", at: [0.95, 1.44, 0.62], dx: 64, dy: -12 },
    { sel: ".pin--client", at: [-0.95, -0.44, -0.25], dx: -58, dy: 30 }
  ];

  /* ---------- 3D ---------- */

  var canvas = document.getElementById("scene");
  if (canvas && window.MCPScene) {
    var pins = PINS.map(function (p) {
      return { el: document.querySelector(p.sel), at: p.at, dx: p.dx, dy: p.dy };
    }).filter(function (p) { return !!p.el; });
    window.MCPScene.mount(canvas, pins);
  }

  /* ---------- scrollspy ---------- */

  var links = Array.prototype.slice.call(document.querySelectorAll(".rail a[href^='#']"));
  var byId = {};
  links.forEach(function (a) { byId[a.getAttribute("href").slice(1)] = a; });

  var sections = Object.keys(byId)
    .map(function (id) { return document.getElementById(id); })
    .filter(Boolean);

  function markActive(id) {
    links.forEach(function (a) { a.classList.toggle("on", a.getAttribute("href") === "#" + id); });
  }

  if ("IntersectionObserver" in globalThis && sections.length) {
    var seen = {};
    var spy = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) { seen[e.target.id] = e.isIntersecting ? e.intersectionRatio : 0; });
      var best = null, bestVal = 0;
      Object.keys(seen).forEach(function (id) { if (seen[id] > bestVal) { bestVal = seen[id]; best = id; } });
      if (best) markActive(best);
    }, { rootMargin: "-15% 0px -60% 0px", threshold: [0, 0.15, 0.4, 0.75] });
    sections.forEach(function (s) { spy.observe(s); });
  }

  /* ---------- copy buttons ---------- */

  function legacyCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.cssText = "position:fixed;top:-1000px;opacity:0";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch (e) { /* clipboard unavailable */ }
    document.body.removeChild(ta);
  }

  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) {
      return navigator.clipboard.writeText(text);
    }
    legacyCopy(text);
    return Promise.resolve();
  }

  document.addEventListener("click", function (e) {
    var btn = e.target.closest ? e.target.closest(".copy") : null;
    if (!btn) return;
    var block = btn.closest(".code");
    var code = block && block.querySelector("pre");
    if (!code) return;
    copyText(code.innerText).then(function () {
      btn.textContent = "copied";
      btn.classList.add("done");
      setTimeout(function () {
        btn.textContent = "copy";
        btn.classList.remove("done");
      }, 1400);
    });
  });

  /* ---------- live filter ---------- */

  var input = document.getElementById("q");
  var count = document.getElementById("qcount");
  var units = Array.prototype.slice.call(document.querySelectorAll("[data-searchable]"));
  var wrappers = Array.prototype.slice.call(document.querySelectorAll("[data-group]"));

  var corpus = units.map(function (el) {
    return { el: el, text: (el.textContent || "").toLowerCase() };
  });

  function run() {
    var q = (input.value || "").trim().toLowerCase();
    var hits = 0;

    corpus.forEach(function (row) {
      var match = !q || row.text.indexOf(q) !== -1;
      row.el.classList.toggle("hid", !match);
      if (match && q) hits++;
    });

    // Drop whole sections once nothing inside them matches.
    wrappers.forEach(function (g) {
      var inner = g.querySelectorAll("[data-searchable]");
      var visible = 0;
      Array.prototype.forEach.call(inner, function (el) {
        if (!el.classList.contains("hid")) visible++;
      });
      var hasHead = g.querySelector("header");
      if (hasHead) hasHead.classList.toggle("hid", q && visible === 0);
      g.classList.toggle("hid", q && visible === 0);
    });

    // Keep the rail honest about what is still on screen.
    links.forEach(function (a) {
      var sec = document.getElementById(a.getAttribute("href").slice(1));
      a.classList.toggle("hid", !!(q && sec && sec.classList.contains("hid")));
    });

    if (count) {
      count.textContent = q ? hits + "/" : "";
      count.style.display = q ? "block" : "none";
    }
  }

  if (input) {
    input.addEventListener("input", run);
    input.addEventListener("keydown", function (e) {
      if (e.key === "Escape") { input.value = ""; run(); input.blur(); }
    });

    document.addEventListener("keydown", function (e) {
      var tag = (e.target.tagName || "").toLowerCase();
      var typing = tag === "input" || tag === "textarea" || e.target.isContentEditable;
      if (e.key === "/" && !typing) { e.preventDefault(); input.focus(); input.select(); }
    });
  }

  /* ---------- stagger reveal on scroll ---------- */

  if ("IntersectionObserver" in globalThis && !window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    var reveal = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (!e.isIntersecting) return;
        e.target.animate(
          [{ opacity: 0, transform: "translateY(10px)" }, { opacity: 1, transform: "none" }],
          { duration: 420, easing: "cubic-bezier(.22,.61,.36,1)", fill: "both" }
        );
        reveal.unobserve(e.target);
      });
    }, { rootMargin: "0px 0px -8% 0px" });
    document.querySelectorAll("section.block > header, .grid, .diag, .tbl-wrap, .code, .note, .steps").forEach(function (el) {
      reveal.observe(el);
    });
  }
})();