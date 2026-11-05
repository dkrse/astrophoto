/* Resumable chunked uploader with drag & drop, folder support and frame-type detection. */
(function () {
  var root = document.querySelector('.uploader');
  if (!root) return;
  var CHUNK = 8 * 1024 * 1024;
  var EXT = /\.(fits?|fts|fz|xisf|ser|cr2|cr3|crw|nef|nrw|arw|srf|sr2|dng|raf|orf|rw2|pef|srw|raw|3fr|erf|kdc|mrw|x3f|jpe?g|png|tiff?|webp|bmp|gif|avi)$/i;
  var TYPES = window.UPLOAD_TYPES || ['light', 'dark', 'bias', 'flat', 'darkflat', 'stack', 'other'];
  var LABELS = window.FRAME_LABELS || {};
  var WORDS = [
    ['darkflat', /dark[\s_-]*flats?|flat[\s_-]*darks?/],
    ['bias', /bias|offsets?|zero/],
    ['dark', /darks?/],
    ['flat', /flats?|flat[\s_-]*field/],
    ['light', /lights?|object|science/],
    ['stack', /stack(ed)?|final|result|integration|processed/]
  ];
  var dz = root.querySelector('.dropzone'), opts = root.querySelector('.up-options');
  var tbody = root.querySelector('.uptable tbody'), totalBar = root.querySelector('.up-total');
  var startBtn = root.querySelector('.up-start'), pauseBtn = root.querySelector('.up-pause');
  var summary = root.querySelector('.up-summary');
  var items = [], running = false, paused = false;

  function fmt(n) { var u = ['B', 'KB', 'MB', 'GB', 'TB'], i = 0; while (n >= 1024 && i < 4) { n /= 1024; i++; } return (i ? n.toFixed(1) : n) + ' ' + u[i]; }
  function typeFromText(s) {
    s = (s || '').toLowerCase();
    for (var i = 0; i < WORDS.length; i++) {
      var re = new RegExp('(^|[^a-z])(' + WORDS[i][1].source + ')([^a-z]|$)');
      if (re.test(s)) return WORDS[i][0];
    }
    return null;
  }
  function fitsCards(file) {
    return file.slice(0, 2880 * 6).text().then(function (txt) {
      var cards = {};
      for (var i = 0; i + 80 <= txt.length; i += 80) {
        var c = txt.substr(i, 80), k = c.substr(0, 8).trim();
        if (k === 'END') break;
        if (c.substr(8, 2) !== '= ') continue;
        var v = c.substr(10).split('/')[0].trim().replace(/^'|'$/g, '').trim();
        cards[k] = v;
      }
      return cards;
    }).catch(function () { return {}; });
  }
  function detect(item) {
    var f = item.file, path = item.path || f.name;
    var byName = typeFromText(f.name.replace(/\.[^.]+$/, ''));
    var parts = path.split('/').slice(0, -1).reverse();
    var byDir = null;
    for (var i = 0; i < parts.length && !byDir; i++) byDir = typeFromText(parts[i]);
    var fallback = /\.(jpe?g|png|tiff?|webp|bmp|gif)$/i.test(f.name) ? 'stack' : 'light';
    if (/\.(fits?|fts)$/i.test(f.name)) {
      return fitsCards(f).then(function (c) {
        item.filter = c.FILTER || '';
        item.exp = c.EXPTIME || c.EXPOSURE || '';
        return typeFromText(c.IMAGETYP || c.FRAME || c.IMAGETYPE || '') || byName || byDir || fallback;
      });
    }
    return Promise.resolve(byName || byDir || fallback);
  }

  function addFiles(list) {
    var added = 0;
    list.forEach(function (it) {
      if (!EXT.test(it.file.name) || it.file.name.startsWith('.')) return;
      if (items.some(function (x) { return x.path === it.path && x.file.size === it.file.size; })) return;
      it.state = 'queued'; it.sent = 0;
      items.push(it); added++;
      renderRow(it);
      detect(it).then(function (t) { it.type = TYPES.indexOf(t) >= 0 ? t : (window.UPLOAD_DEFAULT || TYPES[0]); it.sel.value = it.type; updateSummary(); });
    });
    opts.hidden = items.length === 0;
    if (!added && list.length) alert('No supported files (FITS, RAW, JPEG, PNG, TIFF, XISF …) found.');
    updateSummary();
  }

  function renderRow(it) {
    var tr = document.createElement('tr');
    var sel = '<select>' + TYPES.map(function (t) { return '<option value="' + t + '">' + (LABELS[t] || t) + '</option>'; }).join('') + '</select>';
    tr.innerHTML = '<td class="fname"></td><td class="nowrap">' + fmt(it.file.size) + '</td><td>' + sel + '</td>' +
      '<td class="upprog"><div class="progress"><div class="bar"></div></div><span class="muted small st">queued</span></td>' +
      '<td><button type="button" class="btn small" title="Remove">✕</button></td>';
    tr.querySelector('.fname').textContent = it.path || it.file.name;
    it.row = tr; it.sel = tr.querySelector('select'); it.bar = tr.querySelector('.bar'); it.st = tr.querySelector('.st');
    it.sel.addEventListener('change', function () { it.type = it.sel.value; });
    tr.querySelector('button').onclick = function () {
      if (running || it.state === 'uploading') return;
      items.splice(items.indexOf(it), 1); tr.remove(); opts.hidden = items.length === 0; updateSummary();
    };
    tbody.appendChild(tr);
  }

  function setState(it, state, text) {
    it.state = state; it.st.textContent = text || state;
    it.row.className = 'st-' + state;
    if (state !== 'queued' && state !== 'uploading') { it.sel.disabled = true; }
  }

  function updateSummary() {
    var total = 0, sent = 0, done = 0, dup = 0, failed = 0;
    items.forEach(function (it) {
      total += it.file.size; sent += Math.min(it.sent, it.file.size);
      if (it.state === 'done') done++; if (it.state === 'duplicate') dup++; if (it.state === 'error') failed++;
    });
    totalBar.style.width = total ? (sent / total * 100).toFixed(1) + '%' : '0';
    var counts = {};
    items.forEach(function (it) { counts[it.type || '?'] = (counts[it.type || '?'] || 0) + 1; });
    var byType = Object.keys(counts).map(function (t) { return (LABELS[t] || t) + ': ' + counts[t]; }).join(', ');
    summary.textContent = items.length + ' files, ' + fmt(total) + (byType ? ' — ' + byType : '') +
      (done || dup || failed ? ' — uploaded ' + done + (dup ? ', duplicates skipped ' + dup : '') + (failed ? ', failed ' + failed : '') : '');
  }

  function api(method, url, body, raw) {
    return fetch(url, { method: method, headers: raw ? { 'Content-Type': 'application/octet-stream' } : { 'Content-Type': 'application/json' },
      body: raw ? body : (body ? JSON.stringify(body) : undefined) })
      .then(function (r) { return r.json().catch(function () { return {}; }).then(function (j) { j._status = r.status; return j; }); });
  }
  function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
  function waitWhilePaused() { return paused ? sleep(300).then(waitWhilePaused) : Promise.resolve(); }

  async function uploadOne(it) {
    var common = {};
    common[root.dataset.targetKey] = root.dataset.targetId;
    var sessSel = root.querySelector('.up-session');
    if (!it.uid) {
      var init = await api('POST', '/api/uploads', Object.assign(common, {
        filename: it.file.name, size: it.file.size, frame_type: it.type,
        session_id: sessSel ? sessSel.value : '', skip_dups: root.querySelector('.up-skipdups').checked,
        meta: { filter: it.filter || root.querySelector('.up-filter').value || '', exposure_s: it.exp || '',
                description: root.querySelector('.up-desc').value || '' }
      }));
      if (!init.id) throw new Error(init.error || 'init failed');
      it.uid = init.id; it.sent = 0;
    }
    var t0 = Date.now(), b0 = it.sent, tries = 0;
    while (it.sent < it.file.size) {
      await waitWhilePaused();
      var end = Math.min(it.sent + CHUNK, it.file.size);
      try {
        var r = await api('PUT', '/api/uploads/' + it.uid + '?offset=' + it.sent, it.file.slice(it.sent, end), true);
        if (r._status === 409 && typeof r.offset === 'number') { it.sent = r.offset; continue; }
        if (r._status === 401) throw new Error('Logged out – please sign in again');
        if (r._status === 404) { it.uid = null; it.sent = 0; throw new Error('Upload expired – press Start to upload again'); }
        if (r._status !== 200) throw new Error(r.error || ('HTTP ' + r._status));
        it.sent = r.offset; tries = 0;
      } catch (e) {
        if (/Logged out|expired/.test(e.message) || ++tries > 8) throw e;
        setState(it, 'uploading', 'connection problem – retry ' + tries + '/8…');
        await sleep(Math.min(30000, 1000 * Math.pow(2, tries)));
        var s = await api('GET', '/api/uploads/' + it.uid).catch(function () { return {}; });
        if (typeof s.offset === 'number') it.sent = s.offset;
        continue;
      }
      var secs = (Date.now() - t0) / 1000, speed = secs > 0 ? (it.sent - b0) / secs : 0;
      it.bar.style.width = (it.sent / it.file.size * 100).toFixed(1) + '%';
      setState(it, 'uploading', Math.round(it.sent / it.file.size * 100) + '% · ' + fmt(speed) + '/s');
      updateSummary();
    }
    setState(it, 'uploading', 'verifying…');
    var fin = await api('POST', '/api/uploads/' + it.uid + '/finish');
    if (fin.duplicate) { setState(it, 'duplicate', 'skipped – already exists: ' + fin.existing); return; }
    if (!fin.ok) throw new Error(fin.error || 'finish failed');
    setState(it, 'done', fin.duplicate_of ? 'done (same content as ' + fin.duplicate_of + ')' : 'done ✓');
  }

  async function runQueue() {
    running = true; startBtn.disabled = true; pauseBtn.hidden = false;
    root.querySelectorAll('.up-session, .up-alltype, .up-filter, .up-desc, .up-skipdups').forEach(function (e) { e.disabled = true; });
    var it, tried = new Set();
    while ((it = items.find(function (x) { return (x.state === 'queued' || x.state === 'error') && !tried.has(x); }))) {
      tried.add(it);
      it.type = it.sel.value;
      setState(it, 'uploading', 'starting…');
      try { await uploadOne(it); }
      catch (e) { setState(it, 'error', '✗ ' + e.message + ' – press Start to retry'); }
      updateSummary();
    }
    running = false; startBtn.disabled = false; pauseBtn.hidden = true; paused = false; pauseBtn.textContent = 'Pause';
    root.querySelectorAll('.up-session, .up-alltype, .up-filter, .up-desc, .up-skipdups').forEach(function (e) { e.disabled = false; });
    var ok = items.filter(function (x) { return x.state === 'done'; }).length;
    var failed = items.filter(function (x) { return x.state === 'error'; }).length;
    startBtn.textContent = failed ? 'Retry failed' : 'Start upload';
    var left = items.some(function (x) { return x.state === 'queued' || x.state === 'error'; });
    window.UPLOADER_BUSY = left;
    if (ok && window.startStatusPoll) window.startStatusPoll();
    else if (ok && !left) location.reload();
  }

  // ---- inputs ----
  root.querySelector('.pick-files').addEventListener('change', function (e) {
    addFiles([...e.target.files].map(function (f) { return { file: f, path: f.name }; })); e.target.value = '';
  });
  root.querySelector('.pick-dir').addEventListener('change', function (e) {
    addFiles([...e.target.files].map(function (f) { return { file: f, path: f.webkitRelativePath || f.name }; })); e.target.value = '';
  });
  function readEntry(entry, path, out) {
    return new Promise(function (resolve) {
      if (entry.isFile) { entry.file(function (f) { out.push({ file: f, path: path + f.name }); resolve(); }, resolve); }
      else if (entry.isDirectory) {
        var reader = entry.createReader(), all = [];
        (function next() {
          reader.readEntries(function (ents) {
            if (!ents.length) { Promise.all(all.map(function (en) { return readEntry(en, path + entry.name + '/', out); })).then(resolve); }
            else { all = all.concat(ents); next(); }
          }, resolve);
        })();
      } else resolve();
    });
  }
  ['dragenter', 'dragover'].forEach(function (ev) { dz.addEventListener(ev, function (e) { e.preventDefault(); dz.classList.add('over'); }); });
  ['dragleave', 'drop'].forEach(function (ev) { dz.addEventListener(ev, function (e) { e.preventDefault(); dz.classList.remove('over'); }); });
  dz.addEventListener('drop', function (e) {
    var out = [], entries = [...(e.dataTransfer.items || [])].map(function (i) { return i.webkitGetAsEntry && i.webkitGetAsEntry(); }).filter(Boolean);
    if (entries.length) Promise.all(entries.map(function (en) { return readEntry(en, '', out); })).then(function () { addFiles(out); });
    else addFiles([...e.dataTransfer.files].map(function (f) { return { file: f, path: f.name }; }));
    root.closest('details').open = true;
  });
  // dropping anywhere on the page opens the uploader
  function hasFiles(e) { return e.dataTransfer && [...e.dataTransfer.types].indexOf('Files') >= 0; }
  document.addEventListener('dragover', function (e) { if (hasFiles(e)) { e.preventDefault(); root.closest('details').open = true; } });
  document.addEventListener('drop', function (e) {
    if (!hasFiles(e) || dz.contains(e.target)) return;
    e.preventDefault();                      // never let the browser open/download a dropped file
    var out = [], entries = [...(e.dataTransfer.items || [])].map(function (i) { return i.webkitGetAsEntry && i.webkitGetAsEntry(); }).filter(Boolean);
    if (entries.length) Promise.all(entries.map(function (en) { return readEntry(en, '', out); })).then(function () { addFiles(out); });
    else addFiles([...e.dataTransfer.files].map(function (f) { return { file: f, path: f.name }; }));
  });

  root.querySelector('.up-alltype').addEventListener('change', function (e) {
    if (!e.target.value) return;
    items.forEach(function (it) { if (it.state === 'queued' || it.state === 'error') { it.type = e.target.value; it.sel.value = e.target.value; } });
    e.target.value = ''; updateSummary();
  });
  startBtn.addEventListener('click', function () { if (!running && items.length) runQueue(); });
  pauseBtn.addEventListener('click', function () { paused = !paused; pauseBtn.textContent = paused ? 'Resume' : 'Pause'; });
  root.querySelector('.up-clear').addEventListener('click', function () {
    if (running) return;
    items = []; tbody.innerHTML = ''; opts.hidden = true; startBtn.textContent = 'Start upload'; updateSummary();
  });
  window.addEventListener('beforeunload', function (e) { if (running) { e.preventDefault(); e.returnValue = ''; } });
})();
