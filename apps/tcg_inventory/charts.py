"""SVG chart geometry for Transactions' "Vis grafer" section (and the
dashboard's value-growth preview).

No JS charting library -- these turn a plain data series into pixel
coordinates (points, bar rects, axis ticks) so the templates only ever loop
over ready-made numbers, the same way queries.py's Bucket objects let the
dashboard templates stay dumb renderers.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

_PAD_LEFT, _PAD_RIGHT, _PAD_TOP, _PAD_BOTTOM = 56, 16, 16, 28


def _nice_max(value: float) -> float:
    """Round a max value up to a clean axis ceiling (1/2/2.5/5 x 10^n) --
    y-axis ticks should land on clean numbers, never a raw data max.
    """
    if value <= 0:
        return 1.0
    magnitude = 10 ** math.floor(math.log10(value))
    for step in (1, 2, 2.5, 5, 10):
        candidate = step * magnitude
        if candidate >= value - 1e-9:
            return candidate
    return 10 * magnitude


def _format_kr(v: float) -> str:
    return f"{v:,.0f}".replace(",", " ")


@dataclass
class LineChart:
    width: int
    height: int
    baseline_y: float
    points: list[tuple[float, float]] = field(default_factory=list)
    line_path: str = ""
    area_path: str = ""
    y_ticks: list[tuple[float, str]] = field(default_factory=list)
    x_labels: list[tuple[float, str]] = field(default_factory=list)
    end_point: tuple[float, float] | None = None
    end_label: str | None = None


def build_line_chart(labels: list[str], values: list[float], width: int = 640, height: int = 220) -> LineChart:
    plot_w = width - _PAD_LEFT - _PAD_RIGHT
    plot_h = height - _PAD_TOP - _PAD_BOTTOM
    baseline_y = _PAD_TOP + plot_h

    if not values:
        return LineChart(width, height, baseline_y)

    max_v = _nice_max(max(values))
    n = len(values)

    def x_at(i: int) -> float:
        return _PAD_LEFT if n == 1 else _PAD_LEFT + (i / (n - 1)) * plot_w

    def y_at(v: float) -> float:
        return _PAD_TOP + plot_h - (v / max_v) * plot_h if max_v else baseline_y

    points = [(round(x_at(i), 1), round(y_at(v), 1)) for i, v in enumerate(values)]
    line_path = "M " + " L ".join(f"{x},{y}" for x, y in points)
    area_path = (
        f"M {points[0][0]},{round(baseline_y, 1)} L "
        + " L ".join(f"{x},{y}" for x, y in points)
        + f" L {points[-1][0]},{round(baseline_y, 1)} Z"
    )

    y_ticks = [(round(y_at(max_v * frac), 1), _format_kr(max_v * frac)) for frac in (0, 1 / 3, 2 / 3, 1)]

    if n <= 6:
        idxs = list(range(n))
    else:
        step = (n - 1) / 5
        idxs = sorted({round(i * step) for i in range(6)})
    x_labels = [(points[i][0], labels[i]) for i in idxs]

    return LineChart(
        width,
        height,
        baseline_y,
        points=points,
        line_path=line_path,
        area_path=area_path,
        y_ticks=y_ticks,
        x_labels=x_labels,
        end_point=points[-1],
        end_label=_format_kr(values[-1]),
    )


@dataclass
class BarGroup:
    label: str
    bars: list[tuple[float, float, float, float]]  # (x, y, w, h) per series


@dataclass
class GroupedBarChart:
    width: int
    height: int
    baseline_y: float
    groups: list[BarGroup] = field(default_factory=list)
    y_ticks: list[tuple[float, str]] = field(default_factory=list)


def build_grouped_bar_chart(
    labels: list[str], series: list[list[float]], width: int = 640, height: int = 220
) -> GroupedBarChart:
    """`series` is one value-list per series (e.g. [bought_by_month,
    sold_by_month]), each the same length as `labels`.
    """
    plot_w = width - _PAD_LEFT - _PAD_RIGHT
    plot_h = height - _PAD_TOP - _PAD_BOTTOM
    baseline_y = _PAD_TOP + plot_h
    n = len(labels)
    n_series = len(series)

    if n == 0 or n_series == 0:
        return GroupedBarChart(width, height, baseline_y)

    all_values = [v for s in series for v in s]
    max_v = _nice_max(max(all_values)) if any(all_values) else 1.0

    group_w = plot_w / n
    bar_w = min(24.0, (group_w * 0.7) / n_series)
    gap = 2.0
    total_bars_w = bar_w * n_series + gap * (n_series - 1)

    groups = []
    for i, label in enumerate(labels):
        group_x0 = _PAD_LEFT + i * group_w + (group_w - total_bars_w) / 2
        bars = []
        for s_idx, s in enumerate(series):
            v = s[i]
            bar_h = (v / max_v) * plot_h if max_v else 0.0
            x = group_x0 + s_idx * (bar_w + gap)
            y = baseline_y - bar_h
            bars.append((round(x, 1), round(y, 1), round(bar_w, 1), round(bar_h, 1)))
        groups.append(BarGroup(label=label, bars=bars))

    y_ticks = [(round(baseline_y - frac * plot_h, 1), _format_kr(max_v * frac)) for frac in (0, 1 / 3, 2 / 3, 1)]

    return GroupedBarChart(width, height, round(baseline_y, 1), groups=groups, y_ticks=y_ticks)
