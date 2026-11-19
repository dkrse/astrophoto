/* Every table with a <thead>: click a column header to sort (again = reverse). Dates (dd-mm-yyyy [hh:mm]),
   sizes (12.3 MB), durations (1h 14m), numbers (also "🌕 87%") and text (natural: NGC 224 < NGC 3031) are
   recognised per column; empty cells always go last. The choice is remembered per page and table.
   Paginated lists are sorted on the server (whole list, then pages) – see syncServerSort below.
   Image views (cards, tiles, gallery) keep their own sort select / the standard order. */
(function () {
  var UNITS = { B: 1, KB: 1024, MB: 1024 * 1024, GB: 1024 * 1024 * 1024, TB: 1024 * 1024 * 1024 * 1024 };
  function parsers(t) {
    var m;
    if ((m = /^(\d{2})-(\d{2})-(\d{4})(?:\s+(\d{2}):(\d{2}))?/.exec(t)))
      return ['date', +(m[3] + m[2] + m[1] + (m[4] || '00') + (m[5] || '00'))];
    if ((m = /^(\d+(?:[.,]\d+)?)\s*(B|KB|MB|GB|TB)$/.exec(t))) return ['size', parseFloat(m[1].replace(',', '.')) * UNITS[m[2]]];
    if ((m = /^(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?$/.exec(t)) && (m[1] || m[2] || m[3]))
      return ['dur', (+m[1] || 0) * 3600 + (+m[2] || 0) * 60 + (+m[3] || 0)];
    if ((m = /^[^\w+\-.]*\s*([-+]?\d+(?:[.,]\d+)?)\s*%?$/.exec(t))) return ['num', parseFloat(m[1].replace(',', '.'))];
    return ['text', t];
  }
  function cellText(tr, i) {
    var td = tr.cells[i];
    if (!td) return '';
    var t = (td.dataset.sort != null ? td.dataset.sort : td.textContent).replace(/\s+/g, ' ').trim();
    return /^[—–-]$/.test(t) ? '' : t;                         // a lone dash means "no value"
  }
  function sortable(th) {
    if (!th || th.dataset.nosort != null || th.colSpan > 1) return false;
    if (th.querySelector('input,select,button')) return false;
    return th.textContent.trim() !== '';
  }
  function key(table) {
    var all = [...document.querySelectorAll('table')].filter(function (t) { return t.tHead; });
    return 'sort:' + location.pathname + ':' + (table.id || all.indexOf(table));
  }
  function sortBy(table, col, dir, save) {
    var body = table.tBodies[0];
    if (!body) return;
    var rows = [...body.rows], vals = rows.map(function (r) { return cellText(r, col); });
    var parsed = vals.map(function (v) { return v ? parsers(v) : null; });
    var kinds = new Set(parsed.filter(Boolean).map(function (p) { return p[0]; }));
    var kind = kinds.size === 1 ? [...kinds][0] : 'text';      // mixed column -> natural text order
    var coll = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });
    var idx = rows.map(function (r, i) { return i; });
    idx.sort(function (a, b) {
      var va = vals[a], vb = vals[b];
      if (!va || !vb) return (!va) - (!vb) || a - b;             // empty last, stable
      var c = kind === 'text' ? coll.compare(va, vb) : parsed[a][1] - parsed[b][1];
      return (c || a - b) * (c ? dir : 1);
    });
    idx.forEach(function (i) { body.appendChild(rows[i]); });
    [...table.tHead.rows[0].cells].forEach(function (th, i) {
      th.classList.toggle('sort-asc', i === col && dir === 1);
      th.classList.toggle('sort-desc', i === col && dir === -1);
      if (i === col) th.setAttribute('aria-sort', dir === 1 ? 'ascending' : 'descending'); else th.removeAttribute('aria-sort');
    });
    if (save) try { localStorage.setItem(key(table), JSON.stringify([col, dir])); } catch (e) {}
  }
  /* ---- server-sorted lists (paginated): table[data-sort-param] with th[data-key] ----
     A click reloads the page sorted by that column (the server sorts everything, then pages); the choice is
     remembered per page and list. Lists that are also shown as images (data-view-sort) are sorted only in
     their table view – image views keep the standard order. */
  function server(t) { return t && t.dataset.sortParam != null; }
  function active(t) {
    var v = t.dataset.viewSort;
    if (v === 'files') return document.documentElement.dataset.fview !== 'grid';
    if (v === 'projects') { var p = document.getElementById('projects'); return !p || p.classList.contains('list'); }
    return true;
  }
  function skey(t) { return 'ssort:' + location.pathname + ':' + t.dataset.sortParam; }
  window.syncServerSort = function () {
    var url = new URL(location.href), changed = false;
    document.querySelectorAll('table[data-sort-param]').forEach(function (t) {
      var p = t.dataset.sortParam, saved = null;
      try { saved = localStorage.getItem(skey(t)); } catch (e) {}
      if (active(t)) {
        if (!url.searchParams.has(p) && saved) {
          var kd = saved.split(':');
          url.searchParams.set(p, kd[0]); url.searchParams.set(p + '_dir', kd[1] || 'asc');
          url.searchParams.delete(t.dataset.pageParam || 'page'); changed = true;
        }
      } else if (url.searchParams.has(p)) {
        url.searchParams.delete(p); url.searchParams.delete(p + '_dir');
        url.searchParams.delete(t.dataset.pageParam || 'page'); changed = true;
      }
    });
    if (changed) location.replace(url.toString());
  };
  window.applyViewSort = window.syncServerSort;
  document.addEventListener('click', function (e) {
    var th = e.target.closest('thead th');
    if (!th || e.target.closest('a,input,select,button')) return;
    var table = th.closest('table');
    if (!table || table.dataset.nosort != null || th.parentNode !== table.tHead.rows[0]) return;
    if (server(table)) {
      if (!th.dataset.key) return;
      var dir = th.classList.contains('sort-asc') ? 'desc' : 'asc', url = new URL(location.href);
      url.searchParams.set(table.dataset.sortParam, th.dataset.key);
      url.searchParams.set(table.dataset.sortParam + '_dir', dir);
      url.searchParams.delete(table.dataset.pageParam || 'page');         // new order starts on page 1
      if (table.dataset.anchor) url.hash = table.dataset.anchor;
      try { localStorage.setItem(skey(table), th.dataset.key + ':' + dir); } catch (e) {}
      location.href = url.toString();
      return;
    }
    if (!sortable(th)) return;
    sortBy(table, th.cellIndex, th.classList.contains('sort-asc') ? -1 : 1, true);
  });
  function applySaved(t) {
    try {
      var s = JSON.parse(localStorage.getItem(key(t)) || 'null');
      if (s && sortable(t.tHead.rows[0].cells[s[0]])) sortBy(t, s[0], s[1], false);
    } catch (e) {}
  }
  function init() {
    document.querySelectorAll('table').forEach(function (t) {
      if (!t.tHead || t.dataset.nosort != null || server(t)) return;      // server lists: marked by the template
      [...t.tHead.rows[0].cells].forEach(function (th) { if (sortable(th)) th.classList.add('sortable'); });
      applySaved(t);
    });
    window.syncServerSort();
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init); else init();
  window.sortTable = sortBy;
})();
