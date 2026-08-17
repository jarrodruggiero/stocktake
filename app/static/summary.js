/* The Portfolio page's performance chart.

   Deliberately small and separate from `charts.js`: that file drives the
   charts page's grid of configurable cards, and this is one fixed chart whose
   only interaction is the range bar. Reusing it would mean loading the whole
   builder to draw two lines.

   ## Filtering happens in the browser

   The server sends the daily series once, and a range button slices it. The
   alternative — a request per range — would be a round trip to filter data the
   page already has, and the series is a few hundred points even for a decade.

   Colours come from the `--viz-*` custom properties, which are the palette the
   charts page validated. Reading them at draw time rather than hard-coding is
   what makes the chart follow a theme change. */
(function () {
  "use strict";

  var node = document.getElementById("summarydata");
  var canvas = document.getElementById("summarychart");
  if (!node || !canvas || typeof Chart === "undefined") { return; }

  var data;
  try { data = JSON.parse(node.textContent); } catch (e) { return; }
  if (!data || !data.dates || !data.gain || data.dates.length < 2) { return; }

  function asPercent(value) {
    return (value * 100).toFixed(2) + "%";
  }

  var chart = null;

  /* Chart.js animation is JavaScript, so the stylesheet's global
     `prefers-reduced-motion` override cannot reach it — the same reason
     charts.js checks the query itself. This file was missed when that went in,
     which meant the one chart on the page people land on animated no matter
     what the machine had been asked for. */
  var stillness = window.matchMedia("(prefers-reduced-motion: reduce)");

  /* The skeleton. Added HERE rather than in the template on purpose: without
     JavaScript there is no chart at all, so a placeholder rendered server-side
     would shimmer forever promising something that is never coming. */
  var plot = canvas.closest(".plot");
  if (plot) { plot.classList.add("loading"); }

  function token(name, fallback) {
    var value = getComputedStyle(document.body).getPropertyValue(name);
    return (value || "").trim() || fallback;
  }

  function sliceFrom(from) {
    /* The first index at or after `from`. A plain filter would work, but the
       series is sorted, so this keeps the three arrays in step by construction
       rather than by three filters agreeing. */
    var start = 0;
    if (from) {
      while (start < data.dates.length && data.dates[start] < from) { start++; }
    }
    /* Never fewer than two points: one point draws nothing, and a range button
       that produces an empty chart looks broken rather than empty. */
    if (data.dates.length - start < 2) { start = Math.max(0, data.dates.length - 2); }
    return start;
  }

  function draw(from) {
    var start = sliceFrom(from);
    var labels = data.dates.slice(start);
    /* A slice, NOT a rebase. The series is cumulative gain — money made over
       money in — so every point already means "how far ahead the portfolio is
       today". Rebasing to the start of the range would turn it back into a
       within-window return, which is the thing that read 125% where the
       portfolio had made 47%. Selecting a range changes the window you are
       looking through, never the number at the right-hand end. */
    var pct = data.gain.slice(start);

    if (chart) { chart.destroy(); }
    chart = new Chart(canvas.getContext("2d"), {
      type: "line",
      data: {
        labels: labels,
        datasets: [
          {
            label: "Gain",
            data: pct,
            borderColor: token("--viz-1", "#2a78d6"),
            backgroundColor: "transparent",
            borderWidth: 2,
            pointRadius: 0,
            tension: 0.15,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: stillness.matches ? false : { duration: 420, easing: "easeOutQuart" },
        interaction: { mode: "index", intersect: false },
        plugins: {
          /* One series needs no legend box — the card's heading names it. */
          legend: { display: false },
          tooltip: {
            displayColors: false,
            callbacks: {
              label: function (item) { return asPercent(item.parsed.y / 100); },
            },
          },
        },
        scales: {
          x: {
            /* Tick count from the WIDTH, not a constant. Eight ISO dates need
               roughly 560px; on a 412px phone they ran together into one
               unreadable string along the axis. */
            ticks: {
              color: token("--viz-ink", "#93a0af"),
              maxTicksLimit: Math.max(3, Math.floor((canvas.clientWidth || 600) / 95)),
              maxRotation: 0, autoSkip: true,
            },
            grid: { display: false },
          },
          y: {
            ticks: {
              color: token("--viz-ink", "#93a0af"), maxTicksLimit: 6,
              callback: function (v) { return v.toFixed(0) + "%"; },
            },
            grid: {
              /* Nought is the line that matters on a return chart — above it
                 is profit, below it is loss — so it is drawn harder than the
                 rest of the grid rather than being one gridline among six. */
              lineWidth: function (ctx) { return ctx.tick.value === 0 ? 2 : 1; },
              color: function (ctx) {
                return ctx.tick.value === 0
                  ? token("--viz-axis", "#383835")
                  : token("--viz-grid", "#2c2c2a");
              },
            },
          },
        },
      },
    });
  }

  if (plot) { plot.classList.remove("loading"); }

  var bar = document.getElementById("summaryrange");
  if (bar) {
    bar.addEventListener("click", function (event) {
      var button = event.target.closest("button[data-range]");
      if (!button) { return; }
      bar.querySelectorAll("button").forEach(function (b) {
        b.classList.toggle("active", b === button);
      });
      draw(button.dataset.from);
    });
  }

  /* Start on the last button — "All" — so the first thing shown is the whole
     history rather than a window somebody has to widen. */
  var initial = bar && bar.querySelector("button.active");
  draw(initial ? initial.dataset.from : null);

  /* A theme change re-reads the tokens; without this the chart keeps the old
     palette until the page is reloaded. */
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", function () {
    var active = bar && bar.querySelector("button.active");
    draw(active ? active.dataset.from : null);
  });
})();
