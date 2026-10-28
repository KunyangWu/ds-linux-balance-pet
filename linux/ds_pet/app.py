import argparse
import csv
import fcntl
import getpass
import json
import math
import os
import random
import signal
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gdk, GLib, Gtk

from .animations import AnimationLibrary
from .assets import Art
from .balance import BalanceError, fetch_balance
from .config import Config, Paths, atomic_write_json, read_json
from .render import Renderer
from .render_constants import CANVAS_H, CANVAS_W
from .scale import (
    bottom_right_margin,
    effective_scale,
    mode_canvas,
    mode_geometry,
    normalize_mode_factor,
    normalize_scale,
)
from .tasks import TaskActivity
from .usage import UsageReader, format_count

CSS = b"""
window { background-color: transparent; }
menu {
  background-color: rgba(22, 26, 40, 0.96);
  border: 1px solid rgba(255, 255, 255, 0.18);
  border-radius: 10px;
  padding: 6px;
}
menu menuitem {
  color: #f0f4fc;
  padding: 5px 16px;
  border-radius: 6px;
  font-size: 10pt;
}
menu menuitem:hover {
  background-color: rgba(77, 107, 254, 0.45);
}
menu separator {
  background-color: rgba(255, 255, 255, 0.12);
  margin: 4px 8px;
}
"""


@dataclass
class PetState:
    expression: str = "normal"
    expression_until: float = 0.0
    bubble: str = ""
    bubble_until: float = 0.0
    blink_until: float = 0.0
    next_blink_at: float = 0.0
    last_interaction: float = field(default_factory=time.monotonic)
    balance_text: str = "—"
    status_color: str = "ok"
    low_balance: bool = False
    value: float | None = None
    currency: str | None = None
    last_error: str | None = None
    provider: str = "DeepSeek"
    account_detail: str = ""
    queried_at: str | None = None
    has_snapshot: bool = False
    refreshing: bool = False
    query_started_at: float = 0.0
    task_busy: bool = False
    active_tasks: int = 0
    task_phase: str = "idle"
    work_started_at: float = 0.0
    bob: int = 0
    bob_phase: float = 0.0
    stats_text: str = ""
    show_stats: bool = True
    topmost: bool = True
    no_activate: bool = True
    reset_text: str = ""
    last_signature: tuple | None = None


class SingleInstance:
    def __init__(self, path):
        self.path = Path(path)
        self.handle = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.handle.seek(0)
            self.handle.truncate()
            self.handle.write(str(os.getpid()) + "\n")
            self.handle.flush()
            return True
        except OSError:
            self.handle.close()
            self.handle = None
            return False

    def release(self):
        if self.handle is None:
            return
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self.handle.close()
        self.handle = None


class BalanceBridge:
    def __init__(self, config):
        self.config = config
        self.thread = None
        self.callback = None

    def refresh(self, callback):
        if self.thread is not None and self.thread.is_alive():
            return False
        self.callback = callback

        def worker():
            try:
                result = fetch_balance(self.config)
            except BalanceError as error:
                GLib.idle_add(callback, None, str(error))
            except Exception:
                GLib.idle_add(callback, None, "余额查询失败。")
            else:
                GLib.idle_add(callback, result, None)

        self.thread = threading.Thread(target=worker, daemon=True)
        self.thread.start()
        return True

    def busy(self):
        return self.thread is not None and self.thread.is_alive()


class PetApp:
    def __init__(self, paths, config, scale=1.0, mode=None):
        self.paths = paths
        self.config = config
        self.scale = normalize_scale(scale)
        self.compact_scale_factor = normalize_mode_factor(config.compact_scale_factor)
        self.animations = AnimationLibrary(paths.assets_dir / "dsh-pet")
        saved = read_json(paths.state_file)
        if mode in ("compact", "classic"):
            saved_mode = mode
        else:
            saved_mode = saved.get("mode")
            if saved_mode not in ("compact", "classic"):
                saved_mode = config.animation_mode
        if saved_mode not in ("compact", "classic"):
            saved_mode = "compact" if self.animations.available() else "classic"
        if saved_mode == "compact" and not self.animations.available():
            saved_mode = "classic"
        self.mode = saved_mode
        self.canvas_w, self.canvas_h = mode_canvas(self.mode)
        self.effective_scale = effective_scale(self.mode, self.scale, self.compact_scale_factor)
        self.window_w, self.window_h, self.scale_x, self.scale_y = mode_geometry(
            self.mode, self.scale, self.compact_scale_factor
        )
        self.art = Art(paths.assets_dir)
        self.renderer = Renderer(self.art, config.font_family)
        codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        self.codex_home = codex_home
        self.usage = UsageReader(codex_home / "sessions")
        self.tasks = TaskActivity(codex_home / "sessions", codex_home)
        self.state = PetState()
        self.base_x = 0
        self.base_y = 0
        self.bob = 0
        self.drag_active = False
        self.drag_moved = False
        self.drag_start = (0, 0)
        self.drag_root = (0, 0)
        self.closing = False
        self.frame = None
        self.sources = []
        self.compact_clip = None
        self.compact_player = None
        self.compact_event_clip = None
        self.compact_event_until = 0.0
        self.compact_event_priority = 0
        self.next_idle_look = time.monotonic() + random.uniform(18, 30)
        self.balance = BalanceBridge(config)
        self.load_state()
        self.build_window()
        self.build_menu()
        self.install_css()
        self.sync_frame(force=True)

    def load_state(self):
        saved = read_json(self.paths.state_file)
        saved_effective = saved.get("effective_scale")
        saves_effective = isinstance(saved_effective, (int, float)) and not isinstance(saved_effective, bool)
        saved_scale = saved.get("scale")
        if saves_effective:
            scale_matches = abs(float(saved_effective) - self.effective_scale) < 1e-6
        elif isinstance(saved_scale, (int, float)) and not isinstance(saved_scale, bool):
            scale_matches = (
                abs(float(saved_scale) - self.scale) < 1e-6
                and abs(self.effective_scale - self.scale) < 1e-6
            )
        else:
            scale_matches = False
        saved_mode = saved.get("mode")
        mode_matches = saved_mode == self.mode if saved_mode is not None else self.mode == "classic"
        self.had_saved_position = (
            isinstance(saved.get("x"), int)
            and isinstance(saved.get("y"), int)
            and scale_matches
            and mode_matches
        )
        if self.had_saved_position:
            self.base_x = saved["x"]
            self.base_y = saved["y"]
        if isinstance(saved.get("look"), str):
            self.art.set_look_name(saved["look"])
        if isinstance(saved.get("stats"), bool):
            self.state.show_stats = saved["stats"]
        else:
            self.state.show_stats = self.config.show_stats
        if isinstance(saved.get("topMost"), bool):
            self.state.topmost = saved["topMost"]
        else:
            self.state.topmost = self.config.topmost
        if isinstance(saved.get("noActivate"), bool):
            self.state.no_activate = saved["noActivate"]
        else:
            self.state.no_activate = self.config.no_activate

    def save_state(self):
        atomic_write_json(
            self.paths.state_file,
            {
                "x": self.base_x,
                "y": self.base_y,
                "look": self.art.look_name(),
                "stats": self.state.show_stats,
                "topMost": self.state.topmost,
                "noActivate": self.state.no_activate,
                "scale": self.scale,
                "effective_scale": self.effective_scale,
                "compact_scale_factor": self.compact_scale_factor,
                "mode": self.mode,
                "updatedAt": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
        )

    def install_css(self):
        screen = Gdk.Screen.get_default()
        if screen is None:
            return
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def build_window(self):
        self.window = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        self.window.set_title("余额桌宠")
        self.window.set_role("ds-linux-balance-pet")
        self.window.set_decorated(False)
        self.window.set_resizable(False)
        self.window.set_skip_taskbar_hint(True)
        self.window.set_skip_pager_hint(True)
        self.window.set_type_hint(Gdk.WindowTypeHint.UTILITY)
        self.window.set_default_size(self.window_w, self.window_h)
        self.window.set_size_request(self.window_w, self.window_h)
        screen = self.window.get_screen()
        visual = screen.get_rgba_visual() if screen else None
        if visual is not None and screen.is_composited():
            self.window.set_visual(visual)
        self.window.set_app_paintable(True)
        self.window.set_keep_above(self.state.topmost)
        self.window.set_accept_focus(not self.state.no_activate)
        self.window.set_focus_on_map(not self.state.no_activate)
        self.window.move(self.base_x, self.base_y)
        self.window.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
        )
        self.window.connect("draw", self.on_draw)
        self.window.connect("button-press-event", self.on_button_press)
        self.window.connect("button-release-event", self.on_button_release)
        self.window.connect("motion-notify-event", self.on_motion)
        self.window.connect("map-event", self.on_map)
        self.window.connect("delete-event", self.on_delete)
        self.window.connect("destroy", self.on_destroy)
        if not getattr(self, "had_saved_position", False):
            self.move_bottom_right(save=False)

    def build_menu(self):
        self.menu = Gtk.Menu()
        self.menu.attach_to_widget(self.window, None)
        self.menu.connect("show", self.refresh_menu_state)
        self.item_refresh = self.add_menu_item("立即刷新余额", self.on_menu_refresh)
        self.item_talk = self.add_menu_item("打个招呼", self.on_menu_talk)
        self.item_look = self.add_menu_item(f"换个形象（共 {len(self.art.look_files)} 个）", self.on_menu_look)
        self.item_mode = self.add_menu_item(self.mode_menu_label(), self.on_menu_mode)
        self.item_stats = self.add_menu_check("显示用量信息", self.state.show_stats, self.on_menu_stats)
        self.item_topmost = self.add_menu_check("始终置顶", self.state.topmost, self.on_menu_topmost)
        self.item_nofocus = self.add_menu_check("不抢焦点", self.state.no_activate, self.on_menu_nofocus)
        self.item_usage = self.add_menu_item("用量详情", self.on_menu_usage)
        self.item_account = self.add_menu_item("账户余额详情", self.on_menu_account)
        self.menu.append(Gtk.SeparatorMenuItem())
        self.item_key = self.add_menu_item("设置 DeepSeek 密钥", self.on_menu_key)
        self.item_threshold = self.add_menu_item("设置低余额阈值", self.on_menu_threshold)
        self.item_reset = self.add_menu_item("回到右下角", self.on_menu_reset)
        self.item_exit = self.add_menu_item("退出", self.on_menu_exit)
        self.menu.show_all()

    def add_menu_item(self, label, callback):
        item = Gtk.MenuItem(label=label)
        item.connect("activate", callback)
        self.menu.append(item)
        return item

    def add_menu_check(self, label, active, callback):
        item = Gtk.CheckMenuItem(label=label)
        item.set_active(active)
        item.connect("activate", callback)
        self.menu.append(item)
        return item

    def mode_menu_label(self):
        current = "紧凑动画" if self.mode == "compact" else "经典形象"
        return f"动画模式：{current}（点击切换）"

    def refresh_menu_state(self, *_):
        self.item_stats.set_active(self.state.show_stats)
        self.item_topmost.set_active(self.state.topmost)
        self.item_nofocus.set_active(self.state.no_activate)
        self.item_look.set_visible(self.mode == "classic")
        self.item_mode.set_label(self.mode_menu_label())

    def on_draw(self, _widget, cr):
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        if self.frame is not None:
            cr.save()
            cr.scale(self.scale_x, self.scale_y)
            cr.set_source_surface(self.frame.surface, 0, 0)
            cr.paint()
            cr.restore()
        return False

    def apply_input_shape(self):
        if self.frame is None:
            return
        window = self.window.get_window()
        if window is None:
            return
        region = cairo.Region()
        for x, y, width, height in self.frame.input_rects:
            x0 = max(0, math.floor(x * self.scale_x))
            y0 = max(0, math.floor(y * self.scale_y))
            x1 = min(self.window_w, math.ceil((x + width) * self.scale_x))
            y1 = min(self.window_h, math.ceil((y + height) * self.scale_y))
            if x1 > x0 and y1 > y0:
                region.union(cairo.RectangleInt(x0, y0, x1 - x0, y1 - y0))
        window.input_shape_combine_region(region, 0, 0)

    def on_map(self, *_):
        self.window.move(self.base_x, self.base_y + self.bob)
        self.apply_input_shape()
        self.write_status()
        return False

    def on_delete(self, *_):
        self.quit()
        return True

    def on_destroy(self, *_):
        if not self.closing:
            self.quit()

    def on_button_press(self, _widget, event):
        if event.button == 3:
            self.menu.popup_at_pointer(event)
            return True
        if event.button != 1:
            return False
        self.drag_active = True
        self.drag_moved = False
        self.drag_start = (self.base_x, self.base_y)
        self.drag_root = (event.x_root, event.y_root)
        if self.mode == "compact":
            self.compact_event_clip = None
            self.compact_event_until = 0.0
        self.state.last_interaction = time.monotonic()
        return True

    def on_motion(self, _widget, event):
        if not self.drag_active:
            return False
        new_x = round(self.drag_start[0] + event.x_root - self.drag_root[0])
        new_y = round(self.drag_start[1] + event.y_root - self.drag_root[1])
        if abs(new_x - self.drag_start[0]) > 2 or abs(new_y - self.drag_start[1]) > 2:
            self.drag_moved = True
        self.base_x = new_x
        self.base_y = new_y
        self.window.move(self.base_x, self.base_y + self.bob)
        return True

    def on_button_release(self, _widget, event):
        if event.button != 1 or not self.drag_active:
            return False
        was_dragging = self.drag_active
        moved = self.drag_moved
        self.drag_active = False
        self.state.last_interaction = time.monotonic()
        if not was_dragging:
            return False
        if moved:
            self.save_state()
            self.write_status()
        else:
            if self.mode == "compact":
                self.set_compact_event(random.choice(("click-wave", "click-shy", "click-happy")), 2.6, 3)
            self.refresh_balance()
            self.show_bubble(self.get_talk_line(), 4.0, "sad" if self.state.low_balance else "greeting")
            self.sync_frame()
        return True

    def on_menu_refresh(self, *_):
        self.refresh_balance()
        self.sync_frame()

    def on_menu_talk(self, *_):
        self.state.last_interaction = time.monotonic()
        if self.mode == "compact":
            self.set_compact_event("click-happy", 2.6, 3)
        self.show_bubble(self.get_talk_line(), 4.0, "happy")
        self.sync_frame()

    def on_menu_look(self, *_):
        if len(self.art.look_files) <= 1:
            self.show_bubble("assets 里只放了一个形象哦", 3.0)
            self.sync_frame()
            return
        self.art.next_look()
        self.save_state()
        self.show_bubble(f"形象 {self.art.look_index + 1}/{len(self.art.look_files)}", 3.0)
        self.sync_frame(force=True)

    def on_menu_mode(self, *_):
        target = "classic" if self.mode == "compact" else "compact"
        self.apply_mode(target)
        self.save_state()
        self.write_status()

    def apply_mode(self, mode):
        if mode not in ("compact", "classic"):
            return
        if mode == "compact" and not self.animations.available():
            self.show_bubble("dsh-pet 动画素材未安装", 3.0)
            self.sync_frame()
            return
        if mode == self.mode:
            return
        self.mode = mode
        self.canvas_w, self.canvas_h = mode_canvas(self.mode)
        self.effective_scale = effective_scale(self.mode, self.scale, self.compact_scale_factor)
        self.window_w, self.window_h, self.scale_x, self.scale_y = mode_geometry(
            self.mode, self.scale, self.compact_scale_factor
        )
        self.compact_clip = None
        self.compact_player = None
        self.compact_event_clip = None
        self.compact_event_until = 0.0
        self.compact_event_priority = 0
        self.state.last_signature = None
        self.window.set_default_size(self.window_w, self.window_h)
        self.window.set_size_request(self.window_w, self.window_h)
        self.window.resize(self.window_w, self.window_h)
        self.move_bottom_right(save=False)
        self.refresh_menu_state()
        self.sync_frame(force=True)

    def set_compact_event(self, clip, seconds, priority=1):
        if self.mode != "compact" or not self.animations.has(clip):
            return
        if self.compact_event_clip:
            now = time.monotonic()
            if now < self.compact_event_until and self.compact_event_priority > priority:
                return
        self.compact_event_clip = clip
        self.compact_event_until = time.monotonic() + seconds
        self.compact_event_priority = priority
        self.state.last_signature = None

    def compact_balance_clip(self, result):
        threshold, _ = self.config.alert_threshold()
        value = float(result.value) if result.value is not None else None
        if not result.data.get("is_available", True):
            return "balance-low"
        if threshold is not None and value is not None and value < threshold:
            return "balance-low"
        if threshold is not None and value is not None and threshold > 0 and value > threshold * 3:
            return "balance-high"
        return "balance-normal"

    def compact_clip_for_state(self, now):
        if self.compact_event_clip:
            if now < self.compact_event_until:
                return self.compact_event_clip
            self.compact_event_clip = None
            self.compact_event_until = 0.0
            self.compact_event_priority = 0
        if self.drag_active:
            return "drag"
        if self.state.task_busy:
            if self.state.task_phase == "working":
                return "work-busy"
            return "work-thinking"
        if self.state.expression == "sleepy":
            return "sleep"
        if self.state.last_error:
            return "work-error"
        if self.state.low_balance:
            return "balance-low"
        if now >= self.next_idle_look:
            self.next_idle_look = now + random.uniform(24, 45)
            self.set_compact_event("idle-look", 4.0, 0)
            return self.compact_event_clip or "idle"
        return "idle"

    def update_compact_animation(self, now):
        if self.mode != "compact":
            return None
        clip = self.compact_clip_for_state(now)
        if not self.animations.has(clip):
            clip = "idle" if self.animations.has("idle") else None
        if clip is None:
            return None
        if clip != self.compact_clip or self.compact_player is None:
            self.compact_clip = clip
            self.compact_player = self.animations.player(clip)
            self.state.last_signature = None
        if self.compact_player is not None:
            self.compact_player.update(now)
        return self.compact_player

    def on_menu_stats(self, item):
        self.state.show_stats = bool(item.get_active())
        if self.state.show_stats:
            self.update_usage(False)
            self.update_stats_text()
        self.save_state()
        self.sync_frame(force=True)

    def on_menu_topmost(self, item):
        self.state.topmost = bool(item.get_active())
        self.window.set_keep_above(self.state.topmost)
        self.save_state()
        self.write_status()

    def on_menu_nofocus(self, item):
        self.state.no_activate = bool(item.get_active())
        self.window.set_accept_focus(not self.state.no_activate)
        self.window.set_focus_on_map(not self.state.no_activate)
        self.save_state()
        self.write_status()

    def on_menu_usage(self, *_):
        self.state.last_interaction = time.monotonic()
        self.update_usage(False)
        self.show_bubble(self.get_usage_detail(), 12.0)
        self.sync_frame(force=True)

    def on_menu_account(self, *_):
        if self.state.last_error:
            detail = "刷新失败：" + self.state.last_error
        elif self.state.account_detail:
            detail = self.state.account_detail
        else:
            detail = "暂无账户数据"
        self.show_bubble(detail, 12.0)
        self.sync_frame(force=True)

    def on_menu_key(self, *_):
        dialog = Gtk.Dialog(title="设置 DeepSeek 密钥", parent=self.window, flags=Gtk.DialogFlags.MODAL)
        dialog.set_keep_above(True)
        dialog.add_button("取消", Gtk.ResponseType.CANCEL)
        dialog.add_button("保存", Gtk.ResponseType.OK)
        entry = Gtk.Entry()
        entry.set_visibility(False)
        entry.set_placeholder_text("DeepSeek API Key")
        entry.set_text(self.config.api_key)
        entry.set_activates_default(True)
        dialog.set_default_response(Gtk.ResponseType.OK)
        box = dialog.get_content_area()
        box.set_spacing(8)
        box.set_border_width(12)
        box.add(Gtk.Label(label="密钥只保存在本机配置文件中，权限 600。"))
        box.add(entry)
        dialog.show_all()
        response = dialog.run()
        value = entry.get_text().strip()
        dialog.destroy()
        if response == Gtk.ResponseType.OK and value:
            self.config.api_key = value
            self.config.save(self.paths)
            self.refresh_balance()

    def on_menu_threshold(self, *_):
        dialog = Gtk.Dialog(title="设置低余额阈值", parent=self.window, flags=Gtk.DialogFlags.MODAL)
        dialog.set_keep_above(True)
        dialog.add_button("取消", Gtk.ResponseType.CANCEL)
        dialog.add_button("保存", Gtk.ResponseType.OK)
        entry = Gtk.Entry()
        entry.set_placeholder_text("例如 20，留空表示关闭")
        if self.config.low_threshold is not None:
            entry.set_text(f"{self.config.low_threshold:g}")
        entry.set_activates_default(True)
        dialog.set_default_response(Gtk.ResponseType.OK)
        box = dialog.get_content_area()
        box.set_spacing(8)
        box.set_border_width(12)
        box.add(Gtk.Label(label="余额低于该值时显示讨饭表情；只对 CNY 生效。"))
        box.add(entry)
        dialog.show_all()
        response = dialog.run()
        value = entry.get_text().strip()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return
        if not value:
            self.config.low_threshold = None
            self.config.save(self.paths)
            self.show_bubble("已关闭低余额提醒", 4.0)
        else:
            try:
                threshold = float(value)
            except ValueError:
                self.show_bubble("阈值必须是不小于 0 的数字", 4.0)
                self.sync_frame()
                return
            if threshold < 0:
                self.show_bubble("阈值必须是不小于 0 的数字", 4.0)
                self.sync_frame()
                return
            self.config.low_threshold = threshold
            self.config.low_currency = "CNY"
            self.config.save(self.paths)
            self.show_bubble(f"低余额阈值已设为 ¥{threshold:g}", 4.0)
        self.refresh_balance()
        self.sync_frame()

    def on_menu_reset(self, *_):
        self.move_bottom_right(save=True)
        self.write_status()

    def on_menu_exit(self, *_):
        self.quit()

    def move_bottom_right(self, save=True):
        display = Gdk.Display.get_default()
        if display is None:
            return
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        if monitor is None:
            return
        area = monitor.get_workarea()
        margin = bottom_right_margin(self.scale)
        self.base_x = area.x + area.width - self.window_w - margin
        self.base_y = area.y + area.height - self.window_h - margin
        self.window.move(self.base_x, self.base_y + self.bob)
        if save:
            self.save_state()

    def get_talk_line(self):
        lines = (
            "余额还有 {b} 哦～",
            "人家一直帮你盯着余额呢",
            "点我就能立刻查一次余额",
            "今天的余额也记好啦",
            "主人，要不要喝杯茶？",
            "{b}，还够用一阵子～",
            "记账小能手在此！",
        )
        low_lines = (
            "余额只剩 {b} 啦…",
            "该充值了，不然要断粮了…",
            "呜呜，钱包越来越轻了",
        )
        line = random.choice(low_lines if self.state.low_balance else lines)
        return line.replace("{b}", self.state.balance_text)

    def show_bubble(self, text, seconds=4.0, expression=None):
        now = time.monotonic()
        self.state.bubble = text
        self.state.bubble_until = now + seconds
        if expression:
            self.state.expression = expression
            self.state.expression_until = now + seconds

    def rendered_bubble(self):
        if self.state.refreshing:
            elapsed = max(0.0, time.monotonic() - self.state.query_started_at)
            dots = 1 + (int(elapsed / 0.4) % 3)
            return "查询中" + "." * dots
        return self.state.bubble

    def rendered_expression(self):
        now = time.monotonic()
        if self.state.task_busy and self.art.working_atlas is not None:
            return "working"
        if self.state.expression == "sad" and self.state.low_balance and not self.state.last_error:
            if "hungry" in self.art.sprites:
                return "hungry"
        if (
            self.state.expression in ("normal", "blink")
            and not self.state.low_balance
            and now - self.state.last_interaction >= 45
            and "relaxed" in self.art.sprites
        ):
            return "relaxed"
        return self.state.expression

    def working_frame(self):
        if not self.state.task_busy:
            return 0
        elapsed = max(0.0, time.monotonic() - self.state.work_started_at)
        return int(elapsed / 0.160) % 4

    def sleep_frame(self):
        if self.rendered_expression() != "sleepy":
            return 0
        return int((time.monotonic() * 1000) / 120) % 25

    def sync_frame(self, force=False):
        bubble = self.rendered_bubble()
        if self.mode == "compact":
            now = time.monotonic()
            player = self.update_compact_animation(now)
            clip = self.compact_clip or "idle"
            frame_id = player.frame_id if player is not None else 0
            signature = (
                "compact",
                clip,
                frame_id,
                bubble,
                self.state.balance_text,
                self.state.status_color,
                self.state.stats_text,
                self.state.show_stats,
                self.state.reset_text,
            )
            if not force and signature == self.state.last_signature:
                return
            self.state.last_signature = signature
            pixbuf = player.pixbuf if player is not None else None
            self.frame = self.renderer.render_compact(
                pixbuf,
                bubble,
                self.state.balance_text,
                self.state.status_color,
                bool(self.state.show_stats and self.state.stats_text),
                self.state.stats_text,
                self.state.reset_text,
            )
        else:
            expression = self.rendered_expression()
            animation_frame = self.working_frame()
            sleep_frame = self.sleep_frame()
            signature = (
                "classic",
                expression,
                animation_frame,
                sleep_frame,
                bubble,
                self.state.balance_text,
                self.state.status_color,
                self.state.stats_text,
                self.state.show_stats,
                self.state.reset_text,
            )
            if not force and signature == self.state.last_signature:
                return
            self.state.last_signature = signature
            self.frame = self.renderer.render(
                expression,
                bubble,
                self.state.balance_text,
                self.state.status_color,
                animation_frame,
                sleep_frame,
                bool(self.state.show_stats and self.state.stats_text),
                self.state.stats_text,
                self.state.reset_text,
            )
        self.apply_input_shape()
        self.window.queue_draw()
        if self.state.refreshing or self.state.task_busy:
            self.write_status()

    def refresh_balance(self):
        if self.balance.busy():
            return
        self.state.query_started_at = time.monotonic()
        self.state.refreshing = True
        self.sync_frame()
        self.balance.refresh(self.finish_balance)

    def finish_balance(self, result, error):
        if self.closing:
            return False
        self.state.refreshing = False
        if error:
            if not self.state.has_snapshot:
                self.state.balance_text = "—"
            self.state.status_color = "error"
            self.state.last_error = error
            message = error if len(error) <= 40 else error[:40] + "…"
            self.show_bubble("查询失败：" + message, 6.0, "sad")
            if self.mode == "compact":
                self.set_compact_event("work-error", 3.5, 5)
        else:
            self.state.balance_text = result.text
            self.state.status_color = "low" if result.low else "ok"
            self.state.low_balance = result.low
            self.state.value = float(result.value) if result.value is not None else None
            self.state.currency = result.currency
            self.state.last_error = None
            self.state.account_detail = result.detail
            self.state.queried_at = result.queried_at
            self.state.has_snapshot = True
            if result.low and not self.state.bubble:
                self.state.expression = "sad"
            if self.state.bubble.startswith("查询失败："):
                self.state.bubble = ""
            if self.mode == "compact":
                self.set_compact_event(self.compact_balance_clip(result), 3.5, 2)
            self.write_balance_log()
        self.update_stats_text()
        self.sync_frame()
        self.write_status()
        return False

    def update_usage(self, selected_only):
        self.usage.update(selected_only)
        self.update_stats_text()

    def update_stats_text(self):
        parts = []
        if self.usage.requests > 0:
            parts.append("用量 " + format_count(self.usage.input + self.usage.output))
            if self.usage.input > 0:
                parts.append("缓存 {:.0%}".format(self.usage.cache_ratio()))
        rate = self.usage.rate(30)
        rate_text = format_count(rate) if rate >= 1000 else "{:0.1f}".format(rate)
        parts.append(rate_text + " tok/s")
        cost = self.hourly_cost()
        if cost:
            parts.append(f"¥{cost}/h")
        self.state.stats_text = "｜".join(parts)

    def hourly_cost(self):
        if self.state.last_error or not self.paths.balance_log.is_file():
            return None
        try:
            with self.paths.balance_log.open("r", encoding="utf-8", newline="") as handle:
                rows = [
                    row for row in csv.DictReader(handle)
                    if row.get("currency") == "CNY" and row.get("source") == "deepseek"
                ]
        except OSError:
            return None
        if len(rows) < 2:
            return None
        try:
            first_time = datetime.strptime(rows[0]["timestamp"], "%Y-%m-%d %H:%M:%S")
            last_time = datetime.strptime(rows[-1]["timestamp"], "%Y-%m-%d %H:%M:%S")
            minutes = (last_time - first_time).total_seconds() / 60
            spent = float(rows[0]["balance"]) - float(rows[-1]["balance"])
        except (KeyError, ValueError):
            return None
        if minutes < 10 or spent <= 0:
            return None
        return round(spent / minutes * 60, 1)

    def get_usage_detail(self):
        rate = self.usage.rate(30)
        cache_ratio = self.usage.cache_ratio()
        lines = [
            f"本会话 输入 {format_count(self.usage.input)}（缓存 {cache_ratio:.0%}）",
            f"输出 {format_count(self.usage.output)}（推理 {format_count(self.usage.reasoning)}）",
            f"缓存命中 {format_count(self.usage.cached)} / 输入 {format_count(self.usage.input)}",
            f"{self.usage.requests} 次请求",
            f"近 30 秒输出 {format_count(rate)} tok/s",
        ]
        cost = self.hourly_cost()
        if cost:
            lines.append(f"按记录推算 ¥{cost} / 小时")
        return "；".join(lines)

    def write_balance_log(self, force=False):
        if self.state.last_error or self.state.value is None:
            return
        try:
            self.paths.state_dir.mkdir(parents=True, exist_ok=True)
            now = datetime.now()
            if not force and self.paths.balance_log.is_file():
                with self.paths.balance_log.open("r", encoding="utf-8", newline="") as handle:
                    last = None
                    for last in csv.DictReader(handle):
                        pass
                if last:
                    try:
                        previous = datetime.strptime(last["timestamp"], "%Y-%m-%d %H:%M:%S")
                        if (now - previous).total_seconds() < self.config.log_interval_seconds * 0.9:
                            return
                    except (KeyError, ValueError):
                        pass
            new_file = not self.paths.balance_log.is_file()
            with self.paths.balance_log.open("a", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                if new_file:
                    writer.writerow(["timestamp", "balance", "currency", "source", "window"])
                writer.writerow([
                    now.strftime("%Y-%m-%d %H:%M:%S"),
                    str(self.state.value),
                    self.state.currency,
                    "deepseek",
                    f"{self.base_x},{self.base_y}",
                ])
        except OSError:
            pass

    def update_tasks(self):
        result = self.tasks.update()
        was_busy = self.state.task_busy
        previous_phase = self.state.task_phase
        self.state.task_busy = bool(result["is_busy"])
        self.state.active_tasks = int(result["active_count"])
        self.state.task_phase = result["phase"]
        if self.state.task_busy and not was_busy:
            self.state.work_started_at = time.monotonic()
            if self.mode == "compact":
                self.set_compact_event("work-thinking", 1.5, 4)
        if was_busy and not self.state.task_busy:
            self.state.expression = "sad" if self.state.low_balance else "normal"
            self.state.last_interaction = time.monotonic()
            if self.mode == "compact" and previous_phase in ("working", "thinking"):
                self.set_compact_event("work-success", 3.0, 4)

    def set_bob(self, offset):
        if offset == self.bob:
            return
        self.bob = offset
        self.window.move(self.base_x, self.base_y + self.bob)

    def on_tick(self):
        if self.closing:
            return False
        now = time.monotonic()
        state = self.state
        if state.bubble and now >= state.bubble_until:
            state.bubble = ""
        if state.expression in ("happy", "greeting", "sad", "blink") and now >= state.expression_until and now >= state.blink_until:
            state.expression = "sad" if state.low_balance else "normal"
        if state.expression == "normal" and now >= state.next_blink_at:
            state.expression = "blink"
            state.blink_until = now + 0.14
            state.next_blink_at = now + random.uniform(3, 7)
        if state.expression == "blink" and now >= state.blink_until:
            state.expression = "normal"
        idle = now - state.last_interaction
        if (
            not state.task_busy
            and idle > self.config.idle_sleep_seconds
            and state.expression != "sleepy"
            and not state.bubble
            and not state.refreshing
        ):
            state.expression = "sleepy"
        if idle < 2 and state.expression == "sleepy":
            state.expression = "normal"
            state.last_interaction = now
        amplitude = 2 if state.expression == "sleepy" else 3
        offset = round(math.sin(state.bob_phase) * amplitude * self.scale_y)
        self.set_bob(offset)
        state.bob_phase += 0.12
        self.sync_frame()
        return True

    def on_rate_tick(self):
        if self.closing:
            return False
        if self.state.show_stats:
            previous = self.state.stats_text
            self.update_usage(True)
            if self.state.stats_text != previous:
                self.sync_frame()
                self.write_status()
        return True

    def on_task_tick(self):
        if self.closing:
            return False
        self.update_tasks()
        self.sync_frame()
        self.write_status()
        return True

    def on_stats_tick(self):
        if self.closing:
            return False
        self.update_usage(False)
        self.sync_frame()
        self.write_status()
        return True

    def on_balance_tick(self):
        if self.closing:
            return False
        if not self.drag_active:
            self.refresh_balance()
        return True

    def write_status(self):
        try:
            payload = {
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "pid": os.getpid(),
                "visible": self.window.get_visible(),
                "location": f"{self.base_x},{self.base_y}",
                "scale": self.scale,
                "effective_scale": self.effective_scale,
                "compact_scale_factor": self.compact_scale_factor,
                "mode": self.mode,
                "compact_clip": self.compact_clip,
                "compact_frame": self.compact_player.frame_id if self.compact_player else None,
                "window_size": f"{self.window_w},{self.window_h}",
                "expression": self.rendered_expression(),
                "look": self.art.look_name(),
                "lookCount": len(self.art.look_files),
                "balance": self.state.balance_text,
                "low": self.state.low_balance,
                "bubble": self.rendered_bubble(),
                "last_error": self.state.last_error,
                "provider": self.state.provider,
                "account_detail": self.state.account_detail,
                "queried_at": self.state.queried_at,
                "refreshing": self.state.refreshing,
                "show_stats": self.state.show_stats,
                "topmost": self.state.topmost,
                "no_activate": self.state.no_activate,
                "stats_text": self.state.stats_text,
                "token_requests": self.usage.requests,
                "token_output": self.usage.output,
                "task_busy": self.state.task_busy,
                "active_tasks": self.state.active_tasks,
                "task_phase": self.state.task_phase,
                "animation_frame": self.working_frame(),
                "sleep_animation_frame": self.sleep_frame(),
            }
            atomic_write_json(self.paths.status_file, payload)
        except OSError:
            pass

    def after_show(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, self.on_signal)
        self.on_map()
        self.update_usage(False)
        self.update_tasks()
        self.update_stats_text()
        self.state.expression = "sad" if self.state.low_balance else "normal"
        self.state.next_blink_at = time.monotonic() + random.uniform(3, 7)
        if self.mode == "compact":
            self.set_compact_event("greet", 3.0, 1)
            self.next_idle_look = time.monotonic() + random.uniform(24, 45)
        self.sync_frame(force=True)
        self.refresh_balance()
        self.sources = [
            GLib.timeout_add(80, self.on_tick),
            GLib.timeout_add(1000, self.on_rate_tick),
            GLib.timeout_add(2000, self.on_task_tick),
            GLib.timeout_add(5000, self.on_stats_tick),
            GLib.timeout_add_seconds(max(5, self.config.refresh_seconds), self.on_balance_tick),
        ]
        return False

    def run(self):
        self.window.show_all()
        GLib.idle_add(self.after_show)
        Gtk.main()

    def on_signal(self):
        self.quit()
        return False

    def quit(self):
        if self.closing:
            return
        self.closing = True
        for source in self.sources:
            GLib.source_remove(source)
        self.sources = []
        self.write_balance_log(force=True)
        self.save_state()
        self.write_status()
        try:
            self.window.destroy()
        except Exception:
            pass
        Gtk.main_quit()

    def preview_shots(self, output_dir):
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        shots = [
            ("idle", "normal", "", "¥8.47", "ok", False, "", 0, 0),
            ("blink", "blink", "", "¥8.47", "ok", False, "", 0, 0),
            ("talking", "happy", "余额还有 ¥8.47 哦～", "¥8.47", "ok", False, "", 0, 0),
            ("greeting", "greeting", "摸摸头，继续陪你～", "¥8.47", "ok", False, "", 0, 0),
            ("relaxed", "relaxed", "", "¥8.47", "ok", False, "", 0, 0),
            ("hungry", "hungry", "余额不多啦～", "¥4.20", "low", False, "", 0, 0),
            ("low", "hungry", "余额只剩 ¥4.20 啦…", "¥4.20", "low", False, "", 0, 0),
            ("error", "sad", "查询失败：网络不可用", "查询失败", "error", False, "", 0, 0),
            ("stats", "normal", "", "¥5.61", "ok", True, "用量 58.8M｜缓存 99%｜12.3k tok/s｜¥1.5/h", 0, 0),
            ("query-1", "normal", "查询中.", "¥8.47", "ok", True, "用量 80k｜缓存 66%｜1.2k tok/s", 0, 0),
            ("query-2", "normal", "查询中..", "¥8.47", "ok", True, "用量 80k｜缓存 66%｜1.2k tok/s", 0, 0),
            ("query-3", "normal", "查询中...", "¥8.47", "ok", True, "用量 80k｜缓存 66%｜1.2k tok/s", 0, 0),
            ("sleepy", "sleepy", "", "¥8.47", "ok", False, "", 0, 0),
            ("sleepy-8", "sleepy", "", "¥8.47", "ok", False, "", 0, 8),
            ("sleepy-16", "sleepy", "", "¥8.47", "ok", False, "", 0, 16),
            ("working-0", "working", "", "¥8.47", "ok", True, "用量 80k｜缓存 66%｜1.2k tok/s", 0, 0),
            ("working-1", "working", "", "¥8.47", "ok", True, "用量 80k｜缓存 66%｜1.2k tok/s", 1, 0),
            ("working-2", "working", "", "¥8.47", "ok", True, "用量 80k｜缓存 66%｜1.2k tok/s", 2, 0),
            ("working-3", "working", "", "¥8.47", "ok", True, "用量 80k｜缓存 66%｜1.2k tok/s", 3, 0),
        ]
        for name, expression, bubble, balance_text, color, show_stats, stats_text, animation, sleep in shots:
            frame = self.renderer.render(
                expression, bubble, balance_text, color, animation, sleep, show_stats, stats_text
            )
            frame.surface.write_to_png(str(output_dir / f"{name}.png"))
            print("preview " + str(output_dir / f"{name}.png"))


def set_key(paths, config, value=None):
    if value is None:
        value = getpass.getpass("DeepSeek API Key: ").strip()
    if not value:
        print("密钥为空，未修改。")
        return 1
    config.api_key = value
    config.save(paths)
    print("已保存到 " + str(paths.config_file))
    if os.environ.get("DEEPSEEK_API_KEY"):
        print("注意：当前环境变量 DEEPSEEK_API_KEY 优先于配置文件。")
    return 0


def main(argv=None):
    os.environ.setdefault("GDK_BACKEND", "x11")
    parser = argparse.ArgumentParser()
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--preview", action="store_true")
    parser.add_argument("--preview-dir", default="preview")
    parser.add_argument("--set-key", nargs="?", const="", default=None)
    parser.add_argument("--scale", type=float, default=None)
    parser.add_argument("--mode", choices=("compact", "classic"), default=None)
    parser.add_argument("--no-single-instance", action="store_true")
    args = parser.parse_args(argv)
    paths = Paths()
    paths.ensure()
    config = Config.load(paths)

    if args.set_key is not None:
        return set_key(paths, config, args.set_key or None)

    if args.headless:
        try:
            result = fetch_balance(config)
        except BalanceError as error:
            print(json.dumps({"error": str(error)}, ensure_ascii=False))
            return 1
        payload = result.payload()
        payload.update({"text": result.text, "low": result.low})
        print(json.dumps(payload, ensure_ascii=False))
        return 0

    raw_scale = args.scale if args.scale is not None else config.scale
    try:
        scale = normalize_scale(raw_scale)
    except ValueError as error:
        print(f"缩放比例无效：{error}", file=sys.stderr)
        return 2

    display_ok = False
    try:
        display_ok = bool(Gtk.init_check()[0])
    except Exception:
        pass
    if display_ok:
        GLib.set_prgname("ds-linux-balance-pet")
        GLib.set_application_name("余额桌宠")

    if args.preview:
        app = PetApp.__new__(PetApp)
        app.paths = paths
        app.config = config
        app.art = Art(paths.assets_dir)
        app.renderer = Renderer(app.art, config.font_family)
        app.preview_shots(args.preview_dir)
        return 0

    if not display_ok:
        print("无法连接 X11/XWayland。请在桌面会话中运行，并设置 DISPLAY。", file=sys.stderr)
        return 1

    if not args.no_single_instance:
        lock = SingleInstance(paths.lock_file)
        if not lock.acquire():
            print("桌宠已经在运行了。")
            return 0
    else:
        lock = None

    try:
        app = PetApp(paths, config, scale, args.mode)
    except Exception as error:
        if lock:
            lock.release()
        print(f"启动失败：{error}", file=sys.stderr)
        return 1
    try:
        app.run()
    finally:
        app.quit()
        if lock:
            lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
