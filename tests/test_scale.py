import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "linux"))

from ds_pet.scale import bottom_right_margin, effective_scale, mode_canvas, mode_geometry, normalize_mode_factor, normalize_scale, window_geometry


class ScaleTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_scale(0.3), 0.3)
        self.assertEqual(normalize_scale("0.5"), 0.5)
        self.assertEqual(normalize_scale(1), 1.0)

    def test_invalid(self):
        for value in (0, 0.05, 3.01, -1, "abc", None):
            with self.assertRaises(ValueError):
                normalize_scale(value)

    def test_geometry(self):
        width, height, scale_x, scale_y = window_geometry(0.3)
        self.assertEqual((width, height), (102, 178))
        self.assertAlmostEqual(scale_x, 102 / 340)
        self.assertAlmostEqual(scale_y, 178 / 594)
        width, height, scale_x, scale_y = window_geometry(1.0)
        self.assertEqual((width, height), (340, 594))
        self.assertEqual((scale_x, scale_y), (1.0, 1.0))

    def test_mode_geometry(self):
        width, height, scale_x, scale_y = mode_geometry("compact", 1.0)
        self.assertEqual((width, height), (416, 480))
        self.assertAlmostEqual(scale_x, 1.6)
        self.assertAlmostEqual(scale_y, 1.6)
        width, height, _, _ = mode_geometry("compact", 0.5)
        self.assertEqual((width, height), (208, 240))
        width, height, _, _ = mode_geometry("compact", 1.0, 2.0)
        self.assertEqual((width, height), (520, 600))
        self.assertEqual(mode_canvas("classic"), (340, 594))

    def test_effective_scale_and_area(self):
        self.assertEqual(effective_scale("classic", 0.3), 0.3)
        self.assertAlmostEqual(effective_scale("compact", 0.3), 0.48)
        classic = window_geometry(1.0)
        compact = mode_geometry("compact", 1.0)
        classic_area = classic[0] * classic[1]
        compact_area = compact[0] * compact[1]
        self.assertLess(abs(classic_area - compact_area) / classic_area, 0.02)

    def test_mode_factor_validation(self):
        self.assertEqual(normalize_mode_factor("1.6"), 1.6)
        for value in (0, 0.05, 5.01, "bad", None):
            with self.assertRaises(ValueError):
                normalize_mode_factor(value)

    def test_margin(self):
        self.assertEqual(bottom_right_margin(1.0), 40)
        self.assertEqual(bottom_right_margin(0.3), 12)
        self.assertEqual(bottom_right_margin(0.1), 4)


if __name__ == "__main__":
    unittest.main()
