/* SKVM admin suite — users, fleet, nodes, settings, announcements, incidents.
   Every action talks to /admin/api/v1/* so the audit trail records the actor. */
(function () {
  "use strict";
  const SKVM = window.SKVM;

  SKVM.ready(function () {
    const ROLES = ["user", "support", "operator", "admin", "owner"];
    const QUOTA_KEYS = ["servers", "cpu", "ram_mb", "disk_gb", "snapshots", "bandwidth_gb"];
    const page = document.body.dataset.adminPage || (location.pathname.split("/")[2] || "index");

    async function send(method, url, payload) {
      if (method === "GET") return SKVM.get(url);
      if (method === "DELETE") return SKVM.del(url, payload || {});
      if (method === "PATCH") return SKVM.patch(url, payload || {});
      return SKVM.post(url, payload || {});
    }

    /* --------------------------------------------------------------- forms */
    function fieldRow(label, name, value, options) {
      const opts = options || {};
      if (opts.type === "select") {
        return `<div class="field"><label for="f-${name}">${label}</label>
          <select class="select" id="f-${name}" name="${name}">${opts.choices.map((choice) =>
            `<option value="${SKVM.escape(choice)}"${choice === value ? " selected" : ""}>${SKVM.escape(String(choice).replace(/_/g, " "))}</option>`).join("")}</select></div>`;
      }
      if (opts.type === "textarea") {
        return `<div class="field"><label for="f-${name}">${label}</label>
          <textarea class="input" id="f-${name}" name="${name}" rows="${opts.rows || 3}">${SKVM.escape(value || "")}</textarea>
          ${opts.hint ? `<div class="hint">${opts.hint}</div>` : ""}</div>`;
      }
      if (opts.type === "checkbox") {
        return `<label class="checkbox"><input type="checkbox" id="f-${name}" name="${name}"${value ? " checked" : ""}>
          <span>${label}</span></label>`;
      }
      return `<div class="field"><label for="f-${name}">${label}</label>
        <input class="input" id="f-${name}" name="${name}" type="${opts.type || "text"}" value="${SKVM.escape(value == null ? "" : value)}"
          ${opts.min != null ? `min="${opts.min}"` : ""} ${opts.max != null ? `max="${opts.max}"` : ""} ${opts.step ? `step="${opts.step}"` : ""}
          ${opts.required ? "required" : ""}>
        ${opts.hint ? `<div class="hint">${opts.hint}</div>` : ""}</div>`;
    }

    function readForm(root) {
      const payload = {};
      root.querySelectorAll("[name]").forEach((node) => {
        if (node.type === "checkbox") { payload[node.name] = node.checked; return; }
        if (node.name.indexOf("quota:") === 0) {
          payload.quotas = payload.quotas || {};
          payload.quotas[node.name.slice(6)] = Number(node.value || 0);
          return;
        }
        if (node.name.indexOf("labels:") === 0) {
          payload.labels = payload.labels || {};
          payload.labels[node.name.slice(7)] = node.value.trim();
          return;
        }
        payload[node.name] = node.value;
      });
      return payload;
    }

    function formModal(options) {
      return new Promise((resolve) => {
        SKVM.modal({
          title: options.title,
          subtitle: options.subtitle,
          size: options.size || "wide",
          confirmLabel: options.confirmLabel || "Save",
          html: `<form id="admin-form" class="grid cols-2">${options.html}</form>`,
          onConfirm: (root) => {
            const form = root.querySelector("#admin-form");
            const payload = readForm(form);
            const button = root.querySelector('[data-role="confirm"]');
            button.classList.add("is-loading");
            options.onSubmit(payload)
              .then((result) => { button.classList.remove("is-loading"); resolve(result); })
              .catch((error) => { button.classList.remove("is-loading"); SKVM.handleError(error); });
            return false;
          },
          onClose: (result) => { if (!result) resolve(null); },
        });
      });
    }

    /* --------------------------------------------------------------- users */
    async function userModal(user) {
      const creating = !user;
      const html = [
        fieldRow("Username", "username", user && user.username, { required: true, hint: creating ? "4-64 chars, letters digits . _ -" : "" }),
        fieldRow("E-mail", "email", user && user.email, { type: "email", required: true }),
        fieldRow("Display name", "full_name", user && user.full_name),
        fieldRow("Company", "company", user && user.company),
        fieldRow("Role", "role", user ? user.role : "user", { type: "select", choices: ROLES }),
        fieldRow("Password", "password", "", { type: "text", hint: creating ? "Leave blank to auto-generate and e-mail a welcome link." : "Set only when resetting — use the row action instead." }),
        fieldRow("Credits", "credits", user ? user.credits : 0, { type: "number", step: "0.01" }),
        fieldRow("Active", "is_active", user ? !!user.is_active : true, { type: "checkbox" }),
        fieldRow("Force password change on next login", "must_change_password", creating, { type: "checkbox" }),
        fieldRow("Send welcome e-mail", "send_welcome", true, { type: "checkbox" }),
        `<div class="field full"><label>Quotas (blank = panel default)</label><div class="grid cols-3">${
          QUOTA_KEYS.map((key) => fieldRow(key, `quota:${key}`, user && user.quotas ? user.quotas[key] : "", { type: "number" })).join("")
        }</div></div>`,
        fieldRow("Internal notes", "notes", user && user.notes, { type: "textarea", rows: 2 }),
      ].join("");
      return formModal({
        title: creating ? "Create user" : `Edit ${user.username}`,
        subtitle: creating ? "The account is created with hashed credentials and inherits default quotas." : "Changes are audited against your username.",
        html,
        onSubmit: (payload) => (creating
          ? SKVM.post("/admin/api/v1/users", payload).then((result) => {
              SKVM.modal({ title: `${result.user.username} created`, subtitle: "Share this temporary password once — it is not stored in the panel.", html: `<div class="code-block">${SKVM.escape(result.temporary_password)}</div>`, confirm: false, cancelLabel: "Done" });
              setTimeout(() => window.location.reload(), 900);
              return result;
            })
          : SKVM.patch(`/admin/api/v1/users/${user.id}`, payload).then((result) => { SKVM.ok(result.message || "User updated"); setTimeout(() => window.location.reload(), 600); return result; })),
      });
    }

    document.getElementById("btn-create-user")?.addEventListener("click", () => userModal(null));

    /* --------------------------------------------------------------- nodes */
    async function nodeModal(node) {
      const html = [
        fieldRow("Name", "name", node ? node.name : "", { required: true, hint: "Shown in placement menus and the status page." }),
        fieldRow("Endpoint", "endpoint", node && node.endpoint, { required: true, hint: "https://hypervisor.example:8443" }),
        fieldRow("Region label", "region", node ? node.region : "local"),
        fieldRow("Priority", "priority", node ? node.priority : 100, { type: "number", min: 0, max: 999, hint: "Lower numbers are filled first." }),
        fieldRow("Capacity vCPU", "capacity_cpu", node ? node.capacity_cpu : 0, { type: "number", min: 0, step: "0.5" }),
        fieldRow("Capacity RAM (MB)", "capacity_ram_mb", node ? node.capacity_ram_mb : 0, { type: "number", min: 0, step: "512" }),
        fieldRow("Capacity disk (GB)", "capacity_disk_gb", node ? node.capacity_disk_gb : 0, { type: "number", min: 0 }),
        node ? fieldRow("Rotate shared secret", "rotate_key", false, { type: "checkbox", hint: "The agent config must be updated with the new key." }) : "",
        fieldRow("Shared secret", "secret", "", { hint: node ? "Leave blank to keep the current secret." : "Blank generates a 32-byte secret shown once." }),
        fieldRow("Verify TLS certificate", "tls_verify", node ? node.tls_verify !== false : true, { type: "checkbox" }),
        `<div class="field full"><label>Selector labels</label><div class="grid cols-3">${
          ["pool", "kvm", "nvme"].map((key) => fieldRow(key, `labels:${key}`, node && node.labels ? node.labels[key] : "")).join("")
        }</div></div>`,
      ].join("");
      return formModal({
        title: node ? `Edit ${node.name}` : "Register hypervisor",
        subtitle: node ? "Capacity and placement hints are stored locally; the agent reports the truth." : "After registering, install the agent with the issued key pair.",
        html,
        onSubmit: (payload) => (node
          ? SKVM.patch(`/admin/api/v1/nodes/${node.id}`, payload).then((result) => { afterSecret(result); return result; })
          : SKVM.post("/admin/api/v1/nodes", payload).then((result) => { afterSecret(result); return result; })),
      });
    }

    function afterSecret(result) {
      SKVM.ok(result.message || "Node saved");
      if (result.secret) {
        SKVM.modal({
          title: "Node shared secret",
          subtitle: "Put this in /etc/skvm/node.env on the hypervisor — it is shown only now.",
          html: `<div class="code-block">SKVM_NODE_KEY_ID=${SKVM.escape(result.node.key_id)}\nSKVM_NODE_SECRET=${SKVM.escape(result.secret)}</div>`,
          confirm: false,
          cancelLabel: "Done",
        });
      }
      setTimeout(() => window.location.reload(), 1100);
    }

    document.getElementById("btn-register-node")?.addEventListener("click", () => nodeModal(null));
    document.getElementById("btn-register-node-2")?.addEventListener("click", () => nodeModal(null));

    /* ------------------------------------------------------- fleet actions */
    async function reassign(uid, current) {
      let users = [];
      try {
        const payload = await SKVM.get("/admin/api/v1/users?page=1&size=100");
        users = payload.items || payload.users || [];
      } catch (error) { SKVM.handleError(error); return; }
      const html = [
        fieldRow("New owner", "owner_id", "", { type: "select", choices: users.map((row) => row.id), hint: `Currently: ${current || "unassigned"}. Servers keep running after the move.` }),
        fieldRow("Reason / ticket", "notes", "", { hint: "Recorded in the audit entry." }),
      ].join("");
      const result = await formModal({
        title: "Reassign server",
        html,
        confirmLabel: "Move server",
        onSubmit: (payload) => SKVM.patch(`/admin/api/v1/servers/${uid}`, { owner_id: Number(payload.owner_id) }).then((res) => { SKVM.ok(res.message || "Server updated"); setTimeout(() => window.location.reload(), 600); return res; }),
      });
      return result;
    }

    /* ----------------------------------------------------------- settings */
    function collectSettings() {
      const payload = {};
      document.querySelectorAll("[data-key]").forEach((node) => {
        const key = node.dataset.key;
        if (node.type === "checkbox") { payload[key] = node.checked; return; }
        payload[key] = node.value;
      });
      const quotas = {};
      document.querySelectorAll("[data-quota]").forEach((node) => { quotas[node.dataset.quota] = Number(node.value || 0); });
      if (Object.keys(quotas).length) payload.default_quotas = quotas;
      return payload;
    }

    async function saveSettings() {
      const payload = collectSettings();
      try {
        const result = await SKVM.post("/admin/api/v1/settings", payload);
        SKVM.ok(result.message || "Settings saved");
        const state = document.getElementById("save-state");
        if (state) state.textContent = `${(result.changed || []).length} key(s) changed · ${(result.changed || []).join(", ")}`;
      } catch (error) { SKVM.handleError(error); }
    }
    document.getElementById("btn-save-all")?.addEventListener("click", saveSettings);
    document.getElementById("btn-save")?.addEventListener("click", (event) => { event.preventDefault(); saveSettings(); });
    document.getElementById("btn-reload")?.addEventListener("click", () => window.location.reload());

    /* ------------------------------------------- announcements & incidents */
    function announcementModal(row) {
      return formModal({
        title: row ? "Edit announcement" : "New announcement",
        subtitle: "Pinned notices stay on the dashboard until an operator hides them.",
        html: [
          fieldRow("Title", "title", row && row.title, { required: true }),
          fieldRow("Level", "level", row ? row.level : "info", { type: "select", choices: ["info", "good", "warn", "crit"] }),
          fieldRow("Audience", "audience", row ? row.audience : "all", { type: "select", choices: ["all", "user", "staff"] }),
          fieldRow("Pinned", "pinned", row ? !!row.pinned : false, { type: "checkbox" }),
          fieldRow("Active", "active", row ? row.active !== false : true, { type: "checkbox" }),
          fieldRow("Body", "body", row && row.body, { type: "textarea", rows: 5, hint: "Plain text, one banner per line-break group." }),
        ].join(""),
        onSubmit: (payload) => (row
          ? SKVM.patch(`/admin/api/v1/announcements/${row.id}`, payload)
          : SKVM.post("/admin/announcements", payload)).then((result) => { SKVM.ok(result.message || "Saved"); setTimeout(() => window.location.reload(), 600); return result; }),
      });
    }

    function incidentModal(row) {
      return formModal({
        title: row ? "Edit incident" : "Open incident",
        subtitle: "Published on the public status page while unresolved.",
        html: [
          fieldRow("Title", "title", row && row.title, { required: true }),
          fieldRow("Severity", "severity", row ? row.severity : "warning", { type: "select", choices: ["info", "warning", "critical"] }),
          fieldRow("Status", "status", row ? row.status : "investigating", { type: "select", choices: ["investigating", "identified", "monitoring", "resolved"] }),
          fieldRow("Update", "body", row && row.body, { type: "textarea", rows: 5 }),
        ].join(""),
        onSubmit: (payload) => (row
          ? SKVM.patch(`/admin/api/v1/incidents/${row.id}`, payload)
          : SKVM.post("/admin/incidents", payload)).then((result) => { SKVM.ok(result.message || "Saved"); setTimeout(() => window.location.reload(), 600); return result; }),
      });
    }

    document.getElementById("btn-new-ann")?.addEventListener("click", () => announcementModal(null));
    document.getElementById("btn-new-ann-2")?.addEventListener("click", () => announcementModal(null));
    document.getElementById("btn-new-inc")?.addEventListener("click", () => incidentModal(null));
    document.getElementById("btn-new-inc-2")?.addEventListener("click", () => incidentModal(null));

    /* ------------------------------------------------------ delegated clicks */
    document.addEventListener("click", async (event) => {
      const trigger = event.target;

      const worker = trigger.closest("#btn-worker-stats");
      if (worker) {
        try {
          const result = await SKVM.post("/admin/api/v1/maintenance/worker-stats", {});
          const box = document.getElementById("worker-stats");
          if (box) box.textContent = JSON.stringify(result.stats || {}, null, 2);
          const state = document.getElementById("worker-state");
          if (state) state.textContent = "refreshed " + new Date().toLocaleTimeString();
        } catch (error) { SKVM.handleError(error); }
        return;
      }

      const maintenance = trigger.closest("[data-maintenance]");
      if (maintenance) {
        const action = maintenance.dataset.maintenance;
        maintenance.classList.add("is-loading");
        try {
          const result = await SKVM.post(`/admin/api/v1/maintenance/${action}`, {});
          SKVM.ok(result.message || "Maintenance task finished");
          if (action === "sync") setTimeout(() => window.location.reload(), 800);
        } catch (error) { SKVM.handleError(error); } finally { maintenance.classList.remove("is-loading"); }
        return;
      }

      if (trigger.closest("#btn-sync")) {
        trigger.closest("#btn-sync").classList.add("is-loading");
        try {
          const result = await SKVM.post("/admin/api/v1/maintenance/sync", {});
          SKVM.ok(result.message || "Fleet synced");
          setTimeout(() => window.location.reload(), 800);
        } catch (error) { SKVM.handleError(error); }
        return;
      }
      if (trigger.closest("#btn-sync-all")) {
        try {
          const result = await SKVM.post("/admin/api/v1/maintenance/sync", {});
          SKVM.ok(result.message || "Fleet synced");
          setTimeout(() => window.location.reload(), 800);
        } catch (error) { SKVM.handleError(error); }
        return;
      }
      if (trigger.closest("#btn-prune")) {
        const ok = await SKVM.confirm({ title: "Prune the driver cache?", message: "Removes dangling images and stopped build layers from the hypervisor host." });
        if (!ok) return;
        try {
          const result = await SKVM.post("/admin/api/v1/maintenance/prune", {});
          SKVM.ok(result.message || "Prune complete");
        } catch (error) { SKVM.handleError(error); }
        return;
      }
      if (trigger.closest("#btn-audit")) { window.location.href = "/admin/audit"; return; }

      /* users */
      const editUser = trigger.closest("[data-user-edit]");
      if (editUser) {
        const id = Number(editUser.dataset.userEdit);
        const row = document.querySelector(`[data-user="${id}"]`);
        try {
          const payload = await SKVM.get(`/admin/api/v1/users?page=1&size=100`);
          const user = (payload.items || payload.users || []).find((u) => u.id === id) || { id, username: row?.dataset.username };
          userModal(user);
        } catch (error) { SKVM.handleError(error); }
        return;
      }
      const toggleUser = trigger.closest("[data-user-toggle-active]");
      if (toggleUser) {
        try {
          const result = await SKVM.patch(`/admin/api/v1/users/${toggleUser.dataset.userToggleActive}`, { is_active: toggleUser.dataset.state !== "inactive" });
          SKVM.ok(result.message || "User updated");
          setTimeout(() => window.location.reload(), 500);
        } catch (error) { SKVM.handleError(error); }
        return;
      }
      const pwUser = trigger.closest("[data-user-password]");
      if (pwUser) {
        const ok = await SKVM.confirm({ title: "Reset password?", message: "A temporary password is generated, every session is revoked and the user must choose a new one.", confirmLabel: "Reset password" });
        if (!ok) return;
        try {
          const result = await SKVM.patch(`/admin/api/v1/users/${pwUser.dataset.userPassword}`, { reset_password: true });
          SKVM.modal({ title: "Temporary password", subtitle: "Deliver once — it is not stored.", html: `<div class="code-block">${SKVM.escape(result.temporary_password)}</div>`, confirm: false, cancelLabel: "Done" });
        } catch (error) { SKVM.handleError(error); }
        return;
      }
      const sessUser = trigger.closest("[data-user-sessions]");
      if (sessUser) {
        try {
          const payload = await SKVM.get(`/admin/api/v1/users/${sessUser.dataset.userSessions}/sessions`);
          const rows = payload.sessions || [];
          SKVM.modal({
            title: "Active sessions",
            subtitle: `${rows.length} session(s) — revoke here to sign the user out immediately.`,
            html: `<div class="table-wrap"><table class="data compact"><thead><tr><th>Device</th><th>IP</th><th>Last seen</th></tr></thead><tbody>${
              rows.map((row) => `<tr><td>${SKVM.escape(row.device || "unknown")}</td><td class="mono tiny">${SKVM.escape(row.ip || "")}</td><td class="tiny">${SKVM.escape(row.last_seen || "")}</td></tr>`).join("")
              || '<tr><td colspan="3" class="tiny muted">No live sessions.</td></tr>'}</tbody></table></div>`,
            confirm: false,
            cancelLabel: "Close",
          });
        } catch (error) { SKVM.handleError(error); }
        return;
      }
      const delUser = trigger.closest("[data-user-delete]");
      if (delUser) {
        const ok = await SKVM.confirm({ title: "Delete this account?", message: "Every server owned by this user is destroyed too. This cannot be undone.", requireText: "delete", confirmLabel: "Delete user" });
        if (!ok) return;
        try { const result = await SKVM.del(`/admin/api/v1/users/${delUser.dataset.userDelete}`); SKVM.ok(result.message || "User deleted"); setTimeout(() => window.location.reload(), 600); }
        catch (error) { SKVM.handleError(error); }
        return;
      }

      /* nodes */
      const editNode = trigger.closest("[data-edit]");
      if (editNode && editNode.closest("[data-node]")) {
        const id = Number(editNode.dataset.edit);
        try {
          const payload = await SKVM.get("/admin/api/v1/nodes");
          const node = (payload.nodes || []).find((row) => row.id === id);
          nodeModal(node || { id });
        } catch (error) { SKVM.handleError(error); }
        return;
      }
      const testNode = trigger.closest("[data-test]");
      if (testNode) {
        testNode.classList.add("is-loading");
        try {
          const result = await SKVM.post(`/admin/api/v1/nodes/${testNode.dataset.test}/test`, {});
          if (result.online) SKVM.ok(result.detail || "Node answered");
          else SKVM.warn(result.detail || "Node unreachable");
        } catch (error) { SKVM.handleError(error); } finally { testNode.classList.remove("is-loading"); }
        return;
      }
      const delNode = trigger.closest("[data-delete]");
      if (delNode && delNode.closest("[data-node]")) {
        const ok = await SKVM.confirm({ title: "Remove this hypervisor?", message: "Servers still placed on it must be reassigned or deleted first." });
        if (!ok) return;
        try { const result = await SKVM.del(`/admin/api/v1/nodes/${delNode.closest("[data-node]").dataset.node}`); SKVM.ok(result.message || "Node removed"); setTimeout(() => window.location.reload(), 500); }
        catch (error) { SKVM.handleError(error); }
        return;
      }

      /* settings rows */
      const resetKey = trigger.closest("[data-reset]");
      if (resetKey) {
        try { const result = await SKVM.post("/admin/api/v1/settings/reset", { key: resetKey.dataset.reset }); SKVM.ok(result.message || "Setting reset"); setTimeout(() => window.location.reload(), 500); }
        catch (error) { SKVM.handleError(error); }
        return;
      }

      /* fleet */
      const suspend = trigger.closest("[data-suspend]");
      if (suspend) {
        const reason = await SKVM.prompt({ title: "Suspend server", label: "Reason shown to the owner", value: "billing hold" });
        if (reason === null) return;
        try { const result = await SKVM.patch(`/admin/api/v1/servers/${suspend.dataset.suspend}`, { action: "suspend", reason }); SKVM.ok(result.message || "Suspended"); setTimeout(() => window.location.reload(), 600); }
        catch (error) { SKVM.handleError(error); }
        return;
      }
      const unsuspend = trigger.closest("[data-unsuspend]");
      if (unsuspend) {
        try { const result = await SKVM.patch(`/admin/api/v1/servers/${unsuspend.dataset.unsuspend}`, { action: "unsuspend" }); SKVM.ok(result.message || "Resumed"); setTimeout(() => window.location.reload(), 600); }
        catch (error) { SKVM.handleError(error); }
        return;
      }
      const reassignBtn = trigger.closest("[data-reassign]");
      if (reassignBtn) {
        const row = reassignBtn.closest("tr");
        reassign(reassignBtn.dataset.reassign, row?.querySelector("td:nth-child(2)")?.textContent.trim());
        return;
      }
      const delServer = trigger.closest("[data-delete]");
      if (delServer && delServer.closest("[data-server]")) {
        const uid = delServer.closest("[data-server]").dataset.server;
        const ok = await SKVM.confirm({ title: "Delete this server?", message: "Owner data is destroyed with it.", requireText: "delete", confirmLabel: "Delete server" });
        if (!ok) return;
        try { const result = await SKVM.del(`/admin/api/v1/servers/${uid}`); SKVM.ok(result.message || "Server deleted"); setTimeout(() => window.location.reload(), 600); }
        catch (error) { SKVM.handleError(error); }
        return;
      }

      /* announcements */
      const editAnn = trigger.closest("[data-edit-ann]");
      if (editAnn) {
        const card = editAnn.closest(".glass");
        announcementModal({
          id: Number(editAnn.dataset.editAnn),
          title: card?.querySelector("h3")?.textContent.trim(),
          body: card?.querySelector(".modal-body, .card-body > .small")?.textContent.trim(),
          level: card?.querySelector(".badge")?.textContent.trim().split(" ")[0] || "info",
          pinned: !!card?.querySelector(".badge.accent"),
          active: !card?.querySelector(".badge.muted"),
        });
        return;
      }
      const toggleAnn = trigger.closest("[data-toggle-ann]");
      if (toggleAnn) {
        try { const result = await SKVM.patch(`/admin/api/v1/announcements/${toggleAnn.dataset.toggleAnn}`, { active: toggleAnn.dataset.active !== "1" }); SKVM.ok(result.message || "Updated"); setTimeout(() => window.location.reload(), 500); }
        catch (error) { SKVM.handleError(error); }
        return;
      }
      const delAnn = trigger.closest("[data-delete-ann]");
      if (delAnn) {
        const ok = await SKVM.confirm({ title: "Delete announcement?", message: "It disappears from every dashboard immediately." });
        if (!ok) return;
        try { await SKVM.del(`/admin/api/v1/announcements/${delAnn.dataset.deleteAnn}`); SKVM.ok("Announcement removed"); setTimeout(() => window.location.reload(), 500); }
        catch (error) { SKVM.handleError(error); }
        return;
      }

      /* incidents */
      const incStatus = trigger.closest("[data-inc-status]");
      if (incStatus) {
        try {
          const result = await SKVM.patch(`/admin/api/v1/incidents/${incStatus.dataset.incStatus}`, { status: incStatus.dataset.status });
          SKVM.ok(result.message || `Incident set to ${incStatus.dataset.status}`);
          setTimeout(() => window.location.reload(), 500);
        } catch (error) { SKVM.handleError(error); }
        return;
      }
      const incEdit = trigger.closest("[data-inc-edit]");
      if (incEdit) {
        const card = incEdit.closest(".glass");
        incidentModal({
          id: Number(incEdit.dataset.incEdit),
          title: card?.querySelector("h3")?.textContent.trim(),
          body: card?.querySelector(".card-body > .small")?.textContent.trim(),
          severity: card?.querySelector(".badge")?.textContent.trim().split(" ")[0] || "warning",
          status: card?.querySelector(".badge + .badge")?.textContent.trim().split(" ")[0] || "investigating",
        });
        return;
      }
      const incDelete = trigger.closest("[data-inc-delete]");
      if (incDelete) {
        const ok = await SKVM.confirm({ title: "Delete incident?", message: "Removes it from the status page and history." });
        if (!ok) return;
        try { await SKVM.del(`/admin/api/v1/incidents/${incDelete.dataset.incDelete}`); SKVM.ok("Incident deleted"); setTimeout(() => window.location.reload(), 500); }
        catch (error) { SKVM.handleError(error); }
      }
    });

    if (page === "settings") {
      document.querySelectorAll("#settings-form [data-key]").forEach((node) => {
        node.addEventListener("change", () => {
          const state = document.getElementById("save-state");
          if (state) state.textContent = "Unsaved changes — press Save to apply.";
        });
      });
    }
  });
})();
