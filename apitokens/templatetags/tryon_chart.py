"""Geometry for the inline-SVG daily chart on the try-on statistics page.

The template draws the SVG itself; this tag only does the arithmetic and
hands back plain strings (never floats) so numbers are not localized into
Arabic separators inside SVG attributes.
"""

from django import template

register = template.Library()

WIDTH = 720
HEIGHT = 260
PAD_LEFT = 44
PAD_RIGHT = 12
PAD_TOP = 14
PAD_BOTTOM = 30
MAX_BAR_WIDTH = 28


def _n(value):
    """Short, locale-independent number string for SVG attributes."""
    text = f"{value:.1f}"
    return text[:-2] if text.endswith(".0") else text


@register.simple_tag
def tryon_chart(chart):
    """Build bar/line geometry from `stats.chart` (one dict per day).

    Handles an empty list, a single day and all-zero data (`has_data` False).
    Bars are try-ons, the polyline/dots are attributed orders; both share one
    y scale so the orders line is comparable to the bars.
    """
    chart = list(chart or [])
    plot_w = WIDTH - PAD_LEFT - PAD_RIGHT
    plot_h = HEIGHT - PAD_TOP - PAD_BOTTOM
    base_y = PAD_TOP + plot_h

    peak = 0
    for day in chart:
        peak = max(peak, int(day.get("tryons") or 0), int(day.get("orders") or 0))
    has_data = peak > 0
    scale_max = peak if has_data else 1

    n = len(chart)
    slot = plot_w / n if n else plot_w
    bar_w = max(2.0, min(MAX_BAR_WIDTH, slot * 0.6))

    bars = []
    dots = []
    points = []
    for i, day in enumerate(chart):
        tryons = int(day.get("tryons") or 0)
        orders = int(day.get("orders") or 0)
        cx = PAD_LEFT + slot * (i + 0.5)
        h = plot_h * tryons / scale_max
        bars.append(
            {
                "date": day.get("date", ""),
                "tryons": tryons,
                "orders": orders,
                "x": _n(cx - bar_w / 2),
                "y": _n(base_y - h),
                "w": _n(bar_w),
                "h": _n(h),
            }
        )
        oy = base_y - plot_h * orders / scale_max
        points.append(f"{_n(cx)},{_n(oy)}")
        dots.append({"cx": _n(cx), "cy": _n(oy), "orders": orders})

    return {
        "has_data": has_data,
        "width": WIDTH,
        "height": HEIGHT,
        "left": PAD_LEFT,
        "right": _n(WIDTH - PAD_RIGHT),
        "top": PAD_TOP,
        "base_y": _n(base_y),
        "mid_y": _n(PAD_TOP + plot_h / 2),
        "label_x": PAD_LEFT - 6,
        "label_y": HEIGHT - 8,
        "max": scale_max,
        "mid": scale_max // 2 if scale_max > 1 and scale_max % 2 == 0 else "",
        "bars": bars,
        "dots": dots,
        "points": " ".join(points),
        "first_date": chart[0].get("date", "") if chart else "",
        "last_date": chart[-1].get("date", "") if chart else "",
        "single": n == 1,
        "first_x": _n(PAD_LEFT + slot * 0.5) if n == 1 else PAD_LEFT,
    }
