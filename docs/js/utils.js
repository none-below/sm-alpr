// Shared utilities — loaded before per-page scripts.

function escapeHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function formatDate(iso) {
  if (!iso) return '';
  var d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

// Render an article's publication date from the build-time normalized
// published_ts (epoch seconds) rather than from the raw published_at
// string, pinned to UTC.
//
// formatDate() above renders in the viewer's zone, which disagrees with
// the order the article lists are sorted in: published_ts reads a naive
// "2026-09-16T21:34:48" as UTC while new Date() reads it as local time,
// and a Z-stamped evening article displays as the next day east of
// Greenwich. The lists then look mis-sorted while being correctly
// ordered underneath. Formatting the sort key itself keeps display and
// order consistent, and stops a bare "2026-05-26" (midnight UTC) from
// showing as May 25 west of Greenwich. Falls back to formatDate for a
// record with no published_ts (a stale articles_data.json).
function formatArticleDate(article) {
  if (!article) return '';
  if (typeof article.published_ts === 'number') {
    return new Date(article.published_ts * 1000).toLocaleDateString(undefined, {
      year: 'numeric', month: 'short', day: 'numeric', timeZone: 'UTC'
    });
  }
  return formatDate(article.published_at);
}

function safeUrl(u) {
  if (typeof u !== 'string') return '';
  return /^https?:\/\//.test(u) ? u : '';
}
