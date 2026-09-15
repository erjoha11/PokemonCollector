// Shared Chart.js renderer for every chart card built by macros.html's
// chart_card macro. Reads its config from the <script type="application/json"
// id="{cardId}-data"> sibling instead of inline JS per chart, so this is the
// one place chart look-and-feel lives. Re-runs safely on htmx swaps: htmx
// executes <script> tags in swapped-in content, and any previous Chart.js
// instance on the same canvas is destroyed first so re-filtering a metric
// (or re-opening a lazy-loaded section) never leaves a stale/duplicate chart.
function initTcgChart(cardId) {
  var dataEl = document.getElementById(cardId + "-data");
  var canvas = document.getElementById(cardId + "-canvas");
  if (!dataEl || !canvas) return;

  var cfg = JSON.parse(dataEl.textContent);
  var existing = Chart.getChart(canvas);
  if (existing) existing.destroy();

  new Chart(canvas.getContext("2d"), {
    type: cfg.type,
    data: {
      labels: cfg.labels,
      datasets: cfg.datasets.map(function (ds, i) {
        return {
          label: ds.label,
          data: ds.data,
          borderColor: ds.color,
          backgroundColor: cfg.type === "bar" ? ds.color : ds.color + "26",
          borderWidth: 2,
          borderRadius: cfg.type === "bar" ? 4 : 0,
          borderDash: cfg.type === "line" && i > 0 ? [6, 4] : undefined,
          tension: 0.25,
          pointRadius: 2,
          pointHoverRadius: 4,
          fill: cfg.type === "line" && cfg.datasets.length === 1,
        };
      }),
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      scales: {
        y: {
          beginAtZero: true,
          ticks: {
            callback: function (v) {
              return v.toLocaleString("nb-NO") + " kr";
            },
          },
        },
      },
      plugins: {
        legend: { display: false }, // chart_card renders its own .viz-legend markup
        tooltip: {
          callbacks: {
            label: function (item) {
              return item.dataset.label + ": " + item.parsed.y.toLocaleString("nb-NO") + " kr";
            },
          },
        },
      },
    },
  });
}
