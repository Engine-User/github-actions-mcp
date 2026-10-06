/* MCP UI — 3D wireframe centerpiece.
   Hand-rolled perspective renderer on a 2D canvas: real 3D rotation, projection,
   depth attenuation. No dependencies, so it runs from file:// with no server.

   Swapping in three.js means replacing this file only: keep mount(el, pins) and
   return { project(point) }. Nothing else in the page touches canvas internals. */

(function (global) {
  "use strict";

  var FOCAL = 620;        // camera focal length
  var CAM_Z = 6.4;        // camera distance on +z
  var NEAR = 1.2;
  var FAR = 15;
  var SPIN = 0.00042;     // auto-rotation per ms
  var DAMP = 0.1;         // pointer parallax easing

  /* ---------- geometry builders ---------- */

  function boxEdges(sx, sy, sz, braces) {
    var x = sx / 2, y = sy / 2, z = sz / 2;
    var v = [
      [-x, -y, -z], [x, -y, -z], [x, y, -z], [-x, y, -z],
      [-x, -y, z], [x, -y, z], [x, y, z], [-x, y, z]
    ];
    var e = [
      [0, 1], [1, 2], [2, 3], [3, 0],
      [4, 5], [5, 6], [6, 7], [7, 4],
      [0, 4], [1, 5], [2, 6], [3, 7]
    ];
    if (braces) {
      e.push([0, 6], [1, 7], [2, 4], [3, 5]);
    }
    return { verts: v, edges: e };
  }

  function octaEdges(r) {
    var v = [
      [r, 0, 0], [-r, 0, 0], [0, r, 0],
      [0, -r, 0], [0, 0, r], [0, 0, -r]
    ];
    var e = [
      [0, 2], [2, 1], [1, 3], [3, 0],
      [4, 2], [2, 5], [5, 1], [1, 4],
      [0, 4], [4, 3], [3, 5], [5, 0]
    ];
    return { verts: v, edges: e };
  }

  /* ---------- scene definition ----------
     One exploded MCP host: two clients, two servers, the transport links
     between them, and the grid plane they sit on. */

  var HOST = { pos: [0, 0.1, 0], geo: boxEdges(4.5, 2.9, 2.1), tone: "host" };

  var CLIENTS = [
    { id: "client-1", pos: [-0.95, -0.42, -0.25], geo: boxEdges(1.05, 0.72, 0.86, true), tone: "client" },
    { id: "client-2", pos: [0.95, -0.42, -0.25], geo: boxEdges(1.05, 0.72, 0.86, true), tone: "client" }
  ];

  var SERVERS = [
    { id: "server-stdio", pos: [-0.95, 0.82, 0.62], geo: boxEdges(0.9, 0.9, 0.9), tone: "server", spin: 0.4 },
    { id: "server-http", pos: [0.95, 0.82, 0.62], geo: octaEdges(0.62), tone: "server", spin: -0.4 }
  ];

  var LINKS = [
    { a: [-0.95, -0.06, -0.25], b: [-0.95, 0.42, 0.62], tone: "link" },
    { a: [0.95, -0.06, -0.25], b: [0.95, 0.42, 0.62], tone: "link" }
  ];

  var TONE = {
    host: "rgba(46,232,143,",
    client: "rgba(125,145,162,",
    server: "rgba(46,232,143,",
    link: "rgba(124,255,196,"
  };

  /* ---------- math ---------- */

  function rot(p, ax, ay) {
    var cosY = Math.cos(ay), sinY = Math.sin(ay);
    var x = p[0] * cosY + p[2] * sinY;
    var z = -p[0] * sinY + p[2] * cosY;
    var cosX = Math.cos(ax), sinX = Math.sin(ax);
    var y = p[1] * cosX - z * sinX;
    var z2 = p[1] * sinX + z * cosX;
    return [x, y, z2];
  }

  /* ---------- renderer ---------- */

  function mount(canvas, pins) {
    var ctx = canvas.getContext("2d", { alpha: true });
    if (!ctx) return { project: function () { return null; } };

    var w = 0, h = 0, dpr = 1;
    var ox = 0, oy = 0, ox0 = 0, oy0 = 0;
    var spin = 0, tilt = 0.0;
    var mx = 0, my = 0, tx = 0, ty = 0;
    var running = false, visible = true, onScreen = true;
    var last = 0;
    var reduce = global.matchMedia && global.matchMedia("(prefers-reduced-motion: reduce)").matches;

    function measure() {
      var r = canvas.getBoundingClientRect();
      dpr = Math.min(global.devicePixelRatio || 1, 2);
      w = Math.max(1, Math.round(r.width));
      h = Math.max(1, Math.round(r.height));
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      // Push the object right of the headline on wide screens, centre on narrow.
      ox0 = w * (w > 900 ? 0.68 : 0.5);
      oy0 = h * 0.5;
      ox = ox0;
      oy = oy0;
    }

    /* Project one world point. Also drives the DOM annotation pins. */
    function project(p) {
      var r = rot(p, tilt, spin);
      var d = r[2] + CAM_Z;
      if (d < NEAR) return null;
      var s = FOCAL / d;
      return {
        x: ox + r[0] * s,
        y: oy - r[1] * s,
        depth: d,
        scale: s
      };
    }

    function depthAlpha(d, boost) {
      var t = 1 - (d - NEAR) / (FAR - NEAR);
      if (t < 0) t = 0;
      if (t > 1) t = 1;
      return (0.1 + 0.9 * t * t) * (boost || 1);
    }

    function strokeToned(geo, off, alphaBase) {
      for (var i = 0; i < geo.edges.length; i++) {
        var e = geo.edges[i];
        var pa = geo.verts[e[0]], pb = geo.verts[e[1]];
        var a = project([pa[0] + off[0], pa[1] + off[1], pa[2] + off[2]]);
        var b = project([pb[0] + off[0], pb[1] + off[1], pb[2] + off[2]]);
        if (!a || !b) continue;
        var d = (a.depth + b.depth) / 2;
        ctx.strokeStyle = TONE[alphaBase.tone] + depthAlpha(d, alphaBase.gain) + ")";
        ctx.lineWidth = alphaBase.w;
        ctx.beginPath();
        ctx.moveTo(a.x, a.y);
        ctx.lineTo(b.x, b.y);
        ctx.stroke();
      }
      ctx.lineWidth = 1;
    }

    function drawGround() {
      var ext = 7, step = 1.15, i, t;
      ctx.strokeStyle = "rgba(46,232,143,0.07)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      for (i = -ext; i <= ext; i += step) {
        var ax = project([i, -1.72, -ext]);
        var bx = project([i, -1.72, ext]);
        if (ax && bx) { ctx.moveTo(ax.x, ax.y); ctx.lineTo(bx.x, bx.y); }
        var cx = project([-ext, -1.72, i]);
        var dx = project([ext, -1.72, i]);
        if (cx && dx) { ctx.moveTo(cx.x, cx.y); ctx.lineTo(dx.x, dx.y); }
      }
      ctx.stroke();

      // Fade the far half of the grid so the plane reads as a floor, not a wall.
      t = ctx.createLinearGradient(0, oy - h * 0.5, 0, h);
      t.addColorStop(0, "rgba(5,7,10,0.95)");
      t.addColorStop(0.45, "rgba(5,7,10,0)");
      ctx.fillStyle = t;
      ctx.fillRect(0, 0, w, h);
    }

    function drawPins() {
      if (!pins) return;
      for (var i = 0; i < pins.length; i++) {
        var pin = pins[i];
        var p = project(pin.at);
        if (!p || p.x < -160 || p.x > w + 160 || p.y < -60 || p.y > h + 60) {
          pin.el.classList.remove("in");
          continue;
        }
        pin.el.style.transform = "translate(-50%,-50%) translate(" +
          Math.round(p.x + pin.dx) + "px," + Math.round(p.y + pin.dy) + "px)";
        pin.el.classList.add("in");
      }
    }

    function frame(now) {
      var dt = Math.min(now - last || 16, 48);
      last = now;
      if (!reduce) {
        spin += SPIN * dt;
        mx += (tx - mx) * DAMP;
        my += (ty - my) * DAMP;
      }
      // Camera translation carries the horizontal parallax; rotation only tilts.
      ox = ox0 + mx * 30;
      oy = oy0 + my * 16;
      tilt = 0.34 + my * 0.16;

      ctx.clearRect(0, 0, w, h);

      var i, obj, local;
      ctx.save();

      // ground plane spins against the subject for a parallax read
      var sp = spin;
      spin = sp * 0.6;
      tilt = 0.9;
      drawGround();
      spin = sp;
      tilt = 0.34 + my * 0.16;

      for (i = 0; i < LINKS.length; i++) {
        var L = LINKS[i];
        var p1 = project(L.a);
        var p2 = project(L.b);
        if (!p1 || !p2) continue;
        var al = depthAlpha((p1.depth + p2.depth) / 2, 1);
        ctx.strokeStyle = TONE.link + al * 0.14 + ")";
        ctx.lineWidth = 5;
        ctx.beginPath();
        ctx.moveTo(p1.x, p1.y);
        ctx.lineTo(p2.x, p2.y);
        ctx.stroke();
        ctx.strokeStyle = TONE.link + al + ")";
        ctx.lineWidth = 1.1;
        ctx.beginPath();
        ctx.moveTo(p1.x, p1.y);
        ctx.lineTo(p2.x, p2.y);
        ctx.stroke();
        // travel pulse along each link
        var t = reduce ? 0.5 : ((now * 0.001 + i * 0.5) % 1);
        var px = p1.x + (p2.x - p1.x) * t;
        var py = p1.y + (p2.y - p1.y) * t;
        ctx.fillStyle = "rgba(124,255,196," + (0.85 * al) + ")";
        ctx.beginPath();
        ctx.arc(px, py, 2, 0, Math.PI * 2);
        ctx.fill();
      }
      ctx.lineWidth = 1;

      for (i = 0; i < CLIENTS.length; i++) {
        obj = CLIENTS[i];
        strokeToned(obj.geo, obj.pos, { tone: "client", gain: 0.72, w: 1 });
      }
      for (i = 0; i < SERVERS.length; i++) {
        obj = SERVERS[i];
        if (!reduce) local = spin * obj.spin * 40;
        else local = 0;
        var c = Math.cos(local), s = Math.sin(local);
        var v = obj.geo.verts.map(function (p) {
          return [p[0] * c + p[2] * s, p[1], -p[0] * s + p[2] * c];
        });
        strokeToned({ verts: v, edges: obj.geo.edges }, obj.pos, { tone: "server", gain: 1, w: 1.2 });
      }

      // host shell last: it reads as the containing envelope
      var hb = HOST.geo;
      var hv = hb.verts.map(function (p) {
        var c = Math.cos(-spin * 0.25), s = Math.sin(-spin * 0.25);
        return [p[0] * c + p[2] * s, p[1], -p[0] * s + p[2] * c];
      });
      strokeToned({ verts: hv, edges: hb.edges }, HOST.pos, { tone: "host", gain: 0.5, w: 1 });

      ctx.restore();
      drawPins();

      if (running) requestAnimationFrame(frame);
    }

    function play() {
      if (!running) { running = true; last = performance.now(); requestAnimationFrame(frame); }
    }

    function pause() { running = false; }

    function start() {
      if (!visible || !onScreen) return;
      if (reduce) { last = performance.now(); frame(last); return; }
      play();
    }

    /* pointer parallax, one damped target */
    function onMove(e) {
      var r = canvas.getBoundingClientRect();
      var px = (e.clientX - r.left) / r.width - 0.5;
      var py = (e.clientY - r.top) / r.height - 0.5;
      tx = Math.max(-1, Math.min(1, px * 2));
      ty = Math.max(-1, Math.min(1, py * 2));
    }

    function onLeave() { tx = 0; ty = 0; }

    if (global.ResizeObserver) new ResizeObserver(measure).observe(canvas);
    else global.addEventListener("resize", measure);

    if (global.IntersectionObserver) {
      new IntersectionObserver(function (entries) {
        onScreen = entries[0].isIntersecting;
        if (onScreen) start(); else pause();
      }, { threshold: 0.01 }).observe(canvas);
    }

    document.addEventListener("visibilitychange", function () {
      visible = !document.hidden;
      if (visible) start(); else pause();
    });

    if (!reduce && global.matchMedia) {
      var mq = global.matchMedia("(prefers-reduced-motion: reduce)");
      var onMq = function () { reduce = mq.matches; pause(); start(); };
      if (mq.addEventListener) mq.addEventListener("change", onMq);
    }

    global.addEventListener("pointermove", onMove, { passive: true });
    canvas.addEventListener("pointerleave", onLeave);

    measure();
    start();

    return { project: project, pause: pause, play: play };
  }

  global.MCPScene = { mount: mount };
})(window);