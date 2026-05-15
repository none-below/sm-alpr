'use strict';

const STATUS_LABELS = {
  awaiting_initial: 'Waiting response',
  rolling_production: 'Rolling',
  overdue: 'Overdue',
  closed: 'Closed',
  withdrawn: 'Withdrawn',
  needs_review: 'Needs review',
  unknown: 'Unknown',
};
const STATUS_ORDER = [
  'overdue', 'awaiting_initial', 'rolling_production',
  'needs_review', 'withdrawn', 'closed', 'unknown',
];

const state = {
  registry: null,
  knownIds: new Set(),
  search: '',
  statuses: new Set(),
  tags: new Set(),
  sort: 'filed-desc',
  expanded: new Set(),
};

const PRA_ID_RE = /\bW\d{6}-\d{6}\b/g;

function focusPra(id, filename) {
  if (!state.knownIds.has(id)) return;
  state.expanded.add(id);
  render();
  setTimeout(() => {
    const target = document.querySelector(`.card[data-id="${CSS.escape(id)}"]`);
    if (target) {
      target.scrollIntoView({ behavior: 'smooth', block: 'start' });
      target.classList.add('flash');
      setTimeout(() => target.classList.remove('flash'), 1200);
    }
    if (filename) setTimeout(() => focusFile(id, filename, true), 100);
  }, 0);
}

function blobToRaw(url) {
  return url
    .replace('https://github.com/', 'https://raw.githubusercontent.com/')
    .replace('/blob/', '/');
}

function focusFile(praId, filename, autoView) {
  const decoded = decodeURIComponent(filename);
  const card = document.querySelector(`.card[data-id="${CSS.escape(praId)}"]`);
  if (!card) return;
  const li = card.querySelector(`.file-entry[data-filename="${CSS.escape(decoded)}"]`);
  if (!li) return;
  li.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  li.classList.add('file-highlighted');
  setTimeout(() => li.classList.remove('file-highlighted'), 2000);
  if (autoView) {
    const viewBtn = li.querySelector('.file-view-btn');
    if (viewBtn && !viewBtn.classList.contains('active')) viewBtn.click();
  }
}

function renderFileItem(a, praId) {
  const name = typeof a === 'string' ? a : a.name;
  const url = typeof a === 'object' ? a.url : null;
  const isPdf = name.toLowerCase().endsWith('.pdf');
  const li = el('li', { class: 'file-entry', dataset: { filename: name } });
  if (url) {
    li.appendChild(el('a', { href: url, target: '_blank', rel: 'noopener' }, name));
  } else {
    li.appendChild(el('code', {}, name));
  }
  const actions = el('span', { class: 'file-actions' });
  const linkBtn = el('button', { class: 'file-action-btn', type: 'button', title: 'Copy shareable link' }, 'link');
  linkBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    const frag = '#' + praId + '/' + encodeURIComponent(name);
    navigator.clipboard.writeText(location.href.split('#')[0] + frag);
    history.replaceState(null, '', frag);
    linkBtn.textContent = 'copied!';
    setTimeout(() => { linkBtn.textContent = 'link'; }, 1200);
  });
  actions.appendChild(linkBtn);
  if (isPdf && url) {
    const rawUrl = blobToRaw(url);
    let frame = null;
    const viewBtn = el('button', { class: 'file-action-btn file-view-btn', type: 'button' }, 'view');
    viewBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      if (frame) {
        frame.remove();
        frame = null;
        viewBtn.classList.remove('active');
        viewBtn.textContent = 'view';
      } else {
        frame = el('iframe', { class: 'pdf-viewer-frame', src: rawUrl, title: name });
        li.appendChild(frame);
        viewBtn.classList.add('active');
        viewBtn.textContent = 'close';
      }
    });
    actions.appendChild(viewBtn);
  }
  li.appendChild(actions);
  return li;
}

function praLink(id) {
  if (!state.knownIds.has(id)) {
    return el('span', { class: 'pra-id-unknown' }, id);
  }
  return el('a', {
    class: 'pra-link',
    href: '#' + id,
    onclick: (e) => {
      e.preventDefault();
      e.stopPropagation();
      history.pushState(null, '', '#' + id);
      focusPra(id);
    },
  }, id);
}

function linkifyPras(text) {
  if (!text) return [];
  const out = [];
  let lastIdx = 0;
  PRA_ID_RE.lastIndex = 0;
  let match;
  while ((match = PRA_ID_RE.exec(text)) !== null) {
    if (match.index > lastIdx) out.push(text.slice(lastIdx, match.index));
    out.push(praLink(match[0]));
    lastIdx = match.index + match[0].length;
  }
  if (lastIdx < text.length) out.push(text.slice(lastIdx));
  return out;
}

function paragraphs(text, wrapperClass) {
  // Splits on blank lines, linkifies each paragraph, returns array of <p> elements.
  if (!text) return [];
  return text.split(/\n\s*\n/).filter(p => p.trim()).map(p =>
    el('p', wrapperClass ? { class: wrapperClass } : {}, ...linkifyPras(p.trim()))
  );
}

function fmtDate(iso) {
  if (!iso) return '—';
  return iso.slice(0, 10);
}

function daysFromToday(iso) {
  if (!iso) return null;
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const d = new Date(iso + 'T00:00:00');
  return Math.round((d - today) / 86400000);
}

function deadlineClass(iso) {
  const d = daysFromToday(iso);
  if (d === null) return '';
  if (d < 0) return 'overdue';
  if (d <= 2) return 'due-soon';
  return '';
}

function promiseHistoryTitle(history) {
  if (!history || !history.length) return '';
  return history.map(h => `Set ${h.set_on} → ${h.promise_date}`).join('\n');
}

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === 'class') node.className = v;
    else if (k === 'dataset') Object.assign(node.dataset, v);
    else if (k.startsWith('on') && typeof v === 'function') {
      node.addEventListener(k.slice(2).toLowerCase(), v);
    } else if (v !== null && v !== undefined) {
      node.setAttribute(k, v);
    }
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined) continue;
    node.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
  }
  return node;
}

function matchSearch(pra, q) {
  if (!q) return true;
  q = q.toLowerCase();
  const hay = [
    pra.id,
    pra.curated.title,
    pra.curated.summary,
    pra.curated.filed_because,
    pra.curated.notes,
    ...(pra.curated.tags || []),
  ].filter(Boolean).join(' ').toLowerCase();
  return hay.includes(q);
}

function filterAndSort() {
  const items = state.registry.pras.filter(p => {
    if (state.statuses.size && !state.statuses.has(p.display_status)) return false;
    if (state.tags.size) {
      const t = new Set(p.curated.tags || []);
      for (const required of state.tags) {
        if (!t.has(required)) return false;
      }
    }
    if (!matchSearch(p, state.search)) return false;
    return true;
  });

  const sortFns = {
    'filed-desc': (a, b) => (b.derived.filed_date || '').localeCompare(a.derived.filed_date || ''),
    'filed-asc': (a, b) => (a.derived.filed_date || '').localeCompare(b.derived.filed_date || ''),
    'promised-asc': (a, b) => {
      const av = a.derived.current_promised_date || '9999-99-99';
      const bv = b.derived.current_promised_date || '9999-99-99';
      return av.localeCompare(bv);
    },
    status: (a, b) => {
      const ai = STATUS_ORDER.indexOf(a.display_status);
      const bi = STATUS_ORDER.indexOf(b.display_status);
      return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
    },
  };
  items.sort(sortFns[state.sort] || sortFns['filed-desc']);

  // Exact PRA-ID search: pin that card to the top regardless of sort order
  const exactId = state.search.trim().toUpperCase();
  if (/^W\d{6}-\d{6}$/.test(exactId)) {
    const idx = items.findIndex(p => p.id.toUpperCase() === exactId);
    if (idx > 0) items.unshift(items.splice(idx, 1)[0]);
  }

  return items;
}

function renderTimeline(messages) {
  const list = el('div', { class: 'timeline' });
  messages.forEach((m, idx) => {
    const msg = el('div', { class: `msg ${m.sender_role}`, dataset: { idx } });
    const header = el('div', { class: 'msg-header',
      onclick: (e) => {
        e.stopPropagation();
        msg.classList.toggle('open');
      },
    },
      el('span', { class: 'toggle' }),
      el('span', { class: 'ts' }, m.ts.replace('T', ' ').slice(0, 16)),
      el('span', { class: `role ${m.sender_role}` }, m.sender_role),
      el('span', {}, m.sender_name),
    );
    msg.appendChild(header);
    const body = el('div', { class: 'msg-body', tabindex: '0' }, m.body);
    msg.appendChild(body);
    const expandBtn = el('button', {
      class: 'msg-expand-btn',
      type: 'button',
      onclick: (e) => {
        e.stopPropagation();
        e.preventDefault();
        const ex = body.classList.toggle('expanded');
        expandBtn.textContent = ex ? 'Collapse ↑' : 'Show full message ↓';
      },
    }, 'Show full message ↓');
    msg.appendChild(expandBtn);
    // Re-check overflow after this message opens, since the body is
    // display:none until then and scrollHeight returns 0 in that state.
    header.addEventListener('click', () => {
      if (msg.classList.contains('open')) {
        requestAnimationFrame(() => updateOverflowMarker(body, expandBtn));
      }
    });
    list.appendChild(msg);
  });
  return list;
}

function updateOverflowMarker(bodyEl, btnEl) {
  if (!bodyEl || !btnEl) return;
  // offsetParent is null when the element or an ancestor is display:none.
  if (bodyEl.offsetParent === null) return;
  // If the user has already expanded (max-height removed), keep button visible
  // so they can collapse.
  if (bodyEl.classList.contains('expanded')) {
    btnEl.classList.add('has-overflow');
    return;
  }
  const overflowing = bodyEl.scrollHeight > bodyEl.clientHeight + 2;
  btnEl.classList.toggle('has-overflow', overflowing);
}

function renderAgencyMsgBody(msg) {
  const body = el('pre', {
    class: 'request-text-body agency-msg-body',
    tabindex: '0',
  });
  const segs = msg.body_segments;
  if (segs && segs.length) {
    for (const s of segs) {
      const span = el('span', { class: `seg seg-${s.type}` }, s.text);
      body.appendChild(span);
      body.appendChild(document.createTextNode('\n\n'));
    }
  } else {
    body.textContent = msg.body;
  }
  return body;
}

function renderAgencyMsgItem(msg, isLatest) {
  const wrap = el('div', { class: 'agency-msg' });
  const header = el('div', { class: 'agency-msg-header' });
  header.appendChild(el('span', {
    class: 'agency-msg-tag' + (isLatest ? ' latest' : ''),
  }, isLatest ? 'Latest' : 'Update'));
  header.appendChild(el('span', { class: 'agency-msg-ts' },
    msg.ts.replace('T', ' ').slice(0, 16)));
  header.appendChild(el('span', { class: 'agency-msg-from' }, msg.sender_name));
  wrap.appendChild(header);
  const body = renderAgencyMsgBody(msg);
  wrap.appendChild(body);
  const eb = el('button', {
    class: 'expand-btn',
    type: 'button',
    onclick: (e) => {
      e.stopPropagation();
      e.preventDefault();
      const ex = body.classList.toggle('expanded');
      eb.textContent = ex ? 'Collapse ↑' : 'Show full message ↓';
    },
  }, 'Show full message ↓');
  wrap.appendChild(eb);
  return wrap;
}

function renderRequesterMsgItem(msg) {
  const wrap = el('div', { class: 'requester-msg' });
  const preview = (msg.body || '').trim().split('\n')[0].slice(0, 140);
  const header = el('div', { class: 'requester-msg-header' });
  header.appendChild(el('span', { class: 'requester-msg-toggle' }));
  header.appendChild(el('span', { class: 'agency-msg-ts' },
    msg.ts.replace('T', ' ').slice(0, 16)));
  header.appendChild(el('span', { class: 'requester-msg-from' }, msg.sender_name));
  header.appendChild(el('span', { class: 'requester-msg-preview' },
    '— ' + preview + ((msg.body || '').length > 140 ? '…' : '')));
  wrap.appendChild(header);
  const body = el('pre', {
    class: 'request-text-body requester-msg-body',
    tabindex: '-1',
  }, msg.body);
  wrap.appendChild(body);
  header.addEventListener('click', (e) => {
    e.stopPropagation();
    wrap.classList.toggle('open');
  });
  return wrap;
}

function applyOverflowMarkers(root) {
  root.querySelectorAll('.expand-btn, .msg-expand-btn').forEach(btn => {
    updateOverflowMarker(btn.previousElementSibling, btn);
  });
}

function renderCard(pra) {
  const isExpanded = state.expanded.has(pra.id);
  const card = el('div', {
    class: `card${isExpanded ? ' expanded' : ''}`,
    dataset: { id: pra.id },
    onclick: () => {
      if (state.expanded.has(pra.id)) state.expanded.delete(pra.id);
      else state.expanded.add(pra.id);
      render();
    },
  });

  const status = pra.display_status;
  const override = pra.curated.status_override;

  const head = el('div', { class: 'card-head' });
  head.appendChild(el('div', {},
    el('div', { class: 'card-id-row' },
      el('span', { class: 'card-id' }, pra.id),
      el('span', { class: `badge status-${status}` }, STATUS_LABELS[status] || status),
      override ? el('span', { class: 'badge override-marker', title: `Derived status was: ${pra.derived.status}` }, 'override') : null,
    ),
    el('div', { class: 'card-title' }, pra.curated.title || '(untitled)'),
  ));
  card.appendChild(head);

  const meta = el('div', { class: 'card-meta' });
  const days = pra.derived.days_processing;
  const processingClause = (status === 'closed' || status === 'withdrawn')
    ? `${days} days to ${status === 'closed' ? 'close' : 'withdrawal'}`
    : `${days} days processing`;
  meta.appendChild(el('span', {}, `Filed ${fmtDate(pra.derived.filed_date)} · ${processingClause}`));
  if (pra.derived.current_promised_date) {
    const cls = deadlineClass(pra.derived.current_promised_date);
    const extCount = pra.derived.extension_count || 0;
    const extLabel = extCount > 0 ? ` (ext ×${extCount})` : '';
    meta.appendChild(el('span', { class: cls, title: extCount > 0 ? promiseHistoryTitle(pra.derived.promise_history) : null },
      `Promised ${fmtDate(pra.derived.current_promised_date)}${extLabel}`));
  }
  if (pra.derived.statutory_10day && status === 'awaiting_initial') {
    const cls = deadlineClass(pra.derived.statutory_10day);
    meta.appendChild(el('span', { class: cls }, `10-day ${fmtDate(pra.derived.statutory_10day)}`));
    const cls24 = deadlineClass(pra.derived.statutory_24day);
    meta.appendChild(el('span', { class: cls24 }, `+14 ext ${fmtDate(pra.derived.statutory_24day)}`));
  }
  meta.appendChild(el('span', {}, `${pra.derived.messages.length} msg${pra.derived.messages.length === 1 ? '' : 's'}`));
  card.appendChild(meta);

  if ((pra.curated.tags || []).length) {
    const tags = el('div', { class: 'card-tags' });
    for (const t of pra.curated.tags) {
      tags.appendChild(el('span', { class: 'tag-pill' }, t));
    }
    card.appendChild(tags);
  }

  if (pra.curated.summary && pra.curated.summary !== 'TODO' && !pra.curated.summary.startsWith('TODO')) {
    const summaryBox = el('div', { class: 'card-summary' });
    for (const p of paragraphs(pra.curated.summary)) summaryBox.appendChild(p);
    card.appendChild(summaryBox);
  } else {
    card.appendChild(el('div', { class: 'card-summary' },
      el('em', { style: 'color:#64748b' }, 'No summary yet — metadata.json needs curation.')));
  }

  if (pra.derived.request_text) {
    const details = el('details', { class: 'request-text-collapse' });
    const summary = el('summary', {});
    summary.appendChild(document.createTextNode('Read the request text'));
    const copyBtn = el('button', {
      class: 'copy-btn',
      type: 'button',
      onclick: (e) => {
        e.stopPropagation();
        e.preventDefault();
        navigator.clipboard.writeText(pra.derived.request_text);
        copyBtn.textContent = 'Copied';
        setTimeout(() => { copyBtn.textContent = 'Copy'; }, 1200);
      },
    }, 'Copy');
    summary.appendChild(copyBtn);
    details.appendChild(summary);
    const pre = el('pre', {
      class: 'request-text-body',
      tabindex: '0',
    }, pra.derived.request_text);
    details.appendChild(pre);
    const expandBtn = el('button', {
      class: 'expand-btn',
      type: 'button',
      onclick: (e) => {
        e.stopPropagation();
        e.preventDefault();
        const expanded = pre.classList.toggle('expanded');
        expandBtn.textContent = expanded ? 'Collapse ↑' : 'Show full text ↓';
      },
    }, 'Show full text ↓');
    details.appendChild(expandBtn);
    details.addEventListener('click', (e) => e.stopPropagation());
    card.appendChild(details);
  }

  const allMessages = (pra.derived.messages || []).filter(m =>
    !(m.sender_role === 'agency'
      && /Thank you for your interest in public records of the City of San Mateo/i.test(m.body))
  );
  const agencyCount = allMessages.filter(m => m.sender_role === 'agency').length;
  const requesterCount = allMessages.filter(m => m.sender_role === 'requester').length;

  if (allMessages.length) {
    const aDetails = el('details', { class: 'request-text-collapse agency-responses-collapse' });
    const summaryBits = [];
    if (agencyCount) summaryBits.push(`${agencyCount} agency message${agencyCount === 1 ? '' : 's'}`);
    if (requesterCount) summaryBits.push(`${requesterCount} requester message${requesterCount === 1 ? '' : 's'}`);
    const aSummary = el('summary', {});
    aSummary.appendChild(document.createTextNode(
      'Agency timeline · ' + summaryBits.join(' · ')
    ));
    aDetails.appendChild(aSummary);

    // All messages in reverse chronological order: agency messages render
    // full, with the echoed request muted; your messages render minimized
    // and expand on click.
    const sorted = allMessages.slice().sort((a, b) => (b.ts > a.ts ? 1 : -1));
    let latestAgencyTagged = false;
    for (const m of sorted) {
      if (m.sender_role === 'agency') {
        const isLatest = !latestAgencyTagged;
        latestAgencyTagged = true;
        aDetails.appendChild(renderAgencyMsgItem(m, isLatest));
      } else {
        aDetails.appendChild(renderRequesterMsgItem(m));
      }
    }
    aDetails.addEventListener('click', (e) => e.stopPropagation());
    card.appendChild(aDetails);
  }

  // Expanded details
  const details = el('div', { class: 'details' });

  if (pra.curated.filed_because && !pra.curated.filed_because.startsWith('TODO')) {
    details.appendChild(el('h4', {}, 'Why this PRA was filed'));
    for (const p of paragraphs(pra.curated.filed_because)) details.appendChild(p);
  }

  if ((pra.curated.policy_promises || []).length) {
    details.appendChild(el('h4', {}, 'Policy promises being tested'));
    const ul = el('ul');
    for (const pp of pra.curated.policy_promises) {
      ul.appendChild(el('li', {},
        el('code', {}, pp.policy),
        ' — ',
        ...linkifyPras(pp.tests),
        pp.quote ? el('div', { class: 'promise-quote' }, '"' + pp.quote + '"') : null,
      ));
    }
    details.appendChild(ul);
  }

  if ((pra.curated.prior_pra_refs || []).length) {
    details.appendChild(el('h4', {}, 'Filed in response to'));
    const ul = el('ul');
    for (const r of pra.curated.prior_pra_refs) {
      const ref = typeof r === 'string' ? { ref: r } : r;
      ul.appendChild(el('li', {},
        praLink(ref.ref),
        ref.relation ? ` (${ref.relation})` : null,
        ref.note ? el('div', { style: 'color:#94a3b8;font-size:13px;margin-top:2px' }, ...linkifyPras(ref.note)) : null,
      ));
    }
    details.appendChild(ul);
  }

  if ((pra.derived.cited_by || []).length) {
    details.appendChild(el('h4', {}, 'Cited by later PRAs'));
    const ul = el('ul');
    for (const c of pra.derived.cited_by) {
      ul.appendChild(el('li', {},
        praLink(c.ref),
        c.relation ? ` (${c.relation})` : null,
      ));
    }
    details.appendChild(ul);
  }

  if (pra.curated.notes) {
    details.appendChild(el('h4', {}, 'Notes'));
    for (const p of paragraphs(pra.curated.notes)) details.appendChild(p);
  }

  if ((pra.derived.promise_history || []).length > 1) {
    details.appendChild(el('h4', {}, `Deadline extensions (${pra.derived.extension_count})`));
    const ul = el('ul');
    for (const p of pra.derived.promise_history) {
      ul.appendChild(el('li', {},
        el('code', {}, p.set_on),
        ' → promised ',
        el('code', {}, p.promise_date),
      ));
    }
    details.appendChild(ul);
  }

  if ((pra.derived.attachments || []).length) {
    const headerRow = el('div', { class: 'files-header' },
      el('h4', { style: 'margin:0' }, `Files produced (${pra.derived.attachments.length})`),
    );
    if (pra.derived.download_zip_url) {
      headerRow.appendChild(el('a', {
        class: 'zip-btn',
        href: pra.derived.download_zip_url,
        target: '_blank',
        rel: 'noopener',
        title: 'Bundled by download-directory.github.io',
      }, 'Download all as ZIP ↓'));
    }
    details.appendChild(headerRow);
    const scroller = el('div', { class: 'files-scroll' });
    const ul = el('ul', { class: 'files-list' });
    for (const a of pra.derived.attachments) {
      ul.appendChild(renderFileItem(a, pra.id));
    }
    scroller.appendChild(ul);
    details.appendChild(scroller);
  }

  details.appendChild(el('h4', {}, `Message timeline (${pra.derived.messages.length})`));
  details.appendChild(renderTimeline(pra.derived.messages));

  card.appendChild(details);
  return card;
}

function renderChips(containerId, options, activeSet, onToggle) {
  const container = document.getElementById(containerId);
  container.innerHTML = '';
  for (const opt of options) {
    const chip = el('span', {
      class: `chip${activeSet.has(opt.value) ? ' active' : ''}`,
      onclick: () => { onToggle(opt.value); render(); },
    }, opt.label, el('span', { class: 'count' }, '(' + opt.count + ')'));
    container.appendChild(chip);
  }
}

function buildOptions() {
  const statusCounts = {};
  const tagCounts = {};
  for (const p of state.registry.pras) {
    statusCounts[p.display_status] = (statusCounts[p.display_status] || 0) + 1;
    for (const t of (p.curated.tags || [])) {
      tagCounts[t] = (tagCounts[t] || 0) + 1;
    }
  }
  const statusOpts = STATUS_ORDER
    .filter(s => statusCounts[s])
    .map(s => ({ value: s, label: STATUS_LABELS[s] || s, count: statusCounts[s] }));
  const tagOpts = Object.keys(state.registry.tags || {})
    .filter(t => tagCounts[t])
    .sort()
    .map(t => ({ value: t, label: t, count: tagCounts[t] }));
  return { statusOpts, tagOpts };
}

function render() {
  const filtered = filterAndSort();
  const cards = document.getElementById('cards');
  cards.innerHTML = '';
  if (!filtered.length) {
    cards.appendChild(el('div', { class: 'empty' }, 'No PRAs match the current filters.'));
  } else {
    for (const p of filtered) cards.appendChild(renderCard(p));
  }
  document.getElementById('summary').textContent =
    `Showing ${filtered.length} of ${state.registry.pras.length} PRAs`;

  const { statusOpts, tagOpts } = buildOptions();
  renderChips('status-chips', statusOpts, state.statuses, v => toggleSet(state.statuses, v));
  renderChips('tag-chips', tagOpts, state.tags, v => toggleSet(state.tags, v));

  // Mark overflow-able bodies so their "Show full text" buttons only appear
  // when there is actually content beyond the visible cap. Bodies inside
  // closed <details> are skipped here and re-checked on details toggle.
  applyOverflowMarkers(cards);
  cards.querySelectorAll('details').forEach(d => {
    if (d.dataset.overflowWired) return;
    d.dataset.overflowWired = '1';
    d.addEventListener('toggle', () => {
      if (d.open) applyOverflowMarkers(d);
    });
  });
}

function toggleSet(set, val) {
  if (set.has(val)) set.delete(val);
  else set.add(val);
}

function wireControls() {
  document.getElementById('search').addEventListener('input', e => {
    state.search = e.target.value;
    render();
  });
  document.getElementById('sort').addEventListener('change', e => {
    state.sort = e.target.value;
    render();
  });
}

async function init() {
  const res = await fetch('data/pra_registry.json');
  state.registry = await res.json();
  state.knownIds = new Set(state.registry.pras.map(p => p.id));
  document.getElementById('subtitle').textContent =
    `${state.registry.pras.length} PRAs · registry generated ${state.registry.generated_at.slice(0, 16).replace('T', ' ')} UTC`;
  wireControls();
  render();

  function parseHash() {
    const raw = window.location.hash.slice(1);
    const slash = raw.indexOf('/');
    return slash >= 0
      ? { id: raw.slice(0, slash), filename: raw.slice(slash + 1) }
      : { id: raw, filename: null };
  }
  if (window.location.hash) {
    const { id, filename } = parseHash();
    if (state.knownIds.has(id)) focusPra(id, filename);
  }
  window.addEventListener('hashchange', () => {
    const { id, filename } = parseHash();
    if (state.knownIds.has(id)) focusPra(id, filename);
  });
}

init();
