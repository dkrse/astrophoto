/* Modal dialog for managing lookup lists (telescopes, cameras, …).
   Usage: <button type="button" data-manage="telescope">⚙</button>
   Every <select data-kind="telescope"> on the page is kept in sync. */
(function () {
  var dlg, kind, data;

  function esc(s) { return String(s).replace(/[&<>"']/g, function (c) { return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }

  function build() {
    dlg = document.createElement('dialog');
    dlg.className = 'cat-dialog';
    dlg.innerHTML =
      '<form method="dialog" class="cat-head"><h2 id="cat-title"></h2><button class="btn small" aria-label="Close">✕</button></form>' +
      '<form class="cat-add" id="cat-add"><input name="name" placeholder="Name" required autocomplete="off">' +
      '<input name="notes" placeholder="Notes (optional)" autocomplete="off"><button class="btn primary">+ Add</button></form>' +
      '<div class="cat-err" id="cat-err" hidden></div>' +
      '<input class="cat-search" id="cat-search" placeholder="Search…" autocomplete="off">' +
      '<div class="cat-list" id="cat-list"></div>';
    document.body.appendChild(dlg);
    dlg.addEventListener('click', function (e) { if (e.target === dlg) dlg.close(); }); // click on backdrop
    dlg.querySelector('#cat-add').addEventListener('submit', function (e) {
      e.preventDefault();
      var f = e.target;
      call('POST', '/api/catalog/' + kind, { name: f.name.value, notes: f.notes.value }).then(function (r) {
        if (r) { f.reset(); f.name.focus(); }
      });
    });
    dlg.querySelector('#cat-search').addEventListener('input', renderList);
  }

  function err(msg) { var e = dlg.querySelector('#cat-err'); e.textContent = msg || ''; e.hidden = !msg; }

  function call(method, url, body) {
    err('');
    return fetch(url, { method: method, headers: { 'Content-Type': 'application/json' },
                        body: body ? JSON.stringify(body) : undefined })
      .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
      .then(function (res) {
        if (!res.ok) { err(res.j.error || res.j.detail || 'Error'); return null; }
        apply(res.j); return res.j;
      })
      .catch(function () { err('Request failed'); return null; });
  }

  function apply(j) {
    data = j;
    renderList();
    syncSelects(j);
  }

  function renderList() {
    var q = (dlg.querySelector('#cat-search').value || '').toLowerCase();
    var list = dlg.querySelector('#cat-list');
    var items = data.items.filter(function (it) { return !q || (it.name + ' ' + it.notes).toLowerCase().indexOf(q) >= 0; });
    if (!items.length) { list.innerHTML = '<p class="muted">' + (data.items.length ? 'Nothing matches.' : 'No items yet – add the first one above.') + '</p>'; return; }
    list.innerHTML = items.map(function (it) {
      return '<div class="cat-row" data-id="' + it.id + '">' +
        '<div class="cat-view"><div><b>' + esc(it.name) + '</b>' + (it.notes ? '<div class="muted small">' + esc(it.notes) + '</div>' : '') + '</div>' +
        '<span class="muted small">' + (it.used ? it.used + ' project' + (it.used > 1 ? 's' : '') : 'unused') + '</span>' +
        '<button type="button" class="btn small" data-act="edit" title="Edit">✎</button>' +
        '<button type="button" class="btn small danger" data-act="del" title="Delete">✕</button></div>' +
        '<form class="cat-edit" hidden><input name="name" value="' + esc(it.name) + '" required>' +
        '<input name="notes" value="' + esc(it.notes) + '" placeholder="Notes">' +
        '<button class="btn small primary">Save</button><button type="button" class="btn small" data-act="cancel">Cancel</button></form>' +
        '</div>';
    }).join('');
    list.querySelectorAll('.cat-row').forEach(function (row) {
      var id = row.dataset.id, it = data.items.find(function (x) { return String(x.id) === id; });
      var view = row.querySelector('.cat-view'), form = row.querySelector('.cat-edit');
      row.querySelector('[data-act=edit]').onclick = function () { view.hidden = true; form.hidden = false; form.name.focus(); form.name.select(); };
      row.querySelector('[data-act=cancel]').onclick = function () { view.hidden = false; form.hidden = true; err(''); };
      row.querySelector('[data-act=del]').onclick = function () {
        var msg = 'Delete “' + it.name + '”?' + (it.used ? '\nIt will be cleared from ' + it.used + ' project(s).' : '');
        if (confirm(msg)) call('DELETE', '/api/catalog/items/' + id);
      };
      form.onsubmit = function (e) {
        e.preventDefault();
        var msg = form.name.value.trim() !== it.name && it.used ? 'Rename in ' + it.used + ' project(s) too?' : null;
        if (msg && !confirm(msg)) return;
        call('PUT', '/api/catalog/items/' + id, { name: form.name.value, notes: form.notes.value });
      };
    });
  }

  /* keep every <select data-kind=...> in sync, preserving selection (and following renames) */
  function syncSelects(j) {
    document.querySelectorAll('select[data-kind="' + j.kind + '"]').forEach(function (sel) {
      var cur = sel.value;
      if (j.renamed && cur === j.renamed.from) cur = j.renamed.to;
      if (j.deleted && cur === j.deleted) cur = '';
      sel.querySelectorAll('option[data-item]').forEach(function (o) { o.remove(); });
      var anchor = sel.querySelector('option[value="__new__"]');
      j.items.forEach(function (it) {
        var o = new Option(it.name, it.name); o.dataset.item = '1';
        sel.insertBefore(o, anchor || null);
      });
      // keep a value that is not in the list (legacy) selectable
      if (cur && !['', '__none__', '__new__'].includes(cur) && !j.items.some(function (it) { return it.name === cur; })) {
        var o = new Option(cur, cur); o.dataset.item = '1'; sel.insertBefore(o, anchor || null);
      }
      sel.value = cur;
    });
  }

  window.openCatalog = function (k, opts) {
    if (!dlg) build();
    kind = k; err('');
    dlg.querySelector('#cat-search').value = '';
    dlg.querySelector('#cat-list').innerHTML = '<p class="muted">Loading…</p>';
    dlg.showModal();
    fetch('/api/catalog/' + k).then(function (r) { return r.json(); }).then(function (j) {
      dlg.querySelector('#cat-title').textContent = j.label;
      dlg.querySelector('#cat-add').name.placeholder = 'New ' + j.singular.toLowerCase();
      apply(j);
      var add = dlg.querySelector('#cat-add');
      if (opts && opts.add) { add.name.focus(); } else { add.name.focus(); }
    });
  };

  document.addEventListener('click', function (e) {
    var b = e.target.closest('[data-manage]');
    if (b) { e.preventDefault(); openCatalog(b.dataset.manage); }
  });
})();
