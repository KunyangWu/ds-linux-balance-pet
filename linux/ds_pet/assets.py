from pathlib import Path

import gi

gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf

from .render_constants import SPRITE_H, SPRITE_W

ALPHA_THRESHOLD = 8
EXPRESSIONS = ("happy", "sad", "sleepy", "greeting", "relaxed", "hungry", "request")


class AlphaShape:
    __slots__ = ("runs", "bounds")

    def __init__(self, runs, bounds):
        self.runs = runs
        self.bounds = bounds


def alpha_shape(pixbuf, threshold=ALPHA_THRESHOLD):
    width = pixbuf.get_width()
    height = pixbuf.get_height()
    channels = pixbuf.get_n_channels()
    stride = pixbuf.get_rowstride()
    pixels = pixbuf.get_pixels()
    if channels < 4:
        return AlphaShape([(y, 0, width) for y in range(height)], (0, 0, width, height))
    runs = []
    min_x = width
    min_y = height
    max_x = -1
    max_y = -1
    for y in range(height):
        start = y * stride
        row = pixels[start:start + width * 4]
        x = 0
        while x < width:
            if row[x * 4 + 3] > threshold:
                begin = x
                x += 1
                while x < width and row[x * 4 + 3] > threshold:
                    x += 1
                runs.append((y, begin, x))
                if begin < min_x:
                    min_x = begin
                if x > max_x:
                    max_x = x
                if y < min_y:
                    min_y = y
                if y > max_y:
                    max_y = y
            else:
                x += 1
    if max_x < 0:
        return AlphaShape(runs, (0, 0, width, height))
    return AlphaShape(runs, (min_x, min_y, max_x - min_x, max_y - min_y))


class Art:
    def __init__(self, asset_dir):
        self.asset_dir = Path(asset_dir)
        self.look_files = self._discover_looks()
        if not self.look_files:
            raise RuntimeError(f"缺少立绘：{self.asset_dir}")
        self.sprites = {}
        self.shapes = {}
        self.look_index = 0
        self.subject_bounds = (0, 0, SPRITE_W, SPRITE_H)
        self.sleepy_bounds = None
        self.working_atlas = None
        self.working_frames = []
        self.working_shapes = []
        self.working_cell_w = 0
        self.working_cell_h = 0
        self.working_max_width = 1
        self._load_working_atlas()
        self._load_expressions()
        self.set_look(self.look_files[0])

    def _discover_looks(self):
        files = []
        base = self.asset_dir / "pet-base.png"
        if base.is_file():
            files.append(base)
        for path in sorted(self.asset_dir.glob("pet-look-*.png")):
            if path.stem.endswith("-normal") or path.stem.endswith("-blink"):
                continue
            files.append(path)
        return files

    def _load_working_atlas(self):
        path = self.asset_dir / "pet-working-atlas-v1.png"
        if not path.is_file():
            return
        self.working_atlas = GdkPixbuf.Pixbuf.new_from_file(str(path))
        self.working_cell_w = self.working_atlas.get_width() // 2
        self.working_cell_h = self.working_atlas.get_height() // 2
        for index in range(4):
            x = (index % 2) * self.working_cell_w
            y = (index // 2) * self.working_cell_h
            frame = self.working_atlas.new_subpixbuf(x, y, self.working_cell_w, self.working_cell_h)
            shape = alpha_shape(frame)
            self.working_frames.append(frame)
            self.working_shapes.append(shape)
            if shape.bounds[2] > self.working_max_width:
                self.working_max_width = shape.bounds[2]

    def _load_expressions(self):
        for name in EXPRESSIONS:
            path = self.asset_dir / f"pet-{name}.png"
            if not path.is_file():
                continue
            pixbuf = GdkPixbuf.Pixbuf.new_from_file(str(path))
            self.sprites[name] = pixbuf
            self.shapes[name] = alpha_shape(pixbuf)
        sleepy = self.sprites.get("sleepy")
        if sleepy is not None:
            shape = self.shapes["sleepy"]
            bounds = shape.bounds
            self.sleepy_bounds = (
                bounds[0] * SPRITE_W / sleepy.get_width(),
                bounds[1] * SPRITE_H / sleepy.get_height(),
                bounds[2] * SPRITE_W / sleepy.get_width(),
                bounds[3] * SPRITE_H / sleepy.get_height(),
            )

    def frame_paths(self, look_path):
        look_path = Path(look_path)
        prefix = "pet" if look_path.stem == "pet-base" else look_path.stem
        frames = {}
        for name in ("normal", "blink"):
            path = look_path.parent / f"{prefix}-{name}.png"
            if path.is_file():
                frames[name] = path
        return frames

    def set_look(self, look_path):
        look_path = Path(look_path)
        try:
            index = self.look_files.index(look_path)
        except ValueError:
            return False
        self.look_index = index
        for key in ("base", "normal", "blink"):
            self.sprites.pop(key, None)
            self.shapes.pop(key, None)
        base = GdkPixbuf.Pixbuf.new_from_file(str(look_path))
        self.sprites["base"] = base
        self.shapes["base"] = alpha_shape(base)
        for name, path in self.frame_paths(look_path).items():
            pixbuf = GdkPixbuf.Pixbuf.new_from_file(str(path))
            self.sprites[name] = pixbuf
            self.shapes[name] = alpha_shape(pixbuf)
        normal = self.sprites.get("normal") or self.sprites["base"]
        self.subject_bounds = alpha_shape(normal).bounds
        return True

    def look_name(self):
        return self.look_files[self.look_index].name

    def set_look_name(self, name):
        for path in self.look_files:
            if path.name == name:
                return self.set_look(path)
        return False

    def next_look(self):
        if not self.look_files:
            return False
        return self.set_look(self.look_files[(self.look_index + 1) % len(self.look_files)])

    def sprite_for(self, expression):
        if expression == "blink" and "blink" not in self.sprites:
            return self.sprites.get("base")
        return self.sprites.get(expression) or self.sprites.get("base")

    def shape_for(self, expression):
        if expression == "working":
            return None
        if expression == "blink" and "blink" not in self.shapes:
            expression = "normal"
        return self.shapes.get(expression) or self.shapes.get("base")
