/* File tables: multi-select + bulk actions, and background processing status polling. */
(function () {
  var bar = document.getElementById('bulkbar');
  if (bar) {
    var count = document.getElementById('bulkcount');
    // only what the user can see: files in hidden blocks (another session) never count
    function visibleChecked() { return [...document.querySelectorAll('.selfile:checked')].filter(function (c) { return !c.closest('[hidden]'); }); }
    function selected() { return visibleChecked().map(function (c) { return c.value; }); }
    function allRejected() {          // one Reject / Restore button: Restore only when every selected file is rejected
      var rows = visibleChecked().map(function (c) { return c.closest('tr'); });
      return rows.length > 0 && rows.every(function (r) { return r.classList.contains('rejected'); });
    }
    function refresh() {
      var n = selected().length;
      count.textContent = n; bar.hidden = n === 0;
      var ib = bar.querySelector('[data-bulk="info"]'); if (ib) ib.disabled = n !== 1;
      var rb = bar.querySelector('[data-bulk="rejecttoggle"]');
      if (rb) { var restore = allRejected(); rb.textContent = restore ? '↺ Restore' : '✗ Reject'; rb.dataset.mode = restore ? 'unreject' : 'reject'; }
      document.querySelectorAll('.selgroup').forEach(function (g) {
        var all = document.querySelectorAll('.selfile[data-group="' + g.dataset.group + '"]');
        var on = [...all].filter(function (c) { return c.checked; }).length;
        g.checked = on > 0 && on === all.length; g.indeterminate = on > 0 && on < all.length;
      });
      document.querySelectorAll('.selfile').forEach(function (c) { c.closest('tr').classList.toggle('sel', c.checked); });
    }
    window.bulkRefresh = refresh;
    var last = null;
    document.addEventListener('click', function (e) {
      var c = e.target.closest('.selfile');
      if (!c) return;
      // shift-click selects a range
      if (e.shiftKey && last) {
        var all = [...document.querySelectorAll('.selfile')].filter(function (x) { return !x.closest('[hidden]'); }),
            a = all.indexOf(last), b = all.indexOf(c);
        if (a < 0) a = b;
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
      else if (a === 'rejecttoggle') run(b.dataset.mode || 'reject', null);
      else if (a === 'info') {
        var one = selected(); if (one.length !== 1) return;
        var row = document.querySelector('.ftable tr[data-id="' + one[0] + '"]');
        if (row && window.fileInfo) window.fileInfo(row);
      }
      else if (a === 'download') {
        var ids = selected(); if (!ids.length) return;
        if (ids.length === 1) { location.href = '/files/' + ids[0] + '/download'; return; }
        // many ids: POST (a GET URL with thousands of ids would exceed request-line limits)
        var f = document.createElement('form'); f.method = 'post'; f.action = '/files/zip'; f.hidden = true;
        var inp = document.createElement('input'); inp.name = 'ids'; inp.value = ids.join(','); f.append(inp);
        document.body.append(f); f.submit(); f.remove();
      }
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
        var busy = s.pending || s.importing || (s.jobs && s.jobs.length) || s.analysis;
        var t = (s.jobs || []).map(function (j) {
          return 'Importing ' + (j.done + j.skipped + j.failed) + '/' + j.total + (j.skipped ? ' (' + j.skipped + ' duplicates skipped)' : '');
        });
        if (s.pending) t.push('Processing ' + s.pending + ' file(s) – previews, metadata, statistics…');
        if (s.analysis) t.push((s.analysis.paused ? '⏸ Analysis paused (switched off in Settings) – ' + s.analysis.queued + ' waiting' :
          'Analysing ' + (s.analysis.done + s.analysis.error) + '/' + (s.analysis.done + s.analysis.error + s.analysis.queued) + ' (plate solve / optics)') + (s.analysis.error ? ', ' + s.analysis.error + ' failed' : ''));
        var sp = box.querySelector('.spinner'); if (sp) sp.hidden = !!(s.analysis && s.analysis.paused && !s.pending && !s.importing);
        var cb = document.getElementById('proccancel'); if (cb) cb.hidden = !s.analysis;
        document.getElementById('proctext').textContent = t.join(' · ');
        document.getElementById('proccur').textContent = s.current || '';
        box.hidden = !busy;
        if (busy) { wasBusy = true; later(s.analysis && s.analysis.paused && !s.pending && !s.importing ? 15000 : 2500); }
        else if (wasBusy && !window.UPLOADER_BUSY && !document.querySelector('dialog[open]')) { location.reload(); }   // never while editing in a dialog
        else if (wasBusy) { document.getElementById('proctext').textContent = 'Processing finished – reload the page to see the new files.'; box.hidden = false; box.querySelector('.spinner').hidden = true; }
      }).catch(function (e) { if (e !== 'logout') later(8000); });
    }
    if (wasBusy) later(1500);
    var cancel = document.getElementById('proccancel');
    if (cancel) cancel.onclick = function () {
      if (!confirm('Remove the analyses still waiting in the queue? Finished results are kept.')) return;
      var m = box.dataset.api.match(/projects\/(\d+)/);
      fetch('/api/analysis/cancel', { method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(m ? { project_id: +m[1] } : {}) }).then(function () { later(300); });
    };
    window.startStatusPoll = function () { if (window.activityPoke) activityPoke(); wasBusy = true; box.hidden = false; box.querySelector('.spinner').hidden = false; later(300); };
  }
})();
