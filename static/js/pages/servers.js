/* Server list: view switching, bulk power actions, live metric refresh. */
(function () {
  "use strict";
  const SKVM = window.SKVM;

  SKVM.ready(function () {
    const grid = document.getElementById("servers-grid");
    const table = document.getElementById("servers-table");
    const viewToggle = document.getElementById("view-toggle");
    const selected = new Set();

    const stored = localStorage.getItem("skvm.serverView");
    if (stored === "table") switchView("table");

    function switchView(view) {
      grid.classList.toggle("hidden", view !== "grid");
      table.classList.toggle("hidden", view !== "table");
      viewToggle?.querySelectorAll("button").forEach((node) => node.classList.toggle("active", node.dataset.view === view));
      localStorage.setItem("skvm.serverView", view);
    }
    viewToggle?.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-view]");
      if (button) switchView(button.dataset.view);
    });

    /* ---------------------------------------------------------- bulk mode */
    const bulkBar = document.getElementById("bulk-bar");
    const bulkCount = document.getElementById("bulk-count");
    document.getElementById("bulk-select")?.addEventListener("click", () => {
      document.querySelectorAll("[data-check]").forEach((box) => { box.checked = true; selected.add(box.dataset.check); });
      syncBulk();
    });
    document.getElementById("bulk-clear")?.addEventListener("click", () => {
      selected.clear();
      document.querySelectorAll("[data-check]").forEach((box) => { box.checked = false; });
      syncBulk();
    });
    document.addEventListener("change", (event) => {
      const box = event.target.closest("[data-check]");
      if (box) {
        if (box.checked) selected.add(box.dataset.check); else selected.delete(box.dataset.check);
        syncBulk();
      }
      if (event.target.matches("[data-check-all]")) {
        const checked = event.target.checked;
        document.querySelectorAll("[data-check]").forEach((node) => {
          node.checked = checked;
          if (checked) selected.add(node.dataset.check); else selected.delete(node.dataset.check);
        });
        syncBulk();
      }
    });
    function syncBulk() {
      bulkBar?.classList.toggle("hidden", selected.size === 0);
      if (bulkCount) bulkCount.textContent = selected.size;
    }

    document.addEventListener("click", async (event) => {
      const bulk = event.target.closest("[data-bulk]");
      if (!bulk) return;
      const action = bulk.dataset.bulk;
      const uids = Array.from(selected);
      if (!uids.length) return;
      if (action === "delete") {
        const confirmed = await SKVM.confirm({
          title: `Delete ${uids.length} server(s)?`,
          message: "Snapshots and backups are removed too. This cannot be undone.",
          requireText: "delete",
          confirmLabel: "Delete permanently",
        });
        if (!confirmed) return;
        for (const uid of uids) {
          await SKVM.del(`/servers/${uid}/api/v1/delete`, { confirm: "" }).catch(() => null);
        }
        SKVM.warn("Individual deletion requires typing each server name — opening the first one for confirmation");
        window.location.assign(`/servers/${uids[0]}`);
        return;
      }
      bulk.classList.add("is-loading");
      let done = 0;
      for (const uid of uids) {
        try {
          await SKVM.post(`/servers/${uid}/api/v1/power`, { action });
          done += 1;
        } catch (error) {
          if (uids.length === 1) SKVM.handleError(error);
        }
      }
      bulk.classList.remove("is-loading");
      SKVM.ok(`${action} issued for ${done}/${uids.length} server(s)`);
      setTimeout(() => window.location.reload(), 900);
    });

    /* ------------------------------------------------- per-card power ops */
    document.addEventListener("click", async (event) => {
      const button = event.target.closest("[data-power]");
      if (!button) return;
      const uid = button.dataset.power;
      const action = button.dataset.action;
      const original = button.innerHTML;
      button.classList.add("is-loading");
      try {
        const data = await SKVM.post(`/servers/${uid}/api/v1/power`, { action });
        SKVM.ok(`${action} issued for ${data.server.name}`);
        setTimeout(() => window.location.reload(), 800);
      } catch (error) {
        SKVM.handleError(error);
        button.classList.remove("is-loading");
        button.innerHTML = original;
      }
    });

    /* ------------------------------------------------------- live numbers */
    const live = SKVM.liveSource({
      url: "/api/v1/overview",
      interval: 8,
      onData(data) {
        Object.entries(data.live || {}).forEach(([uid, stats]) => {
          const card = document.querySelector(`[data-server="${uid}"]`);
          if (card) {
            const values = card.querySelectorAll(".metric-value");
            if (values[0]) values[0].textContent = (stats.cpu_pct || 0).toFixed(1) + "%";
            if (values[1]) values[1].textContent = (stats.ram_pct || 0).toFixed(1) + "%";
            const meters = card.querySelectorAll(".meter i");
            if (meters[0]) meters[0].style.width = Math.min(100, stats.cpu_pct || 0) + "%";
            if (meters[1]) meters[1].style.width = Math.min(100, stats.ram_pct || 0) + "%";
          }
          const row = document.querySelector(`[data-row="${uid}"]`);
          if (row) {
            const spans = row.querySelectorAll(".mono.tiny + .mono.tiny, span.mono.tiny");
            const cpuLabel = row.querySelectorAll("td")[6]?.querySelector(".mono.tiny");
            const ramLabel = row.querySelectorAll("td")[7]?.querySelector(".mono.tiny");
            if (cpuLabel) cpuLabel.textContent = Math.round(stats.cpu_pct || 0) + "%";
            if (ramLabel) ramLabel.textContent = Math.round(stats.ram_pct || 0) + "%";
            const meters = row.querySelectorAll(".meter i");
            if (meters[0]) meters[0].style.width = Math.min(100, stats.cpu_pct || 0) + "%";
            if (meters[1]) meters[1].style.width = Math.min(100, stats.ram_pct || 0) + "%";
          }
        });
      },
    });
    window.addEventListener("beforeunload", () => live.stop());
  });
})();
