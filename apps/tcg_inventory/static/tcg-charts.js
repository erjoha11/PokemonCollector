// Shared Chart.js renderer for every chart card built by macros.html's
// chart_card / market_value_card macros. Reads its config from the
// <script type="application/json" id="{cardId}-data"> sibling instead of
// inline JS per chart, so this is the one place chart look-and-feel lives.
// Re-runs safely on htmx swaps: htmx executes <script> tags in swapped-in
// content, and any previous Chart.js instance on the same canvas is
// destroyed first so re-filtering a metric (or re-opening a lazy-loaded
// section) never leaves a stale/duplicate chart.
//
// `timeSeries: true` (the Market Value card) is the portfolio mode: labels
// are YYYY-MM-DD dates plotted on a real time axis (a linear scale in ms --
// no date-adapter dependency), the y-axis is fitted to the visible data
// instead of starting at 0, and the tooltip shows each day's change.
//
// Optional `sourceNotes` (either mode): one entry per point, a list of extra
// tooltip lines (or null) -- used to mark price-source switches (issue #210).
//
// A dataset with `axis: "count"` (timeSeries only -- the Market Value card's
// card-count line, issue #243) is drawn stepped and dotted on its own
// right-hand axis ("y2", whole numbers), fitted separately from the kr axis
// and hidden together with its dataset.
var DAY_MS = 86400000;

function tcgKr(v) {
  return Math.round(v).toLocaleString("nb-NO") + " kr";
}

function tcgSignedKr(v) {
  return (v > 0 ? "+" : "") + tcgKr(v);
}

function tcgParseDay(label) {
  var p = label.split("-");
  return Date.UTC(+p[0], +p[1] - 1, +p[2]);
}

function tcgFormatDay(ms, withYear) {
  var opts = { day: "numeric", month: "short", timeZone: "UTC" };
  if (withYear) opts.year = "numeric";
  return new Date(ms).toLocaleDateString("nb-NO", opts);
}

// Fit the y-axis to whatever datasets are currently visible, with some air
// above and below (a flat line still gets a small band around it), snapped
// to a round step so the top/bottom labels are whole numbers -- never
// dipping below 0 for data that doesn't.
function tcgNiceStep(span) {
  var raw = span / 5;
  var mag = Math.pow(10, Math.floor(Math.log10(raw)));
  var steps = [1, 2, 2.5, 5, 10];
  for (var i = 0; i < steps.length; i++) if (steps[i] * mag >= raw) return steps[i] * mag;
  return 10 * mag;
}

function tcgFitAxis(chart, axisId, wholeNumbers) {
  var scale = chart.options.scales[axisId];
  if (!scale) return;
  var lo = Infinity, hi = -Infinity;
  chart.data.datasets.forEach(function (ds, i) {
    if (!chart.isDatasetVisible(i) || (ds.yAxisID || "y") !== axisId) return;
    ds.data.forEach(function (pt) {
      var y = typeof pt === "object" ? pt.y : pt;
      if (y < lo) lo = y;
      if (y > hi) hi = y;
    });
  });
  if (!isFinite(lo)) return;
  var pad = Math.max((hi - lo) * 0.12, Math.abs(hi) * 0.005, 1);
  var step = tcgNiceStep(hi - lo + 2 * pad);
  if (wholeNumbers) step = Math.max(1, Math.ceil(step));
  var min = Math.floor((lo - pad) / step) * step;
  if (lo >= 0 && min < 0) min = 0;
  scale.min = min;
  scale.max = Math.ceil((hi + pad) / step) * step;
  scale.ticks.stepSize = step;
}

function tcgFitY(chart) {
  tcgFitAxis(chart, "y", false);
  tcgFitAxis(chart, "y2", true);
}

// Day-aligned x ticks (~6 of them), so labels are always whole dates.
function tcgDayTicks(scale) {
  var days = Math.max(1, Math.round((scale.max - scale.min) / DAY_MS));
  var step = Math.max(1, Math.ceil(days / 6));
  var ticks = [];
  for (var t = scale.min; t <= scale.max + 1; t += step * DAY_MS) ticks.push({ value: t });
  scale.ticks = ticks;
}

function tcgToggleDataset(cardId, index, button) {
  var chart = Chart.getChart(document.getElementById(cardId + "-canvas"));
  if (!chart) return;
  var show = !chart.isDatasetVisible(index);
  chart.setDatasetVisibility(index, show);
  tcgFitY(chart);
  chart.update();
  if (button) {
    button.setAttribute("aria-pressed", show ? "true" : "false");
    button.classList.toggle("active", show);
  }
}

function initTcgChart(cardId) {
  var dataEl = document.getElementById(cardId + "-data");
  var canvas = document.getElementById(cardId + "-canvas");
  if (!dataEl || !canvas) return;

  var cfg = JSON.parse(dataEl.textContent);
  var existing = Chart.getChart(canvas);
  if (existing) existing.destroy();

  var timeSeries = !!cfg.timeSeries;
  var xs = timeSeries ? cfg.labels.map(tcgParseDay) : null;
  var nPoints = cfg.labels.length;
  // Number of cards per day (bought/sold/ripped...), so a value jump can be
  // told apart from pure price movement -- drawn as its own line (issue #243)
  // and listed in the value point's tooltip when that line is hidden.
  var counts = timeSeries ? cfg.cardCounts : null;
  var countIndex = -1;
  var hasCountAxis = false;

  var datasets = cfg.datasets.map(function (ds, i) {
    if (timeSeries && ds.axis === "count") {
      countIndex = i;
      hasCountAxis = true;
      return {
        label: ds.label,
        data: ds.data.map(function (y, j) { return { x: xs[j], y: y }; }),
        yAxisID: "y2",
        hidden: !!ds.hidden,
        borderColor: ds.color,
        backgroundColor: ds.color,
        borderWidth: 2,
        borderDash: [2, 3], // dotted -- Net invested is dashed [6, 4]
        // Horizontal, then vertical at the day it changed: a count holds
        // until the next snapshot day (Chart.js calls that "before").
        stepped: "before",
        tension: 0,
        pointRadius: 0,
        pointHoverRadius: 4,
        pointStyle: "rect",
        fill: false,
      };
    }
    return {
      label: ds.label,
      data: timeSeries
        ? ds.data.map(function (y, j) { return { x: xs[j], y: y }; })
        : ds.data,
      hidden: !!ds.hidden,
      borderColor: ds.color,
      backgroundColor: cfg.type === "bar" ? ds.color : ds.color + "26",
      borderWidth: 2,
      borderRadius: cfg.type === "bar" ? 4 : 0,
      borderDash: cfg.type === "line" && i > 0 ? [6, 4] : undefined,
      tension: timeSeries ? 0.15 : 0.25,
      pointRadius: timeSeries ? (nPoints > 45 ? 0 : 2) : 2,
      pointBackgroundColor: ds.color,
      pointBorderColor: ds.color,
      pointHoverRadius: 5,
      // Portfolio mode fills down to the (fitted) bottom of the axis, not 0.
      fill: cfg.type === "line" && i === 0 && (timeSeries ? "start" : cfg.datasets.length === 1),
    };
  });

  var scales;
  if (timeSeries) {
    var xMin = xs[0], xMax = xs[xs.length - 1];
    if (xMin === xMax) { xMin -= DAY_MS; xMax += DAY_MS; }
    var spansYears = new Date(xMin).getUTCFullYear() !== new Date(xMax).getUTCFullYear();
    scales = {
      x: {
        type: "linear",
        min: xMin,
        max: xMax,
        afterBuildTicks: tcgDayTicks,
        grid: { display: false },
        ticks: {
          maxRotation: 0,
          callback: function (v) { return tcgFormatDay(v, spansYears); },
        },
      },
      y: { ticks: { callback: function (v) { return tcgKr(v); } } },
    };
    if (hasCountAxis) {
      scales.y2 = {
        position: "right",
        display: "auto", // only while the count line is shown
        grid: { drawOnChartArea: false }, // keep the kr axis's gridlines only
        ticks: {
          precision: 0,
          callback: function (v) { return v.toLocaleString("nb-NO"); },
        },
        title: { display: true, text: "Cards" },
      };
    }
  } else {
    scales = {
      y: { beginAtZero: true, ticks: { callback: function (v) { return v.toLocaleString("nb-NO") + " kr"; } } },
    };
  }

  var chart = new Chart(canvas.getContext("2d"), {
    type: cfg.type,
    data: { labels: timeSeries ? undefined : cfg.labels, datasets: datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      interaction: { mode: "index", intersect: false },
      scales: scales,
      plugins: {
        legend: { display: false }, // the card renders its own legend/toggle markup
        tooltip: {
          callbacks: {
            title: function (items) {
              return timeSeries ? tcgFormatDay(items[0].parsed.x, true) : items[0].label;
            },
            label: function (item) {
              if (item.datasetIndex === countIndex) {
                var c = item.parsed.y;
                var line = item.dataset.label + ": " + c.toLocaleString("nb-NO");
                if (item.dataIndex > 0) {
                  var d = c - item.dataset.data[item.dataIndex - 1].y;
                  line += d ? " (" + (d > 0 ? "+" : "") + d + ")" : " (no change)";
                }
                return line;
              }
              return item.dataset.label + ": " + tcgKr(item.parsed.y);
            },
            afterLabel: function (item) {
              // Price-source switch at this point (issue #210): the step is a
              // change of source, not (only) a market move.
              var notes = cfg.sourceNotes && item.datasetIndex === 0 ? cfg.sourceNotes[item.dataIndex] : null;
              if (notes && !Array.isArray(notes)) notes = [notes];
              notes = notes || [];
              if (!timeSeries || item.datasetIndex !== 0 || item.dataIndex === 0) return notes;
              var prev = item.dataset.data[item.dataIndex - 1].y;
              var diff = item.parsed.y - prev;
              var pct = prev > 0 ? " (" + (diff > 0 ? "+" : "") + (diff / prev * 100).toFixed(1) + " %)" : "";
              var lines = ["Change: " + tcgSignedKr(diff) + pct];
              // The count line's own tooltip row covers this while it's shown.
              if (counts && (countIndex < 0 || !item.chart.isDatasetVisible(countIndex))) {
                var dc = counts[item.dataIndex] - counts[item.dataIndex - 1];
                lines.push("Cards: " + counts[item.dataIndex] + (dc ? " (" + (dc > 0 ? "+" : "") + dc + ")" : (notes.length ? " (no change)" : " (no change — price only)")));
              }
              return lines.concat(notes);
            },
          },
        },
      },
    },
  });
  if (timeSeries) {
    tcgFitY(chart);
    chart.update("none");
  }
}
