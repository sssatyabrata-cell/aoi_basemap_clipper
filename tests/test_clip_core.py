# -*- coding: utf-8 -*-
"""GDAL tests - run them inside QGIS' Python (or any env with GDAL + numpy):
    python -m unittest discover -s tests
They are skipped automatically when GDAL is missing."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
try:
    import numpy as np
    from osgeo import gdal, ogr, osr
    from aoi_basemap_clipper import clip_core as core
    from aoi_basemap_clipper import xyz_tools as X
except ImportError:                                           # pragma: no cover
    core = None


def _wkt(epsg):
    s = osr.SpatialReference()
    s.ImportFromEPSG(epsg)
    return s.ExportToWkt()


@unittest.skipIf(core is None, "GDAL / numpy not available")
class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def _src(self, name="src.tif", bands=3, palette=False):
        path = os.path.join(self.dir, name)
        ds = gdal.GetDriverByName("GTiff").Create(path, 100, 100, 1 if palette else bands, gdal.GDT_Byte)
        ds.SetGeoTransform((500000, 1, 0, 4000100, 0, -1))
        ds.SetProjection(_wkt(32633))
        rng = np.random.RandomState(1)
        for b in range(1, ds.RasterCount + 1):
            ds.GetRasterBand(b).WriteArray(rng.randint(0, 5 if palette else 256, (100, 100)).astype("uint8"))
        if palette:
            ct = gdal.ColorTable()
            for i in range(5):
                ct.SetColorEntry(i, (i * 50, 255 - i * 50, 10 * i, 255))
            ds.GetRasterBand(1).SetRasterColorTable(ct)
        ds = None
        return path

    def _mask(self, wkt_poly, epsg=32633):
        path = os.path.join(self.dir, "mask.gpkg")
        g = ogr.CreateGeometryFromWkt(wkt_poly)
        core.write_mask(path, _wkt(epsg), [g.ExportToWkb()])
        return path

    def test_exact_clip_is_bit_identical(self):
        src = self._src()
        mask = self._mask("POLYGON((500010 4000010,500060 4000010,500060 4000060,500010 4000060,500010 4000010))")
        out = os.path.join(self.dir, "out.tif")
        info = core.clip_to_aoi(src, mask, "aoi_mask", (500010, 4000010, 500060, 4000060), out)
        self.assertTrue(info["exact"])
        a = gdal.Open(src).ReadAsArray(10, 40, 50, 50)           # same window in the source
        b = gdal.Open(out).ReadAsArray()[:3]
        self.assertTrue(np.array_equal(a, b))

    def test_palette_gets_alpha_and_nearest(self):
        src = self._src("pal.tif", palette=True)
        mask = self._mask("POLYGON((500010 4000010,500060 4000010,500010 4000060,500010 4000010))")   # triangle
        out = os.path.join(self.dir, "pal_out.tif")
        info = core.clip_to_aoi(src, mask, "aoi_mask", (500010, 4000010, 500060, 4000060), out)
        self.assertTrue(info["palette_expanded"])
        ds = gdal.Open(out)
        self.assertEqual(ds.RasterCount, 4)
        alpha = ds.GetRasterBand(4).ReadAsArray()
        self.assertTrue((alpha == 0).any() and (alpha == 255).any())

    def test_reproject_runs_and_reports_resampling(self):
        src = self._src()
        mask = self._mask("POLYGON((500010 4000010,500060 4000010,500060 4000060,500010 4000060,500010 4000010))")
        out = os.path.join(self.dir, "rep.tif")
        s_src, s_dst = osr.SpatialReference(), osr.SpatialReference()
        s_src.ImportFromEPSG(32633)
        s_dst.ImportFromEPSG(4326)
        for s in (s_src, s_dst):
            s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        t = osr.CoordinateTransformation(s_src, s_dst)
        x0, y0, _ = t.TransformPoint(500010, 4000010)
        x1, y1, _ = t.TransformPoint(500060, 4000060)
        info = core.clip_to_aoi(src, mask, "aoi_mask", (500010, 4000010, 500060, 4000060), out,
                                dst_srs_wkt=_wkt(4326), extent_dst=(min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)))
        self.assertTrue(info["reprojected"])
        self.assertEqual(info["resample"], "cubic")

    def test_xyz_download_with_fake_server(self):
        z, tile_px = 2, 256
        px = X.zoom_pixel_size(z, tile_px)
        calls = []

        def fetch(url):
            calls.append(url)
            return url.encode()

        def decode(raw):
            tx, ty = [int(v) for v in raw.decode().split("/")[-2:]]
            arr = np.zeros((tile_px, tile_px, 4), np.uint8)
            arr[..., 0], arr[..., 1], arr[..., 3] = tx * 10, ty * 10, 255
            return arr

        out = os.path.join(self.dir, "x.tif")
        w = h = 2 * tile_px
        x0, y_top = -X.WEB_MERCATOR_ORIGIN + tile_px * px, X.WEB_MERCATOR_ORIGIN - tile_px * px   # tiles (1..2, 1..2)
        stats = core.fetch_xyz_to_geotiff("http://s/{z}/{x}/{y}", z, tile_px, out, _wkt(3857),
                                          x0, y_top, px, w, h, fetch, decode, workers=4)
        self.assertEqual(stats["tiles"], 4)
        self.assertEqual(len(calls), 4)
        arr = gdal.Open(out).ReadAsArray()
        self.assertEqual(int(arr[0, 0, 0]), 10)                  # red = tx*10 of tile (1, 1)
        self.assertEqual(int(arr[0, 0, tile_px]), 20)            # tile (2, 1)
        self.assertEqual(int(arr[1, tile_px, 0]), 20)            # green = ty*10 of tile (1, 2)

    def test_tiles_and_relative_index(self):
        src = self._src()
        tiles = os.path.join(self.dir, "t")
        recs = core.split_into_tiles(src, tiles, "c", 40, 40, skip_empty=False, workers=2)
        self.assertEqual(len(recs), 9)
        idx = os.path.join(self.dir, "i.gpkg")
        core.write_tile_index(recs, idx, _wkt(32633), location_prefix="c_tiles/")
        lyr = ogr.Open(idx).GetLayer(0)
        first = lyr.GetNextFeature()
        self.assertFalse(os.path.isabs(first.GetField("location")))


if __name__ == "__main__":
    unittest.main()
