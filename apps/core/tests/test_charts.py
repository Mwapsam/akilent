"""Geometry tests for apps.core.charts.

A chart bug does not raise — it draws a chart that looks entirely plausible and
is wrong. So these assert coordinates, not "it rendered".

The degenerate inputs get the most attention because they are the ones that
reach production: a brand new account has no data, an account with one day of
history has a single point, and a quiet workflow produces a flat series. All
three break naive scaling.
"""

import pytest

from apps.core import charts


class TestScaling:
    def test_larger_values_sit_higher_on_screen(self):
        """SVG y grows downward, so the scale has to flip. Getting this wrong
        renders every chart upside down and still looks like a chart."""
        points = charts.scale_series([0, 10], width=100, height=100, low=0, high=10)
        assert points[0].y > points[1].y

    def test_the_extremes_land_on_the_edges(self):
        points = charts.scale_series([0, 10], width=100, height=50, low=0, high=10)
        assert (points[0].x, points[0].y) == (0, 50)
        assert (points[1].x, points[1].y) == (100, 0)

    def test_points_are_evenly_spaced_across_the_width(self):
        xs = [p.x for p in charts.scale_series([1, 2, 3, 4, 5], width=100, height=10, low=0, high=5)]
        gaps = {round(b - a, 6) for a, b in zip(xs, xs[1:])}
        assert gaps == {25.0}, "uneven spacing distorts the shape of the trend"

    def test_padding_insets_both_ends(self):
        points = charts.scale_series([0, 10], width=100, height=100, low=0, high=10, pad=10)
        assert points[0].x == 10
        assert points[-1].x == 90

    def test_a_single_reading_is_centred_rather_than_stuck_to_an_edge(self):
        points = charts.scale_series([5], width=100, height=100, low=0, high=10)
        assert len(points) == 1
        assert points[0].x == 50

    def test_no_values_is_no_points_rather_than_an_error(self):
        assert charts.scale_series([], width=100, height=100, low=0, high=1) == []


class TestExtent:
    def test_a_flat_series_does_not_divide_by_zero(self):
        """The case that actually breaks naive scaling. A workflow that ran
        four times every day is flat, and must draw as a straight line."""
        low, high = charts._extent([4, 4, 4], baseline_at_zero=False)
        assert high > low
        ys = {p.y for p in charts.scale_series([4, 4, 4], width=10, height=100, low=low, high=high)}
        assert len(ys) == 1, "a flat series must be a flat line"

    def test_an_all_zero_series_puts_zero_on_the_floor(self):
        """"Nothing happened" belongs at the bottom, not floating mid-chart."""
        low, high = charts._extent([0, 0, 0], baseline_at_zero=True)
        points = charts.scale_series([0, 0, 0], width=10, height=100, low=low, high=high)
        assert {p.y for p in points} == {100}

    def test_a_zero_baseline_is_included_even_when_no_value_reaches_it(self):
        """Truncating the axis to 90-100 exaggerates a 10% wobble into a cliff.
        This is the single most common way a chart lies."""
        low, _ = charts._extent([90, 95, 100], baseline_at_zero=True)
        assert low == 0

    def test_opting_out_of_the_zero_baseline_keeps_the_data_range(self):
        low, _ = charts._extent([90, 95, 100], baseline_at_zero=False)
        assert low == 90


class TestTicks:
    @pytest.mark.parametrize("low,high", [(0, 10), (0, 100), (0, 7), (0, 1), (0, 250), (0, 93)])
    def test_ticks_step_by_a_round_interval(self, low, high):
        """0 / 3.33 / 6.67 / 10 is arithmetically fine and reads as noise.

        The interval is what has to be round, not each value in isolation —
        0/2/4/6 is a perfectly good axis even though 4 is not 1, 2 or 5 times a
        power of ten.
        """
        ticks = charts._nice_ticks(low, high)
        if len(ticks) < 2:
            return
        steps = {round(b - a, 6) for a, b in zip(ticks, ticks[1:])}
        assert len(steps) == 1, f"uneven tick spacing: {ticks}"
        step = steps.pop()
        mantissa = step / (10 ** (len(str(int(step))) - 1)) if step >= 1 else step * 10
        assert mantissa in (1.0, 2.0, 2.5, 5.0, 10.0), f"{step} is not a round interval"

    def test_ticks_cover_the_data(self):
        ticks = charts._nice_ticks(0, 93)
        assert ticks[0] <= 0 and ticks[-1] >= 93

    def test_a_zero_span_still_returns_something_to_draw(self):
        assert charts._nice_ticks(5, 5) == [5]


class TestLineChart:
    def _chart(self):
        return charts.line_chart(
            [("Opens", [10, 20, 15]), ("Clicks", [2, 8, 5])],
            ["Mon", "Tue", "Wed"],
            width=300,
            height=100,
            pad=0,
        )

    def test_both_series_share_one_scale(self):
        """The no-dual-axis rule, asserted. If each series were scaled to its
        own range, Clicks' peak would sit level with Opens' peak and the chart
        would claim they are comparable."""
        chart = self._chart()
        opens, clicks = chart.series
        assert opens.points[1].y < clicks.points[1].y, "20 must plot above 8"

    def test_every_series_gets_a_path(self):
        assert all(s.path.startswith("M") for s in self._chart().series)

    def test_a_single_series_gets_a_fill_and_a_pair_does_not(self):
        """Two overlapping translucent fills are unreadable, so the area is
        reserved for the one-series case."""
        one = charts.line_chart([("Opens", [1, 2, 3])], ["a", "b", "c"])
        assert one.series[0].area
        assert all(not s.area for s in self._chart().series)

    def test_x_labels_are_thinned_rather_than_crowded(self):
        chart = charts.line_chart(
            [("v", list(range(40)))], [f"d{i}" for i in range(40)], width=300
        )
        assert len(chart.x_ticks) < 40
        assert chart.x_ticks[-1]["label"] == "d39", "the most recent day must stay labelled"

    def test_hover_columns_cover_every_reading(self):
        chart = self._chart()
        assert len(chart.columns) == 3
        assert [r["value"] for r in chart.columns[1]["readings"]] == [20, 8]

    def test_hover_columns_are_wider_than_the_line_they_select(self):
        """A 2px line is not a hit target on a phone."""
        assert all(c["width"] > 2 for c in self._chart().columns)

    def test_an_empty_chart_reports_itself_as_empty(self):
        chart = charts.line_chart([("Opens", [])], [])
        assert not chart.has_data


class TestStackedRow:
    def test_segments_total_one_hundred_percent(self):
        rows = charts.stacked_row([("ok", 90), ("error", 10)])
        assert sum(r["percent"] for r in rows) == pytest.approx(100)

    def test_a_tiny_nonzero_segment_stays_visible(self):
        """One failure in ten thousand rounds to nothing and vanishes —
        precisely when the reader most needs to see it is not zero."""
        rows = charts.stacked_row([("ok", 9999), ("error", 1)])
        error = rows[1]
        assert error["percent"] < 0.1
        assert error["width"] >= 1.5

    def test_a_genuinely_zero_segment_draws_nothing(self):
        rows = charts.stacked_row([("ok", 10), ("error", 0)])
        assert rows[1]["width"] == 0

    def test_no_data_is_no_segments(self):
        assert charts.stacked_row([("ok", 0), ("error", 0)]) == []


class TestMeter:
    def test_a_score_becomes_a_percentage_of_its_limit(self):
        assert charts.meter(75, 100)["percent"] == 75

    def test_the_bar_cannot_overflow_its_track(self):
        m = charts.meter(150, 100)
        assert m["percent"] == 100
        assert m["over"] is True, "but the caller still needs to know it exceeded"

    def test_a_zero_limit_does_not_divide_by_zero(self):
        assert charts.meter(5, 0)["percent"] == 100
