// SPDX-License-Identifier: AGPL-3.0-or-later
// SPDX-FileCopyrightText: 2026 zero-below
//
// pdf_vector_redaction.js — client-side port of scripts/pdf_vector_redaction.py.
// Detects vector-path ("outlined") text hidden under an opaque box: a failed-redaction
// style that leaves no font-based text layer for a text extractor to catch. Walks each
// page's pdf.js operator list directly (no canvas render needed to detect), tracking the
// CTM/fill-color/fill-alpha graphics state by hand, exactly as a PDF content-stream
// interpreter would. A solid, opaque, dark axis-aligned rectangle is a redaction-box
// candidate; any *other* dark, filled path mostly inside it, and not itself a bare
// rectangle, is candidate hidden content (outlined glyphs are compound line/curve paths,
// never a single "re" operator). When several such paths cluster in a box, it's flagged.
//
// Recovery: the flagged shapes are ordinary vector paths, so they're redrawn — the
// covering box omitted — onto an offscreen canvas and returned as a PNG data URL. No
// OCR is run client-side; a human looks at the reconstructed image. renderInContext
// does the same on top of a normal render of the whole page, so the recovered content
// reads alongside the rest of its row.
//
// Tunables mirror scripts/pdf_vector_redaction.py so the two checks agree.

(function (global) {
  "use strict";

  var BOX_MAX_LUM = 0.3;
  var BOX_MIN_OPACITY = 0.9;
  var BOX_MIN_W = 6.0;
  var BOX_MIN_H = 4.0;
  // A redaction box is one of two shapes: a LINE (a field or short phrase — wide relative
  // to its height) or a COLUMN (an entire field redacted down every row of a table — tall
  // relative to its width, up to a full page). What it is never shaped like is a paragraph
  // block, a table header row, or a page border: those are wide AND tall/blocky at once.
  // Tuned against two real SMPD audit PDF pages (a 259x203pt paragraph block; a 28x500pt
  // genuinely-solid redacted column) plus the one synthetic true positive — a first pass,
  // not a proven bound. Mirrors scripts/pdf_vector_redaction.py.
  var LINE_MAX_W = 400.0;
  var LINE_MAX_H = 30.0;
  var COLUMN_MAX_W = 60.0;
  var COLUMN_MAX_H = 2000.0;

  function plausibleRedactionShape(w, h) {
    return (w <= LINE_MAX_W && h <= LINE_MAX_H) || (w <= COLUMN_MAX_W && h <= COLUMN_MAX_H);
  }

  var SHAPE_MAX_LUM = 0.4;
  var SHAPE_MAX_AREA_FRAC = 0.25;
  var SHAPE_MIN_CONTAINMENT = 0.8;

  var MIN_SHAPES_TO_FLAG = 3;
  // Second, independent guard: even a full-column redaction down a dense table page is
  // at most a few hundred glyphs, not thousands.
  var MAX_SHAPES_TO_FLAG = 1000;

  function lum(r, g, b) { return (0.299 * r + 0.587 * g + 0.114 * b) / 255; }

  // pdf.js Util.transform(m1, m2): m2 applied locally first, then m1 — the standard
  // "cm" composition (new_ctm = old_ctm ∘ m).
  function mulMat(a, b) {
    return [
      a[0] * b[0] + a[2] * b[1], a[1] * b[0] + a[3] * b[1],
      a[0] * b[2] + a[2] * b[3], a[1] * b[2] + a[3] * b[3],
      a[0] * b[4] + a[2] * b[5] + a[4], a[1] * b[4] + a[3] * b[5] + a[5]
    ];
  }
  function applyMat(m, x, y) {
    return [m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]];
  }

  function bboxOfMinMax(mm, ctm) {
    // mm = [xmin, xmax, ymin, ymax] in local (pre-ctm) path space.
    var pts = [
      applyMat(ctm, mm[0], mm[2]), applyMat(ctm, mm[1], mm[2]),
      applyMat(ctm, mm[0], mm[3]), applyMat(ctm, mm[1], mm[3])
    ];
    var xs = pts.map(function (p) { return p[0]; });
    var ys = pts.map(function (p) { return p[1]; });
    return { x0: Math.min.apply(null, xs), x1: Math.max.apply(null, xs),
             y0: Math.min.apply(null, ys), y1: Math.max.apply(null, ys) };
  }

  function rectArea(r) { return Math.max(0, r.x1 - r.x0) * Math.max(0, r.y1 - r.y0); }
  function rectOverlapArea(a, b) {
    var x0 = Math.max(a.x0, b.x0), x1 = Math.min(a.x1, b.x1);
    var y0 = Math.max(a.y0, b.y0), y1 = Math.min(a.y1, b.y1);
    if (x1 <= x0 || y1 <= y0) return 0;
    return (x1 - x0) * (y1 - y0);
  }

  // A "box" path can be encoded several ways: the dedicated "re" operator (PyMuPDF's
  // get_drawings() — used by scripts/pdf_vector_redaction.py — normalizes rectilinear
  // closed paths to this regardless of how they were drawn), or a raw moveTo + lineTo
  // sequence tracing an axis-aligned rectangle — what pdf.js's operator list actually
  // contains for e.g. Microsoft Print-to-PDF output, which draws rects as explicit line
  // segments, never the "re" operator. That line-segment form itself varies: 3 lineTos
  // to the 3 remaining corners (closePath alone returns to the start), or 4 — a
  // redundant explicit segment back to the start point before closePath (what
  // PyMuPDF's own Shape.draw_line() writes; confirmed both forms exist in real PDFs).
  // A path is a bare box only if it decomposes ENTIRELY into a sequence of such
  // rectangle blocks — any curve or non-rectilinear line makes it a traced (glyph)
  // outline instead. Also handles a single drawing that paints several disjoint
  // rectangles as one multi-subpath fill (one mark per table row, all filled
  // together): each block gets its own local rect rather than one bounding box that
  // overstates what the drawing actually covers.
  // Returns local-space rects, or null if the path isn't purely rectangle blocks.
  function decomposeIntoRects(subOps, coords, OPS) {
    var EPS = 1e-6;
    function near(a, b) { return Math.abs(a - b) < EPS; }
    var rects = [], i = 0, j = 0;
    while (i < subOps.length) {
      if (subOps[i] === OPS.rectangle) {
        var rx = coords[j++], ry = coords[j++], rw = coords[j++], rh = coords[j++];
        rects.push({ x0: rx, y0: ry, x1: rx + rw, y1: ry + rh });
        i++;
        continue;
      }
      if (subOps[i] === OPS.closePath) { i++; continue; }
      if (subOps[i] === OPS.moveTo) {
        var pts = [[coords[j], coords[j + 1]]];
        var k = i + 1, jj = j + 2;
        while (k < subOps.length && subOps[k] === OPS.lineTo && pts.length < 5) {
          pts.push([coords[jj], coords[jj + 1]]);
          jj += 2;
          k++;
        }
        // Must be EXPLICITLY closed (a "closePath"/"h" op right after the line run) —
        // a path that merely happens to trace back to its start point without one is
        // NOT treated as a box. This is the one thing that reliably tells a real box
        // apart from e.g. a letter "l" stem, which can be geometrically an identical
        // axis-aligned rectangle but is drawn open (no closePath): confirmed by
        // checking what PyMuPDF's own get_drawings() normalizes to "re" on the exact
        // same real-world PDFs — it draws the same line, requiring the explicit close.
        if (subOps[k] !== OPS.closePath) return null;
        // a redundant final segment back to the start (PyMuPDF's own draw_line()
        // encoding) — drop it, leaving just the 4 distinct corners
        var last = pts[pts.length - 1], first = pts[0];
        if (pts.length > 1 && near(last[0], first[0]) && near(last[1], first[1])) pts.pop();
        if (pts.length !== 4) return null;
        var axisAligned = true;
        for (var m = 0; m < 4; m++) {
          var a = pts[m], b = pts[(m + 1) % 4]; // includes the implicit closing edge
          var sameX = near(a[0], b[0]), sameY = near(a[1], b[1]);
          if (sameX === sameY) { axisAligned = false; break; } // must move in exactly one axis
        }
        if (!axisAligned) return null;
        var xs = pts.map(function (p) { return p[0]; });
        var ys = pts.map(function (p) { return p[1]; });
        rects.push({ x0: Math.min.apply(null, xs), x1: Math.max.apply(null, xs),
                     y0: Math.min.apply(null, ys), y1: Math.max.apply(null, ys) });
        j = jj;
        i = k;
        continue;
      }
      return null; // curve, non-rectilinear line, or an incomplete block: not a bare box
    }
    return rects;
  }

  // Walk one candidate's raw path onto a canvas 2D context, mirroring pdf.js's own
  // CanvasGraphics.constructPath consumption of the flat coords array (see
  // node_modules/pdfjs-dist build/pdf.js — same op codes, same arg counts).
  function walkPath(ctx, subOps, coords, OPS) {
    var j = 0, x = 0, y = 0;
    for (var i = 0; i < subOps.length; i++) {
      switch (subOps[i]) {
        case OPS.rectangle: {
          var rx = coords[j++], ry = coords[j++], rw = coords[j++], rh = coords[j++];
          var xw = rx + rw, yh = ry + rh;
          ctx.moveTo(rx, ry);
          if (rw === 0 || rh === 0) { ctx.lineTo(xw, yh); }
          else { ctx.lineTo(xw, ry); ctx.lineTo(xw, yh); ctx.lineTo(rx, yh); }
          ctx.closePath();
          x = rx; y = ry;
          break;
        }
        case OPS.moveTo:
          x = coords[j++]; y = coords[j++];
          ctx.moveTo(x, y);
          break;
        case OPS.lineTo:
          x = coords[j++]; y = coords[j++];
          ctx.lineTo(x, y);
          break;
        case OPS.curveTo:
          ctx.bezierCurveTo(coords[j], coords[j + 1], coords[j + 2], coords[j + 3], coords[j + 4], coords[j + 5]);
          x = coords[j + 4]; y = coords[j + 5];
          j += 6;
          break;
        case OPS.curveTo2:
          ctx.bezierCurveTo(x, y, coords[j], coords[j + 1], coords[j + 2], coords[j + 3]);
          x = coords[j + 2]; y = coords[j + 3];
          j += 4;
          break;
        case OPS.curveTo3:
          ctx.bezierCurveTo(coords[j], coords[j + 1], coords[j + 2], coords[j + 3], coords[j + 2], coords[j + 3]);
          x = coords[j + 2]; y = coords[j + 3];
          j += 4;
          break;
        case OPS.closePath:
          ctx.closePath();
          break;
      }
    }
  }

  var PAINT_OPS_KEY = null; // filled in lazily from pdfjsLib.OPS on first use
  function paintKind(op, OPS) {
    if (op === OPS.fill) return "nz";
    if (op === OPS.eoFill) return "eo";
    if (op === OPS.fillStroke) return "nz";
    if (op === OPS.eoFillStroke) return "eo";
    return null;
  }

  // One page's operator list -> {candidates, boxes}. candidates are every filled path
  // (box or glyph-shape), each carrying enough (subOps/coords/ctm) to be redrawn
  // standalone later; boxes are the individually-decomposed redaction-box rects (see
  // decomposeIntoRects) already passed through the box color/opacity/shape filters.
  async function pageCandidates(page, pdfjsLib) {
    var OPS = pdfjsLib.OPS;
    var opl = await page.getOperatorList();
    var fnArray = opl.fnArray, argsArray = opl.argsArray;

    var ctm = [1, 0, 0, 1, 0, 0];
    var fillColor = [0, 0, 0];
    var fillAlpha = 1;
    var stack = [];
    var pending = null;
    var candidates = [];
    var boxes = [];

    for (var i = 0; i < fnArray.length; i++) {
      var op = fnArray[i], args = argsArray[i];
      if (op === OPS.save) {
        stack.push({ ctm: ctm, color: fillColor, alpha: fillAlpha });
      } else if (op === OPS.restore) {
        var s = stack.pop();
        if (s) { ctm = s.ctm; fillColor = s.color; fillAlpha = s.alpha; }
      } else if (op === OPS.transform) {
        ctm = mulMat(ctm, args);
      } else if (op === OPS.setFillRGBColor) {
        fillColor = [args[0], args[1], args[2]];
      } else if (op === OPS.setGState) {
        (args[0] || []).forEach(function (kv) { if (kv[0] === "ca") fillAlpha = kv[1]; });
      } else if (op === OPS.constructPath) {
        pending = { subOps: args[0], coords: args[1], minMax: args[2], ctm: ctm };
      } else {
        var kind = paintKind(op, OPS);
        if (kind && pending && pending.minMax) {
          var localRects = decomposeIntoRects(pending.subOps, pending.coords, OPS);
          var bare = localRects !== null && localRects.length > 0;
          var L = lum(fillColor[0], fillColor[1], fillColor[2]);
          if (bare && fillAlpha >= BOX_MIN_OPACITY && L <= BOX_MAX_LUM) {
            localRects.forEach(function (lr) {
              var pts = [applyMat(pending.ctm, lr.x0, lr.y0), applyMat(pending.ctm, lr.x1, lr.y0),
                         applyMat(pending.ctm, lr.x0, lr.y1), applyMat(pending.ctm, lr.x1, lr.y1)];
              var xs = pts.map(function (p) { return p[0]; });
              var ys = pts.map(function (p) { return p[1]; });
              var r = { x0: Math.min.apply(null, xs), x1: Math.max.apply(null, xs),
                        y0: Math.min.apply(null, ys), y1: Math.max.apply(null, ys) };
              var w = r.x1 - r.x0, h = r.y1 - r.y0;
              if (w < BOX_MIN_W || h < BOX_MIN_H) return;
              if (!plausibleRedactionShape(w, h)) return;
              boxes.push({ rect: r, area: rectArea(r) });
            });
          }
          var rect = bboxOfMinMax(pending.minMax, pending.ctm);
          candidates.push({
            rect: rect,
            area: rectArea(rect),
            bare: bare,
            lum: L,
            alpha: fillAlpha,
            evenOdd: kind === "eo",
            subOps: pending.subOps,
            coords: pending.coords,
            ctm: pending.ctm
          });
        }
        if (kind) pending = null;
      }
    }
    return { candidates: candidates, boxes: boxes };
  }

  function rowAligned(shapes) {
    if (shapes.length < 2) return false;
    var centers = shapes.map(function (s) { return (s.rect.y0 + s.rect.y1) / 2; }).sort(function (a, b) { return a - b; });
    var heights = shapes.map(function (s) { return s.rect.y1 - s.rect.y0; });
    var avgH = Math.max(heights.reduce(function (a, b) { return a + b; }, 0) / heights.length, 1.0);
    return (centers[centers.length - 1] - centers[0]) <= avgH * 1.5;
  }

  function findingsOnPage(candidates, boxes) {
    var out = [];
    boxes.forEach(function (box) {
      var shapes = candidates.filter(function (c) {
        if (c.bare || c.lum > SHAPE_MAX_LUM) return false;
        // A degenerate (zero-area) path — some PDF producers emit these, e.g. for
        // whitespace — would otherwise pass containment trivially for ANY box, since
        // SHAPE_MIN_CONTAINMENT * 0 = 0 and overlap can't be negative. It draws
        // nothing, so it can't be recovered content; exclude it before that math runs.
        if (c.area <= 0) return false;
        if (c.area > SHAPE_MAX_AREA_FRAC * box.area) return false;
        var overlap = rectOverlapArea(c.rect, box.rect);
        return overlap >= SHAPE_MIN_CONTAINMENT * c.area;
      });
      if (shapes.length >= MIN_SHAPES_TO_FLAG && shapes.length <= MAX_SHAPES_TO_FLAG) {
        out.push({
          rect: box.rect, shapes: shapes,
          confidence: (shapes.length >= MIN_SHAPES_TO_FLAG && rowAligned(shapes)) ? "high" : "medium"
        });
      }
    });
    return out;
  }

  // Redraw a finding's hidden shapes alone (the covering box omitted) onto a fresh
  // canvas and return a PNG data URL — the recovered content, for a human to read.
  function reconstructImage(finding, pdfjsLib, scale) {
    scale = scale || 8;
    var r = finding.rect;
    var w = Math.max(1, Math.ceil((r.x1 - r.x0) * scale));
    var h = Math.max(1, Math.ceil((r.y1 - r.y0) * scale));
    var canvas = document.createElement("canvas");
    canvas.width = w; canvas.height = h;
    var ctx = canvas.getContext("2d");
    ctx.fillStyle = "#fff";
    ctx.fillRect(0, 0, w, h);
    // Map this finding's local ("user space") coordinates to canvas pixels, flipping Y
    // (PDF content space is arbitrary-orientation; canvas is top-left, y-down).
    ctx.setTransform(scale, 0, 0, -scale, -r.x0 * scale, r.y1 * scale);
    var OPS = pdfjsLib.OPS;
    ctx.fillStyle = "#000";
    finding.shapes.forEach(function (s) {
      ctx.save();
      ctx.transform(s.ctm[0], s.ctm[1], s.ctm[2], s.ctm[3], s.ctm[4], s.ctm[5]);
      ctx.beginPath();
      walkPath(ctx, s.subOps, s.coords, OPS);
      ctx.fill(s.evenOdd ? "evenodd" : "nonzero");
      ctx.restore();
    });
    return canvas.toDataURL("image/png");
  }

  // Scan an already-open pdf.js document. Returns [{page, rect, shapes, confidence,
  // image}], one entry per flagged box. onProgress(pageNum, numPages), if given,
  // fires after each page — a dense, vector-outlined page can have thousands of
  // drawings, so this scan is the slow part of the whole checker on some real-world
  // files; callers use this to keep the UI from looking frozen.
  // pages (optional): restrict to these 1-indexed page numbers instead of every page
  // in the document — reconstructing every finding across every page of a large,
  // physically-huge-paged PDF (e.g. an architectural sheet set) in one browser session
  // can exhaust memory and crash the page; a caller that already knows which pages
  // matter (e.g. from a fast Python pre-scan) should pass just those.
  async function scanDocument(doc, pdfjsLib, onProgress, pages) {
    var out = [];
    var pageList = pages && pages.length ? pages : Array.from({ length: doc.numPages }, function (_, i) { return i + 1; });
    for (var pi = 0; pi < pageList.length; pi++) {
      var p = pageList[pi];
      var page = await doc.getPage(p);
      var pc;
      try { pc = await pageCandidates(page, pdfjsLib); }
      catch (e) { if (onProgress) onProgress(p, doc.numPages); continue; }
      findingsOnPage(pc.candidates, pc.boxes).forEach(function (f) {
        out.push({
          page: p, rect: f.rect, nShapes: f.shapes.length, confidence: f.confidence,
          image: reconstructImage(f, pdfjsLib),
          shapes: f.shapes  // kept for renderInContext; not serialized by the Python harness
        });
      });
      if (onProgress) onProgress(p, doc.numPages);
    }
    return out;
  }

  // The page as released, with its flagged boxes opened up in place. reconstructImage
  // crops to the box alone, which for a per-row column redaction is a sliver of the one
  // redacted field; this shows what that field sits next to. The page is rendered
  // normally by pdf.js, then each box is repainted a pale highlight and the shapes it
  // covers are redrawn on top in red, in the same user space the operator-list walk
  // tracked (pdf.js renders content under viewport.transform, so applying it first
  // lines the overlay up with the render). Cropped to the full page width and the
  // vertical span of the given findings (all on pageNum) plus a margin. Returns a PNG
  // data URL. The longest side is capped so a huge sheet doesn't exhaust the tab.
  var CONTEXT_SCALE = 2;
  var CONTEXT_MAX_PX = 3000;
  var CONTEXT_PAD_PT = 24;
  var CONTEXT_BOX_FILL = "#fff3a8";
  var CONTEXT_SHAPE_FILL = "#c00000";

  async function renderInContext(doc, pdfjsLib, pageNum, findings, scale) {
    var page = await doc.getPage(pageNum);
    var base = page.getViewport({ scale: 1 });
    scale = Math.min(scale || CONTEXT_SCALE, CONTEXT_MAX_PX / Math.max(base.width, base.height));
    var vp = page.getViewport({ scale: scale });
    var canvas = document.createElement("canvas");
    canvas.width = Math.ceil(vp.width);
    canvas.height = Math.ceil(vp.height);
    var ctx = canvas.getContext("2d");
    await page.render({ canvasContext: ctx, viewport: vp }).promise;

    var OPS = pdfjsLib.OPS;
    var t = vp.transform;
    findings.forEach(function (f) {
      var r = f.rect;
      ctx.setTransform(t[0], t[1], t[2], t[3], t[4], t[5]);
      ctx.fillStyle = CONTEXT_BOX_FILL;
      ctx.fillRect(r.x0, r.y0, r.x1 - r.x0, r.y1 - r.y0);
      ctx.fillStyle = CONTEXT_SHAPE_FILL;
      f.shapes.forEach(function (s) {
        ctx.save();
        ctx.transform(s.ctm[0], s.ctm[1], s.ctm[2], s.ctm[3], s.ctm[4], s.ctm[5]);
        ctx.beginPath();
        walkPath(ctx, s.subOps, s.coords, OPS);
        ctx.fill(s.evenOdd ? "evenodd" : "nonzero");
        ctx.restore();
      });
    });
    ctx.setTransform(1, 0, 0, 1, 0, 0);

    var top = Infinity, bottom = -Infinity;
    findings.forEach(function (f) {
      var q = vp.convertToViewportRectangle([f.rect.x0, f.rect.y0, f.rect.x1, f.rect.y1]);
      top = Math.min(top, q[1], q[3]);
      bottom = Math.max(bottom, q[1], q[3]);
    });
    var pad = CONTEXT_PAD_PT * scale;
    var y0 = Math.max(0, Math.floor(top - pad));
    var y1 = Math.min(canvas.height, Math.ceil(bottom + pad));
    var band = document.createElement("canvas");
    band.width = canvas.width;
    band.height = Math.max(1, y1 - y0);
    band.getContext("2d").drawImage(canvas, 0, y0, canvas.width, band.height, 0, 0, canvas.width, band.height);
    page.cleanup();
    return band.toDataURL("image/png");
  }

  global.PdfVectorRedaction = { scanDocument: scanDocument, renderInContext: renderInContext };
})(window);
