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

  // Blob URLs minted for the per-revision "open / download this version" links;
  // revoked and rebuilt on each new analysis so they don't leak across files.
  var objUrls = [];

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
    // A linearized PDF's FIRST %%EOF ends the first-page cross-reference table, not a
    // complete earlier revision — slicing there yields a rootless fragment ("Invalid
    // Root reference"). Drop it so the first revision is the full document.
    if (ends.length > 1) {
      var head = "";
      for (var h = 0; h < Math.min(bytes.length, 2048); h++) head += String.fromCharCode(bytes[h]);
      if (head.indexOf("/Linearized") !== -1) ends = ends.slice(1);
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

  // A render/print-to-PDF tool writes a fresh single file with no incremental
  // history, so any edit made before it rendered is unrecoverable ("collapsed").
  // Acrobat and the legacy XMP-toolkit native export are NOT flattens — they can
  // append recoverable revisions — so they are deliberately excluded here.
  function isFlatten(label) {
    return !!label && /Print to PDF|Skia|wkhtmltopdf|ReportLab|iText|Quartz|macOS/i.test(label);
  }

  function versionName(n, k) {
    return String(n).replace(/\.pdf$/i, "") + ".rev" + k + ".pdf";
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
    var s = "", pageOf = {}, pre = new RegExp(UUID.source, "gi");
    for (var p = 1; p <= doc.numPages; p++) {
      var page = await doc.getPage(p);
      var tc = await page.getTextContent();
      var ptext = "";
      for (var k = 0; k < tc.items.length; k++) if (typeof tc.items[k].str === "string") ptext += tc.items[k].str + " ";
      pre.lastIndex = 0;
      var mm;
      while ((mm = pre.exec(ptext)) !== null) { var pid = mm[0].toLowerCase(); if (!(pid in pageOf)) pageOf[pid] = p; }
      s += ptext + " ";
    }
    rec.text = cleanText(s);
    rec.rows = rowsOf(rec.text);
    rec.pageOf = pageOf;   // row UUID -> 1-based page it first appears on (for #page= links)
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
  function isPunct(w) { return !/[A-Za-z0-9]/.test(w); }

  function highlightRow(text, removedSet, replaceMap, addedSet) {
    var words = text.split(" ");
    var rem = words.map(function (w) { return !!(removedSet && removedSet[w]); });
    // The word diff ignores bare punctuation, but a removed value usually takes its
    // separator with it (e.g. "- 2602190261"). Extend the strike to punctuation-only
    // tokens immediately adjacent to a removed word so the whole removed span shows red.
    for (var i = 0; i < words.length; i++) {
      if (!rem[i]) continue;
      var j = i - 1; while (j >= 0 && !rem[j] && isPunct(words[j])) { rem[j] = true; j--; }
      var k = i + 1; while (k < words.length && !rem[k] && isPunct(words[k])) { rem[k] = true; k++; }
    }
    return words.map(function (w, idx) {
      if (rem[idx]) {
        var ins = replaceMap && replaceMap[w] ? ' <ins>' + esc(replaceMap[w]) + "</ins>" : "";
        return "<del>" + esc(w) + "</del>" + ins;
      }
      if (addedSet && addedSet[w]) return "<ins>" + esc(w) + "</ins>";
      return esc(w);
    }).join(" ");
  }

  // ---- surrounding-rows context for an edit (a few rows above/below the change) ----
  // We anchor ENTIRELY on the original revision's rows, which are cleanly segmented.
  // The edited revision re-flows its text (an Acrobat re-save detaches the reason
  // column from its row), so its raw extraction can't be split back into rows — see
  // the multiset diff above. We therefore DERIVE the edited column from the original
  // rows plus the recovered token delta, which is exact and immune to that re-flow.
  function contextBox(a, b, d, revNum, urlA, urlB) {
    var ar = a.rows || [];
    if (!ar.length) return "";
    var N = 3;

    var remByRow = {};                    // original-row id -> { removedToken: true }
    d.removed.forEach(function (x) {
      if (x.row) (remByRow[x.row.id] = remByRow[x.row.id] || {})[x.tok] = true;
    });
    var deleted = {};
    d.deletedRows.forEach(function (r) { deleted[r.id] = true; });
    var hot = {};
    Object.keys(remByRow).forEach(function (id) { hot[id] = true; });
    Object.keys(deleted).forEach(function (id) { hot[id] = true; });
    // added text is shown as the replacement on the changed row (green)
    var addedToks = d.added.map(function (x) { return x.tok; });

    var center = -1;
    for (var i = 0; i < ar.length; i++) if (hot[ar[i].id]) { center = i; break; }
    if (center === -1) return "";         // change not locatable in the original rows
    var lo = Math.max(0, center - N), hi = Math.min(ar.length - 1, center + N);

    // jump-to-page links: browser PDF viewers honor #page=N (line-level isn't
    // addressable), so we land the reader on the page holding the changed row.
    var centerId = ar[center].id;
    // the changed row's position on its page — a hint for where to look, since #page
    // can't address a specific line. Derived from the original revision's clean order.
    function rowHint(pageOf) {
      var pg = pageOf && pageOf[centerId];
      if (!pg) return "";
      var onpg = ar.filter(function (r) { return pageOf[r.id] === pg; });
      for (var z = 0; z < onpg.length; z++) if (onpg[z].id === centerId) return " &middot; row " + (z + 1) + " of " + onpg.length;
      return "";
    }
    function openLink(url, pageOf) {
      if (!url) return "";
      var pg = pageOf && pageOf[centerId];
      return ' <a class="ctxopen" href="' + url + (pg ? "#page=" + pg : "") +
             '" target="_blank" rel="noopener">open' + (pg ? " p." + pg : " pdf") + rowHint(pageOf) + " &#8599;</a>";
    }

    function side(edited) {
      var h = "";
      for (var k = lo; k <= hi; k++) {
        var row = ar[k], isHot = !!hot[row.id], txt;
        if (!isHot) {
          txt = esc(row.text);
        } else if (deleted[row.id]) {
          txt = edited ? '<span class="gone">(row deleted)</span>' : "<del>" + esc(row.text) + "</del>";
        } else if (!edited) {
          txt = highlightRow(row.text, remByRow[row.id], null);          // strike removed
        } else {
          var rem = remByRow[row.id] || {};
          var kept = row.text.split(" ").filter(function (w) { return !rem[w] && w !== "-"; }).join(" ");
          txt = esc(kept);
          if (k === center && addedToks.length)
            txt += " " + addedToks.map(function (t) { return "<ins>" + esc(t) + "</ins>"; }).join(" ");
        }
        h += '<div class="ctxrow' + (isHot ? " hot" : "") + '">' + txt + "</div>";
      }
      return h;
    }

    return '<div class="ctx"><h5>context &middot; surrounding rows, before and after</h5>' +
           '<div class="ctxnote">The edited column is the original rows with the recovered change applied — the edited file re-flows its text, so its raw layout can&rsquo;t be split back into rows.</div>' +
           '<div class="ctxside"><div class="ctxlbl">original &mdash; rev ' + revNum + openLink(urlA, a.pageOf) + '</div>' + side(false) + "</div>" +
           '<div class="ctxside"><div class="ctxlbl">edited &mdash; rev ' + (revNum + 1) + openLink(urlB, b.pageOf) + '</div>' + side(true) + "</div>" +
           "</div>";
  }

  async function analyzeBytes(buf, name) {
    objUrls.forEach(function (u) { try { URL.revokeObjectURL(u); } catch (e) { /* ignore */ } });
    objUrls = [];
    var ends = revisionEnds(buf);
    var html = '<div class="file-title">' + esc(name) + "</div>";
    html += '<div class="meta">' + buf.length.toLocaleString() + " bytes &middot; " + ends.length + " revision marker(s)</div>";
    if (!ends.length) { out.innerHTML = html + '<p class="err">No %%EOF marker found &mdash; not a parseable PDF.</p>'; return; }

    var recs = [];
    for (var e = 0; e < ends.length; e++) {
      try { recs.push(await readRevision(buf, ends[e])); }
      catch (err) { recs.push({ error: String(err), text: "", rows: [] }); }
    }

    // diff each transition once; the badge for revision N reflects what the save into
    // it actually changed (base / re-saved with no change / edited).
    var diffs = [null];
    for (var t = 1; t < recs.length; t++) {
      diffs[t] = (recs[t - 1].error || recs[t].error) ? null : diffRevisions(recs[t - 1], recs[t]);
    }
    function changeCount(d) { return d ? d.removed.length + d.added.length + d.deletedRows.length + d.addedRows.length : 0; }
    var anyChange = diffs.some(function (d) { return changeCount(d) > 0; });

    var revUrls = [];   // per-revision standalone blob URL, indexed by revision

    // When the base is a flattened render, the document existed before it but that
    // history was discarded — represent it as an explicit "unknown" placeholder above
    // the first recoverable revision, so a flattened file doesn't read as "clean".
    var base0 = recs[0];
    if (base0 && !base0.error && isFlatten(base0.generator)) {
      html += '<div class="rev ghost"><h3>??? &middot; earlier history unknown</h3>' +
              '<div class="kv">' + esc("This file was produced by a flattened render (" + base0.generator +
              "). That format keeps no prior revision history, so any earlier version — and any edit made " +
              "before this render — was discarded and cannot be recovered. The absence of a recovered edit " +
              "below is therefore no indication of whether or not the content was altered.") + "</div></div>";
    }

    recs.forEach(function (r, i) {
      if (r.error) { html += '<div class="rev"><h3>rev ' + (i + 1) + '</h3><div class="err">' + esc(r.error) + "</div></div>"; return; }
      var changed = i > 0 && changeCount(diffs[i]) > 0;
      var badge = i === 0 ? '<span class="badge base">base</span>'
                : changed ? '<span class="badge edit">EDITED</span>'
                          : '<span class="badge ok">re-saved, no change</span>';
      // only the BASE is a flattened render; later revisions are incremental edits
      // appended on top (they inherit the same Producer string but aren't flattens).
      if (i === 0 && isFlatten(r.generator)) badge += ' <span class="badge flat">flattened</span>';
      html += '<div class="rev' + (changed ? " edited" : "") + '">';
      html += "<h3>rev " + (i + 1) + " &middot; " + r.pages + "pp&ensp;" + badge + "</h3>";
      var kv = "generated by: " + (r.generator || "unidentified");
      if (r.xmpTool && r.xmpTool !== r.generator) kv += "\nXMP last written by: " + r.xmpTool + "  (opened/edited in this app after generation)";
      if (r.editor) kv += "\nAuthor / dc:creator: " + r.editor;
      kv += "\nProducer=" + JSON.stringify(r.producer) + "  xmptk=" + JSON.stringify(r.xmptk);
      kv += "\ncreated=" + fmtDate(r.create) + "   modified=" + fmtDate(r.modify);
      if (r.docId || r.instId) kv += "\nDocumentID " + (r.docId === r.instId ? "==" : "!=  (content re-saved)") + " InstanceID";
      html += '<div class="kv">' + esc(kv) + "</div>";
      // standalone copy of the document exactly as it stood at this revision
      var vurl = URL.createObjectURL(new Blob([buf.slice(0, r.bytes)], { type: "application/pdf" }));
      objUrls.push(vurl);
      revUrls[i] = vurl;
      html += '<div class="revlinks"><a href="' + vurl + '" target="_blank" rel="noopener">open this version &#8599;</a>' +
              '<a href="' + vurl + '" download="' + esc(versionName(name, i + 1)) + '">download</a>' +
              '<span class="vsz">' + r.bytes.toLocaleString() + " bytes</span></div>";
      html += "</div>";
    });

    var good = recs.filter(function (r) { return !r.error; });
    if (good.length) {
      var first = good[0], last = good[good.length - 1];
      var made = first.generator || first.xmpTool || "an unidentified tool";
      var s = "Generated by " + made + (first.create ? " (" + fmtDate(first.create) + ")" : "");
      var editor = last.xmpTool || last.generator;
      if (anyChange) {
        s += (editor && editor !== made) ? "; last edited in " + editor : "; edited after generation";
        if (last.editor) s += " by " + last.editor;
        if (last.modify) s += " (" + fmtDate(last.modify) + ")";
      } else if (good.length > 1) {
        s += "; re-saved after generation with no content change";
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
        var by = b.editor ? " by " + esc(b.editor) : "";
        var d = diffs[i];
        if (!d) continue;
        var total = changeCount(d);
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
            html += '<div class="rowdiff">' + highlightRow(anchor.text, set, rep) + "</div>";
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
            html += '<div class="rowdiff">' + highlightRow(byRow[id].text, byRow[id].set, null) + "</div>";
          });
          noRow.forEach(function (t) { html += '<div class="rowdiff"><del>' + esc(t) + "</del>  (no unique row)</div>"; });
          d.added.forEach(function (x) {
            if (x.row) { var as = {}; as[x.tok] = true; html += '<div class="rowdiff">' + highlightRow(x.row.text, null, null, as) + "</div>"; }
            else html += '<div class="rowdiff"><ins>' + esc(x.tok) + "</ins>  (added)</div>";
          });
        }
        d.deletedRows.forEach(function (r) { html += '<div class="rowdiff"><span class="tag">row deleted</span><del>' + esc(r.text) + "</del></div>"; });
        d.addedRows.forEach(function (r) { html += '<div class="rowdiff"><span class="tag">row added</span><ins>' + esc(r.text) + "</ins></div>"; });
        html += "</div>";
        html += contextBox(a, b, d, i, revUrls[i - 1], revUrls[i]);
      }
    }

    out.innerHTML = html;
  }

  // ---- built-in picker for the W012541 audit PDFs (the only PRA with edits) ----
  // The list is generated at build time by scripts/build_audit_check_manifest.py into
  // docs/data/audit_check_manifest.json (loaded below); this inline copy is the
  // fallback when that artifact isn't deployed. Flags only: `edited` = a content edit
  // recovered from the file's revision history (chip: "altered"); `flattened` = a
  // Print-to-PDF re-render with no recoverable history (chip: "flattened" — opaque,
  // can't be checked). No edit detail or names are baked in — the page re-derives
  // what/who/when live when a chip is clicked.
  var DIR = "assets/san-mateo-public-records/W012541-041426/";
  var MANIFEST = [
    { label: "Jan 2023", path: DIR + "1_1_2023-1_31_2023-San_Mateo_CA_PD-Audit2.pdf" },
    { label: "Feb 2023", path: DIR + "2_1_2023-2_28_2023-San_Mateo_CA_PD-Audit.pdf" },
    { label: "Mar 2023", path: DIR + "3_1_2023-3_31_2023-San_Mateo_CA_PD-Audit.pdf" },
    { label: "Oct 2023", path: DIR + "10_1_2023-10_31_2023-San_Mateo_CA_PD-Audit.pdf", flattened: true },
    { label: "Nov 2023", path: DIR + "11_1_2023-11_30_2023-San_Mateo_CA_PD-Audit.pdf" },
    { label: "Dec 2023", path: DIR + "12_1_2023-12_31_2023-San_Mateo_CA_PD-Audit.pdf", edited: true },
    { label: "Jan 2024", path: DIR + "1_1_2024-1_31_2024-San_Mateo_CA_PD-Audit.pdf", flattened: true },
    { label: "Feb 2024", path: DIR + "2_1_2024-2_29_2024-San_Mateo_CA_PD-Audit.pdf", edited: true },
    { label: "Oct 2024", path: DIR + "10_1_2024-10_31_2024-San_Mateo_CA_PD-Audit.pdf" },
    { label: "Nov 2024", path: DIR + "11_1_2024-11_30_2024-San_Mateo_CA_PD-Audit.pdf" },
    { label: "Dec 2024", path: DIR + "12_1_2024-12_31_2024-San_Mateo_CA_PD-Audit.pdf", flattened: true },
    { label: "Jan 2025 (pt1)", path: DIR + "1_1_2025-1_31_2025-San_Mateo_CA_PD-Audit__Part_1_.pdf", edited: true },
    { label: "Jan 2025 (pt2)", path: DIR + "1_1_2025-1_31_2025-San_Mateo_CA_PD-Audit__Part_2_.pdf" },
    { label: "Feb 2025", path: DIR + "2_1_2025-2_28_2025-San_Mateo_CA_PD-Audit.pdf", flattened: true },
    { label: "Oct 2025", path: DIR + "10_1_2025-10_31_2025-San_Mateo_CA_PD-Audit.pdf", flattened: true },
    { label: "Nov 2025", path: DIR + "11_1_2025-11_30_2025-San_Mateo_CA_PD-Audit.pdf", flattened: true },
    { label: "Dec 2025", path: DIR + "12_1_2025-12_31_2025-San_Mateo_CA_PD_Audit.pdf", flattened: true },
    { label: "Jan 2026 (pt1)", path: DIR + "1_1_2026-1_31_2026-San_Mateo_CA_PD-Audit__1___Part_1_.pdf" },
    { label: "Jan 2026 (pt2)", path: DIR + "1_1_2026-1_31_2026-San_Mateo_CA_PD-Audit__1___Part_2_.pdf", edited: true },
    { label: "Feb 2026", path: DIR + "2_1_2026-2_28_2026-San_Mateo_CA_PD-Audit__2_.pdf", edited: true, flattened: true },
    { label: "Mar 2026", path: DIR + "3_1_2026-3_31_2026-San_Mateo_CA_PD-Audit__1_.pdf", edited: true }
  ];

  function renderPicker() {
    var el = document.getElementById("picker");
    if (!el) return;
    var edited = MANIFEST.filter(function (m) { return m.edited; }).length;
    var flat = MANIFEST.filter(function (m) { return !m.edited && m.flattened; }).length;
    var h = "<h2>W012541 audit PDFs &middot; " + MANIFEST.length + " files on main &middot; " +
            edited + " altered &middot; " + flat + " flattened (history opaque)</h2><div class=\"chips\">";
    MANIFEST.forEach(function (m, i) {
      var cls = m.edited ? " edited" : (m.flattened ? " flat" : "");
      var mark = m.edited ? (m.note || "altered") : (m.flattened ? "flattened" : "");
      h += '<button type="button" class="chip' + cls + '" data-i="' + i + '">' +
           '<span class="dot"></span>' + esc(m.label) + (mark ? ' <small>' + esc(mark) + "</small>" : "") + "</button>";
    });
    h += "</div><div class=\"legend\">Amber = a content edit recovered from the file&rsquo;s revision history. " +
         "Gray dashed = a flattened render (Print-to-PDF) that keeps no history, so it can&rsquo;t be checked either way. " +
         "Loads from <code>main</code> via GitHub; more files appear as PRAs merge.</div>";
    el.innerHTML = h;
    el.querySelectorAll(".chip").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var m = MANIFEST[+this.getAttribute("data-i")];
        document.getElementById("url").value = m.path;
        loadFromUrl(m.path, null, true);
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
  // prefer the build-time manifest (auto-updated as new audit PDFs merge); the inline
  // list above is the fallback when the artifact isn't deployed.
  fetch("data/audit_check_manifest.json")
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (j) { if (j && j.length) { MANIFEST = j; renderPicker(); } })
    .catch(function () { /* keep inline fallback */ });
  (function () {
    var params = new URLSearchParams(location.search);
    var pdf = params.get("pdf");
    if (pdf) { document.getElementById("url").value = pdf; loadFromUrl(pdf, params.get("ref"), false); }
  })();
})();
