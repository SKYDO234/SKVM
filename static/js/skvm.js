/* ==========================================================================
   SKVM · front-end runtime
   Zero dependencies. Provides: API client, toasts, modals, dropdowns,
   command palette, live polling, formatters and small UI utilities.
   ========================================================================== */
(function (window, document) {
  "use strict";

  const SKVM = window.SKVM || {};
  window.SKVM = SKVM;

  /* ------------------------------------------------------------ utilities */
  SKVM.el = (selector, root) => (root || document).querySelector(selector);
  SKVM.els = (selector, root) => Array.from((root || document).querySelectorAll(selector));

  SKVM.escape = function (value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, (ch) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[ch]));
  };

  SKVM.bytes = function (value, digits) {
    let size = Number(value) || 0;
    const units = ["B", "KB", "MB", "GB", "TB", "PB"];
    let index = 0;
    while (Math.abs(size) >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
    const precision = digits == null ? (index < 2 ? 0 : 1) : digits;
    return `${size.toFixed(precision)} ${units[index]}`;
  };

  SKVM.duration = function (seconds) {
    seconds = Math.max(0, Math.floor(Number(seconds) || 0));
    const d = Math.floor(seconds / 86400);
    const h = Math.floor((seconds % 86400) / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    if (d) return `${d}d ${h}h`;
    if (h) return `${h}h ${m}m`;
    return `${m}m`;
  };

  SKVM.timeAgo = function (value) {
    if (!value) return "—";
    const then = new Date(value).getTime();
    if (Number.isNaN(then)) return "—";
    const delta = Math.max(0, Math.floor((Date.now() - then) / 1000));
    const table = [[60, "s", 1], [3600, "m", 60], [86400, "h", 3600], [2592000, "d", 86400], [31536000, "mo", 2592000]];
    for (const [limit, unit, div] of table) {
      if (delta < limit) return `${Math.floor(delta / div)}${unit} ago`;
    }
    return `${Math.floor(delta / 31536000)}y ago`;
  };

  SKVM.tone = function (value, warnAt, critAt) {
    const num = Number(value);
    if (!Number.isFinite(num)) return "muted";
    if (num >= (critAt == null ? 90 : critAt)) return "crit";
    if (num >= (warnAt == null ? 70 : warnAt)) return "warn";
    return "good";
  };

  SKVM.debounce = function (fn, wait) {
    let timer = null;
    return function (...args) {
      clearTimeout(timer);
      timer = setTimeout(() => fn.apply(this, args), wait || 250);
    };
  };

  /* ------------------------------------------------------------- API layer */
  SKVM.csrf = function () {
    const meta = SKVM.el('meta[name="csrf-token"]');
    return (meta && meta.content) || "";
  };

  SKVM.api = async function (path, options) {
    const opts = options || {};
    const headers = Object.assign({ "X-Requested-With": "XMLHttpRequest", Accept: "application/json" }, opts.headers || {});
    if (opts.body && !(opts.body instanceof FormData)) {
      headers["Content-Type"] = "application/json";
      if (typeof opts.body !== "string") opts.body = JSON.stringify(opts.body);
    }
    if (opts.method && opts.method.toUpperCase() !== "GET") headers["X-CSRF-Token"] = SKVM.csrf();

    let response;
    try {
      response = await fetch(path, Object.assign({ credentials: "same-origin" }, opts, { headers }));
    } catch (error) {
      throw new SKVM.ApiError("Network unavailable — check your connection", 0, { offline: true });
    }
    const contentType = response.headers.get("Content-Type") || "";
    let payload = null;
    if (contentType.includes("application/json")) {
      payload = await response.json().catch(() => null);
    } else {
      payload = { ok: response.ok, text: await response.text().catch(() => "") };
    }
    if (!response.ok || (payload && payload.ok === false)) {
      const message = (payload && (payload.error || payload.message)) || `Request failed (${response.status})`;
      throw new SKVM.ApiError(message, response.status, payload || {});
    }
    return payload || { ok: true };
  };

  SKVM.ApiError = class ApiError extends Error {
    constructor(message, status, payload) {
      super(message);
      this.name = "ApiError";
      this.status = status;
      this.payload = payload || {};
    }
  };

  SKVM.get = (path, options) => SKVM.api(path, Object.assign({ method: "GET" }, options));
  SKVM.post = (path, body, options) => SKVM.api(path, Object.assign({ method: "POST", body: body || {} }, options));
  SKVM.patch = (path, body) => SKVM.api(path, { method: "PATCH", body: body || {} });
  SKVM.del = (path, body) => SKVM.api(path, { method: "DELETE", body: body || {} });

  /* --------------------------------------------------------------- icon set */
  const ICONS = {
    activity: '<path d="M3 12h4l3 8 4-16 3 8h4"/>',
    alert: '<path d="M12 9v4m0 4h.01M10.3 3.9L1.8 18a2 2 0 001.7 3h17a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z"/>',
    archive: '<rect x="3" y="4" width="18" height="4" rx="1"/><path d="M5 8v11a1 1 0 001 1h12a1 1 0 001-1V8M10 12h4"/>',
    bell: '<path d="M18 8a6 6 0 10-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9"/><path d="M13.7 21a2 2 0 01-3.4 0"/>',
    bolt: '<path d="M13 2L4 14h6l-1 8 9-12h-6l1-8z"/>',
    book: '<path d="M4 5a2 2 0 012-2h12v18H6a2 2 0 01-2-2V5z"/><path d="M8 3v18"/>',
    camera: '<path d="M4 8h3l1.5-2h7L17 8h3a1 1 0 011 1v9a1 1 0 01-1 1H4a1 1 0 01-1-1V9a1 1 0 011-1z"/><circle cx="12" cy="13" r="3.4"/>',
    chart: '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
    check: '<path d="M20 6L9 17l-5-5"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    cloud: '<path d="M17 18a4 4 0 000-8 5 5 0 00-9.6-1.5A4 4 0 007 18h10z"/>',
    code: '<path d="M8 6l-5 6 5 6M16 6l5 6-5 6"/>',
    copy: '<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 012-2h10"/>',
    cpu: '<rect x="7" y="7" width="10" height="10" rx="2"/><path d="M4 10V8a2 2 0 012-2h2M20 10V8a2 2 0 00-2-2h-2M4 14v2a2 2 0 002 2h2M20 14v2a2 2 0 01-2 2h-2M8 3v2M16 3v2M8 19v2M16 19v2"/>',
    disk: '<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3"/>',
    download: '<path d="M12 3v12m0 0l-4-4m4 4l4-4M4 21h16"/>',
    external: '<path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 01-1 1H5a1 1 0 01-1-1V7a1 1 0 011-1h5"/>',
    eye: '<path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6-10-6-10-6z"/><circle cx="12" cy="12" r="2.6"/>',
    file: '<path d="M14 3v5h5M14 3H7a2 2 0 00-2 2v14a2 2 0 002 2h10a2 2 0 002-2V8l-5-5z"/>',
    folder: '<path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2V7z"/>',
    gauge: '<path d="M12 14l4-4"/><path d="M20.5 14a9 9 0 10-17 0"/>',
    globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.5 2.5 3.8 5.8 3.8 9S14.5 18.5 12 21c-2.5-2.5-3.8-5.8-3.8-9S9.5 5.5 12 3z"/>',
    grid: '<rect x="3" y="3" width="7" height="7" rx="2"/><rect x="14" y="3" width="7" height="7" rx="2"/><rect x="3" y="14" width="7" height="7" rx="2"/><rect x="14" y="14" width="7" height="7" rx="2"/>',
    history: '<path d="M3 12a9 9 0 109-9 9 9 0 00-7.7 4.4M3 4v4h4"/><path d="M12 8v4l3 2"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 8h.01M11 12h1v4h1"/>',
    key: '<circle cx="8" cy="15" r="4"/><path d="M10.8 12.2L20 3m-3 0h3v3"/>',
    layers: '<path d="M12 3l9 5-9 5-9-5 9-5zM3 13l9 5 9-5"/>',
    link: '<path d="M10 13a5 5 0 007 0l2-2a5 5 0 00-7-7l-1 1"/><path d="M14 11a5 5 0 00-7 0l-2 2a5 5 0 007 7l1-1"/>',
    logout: '<path d="M15 17l5-5-5-5M20 12H9M12 3H6a2 2 0 00-2 2v14a2 2 0 002 2h6"/>',
    memory: '<rect x="3" y="7" width="18" height="10" rx="2"/><path d="M7 17v3M12 17v3M17 17v3M7 11h2M11 11h2M15 11h2"/>',
    menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
    pause: '<rect x="7" y="5" width="4" height="14" rx="1"/><rect x="13" y="5" width="4" height="14" rx="1"/>',
    play: '<path d="M6 4l14 8-14 8V4z"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    refresh: '<path d="M21 12a9 9 0 11-3-6.7M21 4v5h-5"/>',
    save: '<path d="M5 3h11l3 3v15H5z"/><path d="M9 3v6h6V3M8 15h8"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/>',
    server: '<rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 16.5h.01"/>',
    settings: '<circle cx="12" cy="12" r="3"/><path d="M12 3v2M12 19v2M4.2 4.2l1.4 1.4M18.4 18.4l1.4 1.4M3 12h2M19 12h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4"/>',
    shield: '<path d="M12 2l8 4v6c0 5-3.5 8.5-8 10-4.5-1.5-8-5-8-10V6l8-4z"/>',
    shieldCheck: '<path d="M12 2l8 4v6c0 5-3.5 8.5-8 10-4.5-1.5-8-5-8-10V6l8-4z"/><path d="M9 12l2 2 4-4"/>',
    spark: '<path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9L12 3z"/>',
    stop: '<rect x="5" y="5" width="14" height="14" rx="2"/>',
    terminal: '<path d="M4 17l6-5-6-5M12 19h8"/>',
    trash: '<path d="M4 7h16M9 7V5a1 1 0 011-1h4a1 1 0 011 1v2m-9 0l1 13a1 1 0 001 1h8a1 1 0 001-1l1-13"/>',
    upload: '<path d="M12 21V9m0 0L8 13m4-4l4 4M4 3h16"/>',
    user: '<circle cx="12" cy="8" r="4"/><path d="M4 21c0-4 3.6-6 8-6s8 2 8 6"/>',
    users: '<circle cx="9" cy="8" r="3.4"/><path d="M2 20c0-3.4 3-5 7-5s7 1.6 7 5"/><path d="M17 11a3 3 0 100-6"/><path d="M19 20c0-2.6-.8-3.9-2-4.6"/>',
    wifi: '<path d="M5 12.5a10 10 0 0114 0M8.5 16a5 5 0 017 0M12 19.5h.01"/>',
    x: '<path d="M18 6L6 18M6 6l12 12"/>',
    zap: '<path d="M13 2L4 14h6l-1 8 9-12h-6l1-8z"/>',
    box: '<path d="M21 8l-9-5-9 5 9 5 9-5z"/><path d="M3 8v8l9 5 9-5V8"/><path d="M12 13v8"/>',
    tag: '<path d="M20.6 13.4L12 22l-9-9V4h9l8.6 8.6a1 1 0 010 1.4z"/><circle cx="7.5" cy="7.5" r="1.4"/>',
    lock: '<rect x="4" y="10" width="16" height="11" rx="2"/><path d="M8 10V7a4 4 0 018 0v3"/>',
    mail: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 7l9 6 9-6"/>',
    arrowRight: '<path d="M5 12h14M13 6l6 6-6 6"/>',
    arrowUpRight: '<path d="M7 17L17 7M9 7h8v8"/>',
    thermometer: '<path d="M14 14V5a2 2 0 10-4 0v9a4 4 0 104 0z"/>',
    rocket: '<path d="M5 15c-1 2-1 5-1 5s3 0 5-1M9 15l-4-4c1-5 5-9 11-9 0 6-4 10-9 11l2 2z"/><circle cx="14.5" cy="9.5" r="1.4"/>',
    database: '<ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/>',
    pulse: '<path d="M3 12h4l2-6 3 12 2.5-8 2 4h5"/>',
    arrowLeft: '<path d="M19 12H5M11 6l-6 6 6 6"/>',
    arrowDown: '<path d="M12 5v14M6 13l6 6 6-6"/>',
    chevronDown: '<path d="M6 9l6 6 6-6"/>',
    chevronRight: '<path d="M9 6l6 6-6 6"/>',
    chevronLeft: '<path d="M15 6l-6 6 6 6"/>',
    chevronUp: '<path d="M6 15l6-6 6 6"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/>',
    moon: '<path d="M20 14.5A8.5 8.5 0 019.5 4a8.5 8.5 0 1010.5 10.5z"/>',
    list: '<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
    filter: '<path d="M3 5h18l-7 8v6l-4 2v-8L3 5z"/>',
    minus: '<path d="M5 12h14"/>',
    plusCircle: '<circle cx="12" cy="12" r="9"/><path d="M12 8v8M8 12h8"/>',
    send: '<path d="M22 2L11 13M22 2l-7 20-4-9-9-4 20-7z"/>',
    dots: '<circle cx="5" cy="12" r="1.4"/><circle cx="12" cy="12" r="1.4"/><circle cx="19" cy="12" r="1.4"/>',
    eyeOff: '<path d="M3 3l18 18M10.6 10.6a2.6 2.6 0 003.6 3.6"/><path d="M6.7 6.9C4 8.4 2 12 2 12s3.5 6 10 6c1.7 0 3.2-.4 4.5-1.1M9.9 6.2A10 10 0 0112 6c6.5 0 10 6 10 6s-.8 1.4-2.3 2.9"/>',
    shieldOff: '<path d="M12 22s8-3.5 8-10V6l-4-2"/><path d="M4 6l-1 .7V12c0 6.5 8 10 8 10"/><path d="M3 3l18 18"/>',
    activitySquare: '<rect x="3" y="3" width="18" height="18" rx="3"/><path d="M7 12h3l2-4 2 8 2-4h1"/>',
    sparkles: '<path d="M12 3l1.8 4.6L18 9l-4.2 1.4L12 15l-1.8-4.6L6 9l4.2-1.4L12 3z"/><path d="M18.5 15.5l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8.8-2z"/>',
    github: '<path d="M9 19c-4 1.2-4-2-6-2.5m12 5v-3.5c0-1 .1-1.4-.5-2 2.8-.3 5.5-1.4 5.5-6a4.6 4.6 0 00-1.3-3.2 4.3 4.3 0 00-.1-3.2s-1.4-.4-4.4 1.7a10.8 10.8 0 00-5.6 0C5.3 3.2 3.9 3.6 3.9 3.6a4.3 4.3 0 00-.1 3.2A4.6 4.6 0 002.5 10c0 4.6 2.7 5.7 5.5 6-.6.6-.6 1.2-.5 2V23"/>',
    wallet: '<rect x="3" y="6" width="18" height="13" rx="2"/><path d="M3 10h18M16 14h2"/>',
    backup: '<path d="M12 3v9m0 0l-3.5-3.5M12 12l3.5-3.5"/><path d="M4 15v3a2 2 0 002 2h12a2 2 0 002-2v-3"/>'
  };

  SKVM.icon = function (name, size) {
    const path = ICONS[name] || ICONS.info;
    const dim = size || 18;
    return `<svg viewBox="0 0 24 24" width="${dim}" height="${dim}" fill="none" stroke="currentColor"
      stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${path}</svg>`;
  };

  /* ---------------------------------------------------------------- toasts */
  const TOAST_ICONS = { good: "check", error: "alert", warn: "alert", info: "info", success: "check" };

  SKVM.toast = function (message, level, title) {
    const kind = ({ success: "good", danger: "error" }[level] || level || "info");
    let host = SKVM.el(".toasts");
    if (!host) {
      host = document.createElement("div");
      host.className = "toasts";
      document.body.appendChild(host);
    }
    const node = document.createElement("div");
    node.className = `toast ${kind}`;
    node.innerHTML = `
      <div class="t-icon">${SKVM.icon(TOAST_ICONS[kind] || "info", 16)}</div>
      <div class="t-body">
        ${title ? `<div class="t-title">${SKVM.escape(title)}</div>` : ""}
        <div class="t-msg">${SKVM.escape(message)}</div>
      </div>
      <button class="t-close" aria-label="Dismiss">${SKVM.icon("x", 14)}</button>`;
    host.appendChild(node);
    const remove = () => {
      node.classList.add("leaving");
      setTimeout(() => node.remove(), 240);
    };
    node.querySelector(".t-close").addEventListener("click", remove);
    setTimeout(remove, kind === "error" ? 9000 : 5200);
    return node;
  };

  SKVM.ok = (message, title) => SKVM.toast(message, "good", title);
  SKVM.err = (message, title) => SKVM.toast(message, "error", title || "Something went wrong");
  SKVM.warn = (message, title) => SKVM.toast(message, "warn", title);

  SKVM.handleError = function (error) {
    if (error instanceof SKVM.ApiError) {
      if (error.status === 401) {
        SKVM.err("Your session expired — signing you back in");
        setTimeout(() => window.location.assign("/login"), 1200);
        return error;
      }
      SKVM.err(error.message);
    } else {
      SKVM.err(error && error.message ? error.message : "Unexpected error");
      console.error(error);
    }
    return error;
  };

  /* ---------------------------------------------------------------- modals */
  SKVM.modal = function (options) {
    const opts = typeof options === "string" ? { body: options } : (options || {});
    const backdrop = document.createElement("div");
    backdrop.className = "modal-backdrop";
    const width = opts.size === "wide" ? "wide" : (opts.size === "narrow" ? "narrow" : "");
    backdrop.innerHTML = `
      <div class="modal ${width}" role="dialog" aria-modal="true" aria-label="${SKVM.escape(opts.title || "Dialog")}">
        ${opts.title ? `<div class="modal-head">
            <div>
              <h3>${SKVM.escape(opts.title)}</h3>
              ${opts.subtitle ? `<div class="sub">${SKVM.escape(opts.subtitle)}</div>` : ""}
            </div>
            <button class="icon-btn ghost close" aria-label="Close">${SKVM.icon("x", 16)}</button>
          </div>` : ""}
        <div class="modal-body"></div>
        <div class="modal-foot">
          ${opts.cancel === false ? "" : `<button class="btn ghost" data-role="cancel">${SKVM.escape(opts.cancelLabel || "Cancel")}</button>`}
          ${opts.confirm === false ? "" : `<button class="btn ${opts.danger ? "danger" : "primary"}" data-role="confirm">${SKVM.escape(opts.confirmLabel || "Confirm")}</button>`}
        </div>
      </div>`;
    const bodyNode = backdrop.querySelector(".modal-body");
    if (opts.html != null) bodyNode.innerHTML = opts.html;
    else if (opts.body != null) bodyNode.appendChild(typeof opts.body === "string" ? document.createTextNode(opts.body) : opts.body);

    document.body.appendChild(backdrop);
    requestAnimationFrame(() => backdrop.classList.add("open"));

    const close = (result) => {
      backdrop.classList.remove("open");
      setTimeout(() => backdrop.remove(), 240);
      document.removeEventListener("keydown", onKey);
      if (opts.onClose) opts.onClose(result);
    };
    const confirm = async () => {
      if (opts.onConfirm) {
        const button = backdrop.querySelector('[data-role="confirm"]');
        button && button.classList.add("is-loading");
        try {
          const outcome = await opts.onConfirm(backdrop);
          button && button.classList.remove("is-loading");
          if (outcome !== false) close(outcome);
        } catch (error) {
          button && button.classList.remove("is-loading");
          SKVM.handleError(error);
        }
      } else close(true);
    };
    const onKey = (event) => {
      if (event.key === "Escape") close(null);
      if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) confirm();
    };
    document.addEventListener("keydown", onKey);
    backdrop.addEventListener("click", (event) => { if (event.target === backdrop) close(null); });
    backdrop.querySelector(".close")?.addEventListener("click", () => close(null));
    backdrop.querySelector('[data-role="cancel"]')?.addEventListener("click", () => close(null));
    backdrop.querySelector('[data-role="confirm"]')?.addEventListener("click", confirm);
    const focusTarget = backdrop.querySelector("input, select, textarea, button[data-role='confirm']");
    if (focusTarget) setTimeout(() => focusTarget.focus(), 60);
    return { element: backdrop, close, body: bodyNode };
  };

  SKVM.confirm = function (options) {
    const opts = typeof options === "string" ? { message: options } : options;
    return new Promise((resolve) => {
      SKVM.modal({
        title: opts.title || "Please confirm",
        size: "narrow",
        danger: opts.danger !== false,
        confirmLabel: opts.confirmLabel || "Confirm",
        html: `<p class="mb-0">${SKVM.escape(opts.message || "Are you sure?")}</p>
          ${opts.requireText ? `<div class="field mt-2"><label>Type <b>${SKVM.escape(opts.requireText)}</b> to confirm</label>
          <input class="input" data-role="confirm-text" autocomplete="off"></div>` : ""}`,
        onConfirm: (root) => {
          if (opts.requireText) {
            const field = root.querySelector('[data-role="confirm-text"]');
            if (!field || field.value.trim() !== opts.requireText) {
              SKVM.warn(`Type “${opts.requireText}” exactly to continue`);
              return false;
            }
          }
          resolve(true);
        },
        onClose: (result) => { if (result !== true) resolve(false); },
      });
    });
  };

  SKVM.prompt = function (options) {
    const opts = typeof options === "string" ? { title: options } : options;
    return new Promise((resolve) => {
      SKVM.modal({
        title: opts.title || "Input required",
        size: "narrow",
        confirmLabel: opts.confirmLabel || "Save",
        html: `<div class="field"><label>${SKVM.escape(opts.label || "Value")}</label>
          <input class="input" data-role="value" value="${SKVM.escape(opts.value || "")}"
            placeholder="${SKVM.escape(opts.placeholder || "")}">
          ${opts.hint ? `<div class="hint">${SKVM.escape(opts.hint)}</div>` : ""}</div>`,
        onConfirm: (root) => {
          const field = root.querySelector('[data-role="value"]');
          const value = field ? field.value.trim() : "";
          if (opts.required !== false && !value) { SKVM.warn("A value is required"); return false; }
          resolve(value);
        },
        onClose: (result) => { if (typeof result !== "string") resolve(null); },
      });
    });
  };

  /* --------------------------------------------------------- copy helpers */
  SKVM.copy = async function (text, label) {
    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
      } else {
        const area = document.createElement("textarea");
        area.value = text;
        area.style.position = "fixed";
        area.style.opacity = "0";
        document.body.appendChild(area);
        area.select();
        document.execCommand("copy");
        area.remove();
      }
      SKVM.toast(label ? `${label} copied` : "Copied to clipboard", "good");
      return true;
    } catch (error) {
      SKVM.err("Clipboard is blocked by the browser — select and copy manually");
      return false;
    }
  };

  /* ------------------------------------------------------------- dropdowns */
  SKVM.initDropdowns = function (root) {
    SKVM.els("[data-dropdown]", root).forEach((trigger) => {
      if (trigger.dataset.bound) return;
      trigger.dataset.bound = "1";
      const wrapper = trigger.closest(".dropdown");
      trigger.addEventListener("click", (event) => {
        event.stopPropagation();
        const isOpen = wrapper.classList.contains("open");
        SKVM.els(".dropdown.open").forEach((node) => node.classList.remove("open"));
        if (!isOpen) wrapper.classList.add("open");
      });
    });
    if (!SKVM._dropdownBound) {
      SKVM._dropdownBound = true;
      document.addEventListener("click", () => SKVM.els(".dropdown.open").forEach((node) => node.classList.remove("open")));
      document.addEventListener("keydown", (event) => {
        if (event.key === "Escape") SKVM.els(".dropdown.open").forEach((node) => node.classList.remove("open"));
      });
    }
  };

  /* ------------------------------------------------------------- data-action */
  SKVM.initActions = function (root) {
    SKVM.els("[data-copy]", root).forEach((node) => {
      if (node.dataset.bound) return;
      node.dataset.bound = "1";
      node.addEventListener("click", (event) => {
        event.preventDefault();
        const source = node.dataset.copyFrom ? SKVM.el(node.dataset.copyFrom) : null;
        SKVM.copy(node.dataset.copyFrom && source ? source.textContent.trim() : node.dataset.copy,
          node.dataset.copyLabel);
      });
    });
    SKVM.els("[data-confirm]", root).forEach((node) => {
      if (node.dataset.bound) return;
      node.dataset.bound = "1";
      node.addEventListener("click", async (event) => {
        event.preventDefault();
        const confirmed = await SKVM.confirm({
          message: node.dataset.confirm,
          requireText: node.dataset.confirmText || null,
          danger: node.dataset.confirmDanger !== "false",
        });
        if (!confirmed) return;
        if (node.tagName === "A") window.location.assign(node.href);
        else if (node.form) node.form.submit();
      });
    });
    SKVM.els("[data-toggle]", root).forEach((node) => {
      if (node.dataset.bound) return;
      node.dataset.bound = "1";
      node.addEventListener("click", () => {
        const target = SKVM.el(node.dataset.toggle);
        if (target) target.classList.toggle("hidden");
      });
    });
  };

  /* ------------------------------------------------------------------ tabs */
  SKVM.initTabs = function (root) {
    SKVM.els("[data-tabs]", root).forEach((group) => {
      if (group.dataset.bound) return;
      group.dataset.bound = "1";
      group.addEventListener("click", (event) => {
        const button = event.target.closest("[data-tab]");
        if (!button) return;
        const scope = group.dataset.tabs;
        SKVM.els(`[data-tab]`, group).forEach((node) => node.classList.toggle("active", node === button));
        SKVM.els(`[data-tab-panel]`).forEach((panel) => {
          if (panel.dataset.tabGroup && scope && panel.dataset.tabGroup !== scope) return;
          panel.classList.toggle("hidden", panel.dataset.tabPanel !== button.dataset.tab);
        });
        if (button.dataset.tabUrl && window.history.replaceState) {
          window.history.replaceState({}, "", button.dataset.tabUrl);
        }
      });
    });
  };

  /* -------------------------------------------------------- command palette */
  const PALETTE_GROUP_ORDER = ["Navigate", "Servers", "Actions", "Account", "Admin"];

  SKVM.commandPalette = function (commands) {
    const list = commands || [];
    let backdrop = SKVM.el(".palette-backdrop");
    if (!backdrop) {
      backdrop = document.createElement("div");
      backdrop.className = "palette-backdrop";
      backdrop.innerHTML = `
        <div class="palette" role="dialog" aria-label="Command palette">
          <input type="text" placeholder="Search servers, pages, actions…" autocomplete="off" spellcheck="false">
          <div class="results"></div>
        </div>`;
      document.body.appendChild(backdrop);
      backdrop.addEventListener("click", (event) => { if (event.target === backdrop) SKVM.closePalette(); });
    }
    const input = backdrop.querySelector("input");
    const results = backdrop.querySelector(".results");
    let filtered = list.slice();
    let cursor = 0;

    function render() {
      if (!filtered.length) {
        results.innerHTML = `<div class="result"><span class="muted">No matches</span></div>`;
        return;
      }
      const groups = {};
      filtered.forEach((item, index) => {
        const key = item.group || "Actions";
        (groups[key] = groups[key] || []).push(Object.assign({ _index: index }, item));
      });
      results.innerHTML = PALETTE_GROUP_ORDER.concat(Object.keys(groups)).filter((key, i, all) => groups[key] && all.indexOf(key) === i)
        .map((key) => `
          <div class="group" style="padding:10px 14px 4px;font-size:11px;letter-spacing:.14em;text-transform:uppercase;color:var(--ink-4)">${SKVM.escape(key)}</div>
          ${groups[key].map((item) => `
            <div class="result ${item._index === cursor ? "sel" : ""}" data-index="${item._index}">
              ${SKVM.icon(item.icon || "chevron", 16)}
              <span>${SKVM.escape(item.label)}</span>
              ${item.hint ? `<span class="kbd-hint">${SKVM.escape(item.hint)}</span>` : ""}
            </div>`).join("")}`).join("");
      SKVM.els(".result", results).forEach((node) => {
        node.addEventListener("mouseenter", () => { cursor = Number(node.dataset.index); paintSelection(); });
        node.addEventListener("click", () => run(Number(node.dataset.index)));
      });
    }

    function paintSelection() {
      SKVM.els(".result", results).forEach((node) =>
        node.classList.toggle("sel", Number(node.dataset.index) === cursor));
      const selected = results.querySelector(".result.sel");
      selected && selected.scrollIntoView({ block: "nearest" });
    }

    function run(index) {
      const item = filtered[index];
      if (!item) return;
      SKVM.closePalette();
      if (item.action) item.action();
      else if (item.url) window.location.assign(item.url);
    }

    function search(term) {
      const needle = term.trim().toLowerCase();
      filtered = !needle ? list.slice() : list.filter((item) => {
        const haystack = [item.label, item.group, item.keywords].filter(Boolean).join(" ").toLowerCase();
        return needle.split(/\s+/).every((part) => haystack.includes(part));
      });
      cursor = 0;
      render();
    }

    input.addEventListener("input", () => search(input.value));
    input.addEventListener("keydown", (event) => {
      if (event.key === "ArrowDown") { event.preventDefault(); cursor = Math.min(cursor + 1, filtered.length - 1); paintSelection(); }
      if (event.key === "ArrowUp") { event.preventDefault(); cursor = Math.max(cursor - 1, 0); paintSelection(); }
      if (event.key === "Enter") { event.preventDefault(); run(cursor); }
      if (event.key === "Escape") SKVM.closePalette();
    });
    SKVM._paletteSearch = search;
    render();
    return { input, render };
  };

  SKVM.openPalette = function () {
    const backdrop = SKVM.el(".palette-backdrop");
    if (!backdrop) return;
    backdrop.classList.add("open");
    const input = backdrop.querySelector("input");
    input.value = "";
    SKVM._paletteSearch && SKVM._paletteSearch("");
    setTimeout(() => input.focus(), 40);
  };

  SKVM.closePalette = function () {
    const backdrop = SKVM.el(".palette-backdrop");
    backdrop && backdrop.classList.remove("open");
  };

  /* --------------------------------------------------------- live refresh */
  SKVM.bus = {
    _handlers: {},
    on(event, handler) { (this._handlers[event] = this._handlers[event] || []).push(handler); },
    emit(event, data) { (this._handlers[event] || []).forEach((handler) => { try { handler(data); } catch (e) { console.error(e); } }); },
  };

  SKVM.liveSource = function (options) {
    const opts = options || {};
    const interval = Math.max(1500, (opts.interval || 5) * 1000);
    let timer = null;
    let stopped = false;
    let failures = 0;

    async function tick() {
      if (stopped) return;
      try {
        const data = await SKVM.get(opts.url);
        failures = 0;
        opts.onData && opts.onData(data);
      } catch (error) {
        failures += 1;
        if (failures === 3) SKVM.warn("Live updates paused — retrying…");
        if (failures > 6) { stop(); return; }
      }
      if (!stopped) timer = setTimeout(tick, interval);
    }
    function start() { stopped = false; tick(); }
    function stop() { stopped = true; clearTimeout(timer); }
    start();
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) { clearTimeout(timer); }
      else if (!stopped) { clearTimeout(timer); tick(); }
    });
    return { start, stop, get failures() { return failures; } };
  };

  SKVM.metricsSocket = function (uid, handlers) {
    const hs = handlers || {};
    let socket = null;
    let retries = 0;
    let closedByUser = false;

    function connect() {
      const proto = window.location.protocol === "https:" ? "wss" : "ws";
      const url = `${proto}://${window.location.host}/ws/server/${uid}/metrics`;
      socket = new WebSocket(url);
      socket.addEventListener("open", () => { retries = 0; hs.onOpen && hs.onOpen(); });
      socket.addEventListener("message", (event) => {
        try {
          const payload = JSON.parse(event.data);
          hs.onMessage && hs.onMessage(payload);
        } catch (error) { /* ignore malformed */ }
      });
      socket.addEventListener("close", () => {
        hs.onClose && hs.onClose();
        if (closedByUser) return;
        retries += 1;
        setTimeout(connect, Math.min(15000, 1200 * retries));
      });
      socket.addEventListener("error", () => socket && socket.close());
    }
    connect();
    return {
      close() { closedByUser = true; socket && socket.close(); },
      get socket() { return socket; },
    };
  };

  /* ------------------------------------------------------------- animation */
  SKVM.countUp = function (node, to, options) {
    const opts = options || {};
    const from = Number(node.dataset.value || 0);
    const duration = opts.duration || 700;
    const decimals = opts.decimals != null ? opts.decimals : (String(to).includes(".") ? 1 : 0);
    const start = performance.now();
    node.dataset.value = to;
    function frame(now) {
      const progress = Math.min(1, (now - start) / duration);
      const eased = 1 - Math.pow(1 - progress, 3);
      const value = from + (to - from) * eased;
      node.textContent = value.toFixed(decimals) + (opts.suffix || "");
      if (progress < 1) requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  };

  /* ------------------------------------------------------------ form helper */
  SKVM.formData = function (form) {
    const data = {};
    new FormData(form).forEach((value, key) => {
      if (data[key] === undefined) data[key] = value;
      else if (Array.isArray(data[key])) data[key].push(value);
      else data[key] = [data[key], value];
    });
    return data;
  };

  SKVM.submit = function (form, options) {
    const opts = options || {};
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = form.querySelector('[type="submit"]');
      button && button.classList.add("is-loading");
      const errors = form.querySelectorAll(".field-error");
      errors.forEach((node) => node.remove());
      try {
        const payload = form.enctype === "multipart/form-data"
          ? new FormData(form)
          : SKVM.formData(form);
        const data = await SKVM.api(opts.url || form.action, { method: opts.method || form.method || "POST", body: payload });
        button && button.classList.remove("is-loading");
        SKVM.ok(data.message || opts.successMessage || "Saved");
        if (data.redirect) setTimeout(() => window.location.assign(data.redirect), 650);
        opts.onSuccess && opts.onSuccess(data);
      } catch (error) {
        button && button.classList.remove("is-loading");
        if (error instanceof SKVM.ApiError && error.payload && error.payload.problems) {
          const list = error.payload.problems;
          list.slice(0, 6).forEach((problem) => {
            const node = document.createElement("div");
            node.className = "field-error";
            node.textContent = problem;
            form.appendChild(node);
          });
        }
        SKVM.handleError(error);
        opts.onError && opts.onError(error);
      }
    });
  };

  /* ------------------------------------------------------------------ boot */
  SKVM.initAll = function (root) {
    SKVM.initDropdowns(root);
    SKVM.initActions(root);
    SKVM.initTabs(root);
    SKVM.els("[data-tip]", root);
  };

  document.addEventListener("DOMContentLoaded", function () {
    SKVM.initAll();

    // Sidebar toggle + mobile rail
    const app = SKVM.el(".app");
    SKVM.els("[data-rail-toggle]").forEach((button) => {
      button.addEventListener("click", () => {
        if (window.matchMedia("(max-width: 960px)").matches) app.classList.toggle("rail-open");
        else {
          app.classList.toggle("rail-collapsed");
          localStorage.setItem("skvm.rail", app.classList.contains("rail-collapsed") ? "collapsed" : "expanded");
        }
      });
    });
    if (localStorage.getItem("skvm.rail") === "collapsed" && window.innerWidth > 960) app.classList.add("rail-collapsed");

    // Theme switch (server-side persisted when signed in)
    SKVM.els("[data-theme-set]").forEach((button) => {
      button.addEventListener("click", async () => {
        const theme = button.dataset.themeSet;
        document.documentElement.dataset.theme = theme;
        localStorage.setItem("skvm.theme", theme);
        try { await SKVM.post("/account/api/v1/theme", { theme }); } catch (error) { /* offline is fine */ }
        SKVM.ok(`Theme: ${theme}`);
      });
    });

    // Global keyboard shortcuts
    document.addEventListener("keydown", (event) => {
      const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName) || document.activeElement.isContentEditable;
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        SKVM.openPalette();
      }
      if (event.key === "?" && !typing) { event.preventDefault(); SKVM.openPalette(); }
      if (event.key === "/" && !typing) { event.preventDefault(); SKVM.openPalette(); }
      if (event.key === "Escape") { SKVM.closePalette(); }
    });

    SKVM.els("[data-open-palette]").forEach((node) => node.addEventListener("click", SKVM.openPalette));

    // Sidebar keyboard navigation niceties: g+d dashboard, g+s servers
    let gPressed = false;
    document.addEventListener("keydown", (event) => {
      const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName);
      if (typing) return;
      if (event.key === "g") { gPressed = true; setTimeout(() => { gPressed = false; }, 900); return; }
      if (!gPressed) return;
      const map = { d: "/dashboard", s: "/servers", a: "/alerts", f: "/files", m: "/admin", n: "/servers/new", p: "/account" };
      const target = map[event.key.toLowerCase()];
      if (target) window.location.assign(target);
      gPressed = false;
    });
  });

  SKVM.ready = (fn) => (document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", fn) : fn());
})(window, document);
