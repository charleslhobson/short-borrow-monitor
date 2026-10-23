/* Borrow fee history chart, shared by the Stocks page and the ETF screen.
 *
 *   const chart = BorrowChart.mount(element, "GME");   // later: chart.destroy()
 *
 * Draws IBKR's borrow fee (step line, left axis) and shares available (light area,
 * right axis) from /api/borrow/history, with stretches this site didn't record shaded
 * and a dotted line where iBorrowDesk's history ends and the site's own readings begin.
 * Needs uPlot (static/uPlot.iife.min.js) and static/charts.css.
 */
(function () {
  "use strict";
  const RANGES = [["1m", "1M"], ["3m", "3M"], ["6m", "6M"], ["1y", "1Y"], ["3y", "3Y"], ["all", "All"]];
  const RANGE_KEY = "sbm.borrowRange.v1", LOG_KEY = "sbm.borrowLog.v1";
  const SRC = { 0: "Recorded by this site from IBKR's file", 1: "iBorrowDesk, daily",
                2: "iBorrowDesk, intraday", 3: "IBKR's latest file" };
  const POLL_MS = 3000, POLL_MAX = 10;               // first backfill: ask again every 3 s for up to 30 s
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
    const muted = cssVar("--muted", "#5A6A7F");
    return { ink: cssVar("--ink", "#13233A"), muted, rule: cssVar("--rule", "#CDD6E0"),
             fee: cssVar("--heat-3", "#D35A22"), area: rgba(muted, 0.16), areaLine: rgba(muted, 0.55),
             gap: rgba(muted, 0.12), focus: cssVar("--focus", "#2F6FEB") };
  }
  const pct = (v) => v == null ? DASH : `${v.toFixed(Math.abs(v) < 10 ? 2 : 1).replace("-", "−")}%`;
  function compact(v) {
    if (v == null) return DASH;
    const a = Math.abs(v);
    if (a >= 1e9) return (v / 1e9).toFixed(1) + "B";
    if (a >= 1e6) return (v / 1e6).toFixed(a >= 1e7 ? 0 : 1) + "M";
    if (a >= 1e3) return (v / 1e3).toFixed(a >= 1e4 ? 0 : 1) + "K";
    return String(Math.round(v));
  }
  const when = (t) => new Date(t * 1000).toLocaleString(undefined,
    { year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  const day = (t) => new Date(t * 1000).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });

  // Aligned arrays for uPlot. Each value holds until the next point (step line); a
  // recording gap with nothing inside it breaks the line, which resumes at the gap's end.
  function build(d) {
    const pts = d.points, rows = [];
    for (const p of pts) rows.push({ t: p.t, fee: p.fee, sh: p.shares, m: { p } });
    const lastBefore = (t) => { let b = null; for (const p of pts) { if (p.t <= t) b = p; else break; } return b; };
    for (const g of d.gaps || []) {
      if (pts.some(p => p.t > g.from && p.t < g.to)) continue;      // iBorrowDesk filled it
      const b = lastBefore(g.from);
      if (!b) continue;
      rows.push({ t: g.from + 1, fee: null, sh: null, m: { gap: g } });
      if (g.to < d.now && !pts.some(p => p.t === g.to)) rows.push({ t: g.to, fee: b.fee, sh: b.shares, m: { p: b, carry: true } });
    }
    rows.sort((a, b) => a.t - b.t || (a.m.p && !a.m.carry ? -1 : 1));
    const out = [];
    for (const r of rows) if (!out.length || out[out.length - 1].t !== r.t) out.push(r);
    const last = out[out.length - 1];
    if (last && last.fee != null && last.t < d.now - 60) out.push({ t: d.now, fee: last.fee, sh: last.sh, m: { p: last.m.p, carry: true } });
    return out;
  }

  function mount(root, symbol) {
    let range = pref(RANGE_KEY, "1y"), log = pref(LOG_KEY, "0") === "1";
    if (!RANGES.some(r => r[0] === range)) range = "1y";
    let data = null, rows = [], plot = null, polls = 0, timer = null, ctrl = null, dead = false;
    root.textContent = "";
    root.classList.add("bchart");

    const bar = el("div", "cbar");
    const seg = el("div", "cseg");
    seg.setAttribute("role", "group");
    seg.setAttribute("aria-label", "Time range");
    for (const [key, label] of RANGES) {
      const b = el("button", null, label);
      b.type = "button"; b.dataset.range = key;
      b.addEventListener("click", () => { range = key; setPref(RANGE_KEY, key); polls = 0; load(); });
      seg.append(b);
    }
    const logBox = el("label", "ccheck");
    const logIn = el("input"); logIn.type = "checkbox"; logIn.checked = log;
    logIn.addEventListener("change", () => { log = logIn.checked; setPref(LOG_KEY, log ? "1" : "0"); draw(); });
    logBox.append(logIn, document.createTextNode("Log scale"));
    const csv = el("button", "quiet", "Download CSV");
    csv.type = "button";
    csv.addEventListener("click", download);
    bar.append(seg, logBox, csv);
    const status = el("div", "cstatus");
    const host = el("div", "cplot");
    const tip = el("div", "ctip");
    const legend = el("div", "clegend");
    const note = el("p", "cnote");
    root.append(bar, status, host, legend, note);

    const ro = typeof ResizeObserver === "function" ? new ResizeObserver(() => {
      if (plot && host.clientWidth && Math.abs(plot.width - host.clientWidth) > 2) plot.setSize(size());
    }) : null;
    if (ro) ro.observe(host);
    const scheme = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
    const recolor = () => draw();
    if (scheme && scheme.addEventListener) scheme.addEventListener("change", recolor);

    function size() {
      const w = Math.max(260, host.clientWidth || root.clientWidth || 600);
      return { width: w, height: w < 600 ? 240 : 270 };
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

    async function load() {
      clearTimeout(timer);
      seg.querySelectorAll("button").forEach(b => b.setAttribute("aria-pressed", String(b.dataset.range === range)));
      if (!window.uPlot) { setStatus("The chart library didn't load. Reload the page to try again."); return; }
      if (ctrl) ctrl.abort();
      const mine = ctrl = new AbortController();
      if (!data) setStatus("Loading borrow history…");
      try {
        const res = await fetch(`/api/borrow/history?symbol=${encodeURIComponent(symbol)}&range=${range}`,
                                { cache: "no-store", signal: mine.signal });
        const d = await res.json();
        if (dead || mine !== ctrl) return;
        if (d.error) { data = null; draw(); setStatus(d.error); return; }
        if (d.pending) {
          if (++polls > POLL_MAX) { setStatus("Still fetching this ticker's history from iBorrowDesk.", true); return; }
          setStatus("Fetching this ticker's history from iBorrowDesk…");
          timer = setTimeout(load, POLL_MS);
          return;
        }
        polls = 0;
        data = d;
        draw();
      } catch (e) {
        if (e.name === "AbortError" || dead) return;
        setStatus("Couldn't load the borrow history.", true);
      }
    }

    function draw() {
      if (dead) return;
      if (plot) { plot.destroy(); plot = null; }
      host.textContent = "";
      legend.textContent = "";
      note.textContent = "";
      if (!data) return;
      rows = build(data);
      const shown = data.points.filter(p => p.fee != null || p.shares != null);
      if (!shown.length) {
        const why = data.backfill && data.backfill.status === "error"
          ? ` iBorrowDesk couldn't be reached (${data.backfill.error || "error"}).` : "";
        setStatus(`No borrow history for ${symbol} yet.${why}` +
          (data.recording ? " This site's hourly readings will appear here as they're recorded." : ""));
        notes();
        return;
      }
      setStatus("");
      const c = colors();
      const xs = rows.map(r => r.t);
      const fees = rows.map(r => r.fee == null ? null : (log && r.fee <= 0 ? null : r.fee));
      const shares = rows.map(r => r.sh);
      const step = window.uPlot.paths.stepped({ align: 1 });
      const fmax = Math.max(1e-9, ...fees.filter(v => v != null));
      const opts = {
        ...size(),
        legend: { show: false },
        cursor: { drag: { x: true, y: false }, points: { show: false } },
        scales: {
          x: { time: true },
          fee: log ? { distr: 3, log: 10, range: (u, lo, hi) => [lo * 0.8, hi * 1.25] } : { range: (u, lo, hi) => [0, (hi || fmax) * 1.12] },
          sh: { range: (u, lo, hi) => [0, Math.max(1, hi || 1) * 1.1] },
        },
        axes: [
          { stroke: c.muted, grid: { stroke: c.rule, width: 1 }, ticks: { stroke: c.rule } },
          { scale: "fee", stroke: c.muted, grid: { stroke: c.rule, width: 1 }, ticks: { stroke: c.rule }, size: 58,
            values: (u, vals) => vals.map(v => v == null ? "" : pct(v)) },
          { scale: "sh", side: 1, stroke: c.muted, grid: { show: false }, ticks: { stroke: c.rule }, size: 52,
            values: (u, vals) => vals.map(compact) },
        ],
        series: [
          {},
          { label: "Shares available", scale: "sh", stroke: c.areaLine, fill: c.area, width: 1, paths: step, points: { show: false } },
          { label: "Borrow fee", scale: "fee", stroke: c.fee, width: 2, paths: step, points: { show: false } },
        ],
        hooks: {
          drawClear: [u => shadeGaps(u, c)],
          draw: [u => handoff(u, c)],
          setCursor: [u => hover(u)],
        },
      };
      plot = new window.uPlot(opts, [xs, shares, fees], host);
      plot.over.append(tip);
      plot.over.addEventListener("mouseleave", () => { tip.style.display = "none"; });
      legend.innerHTML = "";
      const item = (cls, color, text) => {
        const s = el("span"); const i = el("i", cls); i.style.color = color;
        if (cls === "area") i.style.background = c.area;
        if (cls === "gapbox") { i.className = "area"; i.style.background = c.gap; }
        s.append(i, document.createTextNode(text)); legend.append(s);
      };
      item("", c.fee, "Borrow fee / yr (left)");
      item("area", c.muted, "Shares available (right)");
      if ((data.gaps || []).length) item("gapbox", c.muted, "Not recorded");
      if (data.ibd_until) item("dots", c.focus, "iBorrowDesk → this site");
      notes();
    }

    function notes() {
      note.textContent = "";
      if (data.ibd_link) {
        const a = el("a", null, "iBorrowDesk");
        a.href = data.ibd_link; a.target = "_blank"; a.rel = "noopener";
        note.append(data.ibd_until ? `Data before ${day(data.ibd_until)}: ` : "Data: ", a, ". ");
      }
      const parts = [];
      if (data.sources_note) parts.push(data.sources_note);
      for (const n of data.notes || []) parts.push(n);
      if (data.backfill && data.backfill.status === "error" && data.points.length) {
        parts.push(`iBorrowDesk couldn't be reached last time (${data.backfill.error || "error"}).`);
      }
      note.append(parts.join(" "));
    }

    function shadeGaps(u, c) {
      const gaps = data.gaps || [];
      if (!gaps.length) return;
      const { left, top, width, height } = u.bbox, ctx = u.ctx, dpr = window.devicePixelRatio || 1;
      ctx.save();
      ctx.beginPath(); ctx.rect(left, top, width, height); ctx.clip();
      for (const g of gaps) {
        const x0 = u.valToPos(g.from, "x", true), x1 = u.valToPos(g.to, "x", true);
        if (x1 < left || x0 > left + width) continue;
        ctx.fillStyle = c.gap;
        ctx.fillRect(x0, top, Math.max(dpr, x1 - x0), height);
        if (x1 - x0 > 80 * dpr) {
          ctx.fillStyle = c.muted;
          ctx.font = `${11 * dpr}px system-ui, sans-serif`;
          ctx.fillText("not recorded", Math.max(x0, left) + 4 * dpr, top + 14 * dpr);
        }
      }
      ctx.restore();
    }

    function handoff(u, c) {
      if (!data.ibd_until) return;
      const x = u.valToPos(data.ibd_until, "x", true), { left, top, width, height } = u.bbox;
      if (x < left || x > left + width) return;
      const ctx = u.ctx, dpr = window.devicePixelRatio || 1;
      ctx.save();
      ctx.strokeStyle = c.focus; ctx.lineWidth = dpr; ctx.setLineDash([4 * dpr, 4 * dpr]);
      ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, top + height); ctx.stroke();
      ctx.restore();
    }

    function hover(u) {
      const left = u.cursor.left;
      if (left == null || left < 0 || !rows.length) { tip.style.display = "none"; return; }
      const t = u.posToVal(left, "x");
      let i = -1;
      for (let lo = 0, hi = rows.length - 1; lo <= hi;) {            // the point in force at t
        const mid = (lo + hi) >> 1;
        if (rows[mid].t <= t) { i = mid; lo = mid + 1; } else hi = mid - 1;
      }
      if (i < 0) { tip.style.display = "none"; return; }
      const r = rows[i];
      tip.textContent = "";
      tip.append(el("b", null, when(Math.round(t))));
      if (r.m.gap) {
        tip.append(el("span", "src", "Not recorded: this site's recorder wasn't running."));
      } else if (r.fee == null && r.sh == null) {
        tip.append(el("span", "src", "Not in IBKR's file (not borrowable at IBKR)."));
      } else {
        const p = r.m.p || {};
        const line = (k, v) => { const s = el("div"); s.append(el("span", "k", k + " "), v); tip.append(s); };
        line("Fee", pct(p.fee) + " / yr");
        line("Rebate", pct(p.rebate) + " / yr");
        line("Shares", p.shares == null ? DASH : Number(p.shares).toLocaleString());
        const since = p.t != null && p.t < t - 3600 ? ` (unchanged since ${when(p.t)})` : "";
        tip.append(el("span", "src", (SRC[p.src] || "") + since));
      }
      tip.style.display = "block";
      const w = tip.offsetWidth, h = tip.offsetHeight, ow = u.over.clientWidth;
      tip.style.left = `${Math.min(Math.max(0, left + 12), Math.max(0, ow - w))}px`;
      tip.style.top = `${Math.max(0, Math.min(u.cursor.top - h - 10, u.over.clientHeight - h))}px`;
    }

    function download() {
      if (!data || !data.points.length) { alert("There's no borrow history to download yet."); return; }
      const esc = (x) => x == null ? "" : `"${String(x).replace(/"/g, '""')}"`;
      const lines = [["Time (UTC)", "Borrow fee %/yr", "Rebate %/yr", "Shares available", "Source"].map(esc).join(",")];
      for (const p of data.points) {
        lines.push([new Date(p.t * 1000).toISOString().replace(".000Z", "Z"), p.fee, p.rebate, p.shares, SRC[p.src]].map(esc).join(","));
      }
      for (const g of data.gaps || []) {
        lines.push([new Date(g.from * 1000).toISOString().replace(".000Z", "Z"), "", "", "",
          `Not recorded until ${new Date(g.to * 1000).toISOString().replace(".000Z", "Z")}`].map(esc).join(","));
      }
      const a = document.createElement("a");
      a.href = URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/csv" }));
      a.download = `${symbol}-borrow-history-${range}.csv`;
      a.click();
      URL.revokeObjectURL(a.href);
    }

    load();
    return {
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

  window.BorrowChart = { mount };
})();
