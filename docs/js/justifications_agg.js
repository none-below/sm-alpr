/* Client-side re-implementation of scripts/build_justifications.py's
   per-agency aggregation, so the justifications page can re-aggregate a
   narrowed date window live from the already-published raw audit rows
   (data/audit/<slug>.json) without a server round-trip.

   PARITY CONTRACT: aggregate(allRows) must equal the build's per-agency
   entry for every window-dependent field. scripts/verify_justifications_agg.js
   asserts this against docs/data/justifications.json — run it (or
   `make test`, via tests/test_justifications_agg.py) after touching
   either this file or build_justifications.py. The two are a matched
   pair; if you change the parser/tokenizer in one, change it in both.

   Works in the browser (exposes window.JustAgg) and in Node
   (module.exports) so the same code is what's tested and what ships. */

(function (root) {
  'use strict';

  // ── Constants copied verbatim from build_justifications.py ──
  var TOP_VERBATIM = 50;
  var TOP_TOKENS = 50;
  var LONG_ACTIVE_MIN_COUNT = 10;
  var LONG_ACTIVE_MIN_DAYS = 7;
  var LONG_ACTIVE_TOP = 8;
  var BLANK_LABEL = '(blank)';

  var STOPWORDS = {
    'a': 1, 'an': 1, 'and': 1, 'the': 1, 'of': 1, 'to': 1, 'in': 1,
    'on': 1, 'for': 1, 'with': 1, 'by': 1, 'at': 1, 'or': 1, 'is': 1,
    'was': 1, 'be': 1, 'as': 1, 'from': 1, 'this': 1, 'that': 1, 'it': 1,
    'its': 1, 'into': 1, 'out': 1, 'no': 1, 'not': 1,
    'vehicle': 1, 'veh': 1, 'vehicles': 1, 'plate': 1, 'plates': 1,
    'license': 1, 'lic': 1, 'search': 1, 'searches': 1, 'lookup': 1,
    'lookups': 1, 'case': 1, 'report': 1,
    'pc': 1, 'cvc': 1, 'hsc': 1, 'wic': 1, 'vc': 1, 'cvv': 1, 'hs': 1
  };

  var DROP_VERBATIM = {
    '': 1, '-': 1, '.': 1, 'n/a': 1, 'na': 1, 'none': 1,
    'test': 1, 'testing': 1
  };

  // California penal / vehicle / health-and-safety / W&I codes.
  var PENAL_CODES = {
    '10851': 'Vehicle theft (CVC)',
    '20001': 'Hit and run with injury (CVC)',
    '20002': 'Hit and run, property (CVC)',
    '23103': 'Reckless driving (CVC)',
    '23152': 'DUI (CVC)',
    '23153': 'DUI causing injury (CVC)',
    '2800': 'Failure to yield to officer (CVC)',
    '14601': 'Driving on suspended license (CVC)',
    '187': 'Murder (PC)',
    '192': 'Manslaughter (PC)',
    '211': 'Robbery (PC)',
    '215': 'Carjacking (PC)',
    '220': 'Assault to commit a felony (PC)',
    '240': 'Assault (PC)',
    '242': 'Battery (PC)',
    '243': 'Battery — specific (PC)',
    '245': 'Assault with a deadly weapon (PC)',
    '246': 'Shooting at occupied vehicle/dwelling (PC)',
    '261': 'Rape (PC)',
    '273.5': 'Domestic violence (PC)',
    '288': 'Lewd act on child (PC)',
    '459': 'Burglary (PC)',
    '460': 'Burglary — degree (PC)',
    '466': 'Burglary tools (PC)',
    '470': 'Forgery (PC)',
    '484': 'Theft (PC)',
    '487': 'Grand theft (PC)',
    '488': 'Petty theft (PC)',
    '490.4': 'Organized retail theft (PC)',
    '496': 'Receiving stolen property (PC)',
    '496d': 'Receiving stolen vehicle (PC)',
    '530.5': 'Identity theft (PC)',
    '594': 'Vandalism (PC)',
    '207': 'Kidnapping (PC)',
    '314': 'Indecent exposure (PC)',
    '415': 'Disturbing the peace (PC)',
    '422': 'Criminal threats (PC)',
    '451': 'Arson (PC)',
    '452': 'Reckless burning (PC)',
    '417': 'Brandishing a weapon (PC)',
    '25400': 'Carrying a concealed firearm (PC)',
    '25850': 'Carrying a loaded firearm (PC)',
    '29800': 'Felon in possession of firearm (PC)',
    '11350': 'Possession of controlled substance (HSC)',
    '11351': 'Possession for sale (HSC)',
    '11352': 'Sale/transport, narcotics (HSC)',
    '11377': 'Possession of methamphetamine (HSC)',
    '11378': 'Possession for sale, methamphetamine (HSC)',
    '11379': 'Sale/transport, methamphetamine (HSC)',
    '5150': 'Mental-health hold (WIC)',
    '300': 'Dependent child (WIC)',
    '601': 'Status offense — minor (WIC)',
    '602': 'Delinquent minor (WIC)',
    '1030': 'Stolen vehicle (radio code)',
    '1065': 'Missing person (radio)',
    '10-65': 'Missing person (radio)',
    '23101': 'Reckless / DUI causing injury (CVC, historic)'
  };

  // ── Regexes (mirror build_justifications.py; see notes there) ──
  // JS \w is ASCII-only where Python used re.UNICODE; audit reason text
  // is overwhelmingly ASCII, and the parity test guards any divergence.
  var PUNCT_RE = /[^\w\s./()-]+/g;
  var TOKEN_RE = /[A-Za-z]{2,}|[0-9]{2,5}(?:\.[0-9]+)?/g;
  var LONG_DIGITS_RE = /\b\d{8,}\b/g;
  var WS_RE = /\s+/g;
  var CODE_AFFIX = '(?:cvc|cvv|hsc|wic|hs|pc|vc)';
  var CODE_PREFIX_RE = new RegExp('\\b' + CODE_AFFIX + '\\s*(\\d+(?:\\.\\d+)?)\\b', 'g');
  var CODE_SUFFIX_RE = new RegExp('\\b(\\d+(?:\\.\\d+)?)\\s*' + CODE_AFFIX + '\\b', 'g');
  var CASE_NUMBER_RE = /^\d{2,4}-\d{3,8}$/;
  var PLATE_RE = /\b(?=[A-Za-z0-9]{6,8}\b)(?=.*[A-Za-z])(?=.*[0-9])[A-Za-z0-9]+\b/g;

  // ── Reason normalization / tokenization (mirror of the Python) ──

  // Returns a normalized whole-reason string, BLANK_LABEL for empty, or
  // null for filler (test / n/a) that should be dropped entirely.
  function normalizeReason(raw) {
    if (raw === null || raw === undefined) return BLANK_LABEL;
    var s = String(raw).trim().toLowerCase();
    if (!s) return BLANK_LABEL;
    s = s.replace(PUNCT_RE, ' ');
    s = s.replace(WS_RE, ' ').trim();
    if (!s) return BLANK_LABEL;
    if (DROP_VERBATIM[s] === 1) return null;
    return s;
  }

  function tokenize(reasonNorm) {
    var out = [];
    if (!reasonNorm) return out;
    var s = reasonNorm.replace(CODE_PREFIX_RE, '$1');
    s = s.replace(CODE_SUFFIX_RE, '$1');
    s = s.replace(LONG_DIGITS_RE, ' ');
    s = s.replace(PLATE_RE, ' ');
    var m;
    TOKEN_RE.lastIndex = 0;
    while ((m = TOKEN_RE.exec(s)) !== null) {
      var tok = m[0];
      if (STOPWORDS[tok] === 1) continue;
      out.push(tok);
    }
    return out;
  }

  // Ordered, de-duplicated [code, label] pairs detected in a phrase.
  function detectCodes(phrase) {
    var out = [];
    if (!phrase) return out;
    var s = phrase.replace(CODE_PREFIX_RE, '$1');
    s = s.replace(CODE_SUFFIX_RE, '$1');
    s = s.replace(LONG_DIGITS_RE, ' ');
    s = s.replace(PLATE_RE, ' ');
    var seen = {};
    var m;
    TOKEN_RE.lastIndex = 0;
    while ((m = TOKEN_RE.exec(s)) !== null) {
      var tok = m[0];
      var label = PENAL_CODES[tok];
      if (label && seen[tok] !== 1) {
        seen[tok] = 1;
        out.push([tok, label]);
      }
    }
    return out;
  }

  // ── Pacific-time conversion, memoized by UTC-hour (PT date/hour/weekday
  // are constant within a UTC hour, so this bounds Intl calls to the
  // number of distinct UTC hours in the data). Uses the IANA tz database
  // (same source as Python's zoneinfo) so it matches the build. ──
  var _ptFmt = new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/Los_Angeles',
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', hourCycle: 'h23'
  });
  var _ptCache = Object.create(null);

  function ptPartsFromIso(iso) {
    if (!iso) return null;
    var key = iso.slice(0, 13); // YYYY-MM-DDTHH
    var cached = _ptCache[key];
    if (cached !== undefined) return cached;
    var d = new Date(iso);
    if (isNaN(d.getTime())) { _ptCache[key] = null; return null; }
    var parts = _ptFmt.formatToParts(d);
    var y, mo, da, hr;
    for (var i = 0; i < parts.length; i++) {
      var p = parts[i];
      if (p.type === 'year') y = +p.value;
      else if (p.type === 'month') mo = +p.value;
      else if (p.type === 'day') da = +p.value;
      else if (p.type === 'hour') hr = +p.value;
    }
    if (hr === 24) hr = 0; // some engines emit '24' for midnight
    var utcMid = Date.UTC(y, mo - 1, da);
    var res = {
      hour: hr,
      // weekday Mon=0..Sun=6 (ISO), matching Python date.weekday()
      weekday: (new Date(utcMid).getUTCDay() + 6) % 7,
      epochDay: Math.floor(utcMid / 86400000),
      dateISO: pad4(y) + '-' + pad2(mo) + '-' + pad2(da)
    };
    _ptCache[key] = res;
    return res;
  }

  function pad2(n) { return n < 10 ? '0' + n : '' + n; }
  function pad4(n) { return n < 1000 ? ('000' + n).slice(-4) : '' + n; }

  function parseIntStrict(raw) {
    if (raw === null || raw === undefined || raw === '') return null;
    var s = String(raw).trim();
    if (!/^[+-]?\d+$/.test(s)) return null;
    return parseInt(s, 10);
  }

  function isoFromEpochDay(epochDay) {
    var d = new Date(epochDay * 86400000);
    return d.getUTCFullYear() + '-' + pad2(d.getUTCMonth() + 1) + '-' + pad2(d.getUTCDate());
  }

  // ── Per-row precompute (run once per agency load). Derives the
  // expensive fields so window re-aggregation is plain counting. ──
  function precompute(rows) {
    var out = new Array(rows.length);
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      var raw = r.reason;
      if (raw === null || raw === undefined || !String(raw).trim()) raw = r.offenseType;
      if (raw === null || raw === undefined || !String(raw).trim()) raw = r.caseNumber;
      var norm = normalizeReason(raw);
      var pt = ptPartsFromIso(r.searchDate);
      var utcDate = r.searchDate ? String(r.searchDate).slice(0, 10) : '';
      out[i] = {
        norm: norm,                       // null | '(blank)' | phrase
        tokens: (norm && norm !== BLANK_LABEL) ? tokenize(norm) : null,
        isCase: (norm && CASE_NUMBER_RE.test(norm)) || false,
        nc: parseIntStrict(r.networkCount),
        pt: pt,                           // null | {hour,weekday,epochDay,dateISO}
        utcDate: utcDate
      };
    }
    return out;
  }

  // ── Helpers mirroring the Python aggregation pieces ──

  // Stable count-descending order over a Map (insertion order preserved
  // for ties), matching Python Counter.most_common().
  function mostCommon(map, n) {
    var arr = [];
    map.forEach(function (count, key) { arr.push([key, count]); });
    arr.sort(function (a, b) { return b[1] - a[1]; }); // stable in modern JS
    return (n == null) ? arr : arr.slice(0, n);
  }

  function median(sortedNums) {
    var n = sortedNums.length;
    if (!n) return 0;
    return sortedNums[Math.floor(n / 2)];
  }

  // Mirror of compute_phrase_details(): {phrase: detail} for each phrase
  // in the set, single pass over rows. `pre` are precomputed rows.
  function computePhraseDetails(phraseSet, pre) {
    var accum = Object.create(null);
    var phrases = Object.keys(phraseSet);
    if (!phrases.length) return {};
    for (var i = 0; i < phrases.length; i++) {
      accum[phrases[i]] = {
        hourly: new Array(24).fill(0),
        weekly: new Array(7).fill(0),
        ncs: [],
        dates: new Map()
      };
    }
    for (var j = 0; j < pre.length; j++) {
      var p = pre[j];
      // Mirror of `if norm not in accum: continue`. Filler rows (norm
      // null) and any phrase outside the top set are skipped.
      if (p.norm === null) continue;
      var a = accum[p.norm];
      if (a === undefined) continue;
      if (p.pt) {
        a.hourly[p.pt.hour] += 1;
        a.weekly[p.pt.weekday] += 1;
        a.dates.set(p.pt.dateISO, (a.dates.get(p.pt.dateISO) || 0) + 1);
      }
      if (p.nc !== null) a.ncs.push(p.nc);
    }
    var out = {};
    for (var k = 0; k < phrases.length; k++) {
      var phrase = phrases[k];
      var acc = accum[phrase];
      var ncs = acc.ncs.slice().sort(function (x, y) { return x - y; });
      var dateCounts = acc.dates;
      var d = {
        days: dateCounts.size,
        hourly: acc.hourly,
        weekly: acc.weekly
      };
      if (dateCounts.size) {
        var keys = Array.from(dateCounts.keys()).sort();
        var fromIso = keys[0];
        var toIso = keys[keys.length - 1];
        var fromED = epochDayOfIso(fromIso);
        var toED = epochDayOfIso(toIso);
        var span = (toED - fromED) + 1;
        d.from = fromIso;
        d.to = toIso;
        d.span_days = span;
        var series = [];
        var cur;
        if (span <= 365) {
          for (cur = fromED; cur <= toED; cur++) {
            series.push(dateCounts.get(isoFromEpochDay(cur)) || 0);
          }
          d.daily = series;
          d.daily_unit = 'day';
        } else {
          for (cur = fromED; cur <= toED; cur += 7) {
            var wt = 0;
            for (var off = 0; off < 7; off++) {
              wt += dateCounts.get(isoFromEpochDay(cur + off)) || 0;
            }
            series.push(wt);
          }
          d.daily = series;
          d.daily_unit = 'week';
        }
      }
      if (ncs.length) {
        d.nc_min = ncs[0];
        d.nc_med = ncs[Math.floor(ncs.length / 2)];
        d.nc_max = ncs[ncs.length - 1];
        var over = 0;
        for (var z = 0; z < ncs.length; z++) if (ncs[z] >= 100) over++;
        d.nc_over_100 = over;
      }
      out[phrase] = d;
    }
    return out;
  }

  function epochDayOfIso(iso) {
    // iso is a PT calendar date YYYY-MM-DD; treat as UTC midnight for a
    // stable integer day index.
    var y = +iso.slice(0, 4), m = +iso.slice(5, 7), da = +iso.slice(8, 10);
    return Math.floor(Date.UTC(y, m - 1, da) / 86400000);
  }

  // Mirror of find_long_active_cases(). Uses the UTC date slice for the
  // span (as the Python does), not the PT date.
  function findLongActiveCases(pre) {
    var byPhrase = new Map();
    for (var i = 0; i < pre.length; i++) {
      var p = pre[i];
      if (!p.norm || !p.isCase) continue;
      var b = byPhrase.get(p.norm);
      if (!b) { b = { dates: [], ncs: [] }; byPhrase.set(p.norm, b); }
      if (p.utcDate) b.dates.push(p.utcDate);
      if (p.nc !== null) b.ncs.push(p.nc);
    }
    var out = [];
    byPhrase.forEach(function (b, phrase) {
      var dates = b.dates;
      if (!dates.length) return;
      var distinct = new Set(dates).size;
      var count = dates.length;
      if (count < LONG_ACTIVE_MIN_COUNT || distinct < LONG_ACTIVE_MIN_DAYS) return;
      var ncs = b.ncs.slice().sort(function (x, y) { return x - y; });
      var medianNc = ncs.length ? ncs[Math.floor(ncs.length / 2)] : null;
      out.push([phrase, count, distinct, minStr(dates), maxStr(dates), medianNc]);
    });
    out.sort(function (a, b) { return b[1] - a[1]; });
    return out.slice(0, LONG_ACTIVE_TOP);
  }

  function minStr(arr) { var m = arr[0]; for (var i = 1; i < arr.length; i++) if (arr[i] < m) m = arr[i]; return m; }
  function maxStr(arr) { var m = arr[0]; for (var i = 1; i < arr.length; i++) if (arr[i] > m) m = arr[i]; return m; }

  function round1(x) { return Math.round(x * 10) / 10; }

  // ── Main aggregation: mirror of process_agency(), over precomputed
  // rows already filtered to the desired window. Returns the
  // window-dependent fields of the build's per-agency dict. ──
  function aggregate(pre) {
    var verbatim = new Map();
    var tokenCounts = new Map();
    var blank = 0;
    var lengths = [];
    var hourDow = [];
    for (var d = 0; d < 7; d++) hourDow.push(new Array(24).fill(0));
    var timedRows = 0;

    for (var i = 0; i < pre.length; i++) {
      var p = pre[i];
      var norm = p.norm;
      if (norm === null) {
        blank += 1;
      } else if (norm === BLANK_LABEL) {
        blank += 1;
        verbatim.set(norm, (verbatim.get(norm) || 0) + 1);
      } else {
        verbatim.set(norm, (verbatim.get(norm) || 0) + 1);
        lengths.push(norm.length);
        var toks = p.tokens || [];
        for (var t = 0; t < toks.length; t++) {
          tokenCounts.set(toks[t], (tokenCounts.get(toks[t]) || 0) + 1);
        }
      }
      if (p.pt) {
        hourDow[p.pt.weekday][p.pt.hour] += 1;
        timedRows += 1;
      }
    }

    var total = pre.length;
    var result = {
      row_count: total,
      blank_reasons: blank,
      unique_reasons: verbatim.size,
      median_length_chars: 0,
      top1_share_pct: 0,
      top3_share_pct: 0,
      verbatim: [],
      tokens: [],
      penal_codes: [],
      hour_dow: hourDow,
      timed_rows: timedRows,
      long_active_cases: [],
      phrase_details: {},
      verbatim_shown: 0,
      tokens_shown: 0
    };
    if (!verbatim.size) return result;

    var topVerbatimPairs = mostCommon(verbatim, TOP_VERBATIM);
    var phraseSet = Object.create(null);
    for (var v = 0; v < topVerbatimPairs.length; v++) phraseSet[topVerbatimPairs[v][0]] = 1;
    var longActive = findLongActiveCases(pre);
    for (var la = 0; la < longActive.length; la++) phraseSet[longActive[la][0]] = 1;
    var phraseDetails = computePhraseDetails(phraseSet, pre);

    var topVerbatim = [];
    for (var vp = 0; vp < topVerbatimPairs.length; vp++) {
      var phrase = topVerbatimPairs[vp][0];
      var c = topVerbatimPairs[vp][1];
      var codes = (phrase === BLANK_LABEL) ? [] : detectCodes(phrase);
      topVerbatim.push([phrase, c, codes.length ? codes : null, phraseDetails[phrase] || null]);
    }

    var topTokens = [];
    var tokenPairs = mostCommon(tokenCounts, TOP_TOKENS);
    for (var tp = 0; tp < tokenPairs.length; tp++) {
      if (PENAL_CODES[tokenPairs[tp][0]] === undefined) {
        topTokens.push([tokenPairs[tp][0], tokenPairs[tp][1]]);
      }
    }

    var codeRows = [];
    var allTokenPairs = mostCommon(tokenCounts, null);
    for (var ap = 0; ap < allTokenPairs.length; ap++) {
      var label = PENAL_CODES[allTokenPairs[ap][0]];
      if (label) codeRows.push([allTokenPairs[ap][0], label, allTokenPairs[ap][1]]);
    }

    var top1 = topVerbatimPairs.length ? topVerbatimPairs[0][1] : 0;
    var top3 = 0;
    for (var w = 0; w < Math.min(3, topVerbatimPairs.length); w++) top3 += topVerbatimPairs[w][1];

    var sortedLengths = lengths.slice().sort(function (x, y) { return x - y; });

    result.median_length_chars = sortedLengths.length ? Math.trunc(medianFloat(sortedLengths)) : 0;
    result.top1_share_pct = total ? round1(100 * top1 / total) : 0;
    result.top3_share_pct = total ? round1(100 * top3 / total) : 0;
    result.verbatim = topVerbatim;
    result.tokens = topTokens;
    result.penal_codes = codeRows;
    result.long_active_cases = longActive;
    result.phrase_details = phraseDetails;
    result.verbatim_shown = topVerbatim.length;
    result.tokens_shown = topTokens.length;
    return result;
  }

  // Python statistics.median: mean of two middle elements for even n.
  function medianFloat(sorted) {
    var n = sorted.length;
    if (n % 2 === 1) return sorted[(n - 1) / 2];
    return (sorted[n / 2 - 1] + sorted[n / 2]) / 2;
  }

  // Per-phrase daily series over the phrase's FULL span (every row, not
  // a window) — used by the expand panel to show full history with the
  // out-of-window portion grayed. Returns {from,to,span_days,daily,
  // daily_unit,fromEpochDay} or null.
  function fullPhraseSeries(phrase, pre) {
    var dates = new Map();
    for (var i = 0; i < pre.length; i++) {
      var p = pre[i];
      if (p.norm !== phrase || !p.pt) continue;
      dates.set(p.pt.dateISO, (dates.get(p.pt.dateISO) || 0) + 1);
    }
    if (!dates.size) return null;
    var keys = Array.from(dates.keys()).sort();
    var fromED = epochDayOfIso(keys[0]);
    var toED = epochDayOfIso(keys[keys.length - 1]);
    var span = (toED - fromED) + 1;
    var series = [];
    var unit, step;
    var cur;
    if (span <= 365) {
      unit = 'day'; step = 1;
      for (cur = fromED; cur <= toED; cur++) series.push(dates.get(isoFromEpochDay(cur)) || 0);
    } else {
      unit = 'week'; step = 7;
      for (cur = fromED; cur <= toED; cur += 7) {
        var wt = 0;
        for (var off = 0; off < 7; off++) wt += dates.get(isoFromEpochDay(cur + off)) || 0;
        series.push(wt);
      }
    }
    return {
      from: keys[0], to: keys[keys.length - 1], span_days: span,
      daily: series, daily_unit: unit, fromEpochDay: fromED, step: step
    };
  }

  var api = {
    precompute: precompute,
    aggregate: aggregate,
    fullPhraseSeries: fullPhraseSeries,
    epochDayOfIso: epochDayOfIso,
    isoFromEpochDay: isoFromEpochDay,
    // exported for tests / debugging
    normalizeReason: normalizeReason,
    tokenize: tokenize,
    detectCodes: detectCodes,
    BLANK_LABEL: BLANK_LABEL
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  } else {
    root.JustAgg = api;
  }
})(typeof self !== 'undefined' ? self : this);
