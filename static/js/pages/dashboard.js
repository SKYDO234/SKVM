/* Dashboard cockpit — live tiles, fleet chart, alert acknowledgements. */
(function () {
  "use strict";
  const SKVM = window.SKVM;

  SKVM.ready(function () {
    const chart = SKVM.areaChart("#fleet-chart", {
      series: ["cpu", "ram", "rx"],
      labels: { cpu: "CPU %", ram: "Memory %", rx: "Net in" },
      keys: { cpu: "cpu", ram: "ram", rx: "rx" },
      unit: "%",
      height: 250,
      xLabel: (point) => new Date(point.ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
    });

    function paintSpark(id, values, color) {
      const node = document.getElementById(id);
      if (node) SKVM.sparkline(node, values, { color, height: 34 });
    }

    function mergeSeries(data) {
      // Aggregate every server's live series into one fleet curve.
      const buckets = new Map();
      Object.values(data.series || {}).forEach((series) => {
        (series || []).forEach((point) => {
          const key = Math.round((point.t || 0) * 2) / 2;
          const entry = buckets.get(key) || { ts: point.ts, t: point.t, cpu: 0, ram: 0, rx: 0, tx: 0, n: 0 };
          entry.cpu += point.cpu || 0;
          entry.ram += point.ram || 0;
          entry.rx += point.rx || 0;
          entry.tx += point.tx || 0;
          entry.n += 1;
          buckets.set(key, entry);
        });
      });
      return Array.from(buckets.values())
        .sort((a, b) => (a.t || 0) - (b.t || 0))
        .map((entry) => ({
          ts: entry.ts,
          t: entry.t,
          cpu: entry.n ? entry.cpu / entry.n : 0,
          ram: entry.n ? entry.ram / entry.n : 0,
          rx: entry.n ? entry.rx / entry.n : 0,
          tx: entry.n ? entry.tx / entry.n : 0,
        }));
    }

    const live = SKVM.liveSource({
      url: "/api/v1/overview",
      interval: 5,
      onData(data) {
        const totals = data.summary || {};
        document.querySelectorAll("[data-kpi]").forEach((node) => {
          const key = node.dataset.kpi;
          if (totals[key] != null) node.textContent = totals[key];
        });

        const cpuTile = document.querySelector('[data-spark="cpu"]');
        const series = mergeSeries(data);
        chart.set(series);

        const cpuValues = series.map((point) => point.cpu);
        const ramValues = series.map((point) => point.ram);
        paintSpark("spark-cpu", cpuValues, "#7c5cff");
        paintSpark("spark-ram", ramValues, "#22d3ee");
        paintSpark("spark-fleet", series.map((point) => point.rx), "#34d399");

        // Inline per-server live numbers on dashboard cards
        Object.entries(data.live || {}).forEach(([uid, stats]) => {
          const card = document.querySelector(`[data-server="${uid}"]`);
          if (!card) return;
          const values = card.querySelectorAll(".metric-value");
          if (values[0]) values[0].textContent = (stats.cpu_pct != null ? stats.cpu_pct.toFixed(1) : "0.0") + "%";
          if (values[1]) values[1].textContent = (stats.ram_pct != null ? stats.ram_pct.toFixed(1) : "0.0") + "%";
          const meters = card.querySelectorAll(".meter i");
          if (meters[0]) meters[0].style.width = Math.min(100, stats.cpu_pct || 0) + "%";
          if (meters[1]) meters[1].style.width = Math.min(100, stats.ram_pct || 0) + "%";
        });
      },
    });

    // Range toggle switches between live stream and stored rollups
    const toggle = document.getElementById("range-toggle");
    toggle && toggle.addEventListener("click", async (event) => {
      const button = event.target.closest("button[data-range]");
      if (!button) return;
      toggle.querySelectorAll("button").forEach((node) => node.classList.toggle("active", node === button));
      const range = button.dataset.range;
      const label = document.getElementById("chart-window");
      if (range === "live") {
        document.getElementById("chart-live").classList.remove("hidden");
        if (label) label.textContent = "streaming · last 6 minutes";
        live.start();
        return;
      }
      live.stop();
      document.getElementById("chart-live").classList.add("hidden");
      if (label) label.textContent = `stored rollups · last ${range}`;
      try {
        const servers = await SKVM.get("/api/v1/servers?per_page=50");
        const collected = await Promise.all(servers.items.map((server) =>
          SKVM.get(`/servers/${server.uid}/api/v1/metrics?window=${range}`).catch(() => null)));
        const merged = new Map();
        collected.filter(Boolean).forEach((payload) => {
          (payload.history || []).forEach((point) => {
            const key = point.ts;
            const entry = merged.get(key) || { ts: point.ts, cpu: 0, ram: 0, rx: 0, n: 0 };
            entry.cpu += point.cpu || 0;
            entry.ram += point.ram || 0;
            entry.rx += point.rx || 0;
            entry.n += 1;
            merged.set(key, entry);
          });
        });
        chart.set(Array.from(merged.values())
          .sort((a, b) => new Date(a.ts) - new Date(b.ts))
          .map((entry) => ({ ...entry, cpu: entry.cpu / entry.n, ram: entry.ram / entry.n, rx: entry.rx / entry.n })));
      } catch (error) {
        SKVM.handleError(error);
      }
    });

    // Alert acknowledgement buttons
    document.addEventListener("click", async (event) => {
      const button = event.target.closest("[data-ack]");
      if (!button) return;
      button.classList.add("is-loading");
      try {
        await SKVM.post(`/alerts/api/v1/events/${button.dataset.ack}/ack`);
        SKVM.ok("Alert acknowledged");
        button.closest(".row-item").remove();
      } catch (error) {
        SKVM.handleError(error);
      } finally {
        button.classList.remove("is-loading");
      }
    });

    // Animate the KPI numbers once on load
    document.querySelectorAll(".kpi .value").forEach((node) => {
      const raw = parseFloat(node.textContent.replace(/[^0-9.]/g, ""));
      if (Number.isFinite(raw)) SKVM.countUp(node, raw, { decimals: String(raw).includes(".") ? 1 : 0 });
    });
  });
})();
