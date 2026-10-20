/* Web console: xterm.js over the SKVM WebSocket console bridge. */
(function () {
  "use strict";
  const SKVM = window.SKVM;

  function readPayload() {
    const node = document.getElementById("console-data");
    try {
      return JSON.parse(node.textContent);
    } catch (error) {
      return { uid: "unknown", ws: "/ws", running: false };
    }
  }

  SKVM.ready(function () {
    const data = readPayload();
    const host = document.getElementById("terminal");
    if (!host || typeof window.Terminal !== "function") {
      SKVM.err("Terminal library failed to load");
      return;
    }

    const theme = getComputedStyle(document.documentElement);
    const term = new window.Terminal({
      cursorBlink: true,
      cursorStyle: "bar",
      fontFamily: "'JetBrains Mono', 'Fira Code', ui-monospace, SFMono-Regular, Menlo, monospace",
      fontSize: 13.5,
      lineHeight: 1.35,
      letterSpacing: 0.2,
      scrollback: 8000,
      convertEol: false,
      allowProposedApi: true,
      macOptionIsMeta: true,
      theme: {
        background: "#080b16",
        foreground: "#e6ecff",
        cursor: "#a78bfa",
        cursorAccent: "#080b16",
        selectionBackground: "rgba(124,92,255,.34)",
        black: "#1b1f31", red: "#ff6b81", green: "#34d399", yellow: "#fbbf24",
        blue: "#60a5fa", magenta: "#c084fc", cyan: "#22d3ee", white: "#dbe3f7",
        brightBlack: "#4b5570", brightRed: "#ff8fa3", brightGreen: "#6ee7b7", brightYellow: "#fcd34d",
        brightBlue: "#93c5fd", brightMagenta: "#d8b4fe", brightCyan: "#67e8f9", brightWhite: "#f3f6ff",
      },
    });

    let fit = null;
    if (window.FitAddon && window.FitAddon.FitAddon) {
      fit = new window.FitAddon.FitAddon();
      term.loadAddon(fit);
    }
    if (window.WebLinksAddon && window.WebLinksAddon.WebLinksAddon) {
      try {
        term.loadAddon(new window.WebLinksAddon.WebLinksAddon());
      } catch (error) { /* optional */ }
    }

    term.open(host);
    const sizeLabel = document.getElementById("term-size");
    const latencyLabel = document.getElementById("term-latency");
    const connText = document.getElementById("conn-text");
    const connState = document.getElementById("conn-state");

    function refit() {
      if (fit) {
        try { fit.fit(); } catch (error) { /* hidden pane */ }
      }
      if (sizeLabel) sizeLabel.textContent = `${term.cols}×${term.rows}`;
      if (socket && socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ type: "resize", rows: term.rows, cols: term.cols }));
      }
    }
    window.addEventListener("resize", SKVM.debounce(refit, 120));

    /* ------------------------------------------------------------ socket */
    let socket = null;
    let attempts = 0;
    let shell = "/bin/bash";
    let closedByUser = false;

    function banner(text, tone) {
      term.write(`\r\n\x1b[38;5;244m# ${text}\x1b[0m\r\n`);
      if (tone === "error") SKVM.err(text);
    }

    function setState(state, text) {
      if (connState) connState.className = `conn ${state}`;
      if (connText) connText.textContent = text;
    }

    async function connect() {
      if (socket) {
        closedByUser = true;
        try { socket.close(); } catch (error) { /* already closed */ }
      }
      closedByUser = false;
      let token = "";
      try {
        const minted = await SKVM.post(data.token_url);
        token = minted.token;
      } catch (error) {
        SKVM.handleError(error);
        return;
      }
      const scheme = window.location.protocol === "https:" ? "wss" : "ws";
      const url = `${scheme}://${window.location.host}${data.ws}?token=${encodeURIComponent(token)}`;
      socket = new WebSocket(url);

      socket.onopen = () => {
        attempts = 0;
        setState("good", "connected");
        refit();
      };
      socket.onmessage = (event) => {
        const started = performance.now();
        let payload;
        try { payload = JSON.parse(event.data); } catch (error) { return; }
        switch (payload.type) {
          case "hello":
            term.write(`\x1b[38;5;141m◆\x1b[0m ${payload.message}\r\n`);
            break;
          case "ready":
            shell = payload.shell || shell;
            document.getElementById("shell-state").textContent = shell;
            term.focus();
            break;
          case "output":
            term.write(payload.data || "");
            break;
          case "warning":
            banner(payload.message || "warning", "warn");
            break;
          case "error":
            banner(payload.message || "console error", "error");
            setState("crit", "error");
            break;
          case "heartbeat":
          case "pong":
            if (latencyLabel) latencyLabel.textContent = `${Math.round(performance.now() - started)} ms`;
            break;
          default:
            break;
        }
      };
      socket.onclose = (event) => {
        if (closedByUser) return;
        setState("warn", "reconnecting");
        attempts += 1;
        term.write(`\r\n\x1b[38;5;244m# connection closed (${event.code || "?"}) — retrying in ${Math.min(10, attempts * 2)}s\x1b[0m\r\n`);
        if (attempts <= 6) setTimeout(connect, Math.min(10000, attempts * 2000));
        else banner("gave up reconnecting — press Reconnect", "error");
      };
      socket.onerror = () => setState("warn", "socket error");
    }

    const send = (payload) => {
      if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(payload));
      else SKVM.warn("Console is not connected");
    };

    term.onData((chunk) => send({ type: "input", data: chunk }));
    term.onResize(({ rows, cols }) => send({ type: "resize", rows, cols }));

    /* ----------------------------------------------------------- chrome */
    document.getElementById("btn-reconnect")?.addEventListener("click", () => {
      term.write("\r\n\x1b[38;5;244m# reconnecting…\x1b[0m\r\n");
      connect();
    });
    document.getElementById("btn-clear")?.addEventListener("click", () => term.clear());
    document.getElementById("btn-copy-sel")?.addEventListener("click", () => {
      const selection = term.getSelection();
      if (selection) SKVM.copy(selection, "Terminal selection");
      else SKVM.warn("Select some text first");
    });
    document.getElementById("btn-fs")?.addEventListener("click", () => {
      const frame = document.getElementById("console-frame");
      const full = frame.classList.toggle("is-fullscreen");
      document.body.classList.toggle("no-scroll", full);
      setTimeout(refit, 120);
    });
    document.getElementById("shell-picker")?.addEventListener("click", (event) => {
      const button = event.target.closest("button[data-shell]");
      if (!button) return;
      document.querySelectorAll("#shell-picker button").forEach((node) => node.classList.toggle("active", node === button));
      shell = button.dataset.shell;
      SKVM.warn(`Shell switch applies on reconnect — reconnecting with ${shell}`);
      connect();
    });
    document.querySelectorAll("[data-send]").forEach((button) => {
      button.addEventListener("click", () => {
        send({ type: "input", data: (button.dataset.send || "") + "\n" });
        term.focus();
      });
    });
    // Paste handling: xterm handles paste itself, but ⌘V inside an iframe needs help
    host.addEventListener("paste", (event) => {
      const text = (event.clipboardData || window.clipboardData)?.getData("text");
      if (text) {
        event.preventDefault();
        send({ type: "input", data: text });
      }
    });

    /* --------------------------------------------------- live mini-chart */
    const spark = document.getElementById("console-spark");
    const series = (data.series || []).map((point) => point.cpu || 0);
    if (spark && series.length) {
      SKVM.sparkline(spark, series, { color: "#7c5cff", height: 38 });
    }
    const live = SKVM.liveSource({
      url: `/servers/${data.uid}/api/v1/metrics?window=live&points=40`,
      interval: 6,
      onData(payload) {
        const stats = payload.stats || {};
        document.querySelectorAll(".mini-metric").forEach((row) => {
          const label = row.querySelector("span")?.textContent?.trim();
          const value = row.querySelector(".metric-value");
          const meter = row.querySelector(".meter i");
          if (label === "CPU" && value) { value.textContent = `${(stats.cpu_pct || 0).toFixed(0)}%`; if (meter) meter.style.width = Math.min(100, stats.cpu_pct || 0) + "%"; }
          if (label === "Memory" && value) { value.textContent = `${(stats.ram_pct || 0).toFixed(0)}%`; if (meter) meter.style.width = Math.min(100, stats.ram_pct || 0) + "%"; }
          if (label === "Disk" && value) { value.textContent = `${(stats.disk_pct || 0).toFixed(0)}%`; if (meter) meter.style.width = Math.min(100, stats.disk_pct || 0) + "%"; }
        });
        if (spark) SKVM.sparkline(spark, (payload.series || []).map((point) => point.cpu || 0), { color: "#7c5cff", height: 38 });
      },
    });

    window.addEventListener("beforeunload", () => {
      live.stop();
      closedByUser = true;
      send({ type: "close" });
    });

    if (!data.running) {
      banner(`instance is ${data.status} — start it to open a shell`, "error");
      setState("warn", data.status);
    } else {
      connect();
    }
    refit();
    setTimeout(() => term.focus(), 200);
  });
})();
