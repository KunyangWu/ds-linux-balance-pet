import time
from pathlib import Path

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, GLib

CLIP_FILES = {
    "idle": "idle.gif",
    "idle-look": "idle-look.gif",
    "click-wave": "click-wave.gif",
    "click-shy": "click-shy.gif",
    "click-happy": "click-happy.gif",
    "drag": "drag.gif",
    "work-thinking": "work-thinking.gif",
    "work-busy": "work-busy.gif",
    "work-success": "work-success.gif",
    "work-error": "work-error.gif",
    "balance-high": "balance-high.gif",
    "balance-normal": "balance-normal.gif",
    "balance-low": "balance-low.gif",
    "sleep": "sleep.gif",
    "greet": "greet.gif",
}


class GifClip:
    def __init__(self, name, path):
        self.name = name
        self.path = Path(path)
        loader = GdkPixbuf.PixbufLoader.new_with_type("gif")
        loader.write(self.path.read_bytes())
        loader.close()
        self.loader = loader
        self.animation = loader.get_animation()
        self.width = self.animation.get_width()
        self.height = self.animation.get_height()

    def player(self):
        return GifPlayer(self)


class GifPlayer:
    def __init__(self, clip):
        self.clip = clip
        self.iter = clip.animation.get_iter(None)
        self.pixbuf = self.iter.get_pixbuf()
        self.frame_id = 0
        self.next_due = time.monotonic()
        self.started_at = self.next_due

    def reset(self):
        self.iter = self.clip.animation.get_iter(None)
        self.pixbuf = self.iter.get_pixbuf()
        self.frame_id += 1
        self.next_due = time.monotonic()
        self.started_at = self.next_due

    def update(self, now=None):
        now = time.monotonic() if now is None else now
        if now < self.next_due:
            return False
        microseconds = GLib.get_real_time()
        current = GLib.TimeVal()
        current.tv_sec, current.tv_usec = divmod(microseconds, 1_000_000)
        self.iter.advance(current)
        self.pixbuf = self.iter.get_pixbuf()
        self.frame_id += 1
        delay = self.iter.get_delay_time()
        if not delay or delay <= 0:
            delay = 80
        self.next_due = now + max(20, delay) / 1000.0
        return True


class AnimationLibrary:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.clips = {}
        for name, filename in CLIP_FILES.items():
            path = self.directory / filename
            if path.is_file():
                try:
                    self.clips[name] = GifClip(name, path)
                except Exception:
                    pass

    def available(self):
        return bool(self.clips)

    def player(self, name):
        clip = self.clips.get(name)
        if clip is None:
            return None
        return clip.player()

    def has(self, name):
        return name in self.clips
