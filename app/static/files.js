/* File tables: multi-select + bulk actions, and background processing status polling. */
(function () {
  var bar = document.getElementById('bulkbar');
  if (bar) {
    var count = document.getElementById('bulkcount');
    function selected() { return [...document.querySelectorAll('.selfile:checked')].map(function (c) { return c.value; }); }
    function refresh() {
      var n = selected().length;
      count.textContent = n; bar.hidden = n === 0;
      document.querySelectorAll('.selgroup').forEach(function (g) {
        var all = document.querySelectorAll('.selfile[data-group="' + g.dataset.group + '"]');
        var on = [...all].filter(function (c) { return c.checked; }).length;
        g.checked = on > 0 && on === all.length; g.indeterminate = on > 0 && on < all.length;
      });
      document.querySelectorAll('.selfile').forEach(function (c) { c.closest('tr').classList.toggle('sel', c.checked); });
    }
    var last = null;
    document.addEventListener('click', function (e) {
      var c = e.target.closest('.selfile');
      if (!c) return;
      // shift-click selects a range
      if (e.shiftKey && last) {
        var all = [...document.querySelectorAll('.selfile')], a = all.indexOf(last), b = all.indexOf(c);
        all.slice(Math.min(a, b), Math.max(a, b) + 1).forEach(function (x) { x.checked = c.checked; });
      }
      last = c; refresh();
    });
    document.querySelectorAll('.selgroup').forEach(function (g) {
      g.addEventListener('change', function () {
        document.querySelectorAll('.selfile[data-group="' + g.dataset.group + '"]').forEach(function (c) { c.checked = g.checked; });
        refresh();
      });
    });

    function run(action, value, confirmMsg) {
      var ids = selected();
      if (!ids.length) return;
      if (confirmMsg && !confirm(confirmMsg.replace('{n}', ids.length))) return;
      bar.classList.add('busy');
      fetch('/api/files/bulk', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ids: ids, action: action, value: value }) })
        .then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
        .then(function (res) { if (!res.ok) { alert(res.j.error || 'Failed'); bar.classList.remove('busy'); return; } location.reload(); })
        .catch(function () { alert('Request failed'); bar.classList.remove('busy'); });
    }
    bar.addEventListener('click', function (e) {
      var b = e.target.closest('[data-bulk]');
      if (!b) return;
      var a = b.dataset.bulk;
      if (a === 'clear') { document.querySelectorAll('.selfile').forEach(function (c) { c.checked = false; }); refresh(); }
      else if (a === 'delete') run('delete', null, 'Delete {n} file(s)? This cannot be undone.');
      else if (a === 'meta') document.getElementById('meta-dlg').showModal();
      else if (a === 'optics') { if (window.bulkOptics) window.bulkOptics(selected()); }
      else if (a === 'solve') { if (window.bulkSolve) window.bulkSolve(selected()); }
      else run(a, null);
    });
    bar.querySelectorAll('[data-bulk-sel]').forEach(function (s) {
      s.addEventListener('change', function () {
        if (!s.value) return;
        var a = s.dataset.bulkSel, label = s.options[s.selectedIndex].text;
        var msg = { type: 'Change type of {n} file(s) to “' + label + '”?',
                    move: 'Move {n} file(s) to “' + label + '”?',
                    session: 'Assign {n} file(s) to session ' + label + '?' }[a];
        var v = s.value === '__none__' ? '' : s.value;
        s.value = '';
        run(a, v, msg);
      });
    });
    var mf = document.getElementById('meta-form');
    if (mf) mf.addEventListener('submit', function (e) {
      e.preventDefault();
      var v = {}; ['exposure_s', 'gain', 'temperature', 'filter', 'description'].forEach(function (k) { v[k] = mf.elements[k].value; });
      document.getElementById('meta-dlg').close();
      run('meta', v);
    });
    refresh();
  }

  /* processing status: poll while the worker has something to do for this page */
  var box = document.getElementById('procbox');
  if (box) {
    var wasBusy = !box.hidden, timer = null;
    function later(ms) { clearTimeout(timer); timer = setTimeout(poll, ms); }
    function poll() {
      fetch(box.dataset.api, { redirect: 'error' }).then(function (r) {
        if (r.status === 401) throw 'logout';
        return r.json();
      }).then(function (s) {
        var busy = s.pending || s.importing || (s.jobs && s.jobs.length);
        var t = (s.jobs || []).map(function (j) {
          return 'Importing ' + (j.done + j.skipped + j.failed) + '/' + j.total + (j.skipped ? ' (' + j.skipped + ' duplicates skipped)' : '');
        });
        if (s.pending) t.push('Processing ' + s.pending + ' file(s) – previews, metadata, statistics…');
        document.getElementById('proctext').textContent = t.join(' · ');
        document.getElementById('proccur').textContent = s.current || '';
        box.hidden = !busy;
        if (busy) { wasBusy = true; later(2500); }
        else if (wasBusy && !window.UPLOADER_BUSY) { location.reload(); }
        else if (wasBusy) { document.getElementById('proctext').textContent = 'Processing finished – reload the page to see the new files.'; box.hidden = false; box.querySelector('.spinner').hidden = true; }
      }).catch(function (e) { if (e !== 'logout') later(8000); });
    }
    if (wasBusy) later(1500);
    window.startStatusPoll = function () { wasBusy = true; box.hidden = false; box.querySelector('.spinner').hidden = false; later(300); };
  }
})();
