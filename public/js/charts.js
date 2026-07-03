// Minimal SVG chart pieces following the dataviz mark specs:
// 2px lines, recessive baseline, last-point marker, text in ink tokens.

const NS = "http://www.w3.org/2000/svg";

function svgEl(tag, attrs = {}) {
  const n = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  return n;
}

/**
 * Sparkline for a stat tile. points: [[ts, value], ...]
 * Single series — the tile title + current value carry identity,
 * so no legend and no per-point labels.
 */
export function sparkline(points, { color = "#3987e5", width = 220, height = 40 } = {}) {
  const svg = svgEl("svg", {
    viewBox: `0 0 ${width} ${height}`,
    preserveAspectRatio: "none",
    role: "img",
  });
  if (!points || points.length < 2) {
    const line = svgEl("line", {
      x1: 0, y1: height - 1, x2: width, y2: height - 1,
      stroke: "rgba(126,178,209,0.18)", "stroke-width": 1, "stroke-dasharray": "3 4",
    });
    svg.append(line);
    return svg;
  }
  const vals = points.map(p => p[1]);
  let min = Math.min(...vals), max = Math.max(...vals);
  if (min === max) { min -= 1; max += 1; }
  const pad = 3;
  const x = i => (i / (points.length - 1)) * width;
  const y = v => pad + (1 - (v - min) / (max - min)) * (height - pad * 2);

  const d = points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p[1]).toFixed(1)}`).join(" ");

  // faint area fill under the line for atmosphere, then the 2px line
  const area = svgEl("path", {
    d: `${d} L${width},${height} L0,${height} Z`,
    fill: color, opacity: 0.10,
  });
  const line = svgEl("path", {
    d, fill: "none", stroke: color, "stroke-width": 2,
    "stroke-linejoin": "round", "stroke-linecap": "round",
    "vector-effect": "non-scaling-stroke",
  });
  const last = points[points.length - 1];
  const dot = svgEl("circle", {
    cx: x(points.length - 1), cy: y(last[1]), r: 2.6,
    fill: color, stroke: "#0a1016", "stroke-width": 1.5,
  });
  svg.append(area, line, dot);
  return svg;
}

/** Labeled sparkline row: name on the left, current value right, chart under. */
export function sparkRow(label, points, { color = "#3987e5", height = 34, format = v => v } = {}) {
  const wrap = document.createElement("div");
  wrap.style.margin = "10px 0 2px";
  const last = points && points.length ? points[points.length - 1][1] : null;
  const head = document.createElement("div");
  head.className = "meter-label";
  head.innerHTML = `<span>${label}</span><b>${last == null ? "—" : format(last)}</b>`;
  const chart = document.createElement("div");
  chart.style.height = `${height}px`;
  const svg = sparkline(points, { color, height });
  svg.style.width = "100%";
  svg.style.height = `${height}px`;
  chart.append(svg);
  wrap.append(head, chart);
  return wrap;
}

/** Horizontal meter bar (CPU/mem). Value 0–100. Status color by threshold. */
export function meter(label, pct, { detail = "" } = {}) {
  const wrap = document.createElement("div");
  wrap.className = "meter";
  const color = pct == null ? "var(--ink-3)"
    : pct >= 90 ? "var(--critical)"
    : pct >= 75 ? "var(--warning)"
    : "var(--cyan)";
  const shown = pct == null ? "—" : `${pct.toFixed(0)}%`;
  wrap.innerHTML = `
    <div class="meter-label"><span>${label}</span><b>${detail ? detail + " · " : ""}${shown}</b></div>
    <div class="meter-track"><div class="meter-fill" style="width:${pct ?? 0}%;background:${color}"></div></div>`;
  return wrap;
}
