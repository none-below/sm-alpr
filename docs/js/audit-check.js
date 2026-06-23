// SPDX-License-Identifier: AGPL-3.0-or-later
// SPDX-FileCopyrightText: 2026 zero-below
//
// audit-check.js — client-side PDF revision/edit recovery, the browser companion to
// scripts/pdf_audit_revisions.py. Finds incremental-update revisions inside a PDF,
// fingerprints the tool behind each (Producer string + XMP toolkit), and produces a
// timestamped per-edit diff of what changed. Runs entirely in the browser; an
// uploaded PDF is never sent anywhere.
//
// The diff is order-independent (multiset of normalized lines, pairing a removed line
// with an added line that shares a leading token). Editing in Acrobat re-serializes the
// page, so a position/order-sensitive diff would report false changes on unedited rows;
// this mirrors the validated Python tool to avoid that.

(function () {
  "use strict";

  if (window.pdfjsLib) {
    pdfjsLib.GlobalWorkerOptions.workerSrc =
      "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js";
  }

  var out = document.getElementById("out");
  var drop = document.getElementById("drop");
  var fileInput = document.getElementById("file");

  fileInput.addEventListener("change", function () {
    if (this.files && this.files[0]) handleFile(this.files[0]);
  });
  ["dragenter", "dragover"].forEach(function (ev) {
    drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.add("hover"); });
  });
  ["dragleave", "drop"].forEach(function (ev) {
    drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.remove("hover"); });
  });
  drop.addEventListener("drop", function (e) {
    var f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
    if (f) handleFile(f);
  });

  function esc(s) {
    return String(s).replace(/[&<>]/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c];
    });
  }

  function norm(s) { return s.split(/\s+/).filter(Boolean).join(" "); }

  // ---- byte scan for %%EOF markers; each marks the end of one revision ----
  function revisionEnds(bytes) {
    var ends = [];
    var P = [0x25, 0x25, 0x45, 0x4f, 0x46]; // %%EOF
    for (var i = 0; i + P.length <= bytes.length; i++) {
      var hit = true;
      for (var j = 0; j < P.length; j++) {
        if (bytes[i + j] !== P[j]) { hit = false; break; }
      }
      if (hit) ends.push(i + P.length);
    }
    return ends;
  }

  function classify(s) {
    var t = (s || "").toLowerCase();
    if (!t) return "";
    if (/microsoft|word|excel|powerpoint/.test(t)) return "Microsoft (Office / Print to PDF)";
    if (/acrobat|adobe/.test(t)) return "Adobe Acrobat / Adobe PDF Library";
    if (t.indexOf("3.1-7") === 0 || t.indexOf("xmp toolkit 3.1") !== -1)
      return "legacy XMP toolkit 3.1 (older library; consistent with a native platform export)";
    if (t.indexOf("skia") !== -1) return "Chromium / Chrome (Skia/PDF print-to-PDF)";
    if (t.indexOf("wkhtmltopdf") !== -1) return "wkhtmltopdf (HTML-to-PDF)";
    if (t.indexOf("reportlab") !== -1) return "ReportLab";
    if (t.indexOf("itext") !== -1) return "iText";
    if (t.indexOf("quartz") !== -1 || t.indexOf("mac os x") !== -1) return "Apple Quartz / macOS";
    return "";
  }

  function grab(xmp, re) { var m = re.exec(xmp || ""); return m ? m[1].trim() : ""; }

  function fmtDate(d) {
    if (!d) return "";
    var s = d.indexOf("D:") === 0 ? d.slice(2) : d;
    var m = /^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})(.*)$/.exec(s);
    if (m) {
      var tz = m[7].replace(/'/g, ":").replace(/:+$/, "");
      return m[1] + "-" + m[2] + "-" + m[3] + " " + m[4] + ":" + m[5] + ":" + m[6] + (tz ? " " + tz : "");
    }
    m = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(.*)$/.exec(s);
    if (m) return m[1] + " " + m[2] + (m[3] ? " " + m[3] : "");
    return d;
  }

  // ---- reading-order lines via pdf.js hasEOL (no coordinate binning => stable) ----
  function pageLines(textContent) {
    var lines = [], cur = "";
    textContent.items.forEach(function (it) {
      if (typeof it.str !== "string") return;
      cur += it.str + " ";
      if (it.hasEOL) { var L = norm(cur); if (L) lines.push(L); cur = ""; }
    });
    var tail = norm(cur);
    if (tail) lines.push(tail);
    return lines;
  }

  async function readRevision(buf, end) {
    // buf.slice() makes an owned copy so pdf.js can't detach the shared buffer.
    var rec = { bytes: end };
    var doc;
    try {
      doc = await pdfjsLib.getDocument({ data: buf.slice(0, end), stopAtErrors: false }).promise;
    } catch (e) {
      rec.error = "unparseable (" + (e && e.message ? e.message : e) + ")";
      rec.lines = [];
      return rec;
    }
    var md = await doc.getMetadata().catch(function () { return { info: {}, metadata: null }; });
    var info = (md && md.info) || {};
    var xmp = "";
    try { xmp = md && md.metadata ? md.metadata.getRaw() : ""; } catch (e) { xmp = ""; }
    rec.producer = info.Producer || "";
    rec.creator = info.Creator || "";
    rec.create = info.CreationDate || grab(xmp, /<xmp:CreateDate>([^<]*)<\/xmp:CreateDate>/);
    rec.modify = info.ModDate || grab(xmp, /<xmp:ModifyDate>([^<]*)<\/xmp:ModifyDate>/);
    rec.xmptk = grab(xmp, /x:xmptk="([^"]*)"/);
    rec.docId = grab(xmp, /<xmpMM:DocumentID>([^<]*)<\/xmpMM:DocumentID>/);
    rec.instId = grab(xmp, /<xmpMM:InstanceID>([^<]*)<\/xmpMM:InstanceID>/);
    rec.pages = doc.numPages;
    rec.generator = classify(rec.producer) || classify(rec.creator);
    rec.xmpTool = classify(rec.xmptk);
    rec.edited = (!!rec.docId && !!rec.instId && rec.docId !== rec.instId) ||
                 (!!rec.create && !!rec.modify && rec.create !== rec.modify);
    var lines = [];
    for (var p = 1; p <= doc.numPages; p++) {
      var page = await doc.getPage(p);
      var tc = await page.getTextContent();
      lines = lines.concat(pageLines(tc));
    }
    rec.lines = lines;
    await doc.destroy();
    return rec;
  }

  function pushTo(map, k, v) { if (!map.has(k)) map.set(k, []); map.get(k).push(v); }

  function diffRevisions(a, b) {
    var co = new Map(), cn = new Map(), removed = [], added = [];
    (a.lines || []).forEach(function (l) { co.set(l, (co.get(l) || 0) + 1); });
    (b.lines || []).forEach(function (l) { cn.set(l, (cn.get(l) || 0) + 1); });
    co.forEach(function (c, k) { for (var i = 0, d = c - (cn.get(k) || 0); i < d; i++) removed.push(k); });
    cn.forEach(function (c, k) { for (var i = 0, d = c - (co.get(k) || 0); i < d; i++) added.push(k); });

    var key = function (s) { return s.split(/\s+/)[0] || ""; };
    var rem = new Map(), add = new Map();
    removed.forEach(function (r) { pushTo(rem, key(r), r); });
    added.forEach(function (a_) { pushTo(add, key(a_), a_); });
    var changed = [], onlyR = [], onlyA = [];
    var keys = new Set();
    rem.forEach(function (_, k) { keys.add(k); });
    add.forEach(function (_, k) { keys.add(k); });
    keys.forEach(function (k) {
      var rs = rem.get(k) || [], as = add.get(k) || [];
      var m = Math.min(rs.length, as.length);
      for (var i = 0; i < m; i++) changed.push([rs[i], as[i]]);
      onlyR = onlyR.concat(rs.slice(m));
      onlyA = onlyA.concat(as.slice(m));
    });
    if (onlyR.length === 1 && onlyA.length === 1) { changed.push([onlyR[0], onlyA[0]]); onlyR = []; onlyA = []; }
    return { changed: changed, removed: onlyR, added: onlyA };
  }

  async function handleFile(file) {
    out.innerHTML = '<p class="nochange">Reading &amp; reconstructing revisions&hellip;</p>';
    var buf;
    try { buf = new Uint8Array(await file.arrayBuffer()); }
    catch (e) { out.innerHTML = '<p class="err">Could not read file.</p>'; return; }
    analyzeBytes(buf, file.name);
  }

  // ---- load a PDF straight from the repo (or any URL), so a reviewer needn't re-upload ----
  var REPO_RAW = "https://raw.githubusercontent.com/none-below/sm-alpr/";

  function resolveUrl(value, ref) {
    value = (value || "").trim();
    if (!value) return null;
    if (/^https?:\/\//i.test(value)) return value;
    return REPO_RAW + (ref || "main") + "/" + value.replace(/^\/+/, "");
  }

  function baseName(u) {
    try { return decodeURIComponent(u.split("?")[0].split("#")[0].split("/").pop()) || u; }
    catch (e) { return u; }
  }

  async function loadFromUrl(value, ref, updateBar) {
    var url = resolveUrl(value, ref);
    if (!url) return;
    out.innerHTML = '<p class="nochange">Fetching &amp; reconstructing revisions&hellip;</p>';
    var resp;
    try { resp = await fetch(url); }
    catch (e) { out.innerHTML = '<p class="err">Fetch failed (network or CORS): ' + esc(String((e && e.message) || e)) + "</p>"; return; }
    if (!resp.ok) { out.innerHTML = '<p class="err">HTTP ' + resp.status + " fetching " + esc(url) + "</p>"; return; }
    var buf;
    try { buf = new Uint8Array(await resp.arrayBuffer()); }
    catch (e) { out.innerHTML = '<p class="err">Could not read response.</p>'; return; }
    if (updateBar) {
      try {
        var q = "?pdf=" + encodeURIComponent(value.trim()) +
                (ref && ref !== "main" ? "&ref=" + encodeURIComponent(ref) : "");
        history.replaceState(null, "", q);
      } catch (e) { /* ignore */ }
    }
    analyzeBytes(buf, baseName(url));
  }

  async function analyzeBytes(buf, name) {
    var ends = revisionEnds(buf);
    var html = '<div class="file-title">' + esc(name) + "</div>";
    html += '<div class="meta">' + buf.length.toLocaleString() + " bytes &middot; " +
            ends.length + " revision marker(s)</div>";
    if (!ends.length) {
      out.innerHTML = html + '<p class="err">No %%EOF marker found &mdash; not a parseable PDF.</p>';
      return;
    }

    var recs = [];
    for (var e = 0; e < ends.length; e++) {
      try { recs.push(await readRevision(buf, ends[e])); }
      catch (err) { recs.push({ error: String(err), lines: [] }); }
    }

    recs.forEach(function (r, i) {
      if (r.error) {
        html += '<div class="rev"><h3>rev ' + (i + 1) + '</h3><div class="err">' + esc(r.error) + "</div></div>";
        return;
      }
      html += '<div class="rev' + (r.edited ? " edited" : "") + '">';
      html += "<h3>rev " + (i + 1) + " &middot; " + r.pages + "pp" +
              (r.edited ? '<span class="badge edit">EDITED / RE-SAVED</span>'
                        : '<span class="badge ok">original</span>') + "</h3>";
      var kv = "generated by: " + (r.generator || "unidentified");
      if (r.xmpTool && r.xmpTool !== r.generator)
        kv += "\nXMP last written by: " + r.xmpTool + "  (opened/edited in this app after generation)";
      kv += "\nProducer=" + JSON.stringify(r.producer) + "  xmptk=" + JSON.stringify(r.xmptk);
      kv += "\ncreated=" + fmtDate(r.create) + "   modified=" + fmtDate(r.modify);
      if (r.docId || r.instId)
        kv += "\nDocumentID " + (r.docId === r.instId ? "==" : "!=  (content re-saved)") + " InstanceID";
      html += '<div class="kv">' + esc(kv) + "</div></div>";
    });

    var good = recs.filter(function (r) { return !r.error; });
    if (good.length) {
      var first = good[0], last = good[good.length - 1];
      var made = first.generator || first.xmpTool || "an unidentified tool";
      var s = "Generated by " + made + (first.create ? " (" + fmtDate(first.create) + ")" : "");
      var editor = last.xmpTool || last.generator;
      if (good.length > 1 || last.edited) {
        s += (editor && editor !== made) ? "; last edited in " + editor : "; re-saved after generation";
        if (last.modify) s += " (" + fmtDate(last.modify) + ")";
      } else {
        s += "; no later revision recovered";
      }
      html += '<div class="summary">' + esc(s) + "</div>";
    }

    if (recs.length >= 2) {
      html += '<h2 class="tl">edit timeline</h2>';
      var edits = 0;
      for (var i = 1; i < recs.length; i++) {
        var a = recs[i - 1], b = recs[i];
        if (a.error || b.error) continue;
        var ts = fmtDate(b.modify || b.create);
        var tool = b.xmpTool || b.generator || "unidentified tool";
        var d = diffRevisions(a, b);
        var total = d.changed.length + d.removed.length + d.added.length;
        if (total === 0) {
          html += '<p class="nochange">rev ' + (i + 1) + " saved " + esc(ts) + " [" + esc(tool) +
                  "]: re-saved, no text change (linearization or metadata).</p>";
          continue;
        }
        edits++;
        html += '<div class="editblk"><h4>EDIT ' + edits + " &mdash; saved " + esc(ts) +
                "  [" + esc(tool) + "]  (" + total + " change" + (total === 1 ? "" : "s") + ")</h4>";
        d.changed.forEach(function (pair) {
          html += '<div class="change"><div class="o"><span class="tag">original</span>' + esc(pair[0]) +
                  '</div><div class="n"><span class="tag">produced</span>' + esc(pair[1]) + "</div></div>";
        });
        d.removed.forEach(function (r) {
          html += '<div class="change"><div class="rm"><span class="tag">removed</span>' + esc(r) + "</div></div>";
        });
        d.added.forEach(function (ad) {
          html += '<div class="change"><div class="ad"><span class="tag">added</span>' + esc(ad) + "</div></div>";
        });
        html += "</div>";
      }
    }

    out.innerHTML = html;
  }

  // ---- URL loader wiring + shareable ?pdf= auto-load ----
  document.getElementById("loadbtn").addEventListener("click", function () {
    loadFromUrl(document.getElementById("url").value, null, true);
  });
  document.getElementById("url").addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); loadFromUrl(this.value, null, true); }
  });
  (function () {
    var params = new URLSearchParams(location.search);
    var pdf = params.get("pdf");
    if (pdf) {
      document.getElementById("url").value = pdf;
      loadFromUrl(pdf, params.get("ref"), false);
    }
  })();
})();
