import charts


def test_build_line_chart_produces_one_point_per_value_and_a_monotonic_x_axis():
    chart = charts.build_line_chart(["2026-01", "2026-02", "2026-03"], [10, 30, 20])
    assert len(chart.points) == 3
    xs = [x for x, _y in chart.points]
    assert xs == sorted(xs)  # x always increases left to right
    assert chart.end_point == chart.points[-1]
    assert chart.end_label == "20"


def test_build_line_chart_handles_empty_series():
    chart = charts.build_line_chart([], [])
    assert chart.points == []
    assert chart.end_point is None


def test_build_line_chart_y_axis_uses_a_clean_ceiling_not_the_raw_max():
    chart = charts.build_line_chart(["a", "b"], [10, 37])
    # 37's "clean" ceiling should be 40 or 50, never the raw 37.
    top_tick_label = chart.y_ticks[-1][1]
    assert top_tick_label != "37"


def test_build_grouped_bar_chart_places_two_series_side_by_side():
    chart = charts.build_grouped_bar_chart(["2026-01", "2026-02"], [[100, 50], [0, 20]])
    assert len(chart.groups) == 2
    bought_bar, sold_bar = chart.groups[0].bars
    # Same group -> bars sit side by side (sold starts where bought ends, plus the gap).
    assert sold_bar[0] > bought_bar[0]
    # A larger value renders as a taller bar (smaller y, since SVG y grows downward).
    assert bought_bar[1] < sold_bar[1]


def test_build_grouped_bar_chart_handles_all_zero_values_without_crashing():
    chart = charts.build_grouped_bar_chart(["2026-01"], [[0], [0]])
    assert len(chart.groups) == 1
    for bar in chart.groups[0].bars:
        assert bar[3] == 0  # zero height, no division-by-zero error
