/* Price vs total return chart for the ETF screen's details panel.
 *
 *   const chart = TrChart.mount(element, "YMAX", { win: "1y" });   // or { win: "custom", start: "2023-05-01" }
 *   chart.update({ win: "3y" });   // when the screen's window changes (no-op if unchanged)
 *   chart.destroy();
 *
 * Dollar view: the market price and the value of one share bought at the window's start
 * with every distribution reinvested, on one axis; the shaded gap between them is what
 * the distributions added. % view: both as % change since the start, plus SPY if ticked.
 * Data: /api/etf/chart (same dates as the table). Needs uPlot and static/charts.css.
 */
(function () {
  "use strict";
  const VIEW_KEY = "sbm.trView.v1", LOG_KEY = "sbm.trLog.v1", SPY_KEY = "sbm.trSpy.v1";
  const POLL_MS = 3000, POLL_MAX = 10;
  const DASH = "–";

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function pref(k, d) { try { return localStorage.getItem(k) || d; } catch { return d; } }
  function setPref(k, v) { try { localStorage.setItem(k, v); } catch {} }
  function cssVar(name, fallback) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
  }
  function rgba(hex, a) {
    const m = /^#([0-9a-f]{6})$/i.exec(hex);
    if (!m) return hex;
    const n = parseInt(m[1], 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
  }
  function colors() {
    const up = cssVar("--up", "#1C7650");
    return { ink: cssVar("--ink", "#13233A"), muted: cssVar("--muted", "#5A6A7F"), rule: cssVar("--rule", "#CDD6E0"),
             up, band: rgba(up, 0.16), spy: cssVar("--focus", "#2F6FEB"), tick: cssVar("--heat-2", "#C2860F") };
  }
  const minus = (s) => s.replace("-", "−");
  const signed = (v, d = 1) => v == null ? DASH : minus(`${v > 0 ? "+" : ""}${(v * 100).toFixed(d)}%`);
  const money = (v) => v == null ? DASH : `$${v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  // Plot each day at noon UTC so it lands on the right date in any US or European time zone.
  const toX = (s) => Date.UTC(+s.slice(0, 4), +s.slice(5, 7) - 1, +s.slice(8, 10), 12) / 1000;
  const nice = (s) => new Date(toX(s) * 1000).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric", timeZone: "UTC" });

  function mount(root, symbol, params) {
    let view = pref(VIEW_KEY, "usd") === "pct" ? "pct" : "usd";
    let log = pref(LOG_KEY, "0") === "1", spyOn = pref(SPY_KEY, "0") === "1";
    let key = null, data = null, plot = null, polls = 0, timer = null, ctrl = null, dead = false, xs = [];
    root.textContent = "";
    root.classList.add("trchart");

    const bar = el("div", "cbar");
    const seg = el("div", "cseg");
    seg.setAttribute("role", "group");
    seg.setAttribute("aria-label", "Chart view");
    for (const [v, label] of [["usd", "$ value"], ["pct", "% change"]]) {
      const b = el("button", null, label);
      b.type = "button"; b.dataset.view = v;
      b.addEventListener("click", () => { view = v; setPref(VIEW_KEY, v); draw(); });
      seg.append(b);
    }
    const check = (label, on, set) => {
      const l = el("label", "ccheck"), i = el("input");
      i.type = "checkbox"; i.checked = on;
      i.addEventListener("change", () => { set(i.checked); draw(); });
      l.append(i, document.createTextNode(label));
      return [l, i];
    };
    const [logBox, logIn] = check("Log scale", log, (v) => { log = v; setPref(LOG_KEY, v ? "1" : "0"); });
    const [spyBox, spyIn] = check("SPY total return", spyOn, (v) => { spyOn = v; setPref(SPY_KEY, v ? "1" : "0"); });
    const csv = el("button", "quiet", "Download CSV");
    csv.type = "button";
    csv.addEventListener("click", download);
    bar.append(seg, logBox, spyBox, csv);
    const summary = el("p", "csummary");
    const status = el("div", "cstatus");
    const host = el("div", "cplot");
    const tip = el("div", "ctip");
    const legend = el("div", "clegend");
    const note = el("p", "cnote");
    root.append(bar, summary, status, host, legend, note);

    const ro = typeof ResizeObserver === "function" ? new ResizeObserver(() => {
      if (plot && host.clientWidth && Math.abs(plot.width - host.clientWidth) > 2) plot.setSize(size());
    }) : null;
    if (ro) ro.observe(host);
    const scheme = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
    const recolor = () => draw();
    if (scheme && scheme.addEventListener) scheme.addEventListener("change", recolor);

    function size() {
      const w = Math.max(260, host.clientWidth || root.clientWidth || 600);
      return { width: w, height: w < 600 ? 240 : 280 };
    }
    function setStatus(text, retry) {
      status.textContent = text || "";
      status.hidden = !text;
      if (retry) {
        const b = el("button", "quiet", "Try again");
        b.type = "button";
        b.addEventListener("click", () => { polls = 0; load(); });
        status.append(" ", b);
      }
    }
    function query(p) {
      if (p.win === "custom") return p.start ? `win=custom&start=${encodeURIComponent(p.start)}` : null;
      return `win=${encodeURIComponent(p.win || "inception")}`;
    }

    function update(p) {
      const q = query(p || {});
      const k = q || "custom:none";
      if (k === key) return;
      key = k;
      polls = 0;
      if (!q) {
        data = null; draw();
        summary.textContent = "";
        setStatus("Pick a start date above.");
        return;
      }
      load();
    }

    async function load() {
      clearTimeout(timer);
      if (!window.uPlot) { setStatus("The chart library didn't load. Reload the page to try again."); return; }
      const q = query(params);
      if (!q) return;
      if (ctrl) ctrl.abort();
      const mine = ctrl = new AbortController();
      setStatus("Loading price history…");
      try {
        const res = await fetch(`/api/etf/chart?symbol=${encodeURIComponent(symbol)}&${q}`, { cache: "no-store", signal: mine.signal });
        const d = await res.json();
        if (dead || mine !== ctrl) return;
        if (d.pending) {
          if (++polls > POLL_MAX) { setStatus("Still loading the price history from Yahoo.", true); return; }
          timer = setTimeout(load, POLL_MS);
          return;
        }
        polls = 0;
        if (d.error) { data = null; draw(); summary.textContent = ""; setStatus(d.error); return; }
        data = d;
        setStatus("");
        draw();
      } catch (e) {
        if (e.name === "AbortError" || dead) return;
        setStatus("Couldn't load the price history.", true);
      }
    }

    function writeSummary() {
      const s = data.summary;
      summary.textContent = "";
      const part = (label, v, extra) => {
        const span = el("span", v > 0 ? "pos" : v < 0 ? "neg" : null, signed(v));
        summary.append(`${label} `, span, extra || "");
      };
      part("Price", s.price_return);
      summary.append(" · ");
      part("Total return", s.total_return, s.total_ann != null ? ` (${signed(s.total_ann)}/yr)` : "");
      summary.append(" · ");
      const pts = s.added_pts;
      summary.append(s.distributions_count
        ? `Distributions ${money(s.distributions_per_share)}/share added ${minus((pts > 0 ? "+" : "") + pts.toFixed(0))} pts`
        : "No distributions in this window");
    }

    function draw() {
      if (dead) return;
      seg.querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.view === view)));
      logBox.classList.toggle("off", view !== "usd"); logIn.disabled = view !== "usd";
      spyBox.classList.toggle("off", view !== "pct"); spyIn.disabled = view !== "pct";
      if (plot) { plot.destroy(); plot = null; }
      host.textContent = "";
      legend.textContent = "";
      note.textContent = "";
      if (!data) return;
      writeSummary();
      const c = colors();
      xs = data.dates.map(toX);
      const p0 = data.price[0];
      const asPct = (arr) => arr.map(v => v == null ? null : (v / p0 - 1) * 100);
      const usd = view === "usd";
      const priceS = usd ? data.price : asPct(data.price);
      const trS = usd ? data.tr : asPct(data.tr);
      const series = [
        {},
        { label: "Price", stroke: c.ink, width: 1.5, points: { show: false } },
        { label: "Total return", stroke: c.up, width: 2, points: { show: false } },
      ];
      const values = [xs, priceS, trS];
      if (!usd && spyOn) {
        series.push({ label: "SPY total return", stroke: c.spy, width: 1.5, dash: [5, 4], points: { show: false } });
        values.push(asPct(data.spy_tr));
      }
      const yfmt = usd ? (v) => `$${v >= 100 ? v.toFixed(0) : v.toFixed(2)}` : (v) => minus(`${v > 0 ? "+" : ""}${v.toFixed(0)}%`);
      const opts = {
        ...size(),
        legend: { show: false },
        cursor: { drag: { x: true, y: false }, points: { show: false } },
        scales: { x: { time: true }, y: usd && log ? { distr: 3, log: 10, range: (u, lo, hi) => [lo * 0.95, hi * 1.05] } : {} },
        axes: [
          { stroke: c.muted, grid: { stroke: c.rule, width: 1 }, ticks: { stroke: c.rule } },
          { stroke: c.muted, grid: { stroke: c.rule, width: 1 }, ticks: { stroke: c.rule }, size: 60,
            values: (u, vals) => vals.map(v => v == null ? "" : yfmt(v)) },
        ],
        series,
        bands: [{ series: [2, 1], fill: c.band }],
        hooks: { draw: [u => ticks(u, c)], setCursor: [u => hover(u)] },
      };
      plot = new window.uPlot(opts, values, host);
      plot.over.append(tip);
      plot.over.addEventListener("mouseleave", () => { tip.style.display = "none"; });

      const item = (cls, color, text, bg) => {
        const s = el("span"), i = el("i", cls);
        i.style.color = color;
        if (bg) i.style.background = bg;
        s.append(i, document.createTextNode(text));
        legend.append(s);
      };
      item("", c.ink, usd ? "Market price" : "Price, % since start");
      item("", c.up, usd ? "Total return: one share with distributions reinvested" : "Total return, % since start");
      item("area", c.up, "Distributions reinvested", c.band);
      if (!usd && spyOn) item("dots", c.spy, "SPY total return");
      if (data.distributions.length) item("tick", c.tick, "Ex-distribution date", c.tick);

      const lines = [`${data.start} to ${data.end} (${data.years.toFixed(1)} yrs)` +
                     (data.weekly ? ", weekly points." : ".") +
                     " Total return uses Yahoo's adjusted close, the same as the table above."];
      if (data.clamped) lines.push(`Starts at inception, ${data.start}; your date (${data.requested_start}) was earlier.`);
      for (const n of data.notes || []) lines.push(n);
      note.textContent = lines.join(" ");
    }

    function ticks(u, c) {
      if (!data.distributions.length) return;
      const { left, top, width, height } = u.bbox, ctx = u.ctx, dpr = window.devicePixelRatio || 1;
      ctx.save();
      ctx.strokeStyle = c.tick; ctx.lineWidth = 2 * dpr;
      ctx.beginPath();
      for (const d of data.distributions) {
        const x = u.valToPos(toX(d.date), "x", true);
        if (x < left || x > left + width) continue;
        ctx.moveTo(x, top + height); ctx.lineTo(x, top + height - 7 * dpr);
      }
      ctx.stroke();
      ctx.restore();
    }

    function hover(u) {
      const i = u.cursor.idx;
      if (i == null || !data) { tip.style.display = "none"; return; }
      const p0 = data.price[0];
      tip.textContent = "";
      tip.append(el("b", null, nice(data.dates[i])));
      const line = (k, v) => { const s = el("div"); s.append(el("span", "k", k + " "), v); tip.append(s); };
      line("Price", `${money(data.price[i])} (${signed(data.price[i] / p0 - 1)})`);
      line("Total return value", `${money(data.tr[i])} (${signed(data.tr[i] / p0 - 1)})`);
      if (view === "pct" && spyOn) line("SPY total return", signed(data.spy_tr[i] / p0 - 1));
      const from = i > 0 ? data.dates[i - 1] : "";
      const paid = data.distributions.filter(d => d.date > from && d.date <= data.dates[i]);
      for (const d of paid) tip.append(el("span", "src", `Distribution ${money(d.amount)} (ex-date ${nice(d.date)})`));
      tip.style.display = "block";
      const w = tip.offsetWidth, h = tip.offsetHeight, left = u.cursor.left;
      tip.style.left = `${Math.min(Math.max(0, left + 12), Math.max(0, u.over.clientWidth - w))}px`;
      tip.style.top = `${Math.max(0, Math.min(u.cursor.top - h - 10, u.over.clientHeight - h))}px`;
    }

    function download() {
      if (!data) { alert("The chart hasn't loaded yet."); return; }
      const esc = (x) => x == null ? "" : `"${String(x).replace(/"/g, '""')}"`;
      const p0 = data.price[0];
      const paid = new Map();
      for (let i = 0, j = 0; i < data.dates.length; i++) {           // distributions within each plotted period
        let sum = 0;
        while (j < data.distributions.length && data.distributions[j].date <= data.dates[i]) sum += data.distributions[j++].amount;
        if (sum) paid.set(i, +sum.toFixed(6));
      }
      const lines = [["Date", "Price", "Total return value (distributions reinvested)", "SPY total return (scaled)",
        "Price % since start", "Total return % since start", "SPY % since start", "Distributions paid ($/share)"].map(esc).join(",")];
      data.dates.forEach((d, i) => {
        lines.push([d, data.price[i], data.tr[i], data.spy_tr[i],
          ((data.price[i] / p0 - 1) * 100).toFixed(4), ((data.tr[i] / p0 - 1) * 100).toFixed(4),
          ((data.spy_tr[i] / p0 - 1) * 100).toFixed(4), paid.get(i) || ""].map(esc).join(","));
      });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/csv" }));
      a.download = `${symbol}-price-vs-total-return-${data.start}-to-${data.end}.csv`;
      a.click();
      URL.revokeObjectURL(a.href);
    }

    params = params || { win: "inception" };
    update(params);
    return {
      update(p) { params = p || params; update(params); },
      destroy() {
        dead = true;
        clearTimeout(timer);
        if (ctrl) ctrl.abort();
        if (ro) ro.disconnect();
        if (scheme && scheme.removeEventListener) scheme.removeEventListener("change", recolor);
        if (plot) plot.destroy();
        root.textContent = "";
      },
    };
  }

  window.TrChart = { mount };
})();
