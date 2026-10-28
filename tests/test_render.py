import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "linux"))

from ds_pet.animations import AnimationLibrary
from ds_pet.assets import Art
from ds_pet.render import Renderer
from ds_pet.render_constants import CANVAS_H, CANVAS_W


class RenderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.assets = Path(__file__).resolve().parents[1] / "linux" / "assets"
        cls.art = Art(cls.assets)
        cls.renderer = Renderer(cls.art, "Noto Sans CJK SC")
        cls.animations = AnimationLibrary(cls.assets / "dsh-pet")

    def test_looks_and_frames(self):
        self.assertEqual(len(self.art.look_files), 19)
        self.assertIsNotNone(self.art.working_atlas)
        self.assertEqual(len(self.art.working_frames), 4)
        self.assertTrue(self.art.next_look())
        self.assertTrue(self.art.set_look_name("pet-base.png"))

    def test_idle_frame(self):
        frame = self.renderer.render("normal", "", "¥8.47", "ok", 0, 0, False, "")
        self.assertEqual(frame.surface.get_width(), CANVAS_W)
        self.assertEqual(frame.surface.get_height(), CANVAS_H)
        self.assertTrue(frame.input_rects)
        for x, y, width, height in frame.input_rects:
            self.assertGreaterEqual(x, 0)
            self.assertGreaterEqual(y, 0)
            self.assertLessEqual(width, CANVAS_W)
            self.assertLessEqual(height, CANVAS_H)

    def test_animation_library(self):
        self.assertEqual(len(self.animations.clips), 15)
        for clip in self.animations.clips.values():
            self.assertEqual((clip.width, clip.height), (220, 124))
        player = self.animations.player("idle")
        first = player.frame_id
        player.next_due = 0
        self.assertTrue(player.update())
        self.assertGreater(player.frame_id, first)

    def test_compact_frame(self):
        self.assertTrue(self.animations.available())
        player = self.animations.player("idle")
        frame = self.renderer.render_compact(
            player.pixbuf,
            "余额还有 ¥8.47 哦～",
            "¥8.47",
            "ok",
            True,
            "用量 1.4M｜缓存 92%｜0.0 tok/s",
        )
        self.assertEqual(frame.surface.get_width(), 260)
        self.assertEqual(frame.surface.get_height(), 300)
        self.assertTrue(frame.input_rects)

    def test_bubble_and_working_frame(self):
        frame = self.renderer.render("working", "查询中...", "¥8.47", "ok", 2, 0, True, "用量 80k｜缓存 66%｜1.2k tok/s")
        self.assertGreater(frame.surface.get_width(), 0)
        self.assertTrue(any(rect[1] > 480 for rect in frame.input_rects))


if __name__ == "__main__":
    unittest.main()
