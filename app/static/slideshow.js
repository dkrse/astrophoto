/* Fullscreen slideshow of images (by file id). Keys: ←/→, space = pause, Esc = close, F = fullscreen. */
window.Slideshow = (function () {
  var el, img, cap, ids, i, timer, secs, paused, meta = {};
  function build() {
    el = document.createElement('div');
    el.className = 'slideshow';
    el.innerHTML = '<img alt=""><div class="ss-cap"><b></b><span></span></div>' +
      '<div class="ss-ctrl"><button data-a="prev" title="Previous (←)">‹</button><button data-a="play" title="Play / pause (space)">⏸</button>' +
      '<button data-a="next" title="Next (→)">›</button><span class="ss-pos"></span>' +
      '<button data-a="fs" title="Fullscreen (F)">⛶</button><a class="ss-open" title="Open project">↗</a><button data-a="close" title="Close (Esc)">✕</button></div>';
    document.body.appendChild(el);
    img = el.querySelector('img'); cap = el.querySelector('.ss-cap');
    el.addEventListener('click', function (e) {
      var a = e.target.closest('[data-a]'); if (!a) return;
      ({ prev: prev, next: next, play: toggle, close: close, fs: fullscreen })[a.dataset.a]();
    });
    document.addEventListener('keydown', function (e) {
      if (!el || el.hidden) return;
      if (e.key === 'ArrowRight') next(); else if (e.key === 'ArrowLeft') prev();
      else if (e.key === ' ') { e.preventDefault(); toggle(); } else if (e.key === 'Escape') close();
      else if (e.key === 'f' || e.key === 'F') fullscreen();
    });
    var hideT; el.addEventListener('mousemove', function () { el.classList.remove('idle'); clearTimeout(hideT); hideT = setTimeout(function () { el.classList.add('idle'); }, 2500); });
  }
  function loadMeta(list) {
    return fetch('/api/slides?ids=' + list.join(',')).then(function (r) { return r.json(); })
      .then(function (arr) { arr.forEach(function (m) { meta[m.id] = m; }); });
  }
  function show() {
    var id = ids[i];
    var pre = new Image(), my = i;
    pre.onload = function () { if (my !== i) return; img.src = pre.src; img.classList.remove('fade'); void img.offsetWidth; img.classList.add('fade'); };
    pre.src = '/files/' + id + '/thumb';
    var m = meta[id] || {};
    cap.querySelector('b').textContent = m.title || '';
    cap.querySelector('span').textContent = m.sub || '';
    el.querySelector('.ss-open').href = m.url || '#';
    el.querySelector('.ss-pos').textContent = (i + 1) + ' / ' + ids.length;
    new Image().src = '/files/' + ids[(i + 1) % ids.length] + '/thumb';   // preload next
    schedule();
  }
  function schedule() { clearTimeout(timer); if (!paused && ids.length > 1) timer = setTimeout(next, secs * 1000); }
  function next() { i = (i + 1) % ids.length; show(); }
  function prev() { i = (i - 1 + ids.length) % ids.length; show(); }
  function toggle() { paused = !paused; el.querySelector('[data-a=play]').textContent = paused ? '▶' : '⏸'; schedule(); }
  function fullscreen() { if (document.fullscreenElement) document.exitFullscreen(); else el.requestFullscreen && el.requestFullscreen(); }
  function close() { clearTimeout(timer); el.hidden = true; document.body.style.overflow = ''; if (document.fullscreenElement) document.exitFullscreen(); }
  function start(list, startAt, seconds, startPaused) {
    if (!list || !list.length) return;
    if (!el) build();
    ids = list; i = startAt || 0; secs = seconds || 6; paused = !!startPaused;
    el.querySelector('[data-a=play]').textContent = paused ? '▶' : '⏸';
    el.hidden = false; document.body.style.overflow = 'hidden';
    if (!startPaused && el.requestFullscreen) el.requestFullscreen().catch(function () {});
    var need = ids.filter(function (x) { return !meta[x]; });
    (need.length ? loadMeta(need) : Promise.resolve()).then(show);
  }
  return { start: start };
})();
