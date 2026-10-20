/* SKVM server cockpit — live telemetry, gauges, power, snapshots, settings. */
(function () {
  "use strict";
  const SKVM = window.SKVM;

  SKVM.ready(function () {
    const dataEl = document.getElementById("server-data");
    if (!dataEl) return;
    let data = {};
    try { data = JSON.parse(dataEl.textContent); } catch (_) { return; }
    const uid = data.uid;
    let windowKey = "live";

    /* ------------------------------------------------------- root password */
    document.querySelectorAll("[data-reveal]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const span = document.querySelector("#root-password [data-secret]");
        if (!span) return;
        const secret = span.dataset.secret || "";
        if (!secret) return;
        span.textContent = span.textContent.indexOf("•") === 0 ? secret : "••••••••••••";
      });
    });

    /* ---------------------------------------------------------- resize UI */
    [["r-cpu", "r-cpu-out", 1], ["r-ram", "r-ram-out", 1024], ["r-disk", "r-disk-out", 1]].forEach((row) => {
      const slider = document.getElementById(row[0]);
      const out = document.getElementById(row[1]);
      if (!slider || !out) return;
      const divisor = row[2];
      const paint = () => { out.textContent = (Number(slider.value) / divisor).toFixed(divisor > 1 ? 1 : 0); };
      slider.addEventListener("input", paint);
      paint();
    });

    /* -------------------------------------------------------------- chart */
    const chart = SKVM.areaChart("#server-chart", {
      series: ["cpu", "ram", "rx"],
      labels: { cpu: "CPU %", ram: "Memory %", rx: "Net in KB/s" },
      keys: { cpu: "cpu", ram: "ram", rx: "rx" },
      height: 250,
      unit: "",
      xLabel: (point) => (point && point.ts ? new Date(point.ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : ""),
    });
    chart.set(Array.isArray(data.series) ? data.series : []);

    const gauges = {
      cpu: SKVM.gauge("#gauge-cpu", { value: (data.stats && data.stats.cpu_pct) || 0, label: "CPU load" }),
      ram: SKVM.gauge("#gauge-ram", { value: (data.stats && data.stats.ram_pct) || 0, label: "Memory" }),
    };

    function paintSpark(id, values, color) {
      const node = document.getElementById(id);
      if (node && values.length) SKVM.sparkline(node, values, { color, height: 32 });
    }

    function applyStats(stats) {
      if (!stats) return;
      const tiles = {
        "cpu-live": stats.cpu_pct, "ram-live": stats.ram_pct,
        "disk-live": stats.disk_pct, "net-live": stats.net_rx_kbps,
      };
      Object.keys(tiles).forEach((key) => {
        const value = tiles[key];
        if (value == null) return;
        const tile = document.querySelector(`[data-kpi="${key}"] .metric-value`);
        if (tile) tile.textContent = Number(value).toFixed(key === "net-live" ? 0 : 1);
        const meter = document.querySelector(`[data-kpi="${key}"] .meter i`);
        if (meter && key !== "net-live") meter.style.width = Math.min(100, Number(value)) + "%";
      });
      if (gauges.cpu) gauges.cpu.set(stats.cpu_pct || 0);
      if (gauges.ram) gauges.ram.set(stats.ram_pct || 0);
    }
    applyStats(data.stats);

    /* ------------------------------------------------------- window picker */
    const picker = document.getElementById("live-window");
    picker?.addEventListener("click", async (event) => {
      const button = event.target.closest("[data-window]");
      if (!button) return;
      windowKey = button.dataset.window;
      picker.querySelectorAll("button").forEach((node) => node.classList.toggle("active", node === button));
      if (windowKey === "live") {
        chart.set(Array.isArray(data.series) ? data.series : []);
        return;
      }
      try {
        const payload = await SKVM.get(`/servers/${uid}/api/v1/metrics?window=${windowKey}`);
        const rows = payload.history || payload.series || [];
        chart.set(rows);
        paintSpark("spark-cpu-live", rows.map((p) => p.cpu || 0), "#7c5cff");
      } catch (error) {
        SKVM.handleError(error);
      }
    });

    /* --------------------------------------------------------- live stream */
    const stream = SKVM.metricsSocket(uid, {
      onMessage(message) {
        if (!message || message.type !== "metrics") return;
        if (message.series) {
          data.series = message.series;
          if (windowKey === "live") chart.set(message.series);
          paintSpark("spark-cpu-live", message.series.map((p) => p.cpu || 0), "#7c5cff");
          paintSpark("spark-ram-live", message.series.map((p) => p.ram || 0), "#22d3ee");
          paintSpark("spark-disk-live", message.series.map((p) => p.disk || 0), "#34d399");
          paintSpark("spark-net-live", message.series.map((p) => p.rx || 0), "#fbbf24");
        }
        applyStats(message.stats);
        const pill = document.querySelector(".live-pill .state");
        if (pill && message.status) pill.textContent = message.status;
      },
    });
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) { stream.close(); return; }
      location.reload();
    });

    /* -------------------------------------------------------------- power */
    document.addEventListener("click", async (event) => {
      const button = event.target.closest("[data-power]");
      if (!button) return;
      const target = button.dataset.power;
      const action = button.dataset.action;
      let reason = "";
      if (action === "suspend") {
        reason = await SKVM.prompt({ title: "Suspend server", label: "Reason shown to the owner", value: "billing" });
        if (reason === null) return;
      }
      button.classList.add("is-loading");
      try {
        const payload = await SKVM.post(`/servers/${target}/api/v1/power`, { action, reason });
        SKVM.ok(payload.message || `${action} issued`);
        setTimeout(() => window.location.reload(), 900);
      } catch (error) {
        SKVM.handleError(error);
        button.classList.remove("is-loading");
      }
    });

    document.getElementById("btn-sync")?.addEventListener("click", async (event) => {
      const button = event.currentTarget;
      button.classList.add("is-loading");
      try {
        const payload = await SKVM.post(`/servers/${uid}/api/v1/sync`);
        SKVM.ok(payload.message || "State re-read from the driver");
        setTimeout(() => window.location.reload(), 700);
      } catch (error) { SKVM.handleError(error); button.classList.remove("is-loading"); }
    });

    document.getElementById("btn-retry")?.addEventListener("click", async () => {
      try {
        const payload = await SKVM.post(`/servers/${uid}/api/v1/power`, { action: "start" });
        SKVM.ok(payload.message || "Provisioning retried");
        setTimeout(() => window.location.reload(), 900);
      } catch (error) { SKVM.handleError(error); }
    });

    /* ------------------------------------------------------------- resize */
    document.getElementById("btn-resize")?.addEventListener("click", async (event) => {
      const button = event.currentTarget;
      const cpu = Number(document.getElementById("r-cpu")?.value || 0);
      const ramMb = Number(document.getElementById("r-ram")?.value || 0);
      const diskGb = Number(document.getElementById("r-disk")?.value || 0);
      const ok = await SKVM.confirm({
        title: "Apply new resources?",
        message: `${cpu} vCPU · ${Math.round((ramMb / 1024) * 10) / 10} GB RAM · ${diskGb} GB disk — the instance may restart briefly.`,
        confirmLabel: "Apply resize",
        danger: false,
      });
      if (!ok) return;
      button.classList.add("is-loading");
      try {
        const payload = await SKVM.post(`/servers/${uid}/api/v1/resize`, { cpu, ram_mb: ramMb, disk_gb: diskGb });
        SKVM.ok(payload.message || "Resources updated");
        setTimeout(() => window.location.reload(), 800);
      } catch (error) { SKVM.handleError(error); button.classList.remove("is-loading"); }
    });

    /* ----------------------------------------------------------- settings */
    const form = document.getElementById("server-settings");
    form?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const payload = SKVM.formData(form);
      payload.auto_renew = form.querySelector('[name="auto_renew"]').checked;
      payload.backup_enabled = form.querySelector('[name="backup_enabled"]').checked;
      ["snapshot_limit", "bandwidth_gb"].forEach((key) => {
        if (payload[key] != null && payload[key] !== "") payload[key] = Number(payload[key]);
      });
      const button = form.querySelector('[type="submit"]');
      button?.classList.add("is-loading");
      try {
        const result = await SKVM.post(`/servers/${uid}/api/v1/settings`, payload);
        SKVM.ok(result.message || "Settings saved");
        const state = document.getElementById("settings-state");
        if (state) state.textContent = "Saved " + new Date().toLocaleTimeString();
      } catch (error) { SKVM.handleError(error); } finally { button?.classList.remove("is-loading"); }
    });

    /* ------------------------------------------------ reinstall & secrets */
    document.getElementById("btn-reinstall")?.addEventListener("click", async () => {
      const image = document.getElementById("re-os")?.value;
      const ok = await SKVM.confirm({
        title: "Reinstall operating system?",
        message: `${data.name} will be replaced with ${image}. The filesystem is destroyed; snapshots survive.`,
        requireText: data.name,
        confirmLabel: "Reinstall now",
      });
      if (!ok) return;
      try {
        const payload = await SKVM.post(`/servers/${uid}/api/v1/reinstall`, {
          os_image: image,
          root_password: document.getElementById("re-password")?.value || "",
        });
        showSecret(payload);
      } catch (error) { SKVM.handleError(error); }
    });

    document.getElementById("btn-rotate")?.addEventListener("click", async () => {
      try {
        const payload = await SKVM.post(`/servers/${uid}/api/v1/password`, {});
        showSecret(payload);
      } catch (error) { SKVM.handleError(error); }
    });

    function showSecret(payload) {
      if (payload && payload.password) {
        const span = document.querySelector("#root-password [data-secret]");
        if (span) { span.dataset.secret = payload.password; span.textContent = payload.password; }
      }
      SKVM.modal({
        title: "New root credentials",
        subtitle: "Copy them now — the panel only keeps an encrypted copy.",
        html: `<div class="code-block">${SKVM.escape((payload && payload.password) || "auto-generated")}</div>`,
        confirmLabel: "Copied",
      });
    }

    /* -------------------------------------------- snapshots and backups */
    document.addEventListener("click", async (event) => {
      const action = event.target.closest("[data-action]");
      if (action && action.dataset.action === "snapshot-create") {
        const name = await SKVM.prompt({ title: "Take a snapshot", label: "Snapshot name", value: `snap-${new Date().toISOString().slice(0, 10)}` });
        if (name === null) return;
        try {
          const payload = await SKVM.post(`/servers/${uid}/api/v1/snapshots`, { name });
          SKVM.ok(payload.message || "Snapshot created");
          setTimeout(() => window.location.reload(), 700);
        } catch (error) { SKVM.handleError(error); }
        return;
      }
      if (action && action.dataset.action === "backup-create") {
        action.classList.add("is-loading");
        try {
          const payload = await SKVM.post(`/servers/${uid}/api/v1/backups`, { name: `manual-${Date.now()}` });
          SKVM.ok(payload.message || "Backup complete");
          setTimeout(() => window.location.reload(), 700);
        } catch (error) { SKVM.handleError(error); action.classList.remove("is-loading"); }
      }
    });

    document.addEventListener("click", async (event) => {
      const restore = event.target.closest("[data-snapshot-restore]");
      if (restore) {
        const ok = await SKVM.confirm({ title: "Restore snapshot?", message: "The server stops, the filesystem is replaced, then it boots again." });
        if (!ok) return;
        try {
          const payload = await SKVM.post(`/servers/${uid}/api/v1/snapshots/${restore.dataset.snapshotRestore}/restore`, {});
          SKVM.ok(payload.message || "Snapshot restored");
          setTimeout(() => window.location.reload(), 900);
        } catch (error) { SKVM.handleError(error); }
        return;
      }
      const dropSnap = event.target.closest("[data-snapshot-delete]");
      if (dropSnap) {
        const ok = await SKVM.confirm({ title: "Delete snapshot?", message: "The committed image is removed from the hypervisor." });
        if (!ok) return;
        try { await SKVM.del(`/servers/${uid}/api/v1/snapshots/${dropSnap.dataset.snapshotDelete}`); SKVM.ok("Snapshot deleted"); setTimeout(() => window.location.reload(), 500); }
        catch (error) { SKVM.handleError(error); }
        return;
      }
      const dropBackup = event.target.closest("[data-backup-delete]");
      if (dropBackup) {
        const ok = await SKVM.confirm({ title: "Delete backup?", message: "The tarball is erased from the panel host." });
        if (!ok) return;
        try { await SKVM.del(`/servers/${uid}/api/v1/backups/${dropBackup.dataset.backupDelete}`); SKVM.ok("Backup deleted"); setTimeout(() => window.location.reload(), 500); }
        catch (error) { SKVM.handleError(error); }
      }
    });

    /* ------------------------------------------------------------- delete */
    document.getElementById("btn-delete")?.addEventListener("click", async () => {
      const ok = await SKVM.confirm({
        title: `Delete ${data.name}?`,
        message: "The container, its snapshots, backups and metrics history are destroyed. This cannot be undone.",
        requireText: data.name,
        confirmLabel: "Delete permanently",
      });
      if (!ok) return;
      try {
        const payload = await SKVM.post(`/servers/${uid}/api/v1/delete`, { confirm: data.name });
        SKVM.ok(payload.message || "Server deleted");
        setTimeout(() => window.location.assign("/servers/"), 700);
      } catch (error) { SKVM.handleError(error); }
    });

    /* ------------------------------------------------------- ssh command */
    const sshInput = document.getElementById("ssh-command");
    if (sshInput && !sshInput.value) {
      SKVM.get(`/servers/${uid}/api/v1/ssh-command`)
        .then((payload) => { if (payload.command) sshInput.value = payload.command; })
        .catch(() => {});
    }
  });
})();
