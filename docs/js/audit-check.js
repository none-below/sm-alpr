// SPDX-License-Identifier: AGPL-3.0-or-later
// SPDX-FileCopyrightText: 2026 zero-below
//
// audit-check.js — client-side PDF revision/edit recovery, the browser companion to
// scripts/pdf_audit_revisions.py. Finds incremental-update revisions inside a PDF,
// fingerprints the tool behind each (Producer string + XMP toolkit), and diffs the
// extracted text so removed/altered content surfaces. Runs entirely in the browser;
// the PDF is never uploaded.

(function () {
  "use strict";

  if (window.pdfjsLib) {
    // Cross-origin worker; pdf.js falls back to a main-thread worker if the browser blocks it.
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

  // ---- map a Producer/Creator/XMP-toolkit string to a recognizable application ----
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

  // ---- reconstruct visual lines from a page's text items, grouped by y position ----
  function pageLines(textContent) {
    var rows = new Map();
    textContent.items.forEach(function (it) {
      if (!it.str) return;
      var y = Math.round(it.transform[5]);
      var x = it.transform[4];
      if (!rows.has(y)) rows.set(y, []);
      rows.get(y).push([x, it.str]);
    });
    var ys = Array.from(rows.keys()).sort(function (a, b) { return b - a; }); // top -> bottom
    return ys.map(function (y) {
      return rows.get(y).sort(function (a, b) { return a[0] - b[0]; })
        .map(function (p) { return p[1]; }).join(" ");
    });
  }

  function norm(s) { return s.split(/\s+/).filter(Boolean).join(" "); }

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

  function multisetDiff(oldL, newL) {
    var co = new Map(), cn = new Map(), removed = [], added = [];
    oldL.forEach(function (l) { var k = norm(l); if (k) co.set(k, (co.get(k) || 0) + 1); });
    newL.forEach(function (l) { var k = norm(l); if (k) cn.set(k, (cn.get(k) || 0) + 1); });
    co.forEach(function (c, k) { for (var i = 0, d = c - (cn.get(k) || 0); i < d; i++) removed.push(k); });
    cn.forEach(function (c, k) { for (var i = 0, d = c - (co.get(k) || 0); i < d; i++) added.push(k); });
    return { removed: removed, added: added };
  }

  function pushTo(map, k, v) { if (!map.has(k)) map.set(k, []); map.get(k).push(v); }

  function pairByLeadingToken(removed, added) {
    var key = function (s) { return s.split(/\s+/)[0] || ""; };
    var rem = new Map(), add = new Map();
    removed.forEach(function (r) { pushTo(rem, key(r), r); });
    added.forEach(function (a) { pushTo(add, key(a), a); });
    var pairs = [], onlyR = [], onlyA = [];
    var keys = new Set();
    rem.forEach(function (_, k) { keys.add(k); });
    add.forEach(function (_, k) { keys.add(k); });
    keys.forEach(function (k) {
      var rs = rem.get(k) || [], as = add.get(k) || [];
      var n = Math.min(rs.length, as.length);
      for (var i = 0; i < n; i++) pairs.push([rs[i], as[i]]);
      onlyR = onlyR.concat(rs.slice(n));
      onlyA = onlyA.concat(as.slice(n));
    });
    return { pairs: pairs, onlyR: onlyR, onlyA: onlyA };
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
    if (/^https?:\/\//i.test(value)) return value;            // full URL passthrough
    return REPO_RAW + (ref || "main") + "/" + value.replace(/^\/+/, ""); // repo-relative path
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
      kv += "\nProducer=" + JSON.stringify(r.producer) + "  Creator=" + JSON.stringify(r.creator);
      kv += "\nxmptk=" + JSON.stringify(r.xmptk);
      kv += "\ncreate=" + r.create + "   modify=" + r.modify;
      if (r.docId || r.instId)
        kv += "\nDocumentID " + (r.docId === r.instId ? "==" : "!=  (content re-saved)") + " InstanceID";
      html += '<div class="kv">' + esc(kv) + "</div></div>";
    });

    var good = recs.filter(function (r) { return !r.error; });
    if (good.length) {
      var first = good[0], last = good[good.length - 1];
      var made = first.generator || first.xmpTool || "an unidentified tool";
      var s = "Generated by " + made + (first.create ? " (" + first.create + ")" : "");
      var editor = last.xmpTool || last.generator;
      if (good.length > 1 || last.edited) {
        s += (editor && editor !== made) ? "; last edited in " + editor : "; re-saved after generation";
        if (last.modify) s += " (" + last.modify + ")";
      } else {
        s += "; no later revision recovered";
      }
      html += '<div class="summary">' + esc(s) + "</div>";
    }

    for (var i = 1; i < recs.length; i++) {
      var a = recs[i - 1], b = recs[i];
      if (a.error || b.error) continue;
      var d = multisetDiff(a.lines, b.lines);
      html += '<div class="diff"><h4>rev ' + i + " &rarr; " + (i + 1) + "</h4>";
      if (!d.removed.length && !d.added.length) {
        html += '<p class="nochange">No text change (re-save only &mdash; linearization or metadata).</p></div>';
        continue;
      }
      html += '<p class="nochange">Text changed: &minus;' + d.removed.length + " / +" + d.added.length +
              " lines (whitespace-normalized).</p>";
      var pr = pairByLeadingToken(d.removed, d.added);
      pr.pairs.forEach(function (pair) {
        html += '<div class="change"><div class="o"><span class="tag">original</span>' + esc(pair[0]) +
                '</div><div class="n"><span class="tag">produced</span>' + esc(pair[1]) + "</div></div>";
      });
      pr.onlyR.forEach(function (r) {
        html += '<div class="change"><div class="rm"><span class="tag">removed</span>' + esc(r) + "</div></div>";
      });
      pr.onlyA.forEach(function (ad) {
        html += '<div class="change"><div class="ad"><span class="tag">added</span>' + esc(ad) + "</div></div>";
      });
      html += "</div>";
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
