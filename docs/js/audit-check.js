// SPDX-License-Identifier: AGPL-3.0-or-later
// SPDX-FileCopyrightText: 2026 zero-below
//
// audit-check.js — client-side PDF revision/edit recovery, the browser companion to
// scripts/pdf_audit_revisions.py. Finds incremental-update revisions inside a PDF,
// fingerprints the tool AND the person (Author / XMP dc:creator) behind each, and
// produces a timestamped per-edit diff. Runs entirely in the browser; an uploaded PDF
// is never sent anywhere.
//
// The diff is order- and line-independent: editing in Acrobat re-serializes the page,
// so a text extractor returns the same visible content with different line breaks and
// ordering on the edited revision. We therefore compare the MULTISET OF WORDS (immune
// to re-layout) for field changes, and the SET OF ROW UUIDs for deleted/added rows.
// Each changed word is mapped back to the row that uniquely contains it for context.

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

  var UUID = /[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i;
  var UUIDg = new RegExp(UUID.source, "gi");
  var BOILERPLATE = [/Powered by GovQA/ig, /\bGovQA\b/g, /\bPage \d+\b/ig, /ID\s+userID\s+networkCount\s+Search Time\s+Reason/ig];

  function revisionEnds(bytes) {
    var ends = [], P = [0x25, 0x25, 0x45, 0x4f, 0x46]; // %%EOF
    for (var i = 0; i + P.length <= bytes.length; i++) {
      var hit = true;
      for (var j = 0; j < P.length; j++) if (bytes[i + j] !== P[j]) { hit = false; break; }
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
    if (m) { var tz = m[7].replace(/'/g, ":").replace(/:+$/, ""); return m[1] + "-" + m[2] + "-" + m[3] + " " + m[4] + ":" + m[5] + ":" + m[6] + (tz ? " " + tz : ""); }
    m = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(.*)$/.exec(s);
    if (m) return m[1] + " " + m[2] + (m[3] ? " " + m[3] : "");
    return d;
  }

  function cleanText(s) {
    var t = s;
    BOILERPLATE.forEach(function (rx) { t = t.replace(rx, " "); });
    return norm(t);
  }

  function rowsOf(text) {
    var ms = [], m;
    UUIDg.lastIndex = 0;
    while ((m = UUIDg.exec(text)) !== null) ms.push({ id: m[0].toLowerCase(), idx: m.index });
    var rows = [];
    for (var i = 0; i < ms.length; i++) {
      var end = i + 1 < ms.length ? ms[i + 1].idx : text.length;
      rows.push({ id: ms[i].id, text: norm(text.slice(ms[i].idx, end)) });
    }
    return rows;
  }

  function isTrivial(tok) { return !/[A-Za-z0-9]/.test(tok) || UUID.test(tok) && new RegExp("^" + UUID.source + "$", "i").test(tok); }

  function findUniqueRow(rows, tok) {
    var hits = rows.filter(function (r) { return r.text.split(" ").indexOf(tok) !== -1; });
    return hits.length === 1 ? hits[0] : null;
  }

  async function readRevision(buf, end) {
    var rec = { bytes: end };
    var doc;
    try {
      doc = await pdfjsLib.getDocument({ data: buf.slice(0, end), stopAtErrors: false }).promise;
    } catch (e) {
      rec.error = "unparseable (" + (e && e.message ? e.message : e) + ")";
      rec.text = ""; rec.rows = [];
      return rec;
    }
    var md = await doc.getMetadata().catch(function () { return { info: {}, metadata: null }; });
    var info = (md && md.info) || {};
    var xmp = "";
    try { xmp = md && md.metadata ? md.metadata.getRaw() : ""; } catch (e) { xmp = ""; }
    rec.producer = info.Producer || "";
    rec.creator = info.Creator || "";
    rec.author = info.Author || "";
    rec.dcCreator = grab(xmp, /<dc:creator>[\s\S]*?<rdf:li[^>]*>([^<]*)<\/rdf:li>/);
    rec.create = info.CreationDate || grab(xmp, /<xmp:CreateDate>([^<]*)<\/xmp:CreateDate>/);
    rec.modify = info.ModDate || grab(xmp, /<xmp:ModifyDate>([^<]*)<\/xmp:ModifyDate>/);
    rec.xmptk = grab(xmp, /x:xmptk="([^"]*)"/);
    rec.docId = grab(xmp, /<xmpMM:DocumentID>([^<]*)<\/xmpMM:DocumentID>/);
    rec.instId = grab(xmp, /<xmpMM:InstanceID>([^<]*)<\/xmpMM:InstanceID>/);
    rec.pages = doc.numPages;
    rec.generator = classify(rec.producer) || classify(rec.creator);
    rec.xmpTool = classify(rec.xmptk);
    rec.editor = rec.dcCreator || rec.author || "";
    rec.edited = (!!rec.docId && !!rec.instId && rec.docId !== rec.instId) ||
                 (!!rec.create && !!rec.modify && rec.create !== rec.modify);
    var s = "";
    for (var p = 1; p <= doc.numPages; p++) {
      var page = await doc.getPage(p);
      var tc = await page.getTextContent();
      for (var k = 0; k < tc.items.length; k++) if (typeof tc.items[k].str === "string") s += tc.items[k].str + " ";
    }
    rec.text = cleanText(s);
    rec.rows = rowsOf(rec.text);
    await doc.destroy();
    return rec;
  }

  function diffRevisions(a, b) {
    var ida = {}, idb = {};
    (a.rows || []).forEach(function (r) { ida[r.id] = true; });
    (b.rows || []).forEach(function (r) { idb[r.id] = true; });
    var deletedRows = (a.rows || []).filter(function (r) { return !idb[r.id]; });
    var addedRows = (b.rows || []).filter(function (r) { return !ida[r.id]; });

    var wa = (a.text || "").split(" ").filter(function (w) { return w && !isTrivial(w); });
    var wb = (b.text || "").split(" ").filter(function (w) { return w && !isTrivial(w); });
    var ca = {}, cb = {};
    wa.forEach(function (w) { ca[w] = (ca[w] || 0) + 1; });
    wb.forEach(function (w) { cb[w] = (cb[w] || 0) + 1; });
    var removed = [], added = [];
    Object.keys(ca).forEach(function (k) { for (var i = 0, d = ca[k] - (cb[k] || 0); i < d; i++) removed.push(k); });
    Object.keys(cb).forEach(function (k) { for (var i = 0, d = cb[k] - (ca[k] || 0); i < d; i++) added.push(k); });
    return {
      deletedRows: deletedRows, addedRows: addedRows,
      removed: removed.map(function (t) { return { tok: t, row: findUniqueRow(a.rows || [], t) }; }),
      added: added.map(function (t) { return { tok: t, row: findUniqueRow(b.rows || [], t) }; })
    };
  }

  async function handleFile(file) {
    out.innerHTML = '<p class="nochange">Reading &amp; reconstructing revisions&hellip;</p>';
    var buf;
    try { buf = new Uint8Array(await file.arrayBuffer()); }
    catch (e) { out.innerHTML = '<p class="err">Could not read file.</p>'; return; }
    analyzeBytes(buf, file.name);
  }

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
        var q = "?pdf=" + encodeURIComponent(value.trim()) + (ref && ref !== "main" ? "&ref=" + encodeURIComponent(ref) : "");
        history.replaceState(null, "", q);
      } catch (e) { /* ignore */ }
    }
    analyzeBytes(buf, baseName(url));
  }

  function rowCtx(x) {
    return x.row ? "  row " + esc(x.row.id) + ": " + esc(x.row.text)
                 : "  (appears in multiple rows / no unique row)";
  }

  // git-diff-style inline highlight: removed words struck red, their paired
  // replacement (if any) shown green right after.
  function highlightRow(text, removedSet, replaceMap) {
    return text.split(" ").map(function (w) {
      if (removedSet[w]) {
        var ins = replaceMap && replaceMap[w] ? ' <ins>' + esc(replaceMap[w]) + "</ins>" : "";
        return "<del>" + esc(w) + "</del>" + ins;
      }
      return esc(w);
    }).join(" ");
  }

  async function analyzeBytes(buf, name) {
    var ends = revisionEnds(buf);
    var html = '<div class="file-title">' + esc(name) + "</div>";
    html += '<div class="meta">' + buf.length.toLocaleString() + " bytes &middot; " + ends.length + " revision marker(s)</div>";
    if (!ends.length) { out.innerHTML = html + '<p class="err">No %%EOF marker found &mdash; not a parseable PDF.</p>'; return; }

    var recs = [];
    for (var e = 0; e < ends.length; e++) {
      try { recs.push(await readRevision(buf, ends[e])); }
      catch (err) { recs.push({ error: String(err), text: "", rows: [] }); }
    }

    recs.forEach(function (r, i) {
      if (r.error) { html += '<div class="rev"><h3>rev ' + (i + 1) + '</h3><div class="err">' + esc(r.error) + "</div></div>"; return; }
      html += '<div class="rev' + (r.edited ? " edited" : "") + '">';
      html += "<h3>rev " + (i + 1) + " &middot; " + r.pages + "pp" +
              (r.edited ? '<span class="badge edit">EDITED / RE-SAVED</span>' : '<span class="badge ok">original</span>') + "</h3>";
      var kv = "generated by: " + (r.generator || "unidentified");
      if (r.xmpTool && r.xmpTool !== r.generator) kv += "\nXMP last written by: " + r.xmpTool + "  (opened/edited in this app after generation)";
      if (r.editor) kv += "\nAuthor / dc:creator: " + r.editor;
      kv += "\nProducer=" + JSON.stringify(r.producer) + "  xmptk=" + JSON.stringify(r.xmptk);
      kv += "\ncreated=" + fmtDate(r.create) + "   modified=" + fmtDate(r.modify);
      if (r.docId || r.instId) kv += "\nDocumentID " + (r.docId === r.instId ? "==" : "!=  (content re-saved)") + " InstanceID";
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
        if (last.editor) s += " by " + last.editor;
        if (last.modify) s += " (" + fmtDate(last.modify) + ")";
      } else { s += "; no later revision recovered"; }
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
        var by = b.editor ? " by " + esc(b.editor) : "";
        var d = diffRevisions(a, b);
        var total = d.removed.length + d.added.length + d.deletedRows.length + d.addedRows.length;
        if (total === 0) {
          html += '<p class="nochange">rev ' + (i + 1) + " saved " + esc(ts) + by + " [" + esc(tool) + "]: re-saved, no text change.</p>";
          continue;
        }
        edits++;
        html += '<div class="editblk"><h4>EDIT ' + edits + " &mdash; saved " + esc(ts) + by + "  [" + esc(tool) + "]  (" + total + " change" + (total === 1 ? "" : "s") + ")</h4>";
        if (d.removed.length === 1 && d.added.length === 1 && !d.deletedRows.length && !d.addedRows.length) {
          // one field reworded (e.g. typo fix): show the row, old word struck red, new word green
          var rr = d.removed[0], aa = d.added[0], anchor = rr.row || aa.row;
          if (anchor) {
            var rep = {}; rep[rr.tok] = aa.tok;
            var set = {}; set[rr.tok] = true;
            html += '<div class="rowdiff"><span class="tag">row ' + esc(anchor.id.slice(0, 8)) + '&hellip;</span>' +
                    highlightRow(anchor.text, set, rep) + "</div>";
          } else {
            html += '<div class="rowdiff"><del>' + esc(rr.tok) + "</del> <ins>" + esc(aa.tok) + "</ins></div>";
          }
        } else {
          // group removed words by their row so each affected row renders once, words struck red
          var byRow = {}, noRow = [];
          d.removed.forEach(function (x) {
            if (x.row) { if (!byRow[x.row.id]) byRow[x.row.id] = { text: x.row.text, set: {} }; byRow[x.row.id].set[x.tok] = true; }
            else noRow.push(x.tok);
          });
          Object.keys(byRow).forEach(function (id) {
            html += '<div class="rowdiff"><span class="tag">row ' + esc(id.slice(0, 8)) + '&hellip;</span>' +
                    highlightRow(byRow[id].text, byRow[id].set, null) + "</div>";
          });
          noRow.forEach(function (t) { html += '<div class="rowdiff"><del>' + esc(t) + "</del>  (no unique row)</div>"; });
          d.added.forEach(function (x) {
            html += '<div class="rowdiff"><ins>' + esc(x.tok) + "</ins>" + (x.row ? '  <span class="tag">row ' + esc(x.row.id.slice(0, 8)) + "&hellip;</span>" : "  (added)") + "</div>";
          });
        }
        d.deletedRows.forEach(function (r) { html += '<div class="rowdiff"><span class="tag">row deleted</span><del>' + esc(r.text) + "</del></div>"; });
        d.addedRows.forEach(function (r) { html += '<div class="rowdiff"><span class="tag">row added</span><ins>' + esc(r.text) + "</ins></div>"; });
        html += "</div>";
      }
    }

    out.innerHTML = html;
  }

  // ---- built-in picker for the W012541 audit PDFs (the only PRA with edits) ----
  // `edited` reflects a confirmed content change (not a mere re-save), per
  // scripts/pdf_audit_revisions.py over the files currently on `main`.
  var W012541 = "assets/san-mateo-public-records/W012541-041426/";
  var MANIFEST = [
    { label: "Jan 2023", file: "1_1_2023-1_31_2023-San_Mateo_CA_PD-Audit2.pdf" },
    { label: "Oct 2023", file: "10_1_2023-10_31_2023-San_Mateo_CA_PD-Audit.pdf" },
    { label: "Nov 2023", file: "11_1_2023-11_30_2023-San_Mateo_CA_PD-Audit.pdf" },
    { label: "Jan 2024", file: "1_1_2024-1_31_2024-San_Mateo_CA_PD-Audit.pdf" },
    { label: "Oct 2024", file: "10_1_2024-10_31_2024-San_Mateo_CA_PD-Audit.pdf" },
    { label: "Jan 2025 (pt1)", file: "1_1_2025-1_31_2025-San_Mateo_CA_PD-Audit__Part_1_.pdf", edited: true, note: "2 edits" },
    { label: "Jan 2025 (pt2)", file: "1_1_2025-1_31_2025-San_Mateo_CA_PD-Audit__Part_2_.pdf" },
    { label: "Oct 2025", file: "10_1_2025-10_31_2025-San_Mateo_CA_PD-Audit.pdf" },
    { label: "Dec 2025", file: "12_1_2025-12_31_2025-San_Mateo_CA_PD_Audit.pdf" },
    { label: "Jan 2026 (pt1)", file: "1_1_2026-1_31_2026-San_Mateo_CA_PD-Audit__1___Part_1_.pdf" },
    { label: "Jan 2026 (pt2)", file: "1_1_2026-1_31_2026-San_Mateo_CA_PD-Audit__1___Part_2_.pdf", edited: true, note: "1 edit" },
    { label: "Feb 2026", file: "2_1_2026-2_28_2026-San_Mateo_CA_PD-Audit__2_.pdf", edited: true, note: "case # removed · Jodi Ferreira" },
    { label: "Mar 2026", file: "3_1_2026-3_31_2026-San_Mateo_CA_PD-Audit__1_.pdf", edited: true, note: "typo fix" }
  ];

  function renderPicker() {
    var el = document.getElementById("picker");
    if (!el) return;
    var edited = MANIFEST.filter(function (m) { return m.edited; }).length;
    var h = "<h2>W012541 audit PDFs &middot; " + MANIFEST.length + " files on main, " + edited + " with edits</h2><div class=\"chips\">";
    MANIFEST.forEach(function (m, i) {
      h += '<button type="button" class="chip' + (m.edited ? " edited" : "") + '" data-i="' + i + '">' +
           '<span class="dot"></span>' + esc(m.label) + (m.edited ? ' <small>' + esc(m.note || "edited") + "</small>" : "") + "</button>";
    });
    h += "</div><div class=\"legend\">Amber = a confirmed content edit recovered from the file&rsquo;s revision history. Loads from <code>main</code> via GitHub; more files appear as PRAs merge.</div>";
    el.innerHTML = h;
    el.querySelectorAll(".chip").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var m = MANIFEST[+this.getAttribute("data-i")];
        document.getElementById("url").value = W012541 + m.file;
        loadFromUrl(W012541 + m.file, null, true);
      });
    });
  }

  // ---- wiring + shareable ?pdf= auto-load ----
  document.getElementById("loadbtn").addEventListener("click", function () {
    loadFromUrl(document.getElementById("url").value, null, true);
  });
  document.getElementById("url").addEventListener("keydown", function (e) {
    if (e.key === "Enter") { e.preventDefault(); loadFromUrl(this.value, null, true); }
  });
  renderPicker();
  (function () {
    var params = new URLSearchParams(location.search);
    var pdf = params.get("pdf");
    if (pdf) { document.getElementById("url").value = pdf; loadFromUrl(pdf, params.get("ref"), false); }
  })();
})();
