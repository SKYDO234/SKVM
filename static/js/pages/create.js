/* SKVM create wizard */
(function () {
  "use strict";
  SKVM.ready(function () {
    const form = document.getElementById("create-form");
    if (!form) return;
    const steps = Array.from(document.querySelectorAll(".wizard-step"));
    const panes = Array.from(document.querySelectorAll(".wizard-pane"));
    const prev = document.getElementById("wizard-prev");
    const next = document.getElementById("wizard-next");
    const createBtn = document.getElementById("wizard-create");
    const priceEl = document.getElementById("estimate-price");
    let cur = 0;
    function show(i) {
      cur = i;
      panes.forEach((p, idx) => p.classList.toggle("hidden", idx !== i));
      steps.forEach((s, idx) => s.classList.toggle("active", idx === i));
      prev.disabled = i === 0;
      next.classList.toggle("hidden", i === panes.length - 1);
      createBtn.classList.toggle("hidden", i !== panes.length - 1);
      updateReview();
    }
    steps.forEach((s) => s.addEventListener("click", () => show(parseInt(s.dataset.step, 10))));
    prev.addEventListener("click", () => show(Math.max(0, cur - 1)));
    next.addEventListener("click", () => show(Math.min(panes.length - 1, cur + 1)));

    // price estimate: cpu*4 + ramGB*2 + disk*0.08 (mirrors backend)
    function estimate() {
      const cpu = parseInt(document.querySelector('[name="cpu"]')?.value || 2, 10);
      const ram = parseInt(document.querySelector('[name="ram_mb"]')?.value || 4096, 10);
      const disk = parseInt(document.querySelector('[name="disk_gb"]')?.value || 40, 10);
      return (cpu * 4 + (ram / 1024) * 2 + disk * 0.08).toFixed(2);
    }
    function updatePrice() {
      if (priceEl) priceEl.textContent = estimate() + " " + (SKVM.el("html")?.dataset?.currency || "USD");
    }
    ["cpu", "ram_mb", "disk_gb"].forEach((id) => {
      const el = document.getElementById(id);
      const out = document.getElementById(id.replace("_", "-") + "-out") || document.getElementById(id + "-out");
      if (el && out) el.addEventListener("input", () => { out.textContent = el.id === "ram_mb" ? (el.value / 1024) : el.value; updatePrice(); });
      if (el) el.addEventListener("input", updatePrice);
    });
    function updateReview() {
      const grid = document.getElementById("review-grid");
      if (!grid) return;
      const fd = new FormData(form);
      const entries = Array.from(fd.entries());
      const map = {};
      entries.forEach(([k, v]) => { if (map[k]) { map[k] += ", " + v; } else map[k] = v; });
      grid.innerHTML = Object.entries(map).map(([k, v]) => `<div><span class="tiny dim">${SKVM.escape(k)}</span> <b class="mono tiny">${SKVM.escape(v)}</b></div>`).join("") || '<span class="tiny muted">Fill the wizard to see a summary.</span>';
      const img = document.getElementById("next-image");
      if (img) {
        try {
          const data = JSON.parse(document.getElementById("create-data")?.textContent || "{}");
          const base = data.base || {};
          const sel = form.querySelector('[name="os_image"]:checked')?.value;
          if (sel && base[sel]) img.textContent = base[sel];
        } catch (_) {}
      }
    }
    form.addEventListener("change", updateReview);
    updatePrice();

    // plan selection visual
    document.querySelectorAll("[data-plan-option]").forEach((label) => {
      label.addEventListener("click", () => {
        document.querySelectorAll("[data-plan-option]").forEach((l) => l.classList.remove("selected"));
        label.classList.add("selected");
        const plan = label.querySelector('input[name="plan"]')?.value;
        if (plan) {
          // map plan presets to resource sliders (rough)
          const presets = { nano: [1, 1024, 10], starter: [2, 4096, 40], pro: [4, 8192, 80], business: [8, 16384, 160], scale: [16, 32768, 320] };
          if (presets[plan]) {
            const [c, r, d] = presets[plan];
            const ce = document.querySelector('[name="cpu"]'); if (ce) ce.value = c;
            const re = document.querySelector('[name="ram_mb"]'); if (re) re.value = r;
            const de = document.querySelector('[name="disk_gb"]'); if (de) de.value = d;
            updatePrice(); updateReview();
          }
        }
      });
    });
    document.querySelectorAll("[data-image-option]").forEach((label) => {
      label.addEventListener("click", () => {
        document.querySelectorAll("[data-image-option]").forEach((l) => l.classList.remove("selected"));
        label.classList.add("selected");
      });
    });
    document.querySelectorAll("[data-node-option]").forEach((label) => {
      label.addEventListener("click", () => {
        document.querySelectorAll("[data-node-option]").forEach((l) => l.classList.remove("selected"));
        label.classList.add("selected");
      });
    });
    document.getElementById("gen-password")?.addEventListener("click", () => {
      const inp = document.getElementById("root_password");
      if (!inp) return;
      const chars = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@#$%";
      let pw = "";
      for (let i = 0; i < 14; i++) pw += chars[Math.floor(Math.random() * chars.length)];
      inp.value = pw;
      updateReview();
    });

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = createBtn;
      btn.classList.add("is-loading");
      const fd = new FormData(form);
      const payload = {};
      for (const [k, v] of fd.entries()) {
        if (k === "ssh_keys") {
          payload[k] = payload[k] || [];
          payload[k].push(v);
          continue;
        }
        payload[k] = v;
      }
      // coerce numbers
      ["cpu", "ram_mb", "disk_gb", "swap_mb", "bandwidth_gb", "expires_in_days", "node_id"].forEach((k) => {
        if (payload[k] !== undefined && payload[k] !== "") payload[k] = Number(payload[k]);
      });
      payload.start_after_create = !!payload.start_after_create;
      payload.backup_enabled = !!payload.backup_enabled;
      if (payload.tags && typeof payload.tags === "string") payload.tags = payload.tags.split(",").map((s) => s.trim()).filter(Boolean);
      try {
        const data = await SKVM.post("/api/v1/servers", payload);
        SKVM.toast(`Server ${data.server.name} created`, "success");
        window.location.href = "/servers/" + data.server.uid;
      } catch (err) {
        SKVM.handleError(err);
      } finally {
        btn.classList.remove("is-loading");
      }
    });
    show(0);
  });
})();
