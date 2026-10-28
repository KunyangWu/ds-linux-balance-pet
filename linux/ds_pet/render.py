import math
from dataclasses import dataclass

import cairo
import gi

gi.require_version("Gdk", "3.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Gdk, Pango, PangoCairo

from .assets import alpha_shape
from .render_constants import (
    CANVAS_H,
    CANVAS_W,
    COLORS,
    COMPACT_ANIM_H,
    COMPACT_ANIM_W,
    COMPACT_ANIM_X,
    COMPACT_ANIM_Y,
    COMPACT_BADGE_MAX_Y,
    COMPACT_H,
    COMPACT_W,
    FONT_BADGE,
    FONT_BUBBLE,
    FONT_STATS,
    SPRITE_H,
    SPRITE_W,
    SPRITE_X,
    SPRITE_Y,
)


@dataclass
class RenderedFrame:
    surface: cairo.ImageSurface
    input_rects: list


def rounded_path(cr, x, y, width, height, radius):
    radius = min(radius, width / 2, height / 2)
    cr.new_sub_path()
    cr.arc(x + width - radius, y + radius, radius, -math.pi / 2, 0)
    cr.arc(x + width - radius, y + height - radius, radius, 0, math.pi / 2)
    cr.arc(x + radius, y + height - radius, radius, math.pi / 2, math.pi)
    cr.arc(x + radius, y + radius, radius, math.pi, 3 * math.pi / 2)
    cr.close_path()


class Renderer:
    def __init__(self, art, font_family):
        self.art = art
        self.font_family = font_family
        self._measure_surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1)
        self._measure_cr = cairo.Context(self._measure_surface)

    def _font(self, size, bold=False):
        desc = Pango.FontDescription()
        desc.set_family(self.font_family)
        desc.set_absolute_size(float(size) * Pango.SCALE)
        desc.set_weight(Pango.Weight.BOLD if bold else Pango.Weight.NORMAL)
        return desc

    def _layout(self, text, size, bold=False, max_width=None, align_center=False):
        layout = PangoCairo.create_layout(self._measure_cr)
        layout.set_font_description(self._font(size, bold))
        layout.set_text(str(text), -1)
        if max_width is not None:
            layout.set_width(int(max_width * Pango.SCALE))
            layout.set_wrap(Pango.WrapMode.WORD_CHAR)
        if align_center:
            layout.set_alignment(Pango.Alignment.CENTER)
        width, height = layout.get_pixel_size()
        return layout, width, height

    def _draw_text(self, cr, text, size, bold, box, color, align_center=True):
        x, y, width, height = box
        layout, text_width, text_height = self._layout(text, size, bold, width, align_center)
        PangoCairo.update_layout(cr, layout)
        draw_x = x + (width - text_width) / 2 if align_center else x
        draw_y = y + (height - text_height) / 2
        cr.set_source_rgba(*color)
        cr.move_to(draw_x, draw_y)
        PangoCairo.show_layout(cr, layout)

    def _draw_pixbuf(self, cr, pixbuf, x, y, width, height):
        if pixbuf is None:
            return
        cr.save()
        cr.translate(x, y)
        cr.scale(width / pixbuf.get_width(), height / pixbuf.get_height())
        Gdk.cairo_set_source_pixbuf(cr, pixbuf, 0, 0)
        cr.paint()
        cr.restore()

    def _draw_working(self, cr, animation_frame, bob):
        index = animation_frame % 4
        shape = self.art.working_shapes[index]
        scale = SPRITE_W / float(self.art.working_max_width or 1)
        dest_x = CANVAS_W / 2 - (shape.bounds[0] + shape.bounds[2] / 2) * scale
        dest_y = SPRITE_Y + SPRITE_H + bob - (shape.bounds[1] + shape.bounds[3]) * scale
        src_x = (index % 2) * self.art.working_cell_w
        src_y = (index // 2) * self.art.working_cell_h
        cr.save()
        cr.translate(dest_x, dest_y)
        cr.scale(scale, scale)
        cr.rectangle(0, 0, self.art.working_cell_w, self.art.working_cell_h)
        cr.clip()
        Gdk.cairo_set_source_pixbuf(cr, self.art.working_atlas, -src_x, -src_y)
        cr.paint()
        cr.restore()
        return dest_x, dest_y, scale

    def _sprite_rects(self, cr, render_expression, animation_frame, sleep_frame, bob):
        if render_expression == "working" and self.art.working_atlas is not None:
            index = animation_frame % 4
            shape = self.art.working_shapes[index]
            scale = SPRITE_W / float(self.art.working_max_width or 1)
            dest_x = CANVAS_W / 2 - (shape.bounds[0] + shape.bounds[2] / 2) * scale
            dest_y = SPRITE_Y + SPRITE_H + bob - (shape.bounds[1] + shape.bounds[3]) * scale
            return [
                (dest_x + x0 * scale, dest_y + sy * scale, (x1 - x0) * scale, max(1.0, scale))
                for sy, x0, x1 in shape.runs
            ]
        if render_expression == "sleepy":
            breath = 1.5 * (1 + math.sin(2 * math.pi * sleep_frame / 25))
            sprite = self.art.sprites.get("sleepy") or self.art.sprites.get("base")
            shape = self.art.shapes.get("sleepy") or self.art.shapes.get("base")
            width = SPRITE_W
            height = SPRITE_H + breath
            x = SPRITE_X
            y = SPRITE_Y + bob - breath
            return [
                (x + x0 * width / sprite.get_width(), y + sy * height / sprite.get_height(),
                 (x1 - x0) * width / sprite.get_width(), max(1.0, height / sprite.get_height()))
                for sy, x0, x1 in shape.runs
            ]
        sprite = self.art.sprite_for(render_expression)
        shape = self.art.shape_for(render_expression)
        if sprite is None or shape is None:
            return []
        return [
            (SPRITE_X + x0 * SPRITE_W / sprite.get_width(), SPRITE_Y + bob + sy * SPRITE_H / sprite.get_height(),
             (x1 - x0) * SPRITE_W / sprite.get_width(), max(1.0, SPRITE_H / sprite.get_height()))
            for sy, x0, x1 in shape.runs
        ]

    def _draw_sparkle(self, cr, x, y, size, color):
        cr.set_source_rgba(*color)
        cr.set_line_width(max(1.5, size * 0.14))
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.move_to(x - size / 2, y)
        cr.line_to(x + size / 2, y)
        cr.move_to(x, y - size / 2)
        cr.line_to(x, y + size / 2)
        cr.stroke()
        cr.set_source_rgba(1, 1, 1, 1)
        cr.arc(x, y, size * 0.13, 0, 2 * math.pi)
        cr.fill()

    def _draw_teardrop(self, cr, x, y, size, color):
        cr.set_source_rgba(*color)
        cr.arc(x, y + size * 0.6, size / 2, 0, 2 * math.pi)
        cr.fill()
        cr.new_path()
        cr.move_to(x - size * 0.34, y + size * 0.3)
        cr.line_to(x + size * 0.34, y + size * 0.3)
        cr.line_to(x, y - size * 0.5)
        cr.close_path()
        cr.fill()
        cr.set_source_rgba(1, 1, 1, 190 / 255)
        cr.arc(x - size * 0.15, y + size * 0.5, size * 0.13, 0, 2 * math.pi)
        cr.fill()

    def _draw_zzz(self, cr, x, y, width, sleep_frame):
        for index in range(3):
            phase = ((sleep_frame % 25) / 25.0 + index / 3.0) % 1.0
            alpha = int(round(235 * math.sin(math.pi * phase)))
            if alpha <= 0:
                continue
            size = 18 + 12 * phase
            zx = min(CANVAS_W - size - 10, x + width * 0.56 + 48 * phase + 3 * math.sin(phase * 2 * math.pi))
            zy = max(8, y - 22 - 62 * phase)
            letter = "Z" if phase > 0.55 else "z"
            layout = PangoCairo.create_layout(self._measure_cr)
            desc = Pango.FontDescription()
            desc.set_family("Sans")
            desc.set_absolute_size(float(size) * Pango.SCALE)
            desc.set_weight(Pango.Weight.BOLD)
            layout.set_font_description(desc)
            layout.set_text(letter, -1)
            PangoCairo.update_layout(cr, layout)
            cr.move_to(zx, zy)
            PangoCairo.layout_path(cr, layout)
            cr.set_source_rgba(186 / 255, 214 / 255, 255 / 255, alpha / 255)
            cr.fill_preserve()
            cr.set_source_rgba(35 / 255, 53 / 255, 92 / 255, alpha / 255)
            cr.set_line_width(2.6)
            cr.set_line_join(cairo.LINE_JOIN_ROUND)
            cr.stroke()

    def _draw_effects(self, cr, expression, bob, sleep_frame):
        x = self.art.subject_bounds[0] + SPRITE_X
        y = self.art.subject_bounds[1] + SPRITE_Y + bob
        width = self.art.subject_bounds[2]
        height = self.art.subject_bounds[3]
        if expression == "happy":
            self._draw_sparkle(cr, x - 4, y + height * 0.08, 20, (1, 214 / 255, 92 / 255, 1))
            self._draw_sparkle(cr, x + width + 4, y + height * 0.16, 15, (1, 200 / 255, 120 / 255, 1))
            self._draw_sparkle(cr, x + width * 0.08, y - 4, 12, (1, 226 / 255, 150 / 255, 1))
            self._draw_sparkle(cr, x + width * 0.92, y + height * 0.02, 10, (1, 236 / 255, 180 / 255, 1))
        elif expression == "sad":
            self._draw_teardrop(cr, x + width * 0.16, y + height * 0.30, 13, (130 / 255, 200 / 255, 255 / 255, 220 / 255))
            self._draw_teardrop(cr, x + width * 0.10, y + height * 0.40, 8, (150 / 255, 214 / 255, 255 / 255, 200 / 255))
        elif expression == "sleepy":
            if self.art.sleepy_bounds:
                x = self.art.sleepy_bounds[0] + SPRITE_X
                y = self.art.sleepy_bounds[1] + SPRITE_Y + bob
                width = self.art.sleepy_bounds[2]
            self._draw_zzz(cr, x, y, width, sleep_frame)

    def _draw_bubble(self, cr, text, canvas_w=CANVAS_W, bubble_top=158):
        if not text:
            return None
        layout, natural_width, _ = self._layout(text, FONT_BUBBLE, False, None, True)
        bubble_w = min(canvas_w - 24, max(120, natural_width + 30))
        text_width = bubble_w - 26
        layout, _, text_height = self._layout(text, FONT_BUBBLE, False, text_width, True)
        bubble_h = text_height + 22
        bubble_x = round((canvas_w - bubble_w) / 2)
        bubble_y = max(6, bubble_top - bubble_h)
        cr.set_source_rgba(*COLORS["bubble"])
        rounded_path(cr, bubble_x, bubble_y, bubble_w, bubble_h, 14)
        cr.fill_preserve()
        cr.set_source_rgba(*COLORS["bubble_edge"])
        cr.set_line_width(2)
        cr.stroke()
        tail_center = bubble_x + bubble_w / 2
        cr.new_path()
        cr.move_to(tail_center - 14, bubble_y + bubble_h - 1)
        cr.line_to(tail_center + 14, bubble_y + bubble_h - 1)
        cr.line_to(tail_center + 3, bubble_y + bubble_h + 13)
        cr.close_path()
        cr.set_source_rgba(*COLORS["bubble"])
        cr.fill()
        self._draw_text(
            cr,
            text,
            FONT_BUBBLE,
            False,
            (bubble_x + 13, bubble_y + 11, text_width, bubble_h - 20),
            COLORS["bubble_text"],
            True,
        )
        return (bubble_x, bubble_y, bubble_w, bubble_h + 13)

    def _draw_badge(self, cr, balance_text, status_color, show_stats, stats_text, reset_text, canvas_w=CANVAS_W, canvas_h=CANVAS_H, badge_max_y=498, font_badge=FONT_BADGE, font_stats=FONT_STATS):
        badge_size = self._layout(balance_text, font_badge, True)[1:]
        badge_font = font_badge
        max_width = canvas_w - 50
        while badge_size[0] > max_width and badge_font > 12:
            badge_font -= 1
            badge_size = self._layout(balance_text, badge_font, True)[1:]
        stats_size = self._layout(stats_text, font_stats, False)[1:]
        reset_size = self._layout(reset_text, font_stats, False)[1:] if reset_text else (0, 0)
        badge_w = badge_size[0] + 44
        if show_stats and stats_text:
            badge_w = max(badge_w, stats_size[0] + 28)
        if reset_text:
            badge_w = max(badge_w, reset_size[0] + 28)
        badge_w = round(min(badge_w, canvas_w - 10))
        badge_line_h = round(badge_font * 1.25) + 6
        stats_line_h = round(font_stats * 1.25) + 4
        badge_h = badge_line_h + 12
        if show_stats and stats_text:
            badge_h += stats_line_h + 2
        if reset_text:
            badge_h += stats_line_h + 2
        badge_x = round((canvas_w - badge_w) / 2)
        badge_y = min(badge_max_y, canvas_h - badge_h - 6)
        rounded_path(cr, badge_x, badge_y, badge_w, badge_h, 17)
        cr.set_source_rgba(*COLORS["badge"])
        cr.fill_preserve()
        cr.set_source_rgba(*COLORS["badge_edge"])
        cr.set_line_width(1.6)
        cr.stroke()
        dot_top = round(6 + (badge_line_h - 9) / 2)
        cr.set_source_rgba(*COLORS.get(status_color, COLORS["ok"]))
        cr.arc(badge_x + 14 + 4.5, badge_y + dot_top + 4.5, 4.5, 0, 2 * math.pi)
        cr.fill()
        self._draw_text(
            cr,
            balance_text,
            badge_font,
            True,
            (badge_x + 30, badge_y + 6, badge_w - 40, badge_line_h),
            COLORS["text"],
            True,
        )
        if show_stats and stats_text:
            reset_offset = stats_line_h + 2 if reset_text else 0
            self._draw_text(
                cr,
                stats_text,
                font_stats,
                False,
                (badge_x + 6, badge_y + 6 + badge_line_h + 2 + reset_offset, badge_w - 12, stats_line_h),
                (226 / 255, 233 / 255, 250 / 255, 242 / 255),
                True,
            )
        if reset_text:
            self._draw_text(
                cr,
                reset_text,
                font_stats,
                False,
                (badge_x + 6, badge_y + 6 + badge_line_h + 2, badge_w - 12, stats_line_h),
                (180 / 255, 210 / 255, 245 / 255, 240 / 255),
                True,
            )
        return (badge_x, badge_y, badge_w, badge_h)

    def render_compact(self, pixbuf, bubble, balance_text, status_color, show_stats, stats_text, reset_text=""):
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, COMPACT_W, COMPACT_H)
        cr = cairo.Context(surface)
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        if pixbuf is not None:
            self._draw_pixbuf(cr, pixbuf, COMPACT_ANIM_X, COMPACT_ANIM_Y, COMPACT_ANIM_W, COMPACT_ANIM_H)
        bubble_rect = self._draw_bubble(cr, bubble, COMPACT_W, COMPACT_ANIM_Y)
        badge_rect = self._draw_badge(
            cr,
            balance_text,
            status_color,
            show_stats,
            stats_text,
            reset_text,
            COMPACT_W,
            COMPACT_H,
            COMPACT_BADGE_MAX_Y,
            18,
            11,
        )
        input_rects = []
        if pixbuf is not None:
            shape = alpha_shape(pixbuf)
            for sy, x0, x1 in shape.runs:
                input_rects.append((COMPACT_ANIM_X + x0, COMPACT_ANIM_Y + sy, x1 - x0, 1))
        if bubble_rect:
            input_rects.append(bubble_rect)
        input_rects.append(badge_rect)
        surface.flush()
        return RenderedFrame(surface, input_rects)

    def render(self, expression, bubble, balance_text, status_color, animation_frame, sleep_frame, show_stats, stats_text, reset_text=""):
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, CANVAS_W, CANVAS_H)
        cr = cairo.Context(surface)
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        cr.set_antialias(cairo.ANTIALIAS_DEFAULT)
        render_expression = "normal" if expression == "blink" and "blink" not in self.art.sprites else expression
        if render_expression == "working" and self.art.working_atlas is None:
            render_expression = "normal"
        is_working = render_expression == "working" and self.art.working_atlas is not None
        dedicated = is_working or render_expression in self.art.sprites
        sprite = self.art.working_atlas if is_working else self.art.sprite_for(render_expression)
        rotate = 0.0
        squash = 1.0
        if not dedicated:
            if render_expression == "sad":
                rotate = -2.0
            elif render_expression == "happy":
                squash = 1.02
            elif render_expression == "sleepy":
                squash = 0.985
        pivot_x = SPRITE_X + SPRITE_W / 2
        pivot_y = SPRITE_Y + SPRITE_H
        cr.save()
        if rotate or squash != 1.0:
            cr.translate(pivot_x, pivot_y)
            if rotate:
                cr.rotate(math.radians(rotate))
            if squash != 1.0:
                cr.scale(1.0, squash)
            cr.translate(-pivot_x, -pivot_y)
        if is_working:
            self._draw_working(cr, animation_frame, 0)
        elif render_expression == "sleepy":
            breath = 1.5 * (1 + math.sin(2 * math.pi * sleep_frame / 25))
            self._draw_pixbuf(cr, sprite, SPRITE_X, SPRITE_Y - breath, SPRITE_W, SPRITE_H + breath)
        else:
            self._draw_pixbuf(cr, sprite, SPRITE_X, SPRITE_Y, SPRITE_W, SPRITE_H)
        cr.restore()
        if not dedicated or render_expression == "sleepy":
            self._draw_effects(cr, render_expression, 0, sleep_frame)
        bubble_rect = self._draw_bubble(cr, bubble)
        badge_rect = self._draw_badge(cr, balance_text, status_color, show_stats, stats_text, reset_text)
        input_rects = self._sprite_rects(cr, render_expression, animation_frame, sleep_frame, 0)
        if bubble_rect:
            input_rects.append(bubble_rect)
        input_rects.append(badge_rect)
        surface.flush()
        return RenderedFrame(surface, input_rects)
