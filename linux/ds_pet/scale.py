from .render_constants import CANVAS_H, CANVAS_W, COMPACT_H, COMPACT_W

MIN_SCALE = 0.1
MAX_SCALE = 3.0
COMPACT_SCALE_FACTOR = 1.6
MIN_MODE_FACTOR = 0.1
MAX_MODE_FACTOR = 5.0


def normalize_scale(value):
    try:
        scale = float(value)
    except (TypeError, ValueError):
        raise ValueError("缩放比例必须是数字") from None
    if not MIN_SCALE <= scale <= MAX_SCALE:
        raise ValueError(f"缩放比例必须在 {MIN_SCALE:g}–{MAX_SCALE:g} 之间")
    return scale


def window_geometry(scale):
    return mode_geometry("classic", scale)


def normalize_mode_factor(value):
    try:
        factor = float(value)
    except (TypeError, ValueError):
        raise ValueError("模式缩放系数必须是数字") from None
    if not MIN_MODE_FACTOR <= factor <= MAX_MODE_FACTOR:
        raise ValueError(f"模式缩放系数必须在 {MIN_MODE_FACTOR:g}–{MAX_MODE_FACTOR:g} 之间")
    return factor


def mode_canvas(mode):
    if mode == "compact":
        return COMPACT_W, COMPACT_H
    return CANVAS_W, CANVAS_H


def effective_scale(mode, scale, compact_scale_factor=COMPACT_SCALE_FACTOR):
    factor = compact_scale_factor if mode == "compact" else 1.0
    return scale * factor


def mode_geometry(mode, scale, compact_scale_factor=COMPACT_SCALE_FACTOR):
    canvas_w, canvas_h = mode_canvas(mode)
    scale = effective_scale(mode, scale, compact_scale_factor)
    width = max(1, round(canvas_w * scale))
    height = max(1, round(canvas_h * scale))
    return width, height, width / canvas_w, height / canvas_h


def bottom_right_margin(scale):
    return max(4, round(40 * scale))
