/* SKVM telemetry page — windowed charts, gauges, live stream, CSV export. */
(function () {
  "use strict";
  const SKVM = window.SKVM;

  SKVM.ready(function () {
    const dataEl = document.getElementById("metrics-data");
    if (!dataEl) return;
    let data = {};
    try { data = JSON.parse(dataEl.textContent); } catch (_) { return; }
    const uid = data.uid;
    let range = "live";

    const chart = SKVM.areaChart("#main-chart", {
      series: ["cpu", "ram", "rx", "tx"],
      labels: { cpu: "CPU %", ram: "Memory %", rx: "Net in", tx: "Net out" },
      keys: { cpu: "cpu", ram: "ram", rx: "rx", tx: "tx" },
      height: 260,
      unit: "",
      xLabel: (point) => (point && point.ts ? new Date(point.ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : ""),
    });
    chart.set(data.live || []);

    const gauges = {
      cpu: SKVM.gauge("#gauge-cpu", { value: (data.stats && data.stats.cpu_pct) || 0, label: "CPU" }),
      ram: SKVM.gauge("#gauge-ram", { value: (data.stats && data.stats.ram_pct) || 0, label: "Memory" }),
      disk: SKVM.gauge("#gauge-disk", { value: (data.stats && data.stats.disk_pct) || 0, label: "Disk" }),
    };

    function paintSparks(rows) {
      if (!rows || !rows.length) return;
      SKVM.sparkline("spark-cpu", rows.map((p) => p.cpu || 0), { color: "#7c5cff", height: 34 });
      SKVM.sparkline("spark-ram", rows.map((p) => p.ram || 0), { color: "#22d3ee", height: 34 });
      SKVM.sparkline("spark-disk", rows.map((p) => p.disk || 0), { color: "#34d399", height: 34 });
      SKVM.sparkline("spark-net", rows.map((p) => p.rx || 0), { color: "#fbbf24", height: 34 });
    }
    function paintHeat(rows) {
      const node = document.getElementById("heat-strip");
      if (node && rows && rows.length) SKVM.heatStrip(node, rows.map((p) => p.cpu || 0), { title: (v) => `${v}% CPU` });
    }
    paintSparks(data.live);
    paintHeat(data.history24);

    const titles = {
      live: ["CPU · memory · network", "last 6 minutes, 2 s resolution"],
      "1h": ["CPU · memory · network", "1 hour of stored buckets"],
      "6h": ["CPU · memory · network", "6 hours of stored buckets"],
      "24h": ["CPU · memory · network", "24 hours of stored buckets"],
      "7d": ["CPU · memory · network", "7 days · downsampled"],
      "30d": ["CPU · memory · network", "30 days · downsampled"],
    };

    function setRange(next) {
      range = next;
      const heading = titles[range] || titles.live;
      const titleNode = document.getElementById("chart-title");
      const subNode = document.getElementById("chart-sub");
      if (titleNode) titleNode.textContent = heading[0];
      if (subNode) subNode.textContent = heading[1];
    }

    document.getElementById("range-picker")?.addEventListener("click", async (event) => {
      const button = event.target.closest("[data-range]");
      if (!button) return;
      const next = button.dataset.range;
      document.querySelectorAll("#range-picker button").forEach((node) => node.classList.toggle("active", node === button));
      setRange(next);
      if (next === "live") {
        chart.set(data.live || []);
        paintSparks(data.live);
        document.getElementById("sample-count").textContent = `${(data.live || []).length} live points buffered`;
        return;
      }
      try {
        const payload = await SKVM.get(`/servers/${uid}/api/v1/metrics?window=${next}`);
        const rows = payload.history || [];
        chart.set(rows);
        paintSparks(rows);
        if (next === "24h" || next === "7d") paintHeat(rows);
        document.getElementById("sample-count").textContent = `${rows.length} buckets · ${next}`;
      } catch (error) {
        SKVM.handleError(error);
      }
    });

    /* --------------------------------------------------------- live stream */
    let lastLive = null;
    const stream = SKVM.metricsSocket(uid, {
      onMessage(message) {
        if (!message || message.type !== "metrics") return;
        lastLive = message.series || lastLive;
        if (range === "live") chart.set(lastLive || []);
        const stats = message.stats;
        if (!stats) return;
        if (gauges.cpu) gauges.cpu.set(stats.cpu_pct || 0);
        if (gauges.ram) gauges.ram.set(stats.ram_pct || 0);
        if (gauges.disk && stats.disk_pct != null) gauges.disk.set(stats.disk_pct);
        const tiles = { "CPU now": stats.cpu_pct, "Memory now": stats.ram_pct, Disk: stats.disk_pct, Network: stats.net_rx_kbps };
        document.querySelectorAll(".kpi .metric-value, .kpi .value").forEach((node) => {
          const label = (node.closest(".kpi")?.querySelector(".kpi-label")?.textContent || "").trim();
          const key = Object.keys(tiles).find((name) => label.indexOf(name) === 0);
          if (key && tiles[key] != null) node.textContent = Number(tiles[key]).toFixed(key === "Network" ? 0 : 1);
        });
        if (lastLive) paintSparks(lastLive);
      },
      onOpen() {
        const pill = document.getElementById("chart-live");
        if (pill) pill.classList.add("on");
      },
    });

    /* ------------------------------------------------------------ CSV export */
    document.getElementById("btn-csv")?.addEventListener("click", async () => {
      let rows = range === "live" ? (lastLive || data.live || []) : [];
      if (range !== "live") {
        try {
          const payload = await SKVM.get(`/servers/${uid}/api/v1/metrics?window=${range}`);
          rows = payload.history || [];
        } catch (error) { SKVM.handleError(error); return; }
      }
      if (!rows.length) { SKVM.warn("No samples in this window yet"); return; }
      const head = "timestamp,cpu_pct,ram_pct,disk_pct,rx_kbps,tx_kbps,load1,procs";
      const body = rows.map((p) => [
        p.ts || "", p.cpu ?? "", p.ram ?? "", p.disk ?? "", p.rx ?? "", p.tx ?? "", p.load1 ?? "", p.procs ?? "",
      ].join(",")).join("\n");
      const blob = new Blob([head + "\n" + body], { type: "text/csv" });
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `${data.name || uid}-${range}.csv`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 4000);
      SKVM.ok(`${rows.length} samples exported`);
    });

    window.addEventListener("beforeunload", () => stream.close());
  });
})();
