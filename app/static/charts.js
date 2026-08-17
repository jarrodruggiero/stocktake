/* Chart rendering — Chart.js styled to the dataviz spec:
 * thin marks, 4px rounded data-ends, 2px lines with no point dots (surface-
 * ringed on hover), solid hairline grid, text in ink tokens, tooltips on
 * everything, sign-colored bars for gain/loss (diverging poles), fixed
 * entity colors, and a table-view twin per chart. Colors come from CSS
 * custom properties so the light/dark swap re-renders from one source. */
(function () {
  "use strict";

  function tokens() {
    /* **From `body`, not `documentElement`.**
       The theme lives on `<body data-theme="dark">`, so the dark overrides are
       declared on `body` — `:root` only ever holds the light values. Reading
       the computed style of the documentElement therefore returned the LIGHT
       palette no matter which theme was on, and every chart on this page has
       been drawn in light-mode colours on a dark surface.

       That is why the Portfolio page's chart looked right and these did not:
       `summary.js` reads `document.body`. It also explains the doughnut's
       "thick white border" — the ring is painted in `--panel`, which was
       resolving to #ffffff.

       Two symptoms, one line. */
    const s = getComputedStyle(document.body);
    const v = (n) => s.getPropertyValue(n).trim();
    return {
      s1: v("--viz-1"), s2: v("--viz-2"), s3: v("--viz-3"),
      pos: v("--viz-pos"), neg: v("--viz-neg"),
      grid: v("--viz-grid"), axis: v("--viz-axis"), ink: v("--viz-ink"),
      surface: v("--panel"),
    };
  }

  const money = (v) => "$" + Number(v).toLocaleString(undefined, { maximumFractionDigits: 0 });
  const moneyHtml = (v) => '<span class="m">' + money(v) + "</span>";
  const hidden = () => document.body.classList.contains("hide-values");
  const pct = (v) => (v * 100).toFixed(2) + "%";
  const charts = [];

  /* Chart.js animation is JavaScript, so the CSS override that covers every
     transition on the page cannot reach it — this is the one place motion has
     to check the preference itself.

     Short and once: a chart that draws in tells you it is live data rather
     than a picture, and a chart that keeps moving is just in the way. The
     media query is read at construction; a chart is rebuilt on theme change
     anyway, which is the only time it would matter. */
  const stillness = window.matchMedia("(prefers-reduced-motion: reduce)");
  const draw = () => (stillness.matches
    ? false
    : { duration: 420, easing: "easeOutQuart" });

  function base(t) {
    return {
      responsive: true,
      maintainAspectRatio: false,
      animation: draw(),
      interaction: { mode: "index", intersect: false },
      plugins: {
        legend: {
          labels: { color: t.ink, boxWidth: 12, boxHeight: 12, usePointStyle: true, pointStyle: "rectRounded" },
        },
        tooltip: { padding: 10, boxPadding: 4 },
      },
      scales: {
        /* `autoSkipPadding` scales the skipping to the available width, so
           this one needs no explicit limit — but the padding has to be wide
           enough for an ISO date on a phone. */
        x: { grid: { display: false }, border: { color: t.axis },
             ticks: { color: t.ink, maxRotation: 0, autoSkipPadding: 24 } },
        y: { grid: { color: t.grid }, border: { display: false }, ticks: { color: t.ink } },
      },
    };
  }

  function lineDataset(t, color, label, data, dates) {
    return {
      label, data,
      borderColor: color, backgroundColor: color,
      borderWidth: 1.5, pointRadius: 0, pointHitRadius: 12,
      pointHoverRadius: 5, pointHoverBorderColor: t.surface, pointHoverBorderWidth: 2,
      tension: 0, spanGaps: true,
    };
  }


  /* ── Drawing fewer points than the data has ────────────────────────────
     A daily series over three years is ~1,100 points drawn into ~1,100
     device pixels. Every pixel column then gets one or more line segments
     stacked on top of each other, and a 2px stroke over that reads as a
     furry band rather than a line — "too thick, like it hasn't rendered
     properly". It is not a browser difference; Chrome and Firefox produce
     the same picture.

     So the line is decimated to about one point per pixel before it is
     drawn. Min/max bucketing rather than plain sampling: each bucket
     contributes its lowest AND highest point, so a one-day spike survives
     instead of being stepped over. The indices are chosen from the first
     dataset and then applied to every dataset and to the labels, which is
     what keeps the series aligned with each other and with the axis.

     **Only what is DRAWN is reduced.** The table view underneath each chart
     is built from the full series, so nothing is lost — the exact numbers
     are one disclosure triangle away. */
  function decimate(labels, datasets, target) {
    const n = labels.length;
    if (n <= target || !datasets.length) return { labels, datasets };
    const buckets = Math.max(1, Math.floor(target / 2));
    const size = n / buckets;
    const primary = datasets[0].data || [];
    const keep = new Set([0, n - 1]);
    for (let b = 0; b < buckets; b++) {
      const start = Math.floor(b * size);
      const end = Math.min(n, Math.floor((b + 1) * size));
      let lo = -1, hi = -1;
      for (let i = start; i < end; i++) {
        const v = primary[i];
        if (v === null || v === undefined) continue;
        if (lo < 0 || v < primary[lo]) lo = i;
        if (hi < 0 || v > primary[hi]) hi = i;
      }
      if (lo >= 0) keep.add(lo);
      if (hi >= 0) keep.add(hi);
    }
    const idx = Array.from(keep).sort((a, b) => a - b);
    return {
      labels: idx.map((i) => labels[i]),
      datasets: datasets.map((d) => Object.assign({}, d, { data: idx.map((i) => d.data[i]) })),
    };
  }

  function make(id, cfg) {
    const el = document.getElementById(id);
    if (!el) return;
    charts.push(new Chart(el, cfg));
  }

  function table(id, header, rows) {
    const el = document.getElementById(id);
    if (!el) return;
    const h = "<tr>" + header.map((c) => "<th>" + c + "</th>").join("") + "</tr>";
    const b = rows.map((r) => "<tr>" + r.map((c) => "<td>" + c + "</td>").join("") + "</tr>").join("");
    el.innerHTML = "<div class='tablewrap'><table><thead>" + h + "</thead><tbody>" + b + "</tbody></table></div>";
  }

  function signColors(t, values) {
    return values.map((v) => (v < 0 ? t.neg : t.pos));
  }

  const barSpec = { borderRadius: 4, borderSkipped: "start", maxBarThickness: 26, categoryPercentage: 0.7, barPercentage: 0.85 };

  function moneyTicks(o) { o.scales.y.ticks.callback = (v) => (hidden() ? "" : money(v)); return o; }
  function pctTicks(o) { o.scales.y.ticks.callback = (v) => pct(v); return o; }
  function moneyTips(o, lbl) {
    o.plugins.tooltip.callbacks = {
      label: (c) =>
        hidden()
          ? c.dataset.label || ""
          : (lbl ? c.dataset.label + ": " : "") + money(c.parsed.y ?? c.parsed),
    };
    return o;
  }
  function pctTips(o) {
    o.plugins.tooltip.callbacks = { label: (c) => pct(c.parsed.y) };
    o.plugins.legend.display = false;
    return o;
  }

  // ---- portfolio charts page ----

  // ---- single instrument page ----
  function renderInstrument(series) {
    const t = tokens();
    make("c-instrument", {
      type: "line",
      data: {
        labels: series.dates,
        datasets: [
          lineDataset(t, t.s1, "Value", series.value),
          lineDataset(t, t.s2, "Invested", series.invested),
        ],
      },
      options: moneyTips(moneyTicks(base(t)), true),
    });
    const monthly = [];
    for (let i = 0; i < series.dates.length; i++) {
      const last = i === series.dates.length - 1 || series.dates[i].slice(0, 7) !== series.dates[i + 1].slice(0, 7);
      if (last) monthly.push([series.dates[i], moneyHtml(series.invested[i]), moneyHtml(series.value[i])]);
    }
    table("t-instrument", ["Month end", "Invested", "Value"], monthly);
  }

  /* Skeletons. Applied from script, never from the template: with no
     JavaScript there is no chart, so a server-rendered placeholder would
     shimmer for ever promising one. Cleared when a render pass finishes,
     which is the one place that knows every card is drawn. */
  function skeletons(on) {
    document.querySelectorAll(".plot").forEach((p) => p.classList.toggle("loading", on));
  }

  function renderAll() {
    skeletons(true);
    charts.splice(0).forEach((c) => c.destroy());
    // The charts page is entirely spec-driven now: one loop over the user's
    // saved charts, each drawn by the same renderer as the builder preview.
    const pEl = document.getElementById("chart-data");
    if (pEl) {
      for (const [id, data] of Object.entries(JSON.parse(pEl.textContent))) {
        const c = renderSpec("c-" + id, "t-" + id, data);
        if (c) charts.push(c);
      }
    }
    const iEl = document.getElementById("instrument-data");
    if (iEl) renderInstrument(JSON.parse(iEl.textContent));
    skeletons(false);
  }


  /* Draw data produced by a chart SPEC (app/charts_build.py) — used by the
     builder's live preview and by saved charts on this page, so both go
     through one renderer rather than drifting apart. */
  function renderSpec(canvasId, tableId, data) {
    const t = tokens();
    /* The categorical order, and what is NOT in it.
       `--viz-pos` and `--viz-neg` are the diverging poles — gain and loss —
       so using them as "series 4 and 5" both reuses a reserved meaning and,
       because `--viz-pos` IS `--viz-1`, painted the fourth slice the same
       colour as the first. A four-slice doughnut had two identical wedges.
       Three hues then a neutral; beyond that it repeats, which is a signal
       there are too many categories to colour-code at all. */
    const palette = [t.s1, t.s2, t.s3, t.ink];
    const allMoney = data.datasets.every((d) => d.kind === "money");
    const allPercent = data.datasets.every((d) => d.kind === "percent");
    const kind = data.type === "area" ? "line" : data.type === "hbar" ? "bar" : data.type;

    const tableRows = data.labels.map((l, i) => [
      l,
      ...data.datasets.map((d) =>
        d.kind === "percent" ? pct(d.data[i]) : d.kind === "money" ? moneyHtml(d.data[i]) : d.data[i]
      ),
    ]);
    if (tableId) table(tableId, [data.x_label, ...data.datasets.map((d) => d.label)], tableRows);
    if (data.type === "table") return null;

    const el = document.getElementById(canvasId);
    if (!el) return null;

    const datasets = data.datasets.map((d, i) => {
      if (data.type === "doughnut") {
        return {
          data: d.data,
          backgroundColor: data.labels.map((_, j) => palette[j % palette.length]),
          borderColor: t.surface,
          // The ring is the SURFACE colour, so it reads as a gap between
          // segments rather than a drawn border — provided the surface it
          // resolves to is the one actually behind the chart. See `tokens`.
          borderWidth: 1.5,
        };
      }
      if (kind === "line") {
        return {
          label: d.label,
          data: d.data,
          borderColor: palette[i % palette.length],
          backgroundColor: data.type === "area" ? palette[i % palette.length] + "33" : "transparent",
          fill: data.type === "area",
          borderWidth: 1.5, pointRadius: 0, tension: 0,
        };
      }
      const single = data.datasets.length === 1;
      return {
        label: d.label,
        data: d.data,
        // One series: colour by sign, so gains and losses read at a glance.
        // Several: colour by series, or they become indistinguishable — which
        // is what happened to year-end growth and yearly invested & gain.
        backgroundColor:
          single && (d.kind === "money" || d.kind === "percent")
            ? signColors(t, d.data)
            : palette[i % palette.length],
        ...barSpec,
      };
    });

    let options = base(t);
    if (data.type === "hbar") options.indexAxis = "y";
    if (data.stacked) {
      // A split stacks: each holding lands in exactly one series, so the bars
      // total the same as the unsplit chart.
      options.scales.x.stacked = true;
      options.scales.y.stacked = true;
    }
    if (data.type !== "doughnut") {
      options = allMoney ? moneyTips(moneyTicks(options), data.datasets.length > 1)
              : allPercent ? pctTips(pctTicks(options))
              : options;
    } else {
      options = {
        responsive: true, maintainAspectRatio: false, animation: draw(), cutout: "62%",
        plugins: {
          legend: { position: "right", labels: { color: t.ink, boxWidth: 12, usePointStyle: true, pointStyle: "rectRounded" } },
          tooltip: {
            callbacks: {
              label: (c) => c.label + ": " + (hidden() ? "" : allMoney ? money(c.parsed) : c.parsed),
            },
          },
        },
      };
    }
    if (data.datasets.length < 2 && data.type !== "doughnut" && options.plugins) {
      options.plugins.legend = { display: false };
    }
    /* Reduce only what is drawn, and only for the forms that suffer from it —
       a bar or a doughnut has few enough categories to be safe, and dropping
       one of them would drop a whole entity rather than a sample. The table
       above was already built from the full series. */
    const drawn = kind === "line"
      ? decimate(data.labels, datasets, Math.max(320, el.clientWidth || 900))
      : { labels: data.labels, datasets };
    return new Chart(el, {
      type: data.type === "doughnut" ? "doughnut" : kind,
      data: { labels: drawn.labels, datasets: drawn.datasets },
      options,
    });
  }

  window.portfolioCharts = { renderSpec, renderAll };

  document.addEventListener("DOMContentLoaded", () => {
    Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
    Chart.defaults.font.size = 12;
    renderAll();
    // dark/light swap re-reads the tokens
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", renderAll);
    window.addEventListener("values-visibility-changed", renderAll);
    document.querySelectorAll(".rangebar button").forEach((b) =>
      b.addEventListener("click", () => {
        window.__vizRange = b.dataset.range;
        document.querySelectorAll(".rangebar button").forEach((x) => x.classList.toggle("active", x === b));
        renderAll();
      })
    );
  });
})();
