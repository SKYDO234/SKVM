(function () {
  'use strict';

  /* ---------- sparkline charts (real polled data only) ---------- */
  function drawSpark(canvas, seriesList) {
    if (!canvas || !canvas.getContext) return;
    var dpr = window.devicePixelRatio || 1;
    var w = canvas.clientWidth || 300, h = canvas.clientHeight || 80;
    if (canvas.width !== w * dpr) { canvas.width = w * dpr; canvas.height = h * dpr; }
    var ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    var colors = ['#38bdf8', '#a855f7', '#34d399'];
    // gridlines
    ctx.strokeStyle = 'rgba(148,184,255,.09)'; ctx.lineWidth = 1;
    [0.25, 0.5, 0.75].forEach(function (f) {
      ctx.beginPath(); ctx.moveTo(0, h * f); ctx.lineTo(w, h * f); ctx.stroke();
    });
    seriesList.forEach(function (series, si) {
      if (!series || series.length < 2) return;
      var color = colors[si % colors.length];
      var stepX = w / 39; // history window is capped at 40 samples
      var grad = ctx.createLinearGradient(0, 0, 0, h);
      grad.addColorStop(0, color + '44'); grad.addColorStop(1, color + '00');
      ctx.beginPath();
      series.forEach(function (v, i) {
        var x = w - (series.length - 1 - i) * stepX;
        var y = h - (Math.max(0, Math.min(100, v)) / 100) * (h - 8) - 4;
        i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
      });
      ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.lineJoin = 'round'; ctx.stroke();
      // area fill
      var lastX = w, firstX = w - (series.length - 1) * stepX;
      ctx.lineTo(lastX, h); ctx.lineTo(firstX, h); ctx.closePath();
      ctx.fillStyle = grad; ctx.fill();
      // last point
      var lv = series[series.length - 1];
      ctx.beginPath(); ctx.arc(w - 2, h - (Math.max(0, Math.min(100, lv)) / 100) * (h - 8) - 4, 3, 0, Math.PI * 2);
      ctx.fillStyle = color; ctx.fill();
    });
  }
  window.SKVM = { drawSpark: drawSpark };

  /* ---------- toasts ---------- */
  function toast(msg, isErr) {
    var stack = document.getElementById('toastStack');
    if (!stack) return;
    var el = document.createElement('div');
    el.className = 'toast' + (isErr ? ' err' : '');
    el.innerHTML = '<i data-lucide="' + (isErr ? 'circle-alert' : 'circle-check') + '"></i><span></span>';
    el.querySelector('span').textContent = msg;
    stack.appendChild(el);
    if (window.lucide) lucide.createIcons();
    setTimeout(function () { el.style.opacity = '0'; el.style.transition = 'opacity .4s'; setTimeout(function () { el.remove(); }, 420); }, 3600);
  }
  window.SKVM.toast = toast;
  // Auto-dismiss server flash toasts after a while (console dumps stay).
  document.querySelectorAll('.flash-stack .flash:not(.flash-console)').forEach(function (el) {
    setTimeout(function () { el.style.opacity = '0'; el.style.transition = 'opacity .5s'; setTimeout(function () { el.remove(); }, 520); }, 6000);
  });

  /* ---------- confirmation modal (data-confirm on forms/buttons) ---------- */
  var modalRoot = document.getElementById('modalRoot');
  function openConfirm(message, onApprove) {
    if (!modalRoot) { if (window.confirm(message)) onApprove(); return; }
    modalRoot.innerHTML =
      '<div class="modal-backdrop"><div class="modal" role="alertdialog" aria-modal="true">' +
      '<div class="m-ic"><i data-lucide="triangle-alert"></i></div>' +
      '<h3>Are you sure?</h3><p></p>' +
      '<div class="modal-actions"><button class="btn" data-m="cancel" type="button">Cancel</button>' +
      '<button class="btn btn-danger" data-m="ok" type="button">Yes, continue</button></div></div></div>';
    modalRoot.querySelector('p').textContent = message;
    if (window.lucide) lucide.createIcons();
    function close() { modalRoot.innerHTML = ''; document.removeEventListener('keydown', esc); }
    function esc(e) { if (e.key === 'Escape') close(); }
    document.addEventListener('keydown', esc);
    modalRoot.querySelector('[data-m="cancel"]').addEventListener('click', close);
    modalRoot.querySelector('.modal-backdrop').addEventListener('click', function (e) { if (e.target === e.currentTarget) close(); });
    modalRoot.querySelector('[data-m="ok"]').addEventListener('click', function () { close(); onApprove(); });
    modalRoot.querySelector('[data-m="ok"]').focus();
  }
  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (!(form instanceof HTMLFormElement)) return;
    var submitter = e.submitter;
    var msg = form.getAttribute('data-confirm') || (submitter && submitter.getAttribute ? submitter.getAttribute('data-confirm') : null);
    if (!msg || form.dataset.confirmed === '1') return;
    e.preventDefault();
    openConfirm(msg, function () {
      form.dataset.confirmed = '1';
      if (submitter) form.requestSubmit(submitter); else form.requestSubmit();
    });
  });

  /* ---------- copy-to-clipboard buttons ---------- */
  document.addEventListener('click', function (e) {
    var btn = e.target.closest ? e.target.closest('[data-copy]') : null;
    if (!btn) return;
    var text = btn.getAttribute('data-copy') || '';
    function done() { toast('Copied to clipboard'); }
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, function () { toast('Copy failed', true); });
    else {
      var ta = document.createElement('textarea'); ta.value = text; document.body.appendChild(ta);
      ta.select(); try { document.execCommand('copy'); done(); } catch (err) { toast('Copy failed', true); } ta.remove();
    }
  });

  /* ---------- sidebar: mobile drawer + desktop collapse ---------- */
  var menuBtn = document.getElementById('menuBtn');
  var sidebar = document.getElementById('sidebar');
  var shell = document.getElementById('shell');
  if (shell && localStorage.getItem('skvm-sb') === 'collapsed') shell.classList.add('sb-collapsed');
  var sbToggle = document.getElementById('sbToggle');
  if (sbToggle && shell) sbToggle.addEventListener('click', function () {
    shell.classList.toggle('sb-collapsed');
    localStorage.setItem('skvm-sb', shell.classList.contains('sb-collapsed') ? 'collapsed' : 'open');
  });
  if (menuBtn && sidebar) {
    menuBtn.addEventListener('click', function () { sidebar.classList.toggle('open'); });
    document.addEventListener('click', function (e) {
      if (window.innerWidth <= 900 && sidebar.classList.contains('open') &&
          !sidebar.contains(e.target) && e.target !== menuBtn) sidebar.classList.remove('open');
    });
  }

  /* ---------- live provisioning progress (/api/jobs/<id>) ---------- */
  function pollJob(id) {
    fetch('/api/jobs/' + id, { headers: { 'Accept': 'application/json' } })
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
      .then(function (j) {
        var row = document.querySelector('[data-job="' + id + '"]');
        if (row) {
          var bar = row.querySelector('.progress i');
          var msg = row.querySelector('.job-message');
          var pct = row.querySelector('.job-pct');
          if (bar) bar.style.width = (j.progress || 0) + '%';
          if (msg) msg.textContent = j.message || '';
          if (pct) pct.textContent = (j.progress || 0) + '%';
        }
        if (['done', 'error'].indexOf(j.status) === -1) setTimeout(function () { pollJob(id); }, 1800);
        else setTimeout(function () { location.reload(); }, 1000);
      })
      .catch(function () { setTimeout(function () { pollJob(id); }, 3000); });
  }
  (window.SKVM_JOBS || []).forEach(pollJob);

  /* ---------- node stats refresh (host figures, labelled) ---------- */
  function renderNode(d) {
    var box = document.getElementById('nodeStats');
    if (!box) return;
    var set = function (k, txt) { var el = box.querySelector('[data-k="' + k + '"]'); if (el) el.textContent = txt; };
    set('cpu', d.cpu_count + ' cores · ' + d.cpu_percent + '%');
    set('ram', d.memory_used_mb + ' / ' + d.memory_total_mb + ' MiB');
    set('disk', d.disk_used_gb + ' / ' + d.disk_total_gb + ' GiB');
    set('backends', (d.docker.runtime || 'docker') + ': ' + (d.docker.available ? 'ready' : 'unavailable') + ' · Incus: ' + (d.incus.available ? 'ready' : 'unavailable'));
    set('lxcfs', d.lxcfs && d.lxcfs.ok ? 'Virtualized' : 'Unavailable');
  }
  var refreshBtn = document.getElementById('refreshNode');
  if (refreshBtn) {
    refreshBtn.addEventListener('click', function () {
      fetch('/api/node', { headers: { 'Accept': 'application/json' } })
        .then(function (r) { return r.json(); }).then(renderNode).catch(function () { toast('Node refresh failed', true); });
    });
  }

  /* ---------- fleet search + status filters ---------- */
  var searchInput = document.getElementById('fleetSearch');
  var filterRow = document.getElementById('fleetFilters');
  if (searchInput || filterRow) {
    var activeFilter = 'all';
    function applyFilter() {
      var q = searchInput ? searchInput.value.trim().toLowerCase() : '';
      var shown = 0;
      document.querySelectorAll('[data-vps-card]').forEach(function (card) {
        var okState = activeFilter === 'all' ||
          (activeFilter === 'error' ? ['error'].indexOf(card.dataset.state) !== -1 : card.dataset.state === activeFilter);
        var okText = !q || (card.dataset.search || '').indexOf(q) !== -1;
        var show = okState && okText;
        card.style.display = show ? '' : 'none';
        if (show) shown++;
      });
      var count = document.getElementById('fleetCount');
      if (count) count.textContent = shown + ' shown';
      var empty = document.getElementById('fleetEmpty');
      if (empty) empty.classList.toggle('hidden', shown !== 0);
    }
    if (searchInput) searchInput.addEventListener('input', applyFilter);
    if (filterRow) filterRow.addEventListener('click', function (e) {
      var chip = e.target.closest('[data-filter]');
      if (!chip) return;
      filterRow.querySelectorAll('.chip').forEach(function (c) { c.classList.remove('active'); });
      chip.classList.add('active');
      activeFilter = chip.getAttribute('data-filter');
      applyFilter();
    });
  }

  /* ---------- live per-VPS stats + fleet chart (real polling) ---------- */
  var runningIds = window.SKVM_RUNNING || [];
  if (runningIds.length) {
    var cpuHist = [], memHist = [];
    function sample() {
      if (document.hidden) return;
      var jobs = runningIds.map(function (id) {
        return fetch('/api/vps/' + id + '/stats', { headers: { 'Accept': 'application/json' } })
          .then(function (r) { return r.json(); })
          .then(function (d) { return { id: id, d: d }; })
          .catch(function () { return null; });
      });
      Promise.all(jobs).then(function (results) {
        var cpus = [], mems = [];
        results.forEach(function (res) {
          if (!res) return;
          var d = res.d;
          var card = document.querySelector('[data-live-stats="' + res.id + '"]');
          if (card) {
            var cpuBar = card.querySelector('[data-role="cpu-bar"]');
            var cpuVal = card.querySelector('[data-role="cpu-val"]');
            var memBar = card.querySelector('[data-role="mem-bar"]');
            var memVal = card.querySelector('[data-role="mem-val"]');
            if (cpuBar) cpuBar.style.width = (typeof d.cpu_percent === 'number' ? d.cpu_percent : 0) + '%';
            if (cpuVal) cpuVal.textContent = typeof d.cpu_percent === 'number' ? d.cpu_percent + '%' : 'n/a';
            if (memBar) memBar.style.width = (typeof d.memory_percent === 'number' ? d.memory_percent : 0) + '%';
            if (memVal) memVal.textContent = typeof d.memory_percent === 'number' ? d.memory_percent + '%' : 'n/a';
          }
          if (typeof d.cpu_percent === 'number') cpus.push(d.cpu_percent);
          if (typeof d.memory_percent === 'number') mems.push(d.memory_percent);
        });
        var avg = function (a) { return a.length ? a.reduce(function (x, y) { return x + y; }, 0) / a.length : null; };
        var ca = avg(cpus), ma = avg(mems);
        if (ca !== null) cpuHist.push(Math.round(ca * 10) / 10);
        if (ma !== null) memHist.push(Math.round(ma * 10) / 10);
        if (cpuHist.length > 40) cpuHist.shift();
        if (memHist.length > 40) memHist.shift();
        var cpuNow = document.getElementById('cpuNow');
        var memNow = document.getElementById('memNow');
        if (cpuNow) cpuNow.textContent = ca === null ? 'n/a' : ca.toFixed(1) + '%';
        if (memNow) memNow.textContent = ma === null ? 'n/a' : ma.toFixed(1) + '%';
        var state = document.getElementById('chartState');
        if (state) state.textContent = 'live';
        drawSpark(document.getElementById('fleetChart'), [cpuHist, memHist]);
      });
    }
    sample();
    setInterval(sample, 5000);
  }
})();
