/* Alerting console: event feed, rule builder, toggles, test notifications. */
(function () {
  "use strict";
  const SKVM = window.SKVM;

  SKVM.ready(function () {
    let data = { metrics: {}, servers: [], severities: ["warning", "critical"] };
    try {
      data = JSON.parse(document.getElementById("alerts-data").textContent);
    } catch (error) { /* defaults are fine */ }

    const rows = document.getElementById("event-rows");

    /* ------------------------------------------------------ event feed */
    function filterEvents() {
      const severity = document.getElementById("event-filter").value;
      const onlyOpen = document.getElementById("only-open").checked;
      Array.from(rows.querySelectorAll("tr[data-event]")).forEach((row) => {
        const matchesSeverity = !severity || row.dataset.severity === severity;
        const matchesOpen = !onlyOpen || row.dataset.open === "true";
        row.classList.toggle("hidden", !(matchesSeverity && matchesOpen));
      });
    }
    document.getElementById("event-filter")?.addEventListener("change", filterEvents);
    document.getElementById("only-open")?.addEventListener("change", filterEvents);

    document.addEventListener("click", async (event) => {
      const ack = event.target.closest("[data-ack]");
      if (ack) {
        ack.classList.add("is-loading");
        try {
          await SKVM.post(`/alerts/api/v1/events/${ack.dataset.ack}/ack`);
          const row = ack.closest("tr");
          row.dataset.open = "false";
          ack.replaceWith(Object.assign(document.createElement("span"), { className: "tiny dim", textContent: "acked" }));
          SKVM.ok("Alert acknowledged");
          filterEvents();
        } catch (error) {
          SKVM.handleError(error);
          ack.classList.remove("is-loading");
        }
        return;
      }

      const ackAll = event.target.closest("#btn-ack-all");
      if (ackAll) {
        const open = Array.from(rows.querySelectorAll('tr[data-event][data-open="true"]'));
        if (!open.length) return SKVM.warn("Nothing to acknowledge");
        ackAll.classList.add("is-loading");
        let done = 0;
        for (const row of open) {
          try {
            await SKVM.post(`/alerts/api/v1/events/${row.dataset.event}/ack`);
            done += 1;
          } catch (error) { /* keep going */ }
        }
        ackAll.classList.remove("is-loading");
        SKVM.ok(`Acknowledged ${done} event(s)`);
        setTimeout(() => window.location.reload(), 600);
        return;
      }

      const refresh = event.target.closest("#btn-refresh-events");
      if (refresh) {
        refresh.classList.add("is-loading");
        try {
          const payload = await SKVM.get("/alerts/api/v1/events?per_page=60");
          rows.innerHTML = (payload.items || []).map((item) => `
            <tr data-event="${item.id}" data-severity="${item.severity}" data-open="${item.acknowledged_at ? "false" : "true"}">
              <td class="tiny">${SKVM.escape(item.ts)}</td>
              <td><span class="badge ${item.severity === "critical" ? "crit" : "warn"}"><span class="dot"></span>${SKVM.escape(item.severity)}</span></td>
              <td><span class="badge ${item.state === "firing" ? "crit" : "good"}"><span class="dot"></span>${SKVM.escape(item.state)}</span></td>
              <td class="tiny">${SKVM.escape(item.server || "—")}</td>
              <td class="tiny">${SKVM.escape(item.message || "—")}</td>
              <td class="mono tiny">${item.value}</td>
              <td>${item.acknowledged_at ? '<span class="tiny dim">acked</span>'
                    : `<button class="btn sm quiet" data-ack="${item.id}">Ack</button>`}</td>
            </tr>`).join("");
          filterEvents();
          SKVM.ok("Feed refreshed");
        } catch (error) { SKVM.handleError(error); }
        finally { refresh.classList.remove("is-loading"); }
        return;
      }

      const toggle = event.target.closest("[data-rule-toggle]");
      if (event.target.matches("[data-rule-toggle]")) {
        try {
          await SKVM.patch(`/alerts/api/v1/rules/${toggle.dataset.ruleToggle}`, { enabled: toggle.checked });
          SKVM.toast(`Rule ${toggle.checked ? "enabled" : "paused"}`, toggle.checked ? "good" : "warn");
          toggle.closest(".rule-card")?.classList.toggle("disabled", !toggle.checked);
        } catch (error) {
          toggle.checked = !toggle.checked;
          SKVM.handleError(error);
        }
        return;
      }

      const del = event.target.closest("[data-rule-delete]");
      if (del) {
        const confirmed = await SKVM.confirm({
          title: "Delete this rule?",
          message: "Existing events stay in the feed, but the rule stops evaluating.",
          confirmLabel: "Delete rule",
        });
        if (!confirmed) return;
        try {
          await SKVM.del(`/alerts/api/v1/rules/${del.dataset.ruleDelete}`);
          SKVM.ok("Rule deleted");
          del.closest(".rule-card")?.remove();
        } catch (error) { SKVM.handleError(error); }
        return;
      }

      const edit = event.target.closest("[data-rule-edit]");
      if (edit) {
        openRuleEditor(edit.dataset.ruleEdit);
        return;
      }

      if (event.target.closest("#btn-new-rule") || event.target.closest("#btn-new-rule-2")) {
        openRuleEditor(null);
        return;
      }

      if (event.target.closest("#btn-seed")) {
        try {
          const response = await SKVM.post("/alerts/api/v1/seed");
          SKVM.ok(response.message || "Presets created");
          setTimeout(() => window.location.reload(), 700);
        } catch (error) { SKVM.handleError(error); }
        return;
      }

      if (event.target.closest("#btn-test")) {
        try {
          const response = await SKVM.post("/alerts/api/v1/test");
          SKVM.ok(response.message || "Test notification dispatched");
        } catch (error) { SKVM.handleError(error); }
      }
    });

    /* ---------------------------------------------------- rule builder */
    function openRuleEditor(ruleId) {
      const metricKeys = Object.keys(data.metrics);
      const ruleRow = ruleId ? document.querySelector(`[data-rule="${ruleId}"]`) : null;
      const currentMetric = ruleRow ? (ruleRow.querySelector(".tiny.dim.mono")?.textContent || "cpu > 90").split(" ")[0] : "cpu";
      const meta = data.metrics[currentMetric] || { label: currentMetric, default: 90, unit: "%" };

      SKVM.modal({
        title: ruleId ? "Edit alert rule" : "New alert rule",
        subtitle: "Fires when the metric crosses the threshold and stays there for the duration.",
        size: "wide",
        confirmLabel: ruleId ? "Save rule" : "Create rule",
        html: `
          <div class="form-grid">
            <div class="field"><label for="rule-name">Name</label>
              <input class="input" id="rule-name" value="${SKVM.escape((ruleRow?.querySelector("b")?.textContent) || "High CPU")}" maxlength="64"></div>
            <div class="field"><label for="rule-server">Target</label>
              <select class="select" id="rule-server">
                <option value="">All my servers</option>
                ${(data.servers || []).map((server) => `<option value="${SKVM.escape(server.uid)}" ${ruleRow && ruleRow.textContent.includes(server.name) ? "selected" : ""}>${SKVM.escape(server.name)} · ${SKVM.escape(server.image || "")}</option>`).join("")}
              </select></div>
            <div class="field"><label for="rule-metric">Metric</label>
              <select class="select" id="rule-metric">
                ${metricKeys.map((key) => `<option value="${key}" ${key === currentMetric ? "selected" : ""}>${SKVM.escape(data.metrics[key].label)} (${SKVM.escape(data.metrics[key].unit || "ratio")})</option>`).join("")}
              </select></div>
            <div class="field"><label for="rule-comparator">Comparator</label>
              <select class="select" id="rule-comparator">
                <option value=">">greater than</option>
                <option value="<">less than</option>
              </select></div>
            <div class="field"><label for="rule-threshold">Threshold</label>
              <input class="input mono" id="rule-threshold" value="${meta.default}"></div>
            <div class="field"><label for="rule-duration">Duration (seconds)</label>
              <input class="input mono" id="rule-duration" value="300"></div>
            <div class="field"><label for="rule-severity">Severity</label>
              <select class="select" id="rule-severity">
                ${(data.severities || ["warning", "critical"]).map((severity) => `<option value="${severity}" ${severity === "critical" ? "selected" : ""}>${severity}</option>`).join("")}
              </select></div>
            <div class="field"><label for="rule-webhook">Webhook URL (optional)</label>
              <input class="input mono" id="rule-webhook" placeholder="https://hooks.example.com/skvm"></div>
            <div class="field full">
              <label class="checkbox"><input type="checkbox" id="rule-email" checked> <span>Send e-mail notifications</span></label>
              <label class="checkbox"><input type="checkbox" id="rule-enabled" checked> <span>Rule active</span></label>
            </div>
          </div>`,
        onConfirm: async (root) => {
          const payload = {
            name: root.querySelector("#rule-name").value.trim(),
            server_uid: root.querySelector("#rule-server").value || null,
            metric: root.querySelector("#rule-metric").value,
            comparator: root.querySelector("#rule-comparator").value,
            threshold: parseFloat(root.querySelector("#rule-threshold").value) || 0,
            duration_s: parseInt(root.querySelector("#rule-duration").value, 10) || 300,
            severity: root.querySelector("#rule-severity").value,
            notify_webhook: root.querySelector("#rule-webhook").value.trim(),
            notify_email: root.querySelector("#rule-email").checked,
            enabled: root.querySelector("#rule-enabled").checked,
          };
          if (!payload.name) {
            SKVM.warn("Give the rule a name");
            return false;
          }
          if (ruleId) {
            await SKVM.patch(`/alerts/api/v1/rules/${ruleId}`, payload);
            SKVM.ok("Rule updated");
          } else {
            await SKVM.post("/alerts/api/v1/rules", payload);
            SKVM.ok("Rule created");
          }
          setTimeout(() => window.location.reload(), 600);
        },
      });
      // Swap the suggested threshold when the metric changes
      setTimeout(() => {
        const metricSelect = document.getElementById("rule-metric");
        metricSelect?.addEventListener("change", () => {
          const chosen = data.metrics[metricSelect.value];
          if (chosen) document.getElementById("rule-threshold").value = chosen.default;
        });
      }, 60);
    }
  });
})();
