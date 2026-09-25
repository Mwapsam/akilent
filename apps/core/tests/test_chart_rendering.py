"""The chart components as they actually render.

apps/core/tests/test_charts.py proves the geometry. This proves the geometry
reaches the page: that the tags render, that the numbers a reader can look up
match the ones the hover layer reads, and that the accessibility affordances
the dataviz rules require are really in the markup rather than only intended.
"""

import json
import re

import pytest
from django.template import Context, Template


def _render(source, **context):
    return Template("{% load charts %}" + source).render(Context(context))


class TestLineChart:
    SERIES = [("Opens", [10, 20, 15]), ("Clicks", [2, 8, 5])]
    DAYS = ["Mon", "Tue", "Wed"]

    def _html(self):
        return _render(
            "{% line_chart series days title='Engagement' unit='Day' %}",
            series=self.SERIES,
            days=self.DAYS,
        )

    def test_it_renders_a_path_per_series(self):
        assert self._html().count("<path") >= 2

    def test_series_colours_are_tokens_not_literals(self):
        """The whole reason these are SVG and not canvas: a var() re-resolves
        when the theme changes, so dark mode costs nothing. A literal hex here
        would be invisible on one of the two surfaces."""
        html = self._html()
        assert "var(--chart-series-1)" in html
        assert "var(--chart-series-2)" in html
        paths = re.findall(r'stroke="(#[0-9a-fA-F]{3,8})"', html)
        assert not paths, "literal stroke colours break dark mode: %s" % paths

    def test_both_series_are_named_in_the_legend(self):
        """Identity is never colour alone."""
        html = self._html()
        assert "chart-legend" in html
        assert "Opens" in html and "Clicks" in html

    def test_a_table_view_carries_the_same_numbers(self):
        """Required, not a nicety — it is how the chart is read by a screen
        reader, in print, and by anyone who needs an exact figure."""
        html = self._html()
        assert "View as table" in html
        table = html[html.index("chart-table"):]
        for value in ("10", "20", "15", "2", "8", "5"):
            assert value in table

    def test_the_hover_data_matches_what_the_table_shows(self):
        """Two renderings of one set of numbers is two chances to disagree.
        They come from the same object, and this is what pins that."""
        html = self._html()
        island = re.search(
            r'<script id="chart-\d+" type="application/json">(.*?)</script>', html, re.S
        )
        assert island, "no json_script data island for the hover layer"
        columns = json.loads(island.group(1))
        assert [c["label"] for c in columns] == self.DAYS
        assert [r["value"] for r in columns[1]["readings"]] == [20, 8]

    def test_each_chart_on_a_page_gets_its_own_data_island(self):
        """Two charts sharing an id would both read the first one's numbers."""
        html = _render(
            "{% line_chart series days %}{% line_chart series days %}",
            series=self.SERIES,
            days=self.DAYS,
        )
        ids = re.findall(r'<script id="(chart-\d+)"', html)
        assert len(ids) == 2 and len(set(ids)) == 2

    def test_an_empty_series_renders_nothing_rather_than_empty_axes(self):
        assert _render("{% line_chart series days %}", series=[("Opens", [])], days=[]).strip() == ""

    def test_mismatched_series_and_labels_are_refused(self):
        """A chart drawn from mismatched columns plots real numbers against the
        wrong dates — wrong without looking wrong, so it fails loudly."""
        with pytest.raises(ValueError, match="without looking wrong"):
            _render("{% line_chart series days %}", series=[("Opens", [1, 2])], days=["Mon"])


class TestSparkline:
    def test_it_draws_a_line_for_a_real_series(self):
        assert "<path" in _render("{% sparkline values %}", values=[1, 5, 3, 8])

    def test_one_reading_draws_nothing(self):
        """A single point has no direction, so a sparkline would be claiming
        something it cannot know."""
        assert _render("{% sparkline values %}", values=[5]).strip() == ""

    def test_it_carries_a_text_summary_for_screen_readers(self):
        html = _render("{% sparkline values label='Opens, last 7 days' %}", values=[1, 2, 3])
        assert 'aria-hidden="true"' in html
        assert "Opens, last 7 days" in html


class TestMeter:
    def test_it_exposes_its_value_to_assistive_tech(self):
        html = _render("{% meter 75 100 label='Score' %}")
        assert 'role="meter"' in html
        assert 'aria-valuenow="75"' in html and 'aria-valuemax="100"' in html

    def test_the_fill_cannot_exceed_the_track(self):
        assert "width: 100" in _render("{% meter 150 100 %}")


class TestStackedBar:
    def test_every_segment_is_labelled_in_text(self):
        """Status is never colour alone."""
        html = _render(
            "{% stacked_bar segments %}",
            segments=[("Succeeded", 90, "good"), ("Failed", 10, "critical")],
        )
        assert "Succeeded" in html and "Failed" in html

    def test_a_tiny_failure_count_is_still_visible(self):
        html = _render(
            "{% stacked_bar segments %}",
            segments=[("Succeeded", 9999, "good"), ("Failed", 1, "critical")],
        )
        assert "stacked-bar-segment--critical" in html
