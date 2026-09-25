"""Template tags that turn data into chart geometry.

Inclusion tags rather than context variables, so a screen adds a chart without
its view learning anything about pixels:

    {% load charts %}
    {% line_chart series labels title="Engagement" %}

The geometry lives in apps.core.charts and is tested there. This module is the
thin Django-facing layer: it validates what a template handed it and picks the
partial.
"""

from itertools import count

from django import template

from apps.core import charts as geometry

# Several charts can share a page, and each needs its own data island and its
# own Alpine scope, so the ids have to differ.
_uid = count(1)

register = template.Library()


@register.inclusion_tag("components/charts/_line_chart.html")
def line_chart(series, labels, title="", unit="", height=200, baseline_at_zero=True):
    """A multi-series trend over time.

    ``series`` is an iterable of ``(label, values)``. Every series must be the
    same length as ``labels`` — a short series would otherwise plot against the
    wrong dates, silently, which is the kind of chart bug nobody catches by
    looking.
    """
    pairs = [(str(label), [float(v or 0) for v in values]) for label, values in series]
    labels = [str(x) for x in labels]
    for label, values in pairs:
        if len(values) != len(labels):
            raise ValueError(
                "series %r has %d values but there are %d labels; a chart drawn "
                "from mismatched columns is wrong without looking wrong"
                % (label, len(values), len(labels))
            )
    chart = geometry.line_chart(
        pairs, labels, height=float(height), baseline_at_zero=baseline_at_zero
    )
    return {
        "chart": chart,
        "title": title,
        "unit": unit,
        "uid": "chart-%d" % next(_uid),
        "columns_data": chart.columns,
    }


@register.inclusion_tag("components/charts/_sparkline.html")
def sparkline(values, label=""):
    """Trend shape beside a number. No axes, no hover, no legend.

    ``label`` is for assistive tech — a sparkline with no text is decoration a
    screen reader cannot read, so it gets a one-line summary instead.
    """
    numbers = [float(v or 0) for v in values]
    return {
        "line": geometry.sparkline(numbers),
        "label": label,
        "has_data": len(numbers) > 1,
    }


@register.inclusion_tag("components/charts/_meter.html")
def meter(value, maximum=100, label="", tone="brand"):
    """A single value against its limit. Not a two-slice pie."""
    return {"meter": geometry.meter(float(value or 0), float(maximum or 100)),
            "label": label, "tone": tone}


@register.inclusion_tag("components/charts/_stacked_bar.html")
def stacked_bar(segments, label=""):
    """Part-to-whole in one bar. ``segments`` is ``(label, value, tone)``."""
    tones = {}
    pairs = []
    for row in segments:
        name, value = row[0], row[1]
        tones[str(name)] = row[2] if len(row) > 2 else "series-1"
        pairs.append((str(name), float(value or 0)))
    built = geometry.stacked_row(pairs)
    for seg in built:
        seg["tone"] = tones.get(seg["label"], "series-1")
    return {"segments": built, "label": label}
