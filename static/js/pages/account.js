/* SKVM Account — profile, 2FA, tokens, ssh keys, sessions */
(function () {
  "use strict";
  SKVM.ready(function () {
    const dataEl = document.getElementById("account-data");
    let cfg = {};
    try { cfg = JSON.parse(dataEl?.textContent || "{}"); } catch (_) {}

    // Hash-based tab switching (deep link)
    function activateFromHash() {
      const h = location.hash.replace("#", "") || "overview";
      document.querySelectorAll("[data-tab]").forEach((b) => b.classList.toggle("active", b.dataset.tab === h));
      document.querySelectorAll("[data-tab-panel]").forEach((p) => p.classList.toggle("active", p.dataset.tabPanel === h));
    }
    window.addEventListener("hashchange", activateFromHash);
    activateFromHash();
    // also support clicking tabs to set hash
    document.querySelectorAll("[data-tab]").forEach((b) => b.addEventListener("click", () => { location.hash = b.dataset.tab; }));

    // Profile form
    document.getElementById("profile-form")?.addEventListener("submit", async (e) => {
      e.preventDefault();
      const payload = {
        full_name: document.getElementById("full_name").value,
        company: document.getElementById("company").value,
        email: document.getElementById("email").value,
        timezone: document.getElementById("timezone").value,
        theme: document.getElementById("theme").value,
        notify_prefs: { email: document.getElementById("notify-email").checked, webhook: document.getElementById("notify-webhook").value.trim() }
      };
      try { await SKVM.post("/account/api/v1/profile", payload); SKVM.toast("Profile saved", "success"); document.getElementById("profile-state").textContent = "saved"; const th = payload.theme; if (th) document.documentElement.dataset.theme = th; } catch (err) { SKVM.handleError(err); }
      // theme endpoint also
      try { await SKVM.post("/account/api/v1/theme", { theme: payload.theme }); } catch (_) {}
    });

    // Password + strength
    const pw = document.getElementById("new-pass");
    const bar = document.getElementById("pw-bar");
    const label = document.getElementById("pw-label");
    if (pw) pw.addEventListener("input", async () => {
      try {
        const r = await SKVM.post("/account/api/v1/strength", { password: pw.value });
        const s = r.strength || { score: 0, label: "...", color: "#64748b" };
        bar.style.width = s.score + "%"; bar.style.background = s.color;
        label.textContent = s.label + " · " + s.score + "/100";
      } catch (_) {}
    });
    document.getElementById("password-form")?.addEventListener("submit", async (e) => {
      e.preventDefault();
      const current = document.getElementById("cur-pass").value;
      const password = document.getElementById("new-pass").value;
      const confirm = document.getElementById("confirm-pass").value;
      if (password !== confirm) { SKVM.toast("Passwords do not match", "error"); return; }
      try { const r = await SKVM.post("/account/api/v1/password", { current_password: current, password }); SKVM.toast(r.message || "Password updated — please sign in again", "success"); setTimeout(() => location.href = "/login", 1200); } catch (err) { SKVM.handleError(err); }
    });

    // 2FA
    document.getElementById("btn-2fa-setup")?.addEventListener("click", async () => {
      try {
        const r = await SKVM.post("/account/api/v1/2fa/setup", {});
        document.getElementById("twofa-setup").classList.remove("hidden");
        document.getElementById("twofa-secret").textContent = r.secret;
        const qrWrap = document.getElementById("qr-wrap");
        qrWrap.classList.remove("hidden");
        try { QRCode.toCanvas(document.createElement("canvas"), r.uri || r.qr || "", () => {}); } catch (_) {}
        // Try bundled qrcode if global exists
        if (window.QRCode || window.qrcode) {
          const c = document.createElement("canvas");
          document.getElementById("qrcode").innerHTML = "";
          document.getElementById("qrcode").appendChild(c);
          try { (window.QRCode || window.qrcode).toCanvas(c, r.uri); } catch (_) {}
        }
      } catch (err) { SKVM.handleError(err); }
    });
    document.getElementById("btn-2fa-enable")?.addEventListener("click", async () => {
      const code = document.getElementById("twofa-code").value.trim();
      try { const r = await SKVM.post("/account/api/v1/2fa/enable", { code }); SKVM.toast("Two-factor enabled", "success"); const box = document.getElementById("recovery-box"); box.classList.remove("hidden"); document.getElementById("recovery-codes").textContent = (r.recovery_codes || []).join("\n"); } catch (err) { SKVM.handleError(err); }
    });
    document.getElementById("btn-2fa-disable")?.addEventListener("click", async () => {
      const password = await SKVM.prompt({ title: "Disable 2FA", label: "Confirm your password", value: "" });
      if (password === null) return;
      try { await SKVM.post("/account/api/v1/2fa/disable", { password }); SKVM.toast("2FA disabled", "success"); setTimeout(() => location.reload(), 600); } catch (err) { SKVM.handleError(err); }
    });
    document.getElementById("btn-2fa-codes")?.addEventListener("click", async () => {
      const password = await SKVM.prompt({ title: "Regenerate recovery codes", label: "Your password", value: "" });
      if (password === null) return;
      try { const r = await SKVM.post("/account/api/v1/2fa/codes", { password }); SKVM.modal({ title: "New recovery codes", html: `<div class="code-block">${SKVM.escape(r.recovery_codes.join("\n"))}</div><div class="tiny dim mt-1">Copy now — old codes are invalid.</div>`, confirmLabel: "Close" }); } catch (err) { SKVM.handleError(err); }
    });

    // Tokens
    async function newToken() {
      const name = await SKVM.prompt({ title: "New API token", label: "Token name", value: "my-token" });
      if (name === null) return;
      const scopeStr = prompt("Scopes comma-separated (read,write,power,admin). Default: read", "read");
      if (scopeStr === null) return;
      const scopes = scopeStr.split(",").map((s) => s.trim()).filter(Boolean);
      try {
        const r = await SKVM.post("/account/api/v1/tokens", { name, scopes, expires_in_days: null });
        SKVM.modal({ title: "Token created", subtitle: "Copy now — shown only once", html: `<div class="code-block">${SKVM.escape(r.secret)}</div><div class="tiny dim mt-1">Prefix ${SKVM.escape(r.token.prefix)} · scopes: ${SKVM.escape((r.token.scopes||[]).join(", "))}</div>`, size: "wide", confirmLabel: "Close" });
        setTimeout(()=>location.reload(), 1200);
      } catch (err) { SKVM.handleError(err); }
    }
    document.getElementById("btn-new-token")?.addEventListener("click", newToken);
    document.getElementById("btn-new-token-2")?.addEventListener("click", newToken);
    document.addEventListener("click", async (e) => {
      const rev = e.target.closest("[data-token-revoke]");
      if (rev) {
        const id = rev.dataset.tokenRevoke;
        const ok = await SKVM.confirm({ title: "Revoke token?", message: "Calls with this token will start failing (401).", confirmLabel: "Revoke" });
        if (!ok) return;
        try { await SKVM.del(`/account/api/v1/tokens/${id}`); SKVM.toast("Token revoked", "success"); setTimeout(()=>location.reload(),600);} catch(err){SKVM.handleError(err);}
      }
      const ed = e.target.closest("[data-token-edit]");
      if (ed) {
        const id = ed.dataset.tokenEdit;
        const name = await SKVM.prompt({ title: "Rename token", label: "New name", value: "" }); if (name===null) return;
        try { await SKVM.patch(`/account/api/v1/tokens/${id}`, { name }); SKVM.toast("Renamed", "success"); setTimeout(()=>location.reload(),600);}catch(err){SKVM.handleError(err);}
      }
    });

    // SSH keys
    document.getElementById("btn-add-key")?.addEventListener("click", async () => {
      const pub = await SKVM.prompt({ title: "Add SSH public key", label: "ssh-ed25519 / ssh-rsa line (OpenSSH)", value: "" });
      if (pub === null || !pub.trim()) return;
      const name = await SKVM.prompt({ title: "Key name", label: "Label for this key", value: pub.split(" ").pop()?.slice(0,20) || "key" });
      if (name === null) return;
      try { await SKVM.post("/account/api/v1/ssh-keys", { public_key: pub, name }); SKVM.toast("SSH key added", "success"); setTimeout(()=>location.reload(),600);} catch(err){SKVM.handleError(err);}
    });
    document.getElementById("btn-gen-key")?.addEventListener("click", async () => {
      const name = await SKVM.prompt({ title: "Generate keypair", label: "Name for the new key", value: "SKVM generated key" }); if (name===null) return;
      try {
        const r = await SKVM.post("/account/api/v1/ssh-keys/generate", { name });
        SKVM.modal({ title: "Keypair generated", subtitle: r.fingerprint, html: `<div class="field"><label>Public key</label><div class="code-block">${SKVM.escape(r.public_key)}</div></div><div class="field"><label>Private key — copy now, not stored</label><div class="code-block">${SKVM.escape(r.private_key)}</div></div>`, confirmLabel: "Close" });
      } catch(err){SKVM.handleError(err);}
    });
    document.addEventListener("click", async (e) => {
      const del = e.target.closest("[data-key-delete]");
      if (!del) return;
      const id = del.dataset.keyDelete;
      const ok = await SKVM.confirm({ title: "Delete SSH key?", message: "New servers will not get this key; existing ones are untouched." }); if (!ok) return;
      try { await SKVM.del(`/account/api/v1/ssh-keys/${id}`); SKVM.toast("Deleted", "success"); setTimeout(()=>location.reload(),600);}catch(err){SKVM.handleError(err);}
    });

    // Sessions
    document.addEventListener("click", async (e) => {
      const rev = e.target.closest("[data-session-revoke]");
      if (!rev) return;
      const id = rev.dataset.sessionRevoke;
      try { await SKVM.post(`/account/api/v1/sessions/${id}/revoke`, {}); SKVM.toast("Session revoked","success"); setTimeout(()=>location.reload(),600);} catch(err){SKVM.handleError(err);}
    });
    document.getElementById("btn-revoke-others")?.addEventListener("click", async () => {
      const ok = await SKVM.confirm({ title: "Sign out other devices?", message: "Ends every other session." }); if (!ok) return;
      try { const r = await SKVM.post("/account/api/v1/sessions/revoke-all", { keep_current: true }); SKVM.toast(`Revoked ${r.count} session(s)`, "success"); setTimeout(()=>location.reload(),600);}catch(err){SKVM.handleError(err);}
    });

    document.getElementById("btn-export")?.addEventListener("click", () => { window.location.href = "/account/api/v1/export"; });
  });
})();
