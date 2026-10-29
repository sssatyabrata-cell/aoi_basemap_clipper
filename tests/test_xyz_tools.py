# -*- coding: utf-8 -*-
"""Pure-Python tests (no QGIS / GDAL needed):  python -m unittest discover -s tests"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from aoi_basemap_clipper import xyz_tools as X  # noqa: E402


class ParseTests(unittest.TestCase):
    def test_osm(self):
        p = X.parse_xyz_source("type=xyz&url=https://tile.openstreetmap.org/%7Bz%7D/%7Bx%7D/%7By%7D.png&zmax=19&zmin=0")
        self.assertEqual(p["url"], "https://tile.openstreetmap.org/{z}/{x}/{y}.png")
        self.assertEqual((p["zmin"], p["zmax"]), (0, 19))

    def test_default_zmax(self):
        p = X.parse_xyz_source("type=xyz&url=https://a/%7Bz%7D/%7Bx%7D/%7By%7D")
        self.assertEqual(p["zmax"], 18)

    def test_auth_and_headers(self):
        p = X.parse_xyz_source("type=xyz&url=https://a/%7Bz%7D/%7Bx%7D/%7By%7D&authcfg=abc123&http-header:referer=https://x.y")
        self.assertEqual(p["authcfg"], "abc123")
        self.assertEqual(p["headers"], {"referer": "https://x.y"})

    def test_unsupported_falls_back(self):
        self.assertIsNone(X.parse_xyz_source("type=xyz&url=https://a/%7Bq%7D.jpg"))
        self.assertIsNone(X.parse_xyz_source("type=xyz&url=https://%7Bs%7D.a/%7Bz%7D/%7Bx%7D/%7By%7D"))
        self.assertIsNone(X.parse_xyz_source("type=xyz&url=file:///tiles/%7Bz%7D/%7Bx%7D/%7By%7D.png"))
        self.assertIsNone(X.parse_xyz_source("url=https://a/%7Bz%7D/%7Bx%7D/%7By%7D"))   # not xyz

    def test_plus_sign_is_kept(self):
        p = X.parse_xyz_source("type=xyz&url=https://a/%7Bz%7D/%7Bx%7D/%7By%7D?k=a+b&zmax=17")
        self.assertEqual(p["url"], "https://a/{z}/{x}/{y}?k=a+b")
        self.assertEqual(p["zmax"], 17)

    def test_format_url_tms(self):
        self.assertEqual(X.format_url("u/{z}/{x}/{y}", 3, 2, 1), "u/3/2/1")
        self.assertEqual(X.format_url("u/{z}/{x}/{-y}", 3, 2, 1), "u/3/2/6")


class GridTests(unittest.TestCase):
    def test_pixel_size(self):
        self.assertAlmostEqual(X.zoom_pixel_size(0), X.WEB_MERCATOR_RES0, places=6)
        self.assertAlmostEqual(X.zoom_pixel_size(1, 512), X.zoom_pixel_size(2), places=9)   # @2x tiles

    def test_tile_for_point_and_bounds_roundtrip(self):
        for z in (0, 5, 18):
            tx, ty = X.tile_for_point(123456.0, 654321.0, z)
            x0, y0, x1, y1 = X.tile_bounds(tx, ty, z)
            self.assertTrue(x0 <= 123456.0 <= x1 and y0 <= 654321.0 <= y1)

    def test_plan_covers_window_exactly_once(self):
        tile_px, z = 256, 6
        c0, r0, w, h = 1000, 2100, 700, 530          # arbitrary window
        covered = {}
        for tx, ty, dx, dy, sx, sy, cw, ch in X.iter_tile_plan(c0, r0, w, h, tile_px, z):
            self.assertTrue(0 <= sx and sx + cw <= tile_px and 0 <= sy and sy + ch <= tile_px)
            for yy in range(dy, dy + ch):
                for xx in range(dx, dx + cw):
                    self.assertNotIn((xx, yy), covered)
                    # the world pixel this destination pixel represents
                    self.assertEqual(c0 + xx, tx * tile_px + sx + (xx - dx))
                    self.assertEqual(r0 + yy, ty * tile_px + sy + (yy - dy))
                    covered[(xx, yy)] = 1
        self.assertEqual(len(covered), w * h)

    def test_plan_skips_tiles_outside_world(self):
        plan = list(X.iter_tile_plan(-300, 0, 600, 256, 256, 2))   # x < 0 is outside the world
        self.assertTrue(all(p[0] >= 0 for p in plan))


if __name__ == "__main__":
    unittest.main()
