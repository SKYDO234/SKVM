/* ==========================================================================
   SKVM · chart primitives
   Hand-rolled SVG charts (no dependencies): sparklines, multi-series area
   charts with crosshair tooltips, radial gauges and heat strips.
   ========================================================================== */
(function (window, document) {
  "use strict";

  const SKVM = window.SKVM || (window.SKVM = {});
  const NS = "http://www.w3.org/2000/svg";

  function el(name, attrs) {
    const node = document.createElementNS(NS, name);
    Object.entries(attrs || {}).forEach(([key, value]) => {
      if (value != null) node.setAttribute(key, value);
    });
    return node;
  }

  function path(points, smooth) {
    if (!points.length) return "";
    if (!smooth || points.length < 3) {
      return points.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(2)},${p[1].toFixed(2)}`).join(" ");
    }
    let d = `M${points[0][0].toFixed(2)},${points[0][1].toFixed(2)}`;
    for (let i = 0; i < points.length - 1; i += 1) {
      const current = points[i];
      const next = points[i + 1];
      const cx = (current[0] + next[0]) / 2;
      d += ` C${cx.toFixed(2)},${current[1].toFixed(2)} ${cx.toFixed(2)},${next[1].toFixed(2)} ${next[0].toFixed(2)},${next[1].toFixed(2)}`;
    }
    return d;
  }

  /* ------------------------------------------------------------- sparkline */
  SKVM.sparkline = function (target, values, options) {
    const node = typeof target === "string" ? document.querySelector(target) : target;
    if (!node) return;
    const opts = options || {};
    const width = opts.width || node.clientWidth || 240;
    const height = opts.height || node.clientHeight || 34;
    const data = (values || []).map(Number).filter((value) => Number.isFinite(value));
    node.innerHTML = "";
    if (data.length < 2) {
      node.innerHTML = `<div class="skeleton" style="height:${height}px"></div>`;
      return;
    }
    const svg = el("svg", { class: "spark", viewBox: `0 0 ${width} ${height}`, preserveAspectRatio: "none" });
    const min = Math.min(...data);
    const max = Math.max(...data, min + 1);
    const step = width / (data.length - 1);
    const points = data.map((value, index) => [
      index * step,
      height - ((value - min) / (max - min)) * (height - 4) - 2,
    ]);
    const stroke = opts.color || "currentColor";
    const id = `spark-${Math.random().toString(36).slice(2, 9)}`;
    const gradient = el("linearGradient", { id, x1: "0", y1: "0", x2: "0", y2: "1" });
    gradient.appendChild(el("stop", { offset: "0%", "stop-color": stroke, "stop-opacity": "0.45" }));
    gradient.appendChild(el("stop", { offset: "100%", "stop-color": stroke, "stop-opacity": "0" }));
    const defs = el("defs");
    defs.appendChild(gradient);
    svg.appendChild(defs);

    const line = path(points, true);
    svg.appendChild(el("path", { d: `${line} L${width},${height} L0,${height} Z`, fill: `url(#${id})`, stroke: "none" }));
    svg.appendChild(el("path", { d: line, fill: "none", stroke, "stroke-width": opts.weight || 1.8, "stroke-linecap": "round" }));
    const last = points[points.length - 1];
    svg.appendChild(el("circle", { cx: last[0] - 1.5, cy: last[1], r: 2.4, fill: stroke }));
    node.appendChild(svg);
  };

  /* ------------------------------------------------------------ area chart */
  const SERIES_COLORS = {
    cpu: "#7c5cff",
    ram: "#22d3ee",
    disk: "#f472b6",
    rx: "#34d399",
    tx: "#fbbf24",
    load: "#a78bfa",
  };

  SKVM.areaChart = function (target, options) {
    const node = typeof target === "string" ? document.querySelector(target) : target;
    if (!node) return { update() {} };
    const opts = Object.assign({
      series: ["cpu", "ram"],
      labels: { cpu: "CPU", ram: "Memory", disk: "Disk", rx: "In", tx: "Out", load: "Load" },
      keys: { cpu: "cpu", ram: "ram", disk: "disk", rx: "rx", tx: "tx", load: "load" },
      height: 220,
      max: null,
      unit: "%",
      smooth: true,
      yTicks: 4,
      xLabel: (value) => value,
      showLegend: true,
    }, options || {});

    let points = [];
    node.innerHTML = "";

    function draw() {
      const width = node.clientWidth || 640;
      const height = opts.height;
      const padLeft = 42;
      const padRight = 12;
      const padTop = 14;
      const padBottom = 26;
      const innerW = Math.max(10, width - padLeft - padRight);
      const innerH = Math.max(10, height - padTop - padBottom);

      const values = [];
      opts.series.forEach((key) => points.forEach((point) => {
        const value = Number(point[opts.keys[key] || key]);
        if (Number.isFinite(value)) values.push(value);
      }));
      const maxValue = opts.max != null ? opts.max : Math.max(10, Math.ceil(Math.max(...values, 1) * 1.15));

      node.innerHTML = "";
      const svg = el("svg", { class: "chart", viewBox: `0 0 ${width} ${height}`, preserveAspectRatio: "none", style: `height:${height}px` });
      const defs = el("defs");
      svg.appendChild(defs);

      // grid + y axis
      for (let tick = 0; tick <= opts.yTicks; tick += 1) {
        const y = padTop + (innerH / opts.yTicks) * tick;
        svg.appendChild(el("line", { class: "grid-line", x1: padLeft, x2: width - padRight, y1: y, y2: y }));
        const label = el("text", { class: "label", x: padLeft - 8, y: y + 3.5, "text-anchor": "end" });
        label.textContent = `${Math.round(maxValue - (maxValue / opts.yTicks) * tick)}${opts.unit}`;
        svg.appendChild(label);
      }

      if (points.length < 2) {
        const note = el("text", { class: "label", x: width / 2, y: height / 2, "text-anchor": "middle" });
        note.textContent = "collecting samples…";
        svg.appendChild(note);
        node.appendChild(svg);
        return;
      }

      const stepX = innerW / Math.max(1, points.length - 1);
      const toXY = (value, index) => [
        padLeft + index * stepX,
        padTop + innerH - (Math.max(0, Math.min(maxValue, value)) / maxValue) * innerH,
      ];

      opts.series.forEach((key) => {
        const field = opts.keys[key] || key;
        const color = SERIES_COLORS[key] || "#7c5cff";
        const coords = points.map((point, index) => {
          const value = Number(point[field]);
          return toXY(Number.isFinite(value) ? value : 0, index);
        });
        const gradientId = `grad-${key}-${Math.random().toString(36).slice(2, 7)}`;
        const gradient = el("linearGradient", { id: gradientId, x1: "0", y1: "0", x2: "0", y2: "1" });
        gradient.appendChild(el("stop", { offset: "0%", "stop-color": color, "stop-opacity": "0.34" }));
        gradient.appendChild(el("stop", { offset: "100%", "stop-color": color, "stop-opacity": "0.02" }));
        defs.appendChild(gradient);

        const linePath = path(coords, opts.smooth);
        svg.appendChild(el("path", {
          d: `${linePath} L${coords[coords.length - 1][0]},${padTop + innerH} L${coords[0][0]},${padTop + innerH} Z`,
          fill: `url(#${gradientId})`,
        }));
        svg.appendChild(el("path", {
          d: linePath, fill: "none", stroke: color, "stroke-width": 2, "stroke-linecap": "round", "stroke-linejoin": "round",
        }));
        const last = coords[coords.length - 1];
        svg.appendChild(el("circle", { cx: last[0], cy: last[1], r: 3, fill: color }));
        svg.appendChild(el("circle", { cx: last[0], cy: last[1], r: 6.5, fill: color, opacity: "0.18" }));
      });

      // x axis labels (first / middle / last)
      [0, Math.floor(points.length / 2), points.length - 1].forEach((index) => {
        if (!points[index]) return;
        const label = el("text", {
          class: "label",
          x: padLeft + index * stepX,
          y: height - 8,
          "text-anchor": index === 0 ? "start" : (index === points.length - 1 ? "end" : "middle"),
        });
        label.textContent = opts.xLabel(points[index]);
        svg.appendChild(label);
      });

      // crosshair
      const crosshair = el("line", { class: "grid-line", x1: 0, x2: 0, y1: padTop, y2: padTop + innerH, opacity: "0" });
      const marker = el("g", { opacity: "0" });
      svg.appendChild(crosshair);
      svg.appendChild(marker);
      const tooltip = document.createElement("div");
      tooltip.style.cssText = "position:absolute;pointer-events:none;padding:8px 10px;border-radius:10px;font-size:12px;"
        + "background:rgba(10,14,28,.94);border:1px solid var(--line-strong);backdrop-filter:blur(12px);"
        + "box-shadow:var(--shadow);opacity:0;transition:opacity .12s;z-index:5;white-space:nowrap";
      node.style.position = "relative";
      node.appendChild(tooltip);

      const overlay = el("rect", { x: padLeft, y: padTop, width: innerW, height: innerH, fill: "transparent" });
      svg.appendChild(overlay);

      function onMove(event) {
        const rect = svg.getBoundingClientRect();
        const x = ((event.clientX - rect.left) / rect.width) * width;
        const index = Math.max(0, Math.min(points.length - 1, Math.round((x - padLeft) / stepX)));
        const point = points[index];
        const px = padLeft + index * stepX;
        crosshair.setAttribute("x1", px);
        crosshair.setAttribute("x2", px);
        crosshair.setAttribute("opacity", "1");
        marker.innerHTML = "";
        marker.setAttribute("opacity", "1");
        opts.series.forEach((key) => {
          const value = Number(point[opts.keys[key] || key]);
          if (!Number.isFinite(value)) return;
          const y = padTop + innerH - (Math.max(0, Math.min(maxValue, value)) / maxValue) * innerH;
          marker.appendChild(el("circle", { cx: px, cy: y, r: 3.4, fill: SERIES_COLORS[key] || "#7c5cff", stroke: "#0b0f1a", "stroke-width": 1.4 }));
        });
        tooltip.innerHTML = `<b>${new Date(point.ts || Date.now()).toLocaleTimeString()}</b><br>` +
          opts.series.map((key) => {
            const value = Number(point[opts.keys[key] || key]);
            const label = opts.labels[key] || key.toUpperCase();
            return `<span style="color:${SERIES_COLORS[key]}">●</span> ${label} ${Number.isFinite(value) ? value.toFixed(1) : "—"}${opts.unit}`;
          }).join("<br>");
        const ratio = (px / width);
        tooltip.style.left = `${Math.min(Math.max(6, ratio * 100), 70)}%`;
        tooltip.style.top = `${padTop}px`;
        tooltip.style.opacity = "1";
      }
      function onLeave() {
        crosshair.setAttribute("opacity", "0");
        marker.setAttribute("opacity", "0");
        tooltip.style.opacity = "0";
      }
      overlay.addEventListener("mousemove", onMove);
      overlay.addEventListener("mouseleave", onLeave);

      if (opts.showLegend) {
        const legend = document.createElement("div");
        legend.className = "legend";
        legend.style.marginTop = "8px";
        legend.innerHTML = opts.series.map((key) =>
          `<span><i style="background:${SERIES_COLORS[key] || "#7c5cff"}"></i>${opts.labels[key] || key.toUpperCase()}</span>`).join("");
        node.appendChild(legend);
      }
      svg.addEventListener("touchmove", (event) => {
        if (event.touches[0]) onMove({ clientX: event.touches[0].clientX });
      });
      node.appendChild(svg);
    }

    draw();
    const api = {
      set(data, options2) {
        points = Array.isArray(data) ? data : [];
        if (options2) Object.assign(opts, options2);
        draw();
      },
      push(point) {
        points = points.concat([point]).slice(-180);
        draw();
      },
      redraw: draw,
    };
    if (window.ResizeObserver) {
      let raf = null;
      const observer = new ResizeObserver(() => {
        cancelAnimationFrame(raf);
        raf = requestAnimationFrame(draw);
      });
      observer.observe(node);
    }
    return api;
  };

  /* ----------------------------------------------------------------- gauge */
  SKVM.gauge = function (target, options) {
    const node = typeof target === "string" ? document.querySelector(target) : target;
    if (!node) return { set() {} };
    const opts = Object.assign({ value: 0, max: 100, label: "CPU", unit: "%", small: false, caption: "" }, options || {});
    const size = opts.small ? 96 : 132;
    const stroke = opts.small ? 9 : 11;
    const radius = (size - stroke) / 2;
    const circumference = 2 * Math.PI * radius;
    node.classList.add("gauge");
    if (opts.small) node.classList.add("sm");
    node.innerHTML = `
      <svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}">
        <circle class="track" cx="${size / 2}" cy="${size / 2}" r="${radius}" stroke-width="${stroke}"></circle>
        <circle class="bar" cx="${size / 2}" cy="${size / 2}" r="${radius}" stroke-width="${stroke}"
          stroke-dasharray="${circumference}" stroke-dashoffset="${circumference}"></circle>
      </svg>
      <div class="inner">
        <div>
          <div class="val">0${opts.unit}</div>
          <div class="cap">${opts.caption || opts.label}</div>
        </div>
      </div>`;
    const bar = node.querySelector(".bar");
    const val = node.querySelector(".val");
    let current = 0;

    const palette = (pct) => (pct >= 90 ? ["#f87171", "#ef4444"] : (pct >= 70 ? ["#fbbf24", "#f59e0b"] : ["#7c5cff", "#22d3ee"]));

    function render(value, instant) {
      const pct = Math.max(0, Math.min(100, Number(value) || 0));
      const [from, to] = palette(pct * (opts.max === 100 ? 1 : (100 / (opts.max || 100))));
      const gradId = `g-${Math.random().toString(36).slice(2, 7)}`;
      const defs = node.querySelector("defs") || el("defs");
      if (!defs.parentNode) node.querySelector("svg").prepend(defs);
      defs.innerHTML = `<linearGradient id="${gradId}" x1="0" y1="0" x2="1" y2="1">
        <stop offset="0%" stop-color="${from}"/><stop offset="100%" stop-color="${to}"/></linearGradient>`;
      bar.setAttribute("stroke", `url(#${gradId})`);
      const offset = circumference * (1 - pct / 100);
      if (instant) {
        bar.style.transition = "none";
        bar.setAttribute("stroke-dashoffset", offset);
        requestAnimationFrame(() => { bar.style.transition = ""; });
      } else {
        bar.setAttribute("stroke-dashoffset", offset);
      }
      if (instant) val.textContent = `${Math.round((pct / 100) * opts.max)}${opts.unit}`;
      else SKVM.countUp(val, (pct / 100) * opts.max, { decimals: opts.max > 1000 ? 0 : 0, suffix: opts.unit, duration: 500 });
      current = pct;
    }
    render(opts.value, true);
    return { set: (value) => render(value, false), get value() { return current; } };
  };

  /* ------------------------------------------------------------ heat strip */
  SKVM.heatStrip = function (target, values, options) {
    const node = typeof target === "string" ? document.querySelector(target) : target;
    if (!node) return;
    const opts = options || {};
    const data = values || [];
    node.classList.add("heat");
    node.innerHTML = data.map((value) => {
      const level = Math.max(0, Math.min(4, Math.round((Number(value) || 0) / 25)));
      const colors = ["rgba(255,255,255,.06)", "rgba(52,211,153,.35)", "rgba(52,211,153,.65)", "rgba(251,191,36,.7)", "rgba(248,113,113,.75)"];
      return `<i style="background:${colors[level]}" title="${opts.title ? opts.title(value) : value}"></i>`;
    }).join("");
  };

  SKVM.chart = { path, colors: SERIES_COLORS };
})(window, document);
