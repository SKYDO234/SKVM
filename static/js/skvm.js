(function () {
  'use strict';
  // Mobile sidebar
  var menuBtn = document.getElementById('menuBtn');
  var sidebar = document.getElementById('sidebar');
  if (menuBtn && sidebar) {
    menuBtn.addEventListener('click', function () { sidebar.classList.toggle('open'); });
    document.addEventListener('click', function (e) {
      if (window.innerWidth <= 860 && sidebar.classList.contains('open') &&
          !sidebar.contains(e.target) && e.target !== menuBtn) sidebar.classList.remove('open');
    });
  }

  // Live provisioning progress (real job state from /api/jobs/<id>)
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

  // Node stats refresh (host figures, clearly labelled in the UI)
  var refreshBtn = document.getElementById('refreshNode');
  if (refreshBtn) {
    refreshBtn.addEventListener('click', function () {
      fetch('/api/node', { headers: { 'Accept': 'application/json' } })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          var box = document.getElementById('nodeStats');
          if (!box) return;
          box.querySelector('[data-k="cpu"]').textContent = d.cpu_count + ' cores · ' + d.cpu_percent + '%';
          box.querySelector('[data-k="ram"]').textContent = d.memory_used_mb + ' / ' + d.memory_total_mb + ' MiB';
          box.querySelector('[data-k="disk"]').textContent = d.disk_used_gb + ' / ' + d.disk_total_gb + ' GiB';
          box.querySelector('[data-k="backends"]').textContent =
            (d.docker.runtime || 'docker') + ': ' + (d.docker.available ? 'ready' : 'unavailable') + ' · Incus: ' + (d.incus.available ? 'ready' : 'unavailable');
        })
        .catch(function () {});
    });
  }
})();
