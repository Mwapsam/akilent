"""Chart geometry, as pure functions over plain numbers.

No Django, no rendering, no colour. These return coordinates; templates turn
them into SVG and the token layer decides what colour that SVG is. Keeping the
split there is what makes the charts free in dark mode: an SVG that says
``stroke="var(--chart-series-1)"`` re-resolves when the theme changes, with no
JavaScript and no redraw.

It is also why this module is worth testing. Chart bugs are quiet — an
off-by-one in a scale draws a chart that looks entirely plausible and is wrong.
Every function here is total: empty input, one point, and a flat series all have
a defined answer rather than a ZeroDivisionError.

``None`` in a line chart's series means "no reading", which is not zero: a week
with no replies has no median reply time, and plotting it as 0 would draw the
best week on record. A missing reading is a break in the line instead.

Geometry only. Which *form* to use for which data is a design decision that
lives with the templates, not here.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field


def _extent(values: list[float], baseline_at_zero: bool) -> tuple[float, float]:
    """The (low, high) the y-axis spans, never zero-height.

    A flat series is the case that breaks naive scaling: low == high makes every
    scale divide by zero. It is also not rare — a workflow that ran twice a day
    all week is flat, and it should draw as a straight line through the middle,
    not crash and not explode to full height.
    """
    if not values:
        return 0.0, 1.0
    low, high = min(values), max(values)
    if baseline_at_zero:
        low = min(0.0, low)
    if high == low:
        # Pad symmetrically so the line lands mid-height. For an all-zero
        # series that means 0 sits on the floor, which is where a reader
        # expects "nothing happened" to be.
        return (
            (low, high + 1.0)
            if low == 0
            else (low - abs(low) * 0.5, high + abs(high) * 0.5)
        )
    return low, high


@dataclass(frozen=True)
class Point:
    x: float
    y: float | None  # None: no reading here (a gap in the line)
    value: float | None
    label: str = ""


@dataclass
class Series:
    """One plotted line, already scaled to the viewport."""

    label: str
    values: Sequence[float | None]
    points: list[Point] = field(default_factory=list)
    path: str = ""
    area: str = ""
    # Readings with a gap (or the edge) on both sides: a line can't show them, so they're dots.
    dots: list[Point] = field(default_factory=list)

    @property
    def last(self) -> Point | None:
        """The end of the line — where a direct label belongs."""
        return self.points[-1] if self.points else None

    @property
    def total(self) -> float:
        return sum(v for v in self.values if v is not None)


def _round(n: float) -> float:
    """Two decimals is finer than a pixel at these sizes and keeps the rendered
    SVG small — path data is the bulk of an inline chart's bytes."""
    return round(n, 2)


def scale_series(
    values: Sequence[float | None],
    *,
    width: float,
    height: float,
    low: float,
    high: float,
    pad: float = 0.0,
) -> list[Point]:
    """Map values onto a viewport, y flipped so larger is higher on screen."""
    inner_w = max(width - pad * 2, 1.0)
    inner_h = max(height - pad * 2, 1.0)
    span = high - low or 1.0
    if not values:
        return []

    def y(v: float | None) -> float | None:
        return (
            None if v is None else _round(pad + inner_h - ((v - low) / span) * inner_h)
        )

    if len(values) == 1:
        # One reading is a dot in the middle, not a line from nowhere.
        return [Point(_round(pad + inner_w / 2), y(values[0]), values[0])]
    step = inner_w / (len(values) - 1)
    return [Point(_round(pad + i * step), y(v), v) for i, v in enumerate(values)]


def _path(points: list[Point]) -> str:
    """The line, broken wherever a reading is missing. A reading with no neighbour on either
    side is a zero-length segment, which the round line cap draws as a dot."""
    runs: list[list[Point]] = [[]]
    for p in points:
        if p.y is None:
            if runs[-1]:
                runs.append([])
        else:
            runs[-1].append(p)
    parts = []
    for run in runs:
        if not run:
            continue
        if len(run) == 1:
            run = run * 2
        parts.append("M" + " L".join(f"{p.x},{p.y}" for p in run))
    return " ".join(parts)


def _isolated(points: list[Point]) -> list[Point]:
    """Readings with no reading either side of them, in a line that has gaps."""
    if not any(p.y is None for p in points):
        return []
    return [
        p
        for i, p in enumerate(points)
        if p.y is not None
        and (i == 0 or points[i - 1].y is None)
        and (i == len(points) - 1 or points[i + 1].y is None)
    ]


def _area(points: list[Point], height: float) -> str:
    """The line closed down to the floor, for a single-series fill. None when the line has a
    gap: a fill bridging the gap would claim readings that aren't there."""
    if len(points) < 2 or any(p.y is None for p in points):
        return ""
    floor = _round(height)
    return (
        f"M{points[0].x},{floor} L"
        + " L".join(f"{p.x},{p.y}" for p in points)
        + f" L{points[-1].x},{floor} Z"
    )


def sparkline(
    values: list[float], *, width: float = 96.0, height: float = 24.0
) -> Series:
    """A trend with no axes, no grid and no hover — context beside a number.

    Deliberately not a chart: it carries shape only, which is why it keeps the
    stroke inset by a pixel (so a peak is not clipped by the viewBox) and has no
    labels. The number it sits beside is the value; this says which way it went.
    """
    low, high = _extent(values, baseline_at_zero=False)
    points = scale_series(
        values, width=width, height=height, low=low, high=high, pad=1.0
    )
    return Series(
        label="",
        values=values,
        points=points,
        path=_path(points),
        area=_area(points, height - 1.0),
    )


def kpi_spark(
    values: Sequence[float | None],
    labels: Sequence[str],
    shown: Sequence[str] | None = None,
) -> dict:
    """A tile's trend for templates/publishing/_kpi_spark.html (viewBox 0 0 100 28): a line
    through the slots, the latest reading marked, and one hover column per slot saying
    ``labels[i]: shown[i]`` (the value itself when ``shown`` is None).

    A slot with no reading (None, such as a delivery rate in a week nothing was sent) is left
    out of the line rather than drawn as 0. One reading has no direction, so with fewer than
    two there is no line and ``end`` is None (the hover columns stay)."""
    n = len(values)
    step = 100 / max(1, n - 1)
    top = max((v for v in values if v is not None), default=0) or 1
    pts = [
        (round(i * step, 2), round(26 - v / top * 24, 2))
        for i, v in enumerate(values)
        if v is not None
    ]
    if len(pts) < 2:
        pts = []
    return {
        "points": " ".join(f"{x},{y}" for x, y in pts),
        "end": pts[-1] if pts else None,
        "slots": [
            {
                "x": round(i * 100 / max(1, n), 2),
                "w": round(100 / max(1, n), 2),
                "label": labels[i],
                "value": shown[i] if shown is not None else v,
            }
            for i, v in enumerate(values)
        ],
    }


@dataclass
class LineChart:
    """A plotted multi-series chart plus everything its axes need."""

    series: list[Series]
    labels: list[str]
    width: float
    height: float
    pad: float
    low: float
    high: float
    y_ticks: list[dict] = field(default_factory=list)
    x_ticks: list[dict] = field(default_factory=list)
    columns: list[dict] = field(default_factory=list)

    @property
    def has_data(self) -> bool:
        return any(v is not None for s in self.series for v in s.values)


def _nice_ticks(low: float, high: float, count: int = 4) -> list[float]:
    """Tick values a person would have chosen: 1/2/5 x a power of ten.

    Ticks at 0, 3.33, 6.67, 10 are arithmetically fine and read as noise. This
    is the difference between a chart that looks computed and one that looks
    considered.
    """
    span = high - low
    if span <= 0:
        return [low]
    raw = span / max(count, 1)
    magnitude = 10 ** (len(str(int(abs(raw)))) - 1) if abs(raw) >= 1 else 1
    for multiple in (1, 2, 5, 10):
        step = multiple * magnitude
        if span / step <= count:
            break
    # Round the ends *outward* to the step. Truncating instead leaves the top
    # tick below the tallest value, so the highest point on the chart floats
    # above every gridline with nothing to read it against.
    start = math.floor(low / step) * step
    end = math.ceil(high / step) * step
    ticks: list[float] = []
    value: float = float(start)
    while value <= end + step * 0.001:
        ticks.append(round(value, 6))
        value += step
    return ticks or [float(low), float(high)]


def line_chart(
    series: list[tuple[str, list[float | None]]],
    labels: list[str],
    *,
    width: float = 640.0,
    height: float = 200.0,
    pad: float = 8.0,
    baseline_at_zero: bool = True,
) -> LineChart:
    """Scale several equal-length series onto one set of axes.

    One y-axis, always — two measures of different magnitude belong in two
    charts, not on a second scale. ``columns`` are the full-height hover targets
    the crosshair uses, so the hit area is the whole column rather than the
    2px line.
    """
    flat = [v for _, vs in series for v in vs if v is not None]
    low, high = _extent(flat, baseline_at_zero=baseline_at_zero)
    ticks = _nice_ticks(low, high)
    if ticks:
        high = max(high, ticks[-1])
        low = min(low, ticks[0])

    built = []
    for label, values in series:
        points = scale_series(
            values, width=width, height=height, low=low, high=high, pad=pad
        )
        built.append(
            Series(
                label=label,
                values=values,
                points=points,
                path=_path(points),
                area=_area(points, height - pad) if len(series) == 1 else "",
                dots=_isolated(points),
            )
        )

    span = high - low or 1.0
    inner_h = max(height - pad * 2, 1.0)
    y_ticks = [
        {"value": t, "y": _round(pad + inner_h - ((t - low) / span) * inner_h)}
        for t in ticks
    ]

    # Label every point only when they all fit; otherwise first, last and a
    # couple between. A crowded axis is unreadable, and rotating the labels to
    # fit them all in is the usual wrong answer.
    count = len(labels)
    keep = set()
    if count:
        stride = max(1, count // 5)
        keep = set(range(0, count, stride)) | {count - 1}
    reference = built[0].points if built else []
    x_ticks = [
        {"label": labels[i], "x": reference[i].x}
        for i in sorted(keep)
        if i < len(reference) and i < count
    ]

    columns = []
    if reference:
        col_w = _round((width - pad * 2) / max(len(reference), 1))
        for i, p in enumerate(reference):
            columns.append(
                {
                    "index": i,
                    "x": _round(max(p.x - col_w / 2, 0)),
                    "centre": p.x,
                    "width": col_w,
                    "label": labels[i] if i < count else "",
                    "readings": [
                        {"label": s.label, "value": s.values[i], "y": s.points[i].y}
                        for s in built
                        if i < len(s.points)
                    ],
                }
            )

    return LineChart(
        series=built,
        labels=labels,
        width=width,
        height=height,
        pad=pad,
        low=low,
        high=high,
        y_ticks=y_ticks,
        x_ticks=x_ticks,
        columns=columns,
    )


def stacked_row(
    segments: list[tuple[str, float]], *, minimum_visible: float = 1.5
) -> list[dict]:
    """Part-to-whole as percentages of one bar.

    ``minimum_visible`` keeps a nonzero-but-tiny segment on screen. One failure
    in ten thousand runs is 0.01% — which rounds to nothing and disappears,
    exactly when a reader most needs to see that it is not zero. A segment that
    is genuinely zero still gets nothing.
    """
    total = sum(max(v, 0) for _, v in segments)
    if total <= 0:
        return []
    out = []
    for label, value in segments:
        value = max(value, 0)
        pct = (value / total) * 100
        out.append(
            {
                "label": label,
                "value": value,
                "percent": round(pct, 2),
                "width": round(max(pct, minimum_visible) if value else 0, 2),
            }
        )
    return out


def meter(value: float, maximum: float = 100.0) -> dict:
    """A single ratio against a limit — a meter, not a two-slice pie."""
    maximum = maximum or 1.0
    ratio = max(0.0, min(value / maximum, 1.0))
    return {
        "value": value,
        "maximum": maximum,
        "percent": round(ratio * 100, 2),
        "over": value > maximum,
    }
