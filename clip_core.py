# -*- coding: utf-8 -*-
"""
AOI Basemap Clipper - processing core (pure GDAL / OGR / numpy, no QGIS).

Design rules
------------
1. Exact pixel grid. Rendered / downloaded rasters are produced on an explicit
   grid (origin, square pixel, integer size). XYZ layers are anchored to the
   tile grid, so one output pixel == one source tile pixel.
2. Direct-from-file clipping. File rasters are never re-rendered: they are cut
   with ONE gdal.Warp on the source's own pixel grid (nearest neighbour,
   bit-identical pixels, native bit depth / band layout).
3. At most one resampling step; clip + reproject happen in the same warp.
4. Real transparency (alpha band / source nodata). Palette rasters are expanded
   to RGBA first so the area outside the AOI is genuinely transparent and
   colours are never interpolated as class indices.
5. Speed: parallel tile download, multi-threaded warp + compression, parallel
   tiling, uncompressed sparse temporary files, tiles outside the AOI are never
   fetched or rendered.
6. No global GDAL state: this module does NOT call gdal.UseExceptions() (that
   would change error handling for every other QGIS plugin). Every call checks
   its result and also tolerates exceptions being enabled by someone else.
"""

import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED

import numpy as np
from osgeo import gdal, ogr, osr

from . import xyz_tools
from .xyz_tools import WEB_MERCATOR_ORIGIN, WEB_MERCATOR_RES0  # noqa: F401

_EPS = 1e-4          # tolerance (pixels) when snapping extents to a grid


class Cancelled(Exception):
    """Raised when the user pressed Cancel."""


# ---------------------------------------------------------------- helpers
def _last_error(default="unknown error"):
    msg = gdal.GetLastErrorMsg()
    return msg if msg else default


def open_ds(path, update=False):
    """gdal.Open that always raises ValueError on failure (any GDAL mode)."""
    try:
        ds = gdal.Open(path, gdal.GA_Update if update else gdal.GA_ReadOnly)
    except RuntimeError as exc:
        raise ValueError("Cannot open %s: %s" % (path, exc))
    if ds is None:
        raise ValueError("Cannot open %s: %s" % (path, _last_error()))
    return ds


def zoom_pixel_size(zoom):
    return xyz_tools.zoom_pixel_size(zoom, 256)


def snap_extent(xmin, ymin, xmax, ymax, px, py, origin_x, origin_y):
    """Snap an extent *outwards* to the pixel grid whose top-left corner is
    (origin_x, origin_y) with pixel size (px, py).
    Returns (x0, y_top, width_px, height_px)."""
    c0 = math.floor((xmin - origin_x) / px + _EPS)
    c1 = math.ceil((xmax - origin_x) / px - _EPS)
    r0 = math.floor((origin_y - ymax) / py + _EPS)
    r1 = math.ceil((origin_y - ymin) / py - _EPS)
    width = max(1, int(c1 - c0))
    height = max(1, int(r1 - r0))
    return origin_x + c0 * px, origin_y - r0 * py, width, height


def zstd_available():
    drv = gdal.GetDriverByName("GTiff")
    opts = drv.GetMetadataItem("DMD_CREATIONOPTIONLIST") if drv else ""
    return "ZSTD" in (opts or "")


def creation_options(compress="DEFLATE_FAST", dtype=gdal.GDT_Byte,
                     threads=True):
    """GeoTIFF creation options.

    compress: DEFLATE_FAST (default, level 1) | DEFLATE | ZSTD | LZW | NONE
    ``threads`` enables multi-threaded compression (NUM_THREADS=ALL_CPUS).
    """
    key = (compress or "DEFLATE_FAST").upper()
    opts = ["TILED=YES", "BIGTIFF=IF_SAFER"]
    if threads:
        opts.append("NUM_THREADS=ALL_CPUS")
    if key == "NONE":
        return opts
    if key == "ZSTD" and not zstd_available():
        key = "DEFLATE_FAST"
    if key in ("DEFLATE_FAST", "DEFLATE"):
        opts += ["COMPRESS=DEFLATE", "ZLEVEL=%d" % (1 if key == "DEFLATE_FAST" else 6)]
    elif key == "ZSTD":
        opts += ["COMPRESS=ZSTD", "ZSTD_LEVEL=5"]
    elif key == "LZW":
        opts.append("COMPRESS=LZW")
    else:
        opts += ["COMPRESS=DEFLATE", "ZLEVEL=1"]
    if dtype in (gdal.GDT_Byte, gdal.GDT_UInt16, gdal.GDT_Int16,
                 gdal.GDT_UInt32, gdal.GDT_Int32):
        opts.append("PREDICTOR=2")
    elif dtype in (gdal.GDT_Float32, gdal.GDT_Float64):
        opts.append("PREDICTOR=3")
    return opts


def _same_srs(wkt_a, wkt_b):
    if not wkt_a or not wkt_b:
        return False
    a, b = osr.SpatialReference(), osr.SpatialReference()
    a.ImportFromWkt(wkt_a)
    b.ImportFromWkt(wkt_b)
    return bool(a.IsSame(b))


def _traditional(srs):
    if hasattr(srs, "SetAxisMappingStrategy") and hasattr(osr, "OAMS_TRADITIONAL_GIS_ORDER"):
        srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    return srs


def _progress_cb(progress, cancel):
    """Build a GDAL callback from our (progress, cancel) callables."""
    state = {"cancelled": False}

    def cb(complete, message, data):
        if cancel is not None and cancel():
            state["cancelled"] = True
            return 0
        if progress is not None:
            progress(complete)
        return 1

    return cb, state


def copy_metadata(src_ds, dst_ds, extra=None):
    """Copy (not replace) the default + IMAGERY metadata domains."""
    for domain in ("", "IMAGERY"):
        md = src_ds.GetMetadata(domain)
        if isinstance(md, dict):
            for k, v in md.items():
                dst_ds.SetMetadataItem(k, v, domain)
    for k, v in (extra or {}).items():
        dst_ds.SetMetadataItem(k, str(v))


def _write_rgba(ds, arr, xoff, yoff):
    """Write an (h, w, 4) uint8 array with ONE call (pixel-interleaved)."""
    h, w = arr.shape[:2]
    buf = np.ascontiguousarray(arr).tobytes()
    try:
        ds.WriteRaster(xoff, yoff, w, h, buf, buf_pixel_space=4,
                       buf_line_space=4 * w, buf_band_space=1)
        return
    except (AttributeError, TypeError):
        pass                                    # very old bindings
    for b in range(4):
        ds.GetRasterBand(b + 1).WriteArray(
            np.ascontiguousarray(arr[:, :, b]), xoff, yoff)


def _create_rgba_tiff(path, width, height, srs_wkt, x0, y_top, px, py):
    """Uncompressed, tiled, sparse RGBA GeoTIFF (fastest temp format)."""
    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(path, width, height, 4, gdal.GDT_Byte,
                    ["TILED=YES", "BLOCKXSIZE=256", "BLOCKYSIZE=256",
                     "BIGTIFF=IF_SAFER", "SPARSE_OK=TRUE",
                     "PHOTOMETRIC=RGB", "ALPHA=YES"])
    if ds is None:
        raise RuntimeError("Cannot create %s: %s" % (path, _last_error()))
    ds.SetGeoTransform((x0, px, 0.0, y_top, 0.0, -py))
    ds.SetProjection(srs_wkt)
    for i, ci in enumerate((gdal.GCI_RedBand, gdal.GCI_GreenBand,
                            gdal.GCI_BlueBand, gdal.GCI_AlphaBand), 1):
        ds.GetRasterBand(i).SetColorInterpretation(ci)
    return ds


# ------------------------------------------------------- AOI mask (OGR)
def write_mask(path, srs_wkt, wkb_geometries, layer_name="aoi_mask"):
    """Write AOI polygons (already in the CRS ``srs_wkt``) as a GPKG cutline."""
    drv = ogr.GetDriverByName("GPKG")
    if os.path.exists(path):
        drv.DeleteDataSource(path)
    ds = drv.CreateDataSource(path)
    if ds is None:
        raise RuntimeError("Cannot create AOI mask: %s" % _last_error())
    srs = osr.SpatialReference()
    srs.ImportFromWkt(srs_wkt)
    _traditional(srs)
    lyr = ds.CreateLayer(layer_name, srs, ogr.wkbMultiPolygon)
    count = 0
    for wkb in wkb_geometries:
        geom = ogr.CreateGeometryFromWkb(bytes(wkb))
        if geom is None or geom.IsEmpty():
            continue
        geom.FlattenTo2D()                       # drop Z / M
        if geom.HasCurveGeometry():              # CurvePolygon etc. -> plain polygons
            geom = geom.GetLinearGeometry()
        geom = ogr.ForceToMultiPolygon(geom)
        feat = ogr.Feature(lyr.GetLayerDefn())
        feat.SetGeometry(geom)
        lyr.CreateFeature(feat)
        count += 1
    ds = None
    if count == 0:
        raise ValueError("The AOI layer has no usable polygon geometry.")
    return layer_name


def make_block_filter(mask_path, layer_name="aoi_mask"):
    """f(xmin, ymin, xmax, ymax) -> bool: does the box touch the AOI?
    Lets renderers / downloaders skip everything outside the AOI shape."""
    ds = ogr.Open(mask_path)
    if ds is None:
        raise RuntimeError("Cannot open AOI mask: %s" % _last_error())
    lyr = ds.GetLayerByName(layer_name)
    multi = ogr.Geometry(ogr.wkbMultiPolygon)
    for feat in lyr:
        g = feat.GetGeometryRef()
        if g is None or g.IsEmpty():
            continue
        g = ogr.ForceToMultiPolygon(g.Clone())
        for i in range(g.GetGeometryCount()):
            multi.AddGeometry(g.GetGeometryRef(i))
    ds = None
    union = multi
    if multi.GetGeometryCount() > 1:
        try:
            union = multi.UnionCascaded() or multi
        except Exception:
            union = multi

    def touches(xmin, ymin, xmax, ymax):
        ring = ogr.Geometry(ogr.wkbLinearRing)
        for x, y in ((xmin, ymin), (xmax, ymin), (xmax, ymax),
                     (xmin, ymax), (xmin, ymin)):
            ring.AddPoint_2D(x, y)
        box = ogr.Geometry(ogr.wkbPolygon)
        box.AddGeometry(ring)
        try:
            return bool(union.Intersects(box))
        except Exception:
            return True

    return touches


# ------------------------------------------- fast path 1: XYZ downloader
def fetch_xyz_to_geotiff(url_template, zoom, tile_px, out_path, srs_wkt,
                         x0, y_top, px, width, height, fetch, decode,
                         block_filter=None, workers=16, retries=3,
                         progress=None, cancel=None):
    """Download XYZ tiles in parallel and write them straight into a GeoTIFF.

    fetch(url)  -> bytes, or None for "no such tile" (404 / empty)
    decode(raw) -> uint8 array (h, w, 4) RGBA, h == w == tile_px
    The window (x0, y_top, width, height) MUST lie on the tile grid at
    ``zoom`` (use snap_extent with origin (-ORIGIN, +ORIGIN)).
    Only tiles that touch the AOI (block_filter) are requested.
    """
    c0 = int(round((x0 + WEB_MERCATOR_ORIGIN) / px))
    r0 = int(round((WEB_MERCATOR_ORIGIN - y_top) / px))

    jobs = []
    for plan in xyz_tools.iter_tile_plan(c0, r0, width, height, tile_px, zoom):
        tx, ty = plan[0], plan[1]
        if block_filter is not None:
            if not block_filter(*xyz_tools.tile_bounds(tx, ty, zoom)):
                continue
        jobs.append(plan)
    total_all = sum(1 for _ in xyz_tools.iter_tile_plan(
        c0, r0, width, height, tile_px, zoom))
    stats = {"tiles": len(jobs), "outside_aoi": total_all - len(jobs),
             "missing": 0, "failed": 0}

    ds = _create_rgba_tiff(out_path, width, height, srs_wkt, x0, y_top, px, px)
    try:
        if not jobs:
            ds = None
            return stats

        def task(job):
            tx, ty, dx, dy, sx, sy, w, h = job
            url = xyz_tools.format_url(url_template, zoom, tx, ty)
            last = None
            for attempt in range(retries + 1):
                if cancel is not None and cancel():
                    return None
                try:
                    raw = fetch(url)
                    last = None
                    break
                except Exception as exc:                       # network hiccup
                    last = exc
                    if attempt < retries:
                        time.sleep(min(2.0, 0.3 * (2 ** attempt)))
            if last is not None:
                raise last
            if not raw:
                return "missing"
            arr = decode(raw)
            if arr.shape[0] != tile_px or arr.shape[1] != tile_px:
                raise ValueError("Tile %s is %dx%d, expected %d" % (
                    url, arr.shape[1], arr.shape[0], tile_px))
            return arr[sy:sy + h, sx:sx + w, :]

        done = 0
        failures = []
        window = max(workers * 4, 8)
        ex = ThreadPoolExecutor(max_workers=max(1, int(workers)))
        inflight = {}
        job_iter = iter(jobs)

        def fill():
            while len(inflight) < window:
                job = next(job_iter, None)
                if job is None:
                    return
                inflight[ex.submit(task, job)] = job

        try:
            fill()
            while inflight:
                if cancel is not None and cancel():
                    for f in inflight:
                        f.cancel()
                    raise Cancelled()
                finished, _ = wait(list(inflight), timeout=0.2,
                                   return_when=FIRST_COMPLETED)
                for f in finished:
                    job = inflight.pop(f)
                    try:
                        res = f.result()
                    except Exception as exc:
                        failures.append((job, exc))
                        if len(failures) >= 25 and len(failures) > done:
                            for g in inflight:
                                g.cancel()
                            raise RuntimeError(
                                "The server keeps refusing tiles (%d failures, %d ok). "
                                "First error: %s. Lower 'Download threads' or check "
                                "your connection / licence." % (len(failures), done, exc))
                        continue
                    if isinstance(res, str):
                        stats["missing"] += 1
                    elif res is not None:
                        _write_rgba(ds, res, job[2], job[3])
                    done += 1
                    if progress is not None:
                        progress(done / float(len(jobs)))
                fill()
        finally:
            ex.shutdown(wait=True)

        # one sequential second chance for tiles that failed in the pool
        still = []
        for job, exc in failures:
            if cancel is not None and cancel():
                ds = None
                raise Cancelled()
            try:
                res = task(job)
                if isinstance(res, str):
                    stats["missing"] += 1
                elif res is not None:
                    _write_rgba(ds, res, job[2], job[3])
            except Exception as exc2:
                still.append((job, exc2))
        ds.FlushCache()
        ds = None
        stats["failed"] = len(still)
        if still:
            job, exc = still[0]
            raise RuntimeError(
                "%d of %d tiles could not be downloaded (first error: %s)"
                % (len(still), len(jobs), exc))
        return stats
    finally:
        ds = None                                   # always release the file


# --------------------------------------- fallback: block renderer (WMS...)
def render_to_geotiff(render_block, out_path, srs_wkt, x0, y_top, px, py,
                      width, height, block=2048, progress=None, cancel=None,
                      render_batch=None, batch_size=1, block_filter=None):
    """Render an image on an exact pixel grid, block by block.

    ``render_batch(list_of_extents) -> list_of_arrays`` renders several blocks
    concurrently; every extent is exactly w*px by h*py so no aspect-ratio
    adjustment (shift / squeeze) can happen. Blocks outside the AOI are never
    rendered and stay transparent (sparse temp file).
    """
    ds = _create_rgba_tiff(out_path, width, height, srs_wkt, x0, y_top, px, py)

    specs, total = [], 0
    for by in range(0, height, block):
        bh = min(block, height - by)
        for bx in range(0, width, block):
            total += 1
            bw = min(block, width - bx)
            xmin = x0 + bx * px
            xmax = xmin + bw * px
            ymax = y_top - by * py
            ymin = ymax - bh * py
            if block_filter is not None and not block_filter(
                    xmin - px, ymin - py, xmax + px, ymax + py):
                continue
            specs.append((bx, by, (xmin, ymin, xmax, ymax, bw, bh)))
    skipped = total - len(specs)
    done = skipped
    if progress is not None:
        progress(done / float(total))

    batch_size = max(1, int(batch_size))
    try:
        for i in range(0, len(specs), batch_size):
            if cancel is not None and cancel():
                raise Cancelled()
            group = specs[i:i + batch_size]
            extents = [g[2] for g in group]
            if render_batch is not None:
                arrays = render_batch(extents)
            else:
                arrays = [render_block(*e) for e in extents]
            for (bx, by, e), arr in zip(group, arrays):
                bw, bh = e[4], e[5]
                if arr.shape != (bh, bw, 4):
                    raise RuntimeError("Renderer returned %s, expected %s"
                                       % (arr.shape, (bh, bw, 4)))
                _write_rgba(ds, arr, bx, by)
                done += 1
            if progress is not None:
                progress(done / float(total))
        ds.FlushCache()
    finally:
        ds = None                                   # always release the file
    return {"blocks": total, "skipped": skipped}


# --------------------------------------------------------------- clipping
def _auto_resample(first_band, dtype, band_count):
    """Pick a resampler for 'auto': never interpolate class data."""
    try:
        if first_band.GetDefaultRAT() is not None:
            return "near"
    except Exception:
        pass
    if dtype in (gdal.GDT_Byte, gdal.GDT_UInt16):
        return "cubic"
    return "bilinear"


def clip_to_aoi(src_path, mask_path, mask_layer, extent, out_path,
                dst_srs_wkt=None, extent_dst=None, res_scale=1.0,
                resample="auto", compress="DEFLATE_FAST",
                src_srs_override=None, extra_metadata=None,
                progress=None, cancel=None):
    """Clip ``src_path`` to the AOI cutline in ONE gdal.Warp.

    extent      (xmin, ymin, xmax, ymax) of the AOI in the SOURCE CRS.
    dst_srs_wkt target CRS; None / same as source => no reprojection.
    extent_dst  AOI extent in the target CRS (required when reprojecting).
    res_scale   output pixel size relative to native (1.0 = full resolution).
    resample    auto | near | bilinear | cubic | lanczos (only used when the
                output grid differs from the source grid).
    """
    ds = open_ds(src_path)
    gt = ds.GetGeoTransform()
    src_wkt = ds.GetProjection() or src_srs_override or ""
    if not src_wkt:
        raise ValueError("The source image has no CRS.")
    needs_srs = not ds.GetProjection()
    reproject = bool(dst_srs_wkt) and not _same_srs(src_wkt, dst_srs_wkt)
    north_up = (gt[2] == 0 and gt[4] == 0 and gt[5] < 0)
    xsize, ysize = ds.RasterXSize, ds.RasterYSize

    # ---- palette rasters -> RGBA (real transparency, no index blending) ----
    first = ds.GetRasterBand(1)
    has_palette = first.GetColorTable() is not None
    work = ds
    if has_palette:
        work = gdal.Translate("", ds, format="VRT", rgbExpand="rgba")
        if work is None:
            raise RuntimeError("Cannot expand the palette: %s" % _last_error())
        first = work.GetRasterBand(1)

    has_alpha = any(
        work.GetRasterBand(i).GetColorInterpretation() == gdal.GCI_AlphaBand
        for i in range(1, work.RasterCount + 1))
    has_nodata = first.GetNoDataValue() is not None
    use_dst_alpha = not (has_alpha or has_nodata)
    dtype = first.DataType

    exact = (not reproject) and north_up and abs(res_scale - 1.0) < 1e-9
    xmin, ymin, xmax, ymax = extent
    used_resample = "near"

    if exact:
        # --- lossless path: stay on the source's own pixel grid -----------
        rx, ry = gt[1], -gt[5]
        rminx, rmaxy = gt[0], gt[3]
        rmaxx, rminy = rminx + rx * xsize, rmaxy - ry * ysize
        xmin, xmax = max(xmin, rminx), min(xmax, rmaxx)
        ymin, ymax = max(ymin, rminy), min(ymax, rmaxy)
        if xmin >= xmax or ymin >= ymax:
            raise ValueError("The AOI does not overlap the selected image.")
        x0, y_top, w, h = snap_extent(xmin, ymin, xmax, ymax, rx, ry, rminx, rmaxy)
        bounds = (x0, y_top - h * ry, x0 + w * rx, y_top)
        xres, yres = rx, ry
        alg = "near"
        dst_wkt = src_wkt
    else:
        if reproject or not north_up:
            # derive extent / native resolution of the *output* CRS
            target = dst_srs_wkt if reproject else src_wkt
            vrt = gdal.AutoCreateWarpedVRT(work, src_wkt, target,
                                           gdal.GRA_NearestNeighbour, 0.0)
            if vrt is None:
                raise RuntimeError("Cannot compute the output grid: %s" % _last_error())
            vgt = vrt.GetGeoTransform()
            native = min(abs(vgt[1]), abs(vgt[5]))
            vminx, vmaxy = vgt[0], vgt[3]
            vmaxx = vminx + vgt[1] * vrt.RasterXSize
            vminy = vmaxy + vgt[5] * vrt.RasterYSize
            vrt = None
            dst_wkt = target
            if reproject:
                if extent_dst is None:
                    raise ValueError("extent_dst is required when reprojecting.")
                xmin, ymin, xmax, ymax = extent_dst
        else:
            native = abs(gt[1])
            vminx, vmaxy = gt[0], gt[3]
            vmaxx = vminx + gt[1] * xsize
            vminy = vmaxy + gt[5] * ysize
            dst_wkt = src_wkt
        res = native * res_scale
        xmin, xmax = max(xmin, vminx), min(xmax, vmaxx)
        ymin, ymax = max(ymin, vminy), min(ymax, vmaxy)
        if xmin >= xmax or ymin >= ymax:
            raise ValueError("The AOI does not overlap the selected image.")
        x0, y_top, w, h = snap_extent(xmin, ymin, xmax, ymax, res, res, xmin, ymax)
        bounds = (x0, y_top - h * res, x0 + w * res, y_top)
        xres = yres = res
        key = (resample or "auto").lower()
        if has_palette:
            key = "near"                    # class colours: never blend
        elif key == "auto":
            key = _auto_resample(first, dtype, work.RasterCount)
        alg = used_resample = key

    kwargs = dict(
        format="GTiff",
        outputBounds=bounds,
        xRes=xres, yRes=yres,
        resampleAlg=alg,
        cutlineDSName=mask_path, cutlineLayer=mask_layer,
        dstSRS=dst_wkt,
        creationOptions=creation_options(compress, dtype, threads=True),
        multithread=True,
        warpOptions=["NUM_THREADS=ALL_CPUS"],
        warpMemoryLimit=1024,
    )
    if needs_srs and src_srs_override:
        kwargs["srcSRS"] = src_srs_override
    if use_dst_alpha:
        kwargs["dstAlpha"] = True

    cb, state = _progress_cb(progress, cancel)
    kwargs["callback"] = cb
    try:
        out = gdal.Warp(out_path, work, **kwargs)
    except RuntimeError as exc:
        if state["cancelled"]:
            raise Cancelled()
        raise RuntimeError("gdal.Warp failed: %s" % exc)
    if state["cancelled"]:
        raise Cancelled()
    if out is None:
        raise RuntimeError("gdal.Warp failed while clipping the image: %s"
                           % _last_error())
    copy_metadata(ds, out, extra_metadata)
    out.FlushCache()
    info = {"width": out.RasterXSize, "height": out.RasterYSize,
            "pixel_x": xres, "pixel_y": yres, "exact": exact,
            "reprojected": reproject, "bands": out.RasterCount,
            "resample": used_resample, "palette_expanded": has_palette}
    out = None
    work = None
    ds = None
    return info


# ----------------------------------------------------------------- tiling
def _window_is_empty(ds, xoff, yoff, w, h):
    alpha = None
    for i in range(1, ds.RasterCount + 1):
        if ds.GetRasterBand(i).GetColorInterpretation() == gdal.GCI_AlphaBand:
            alpha = ds.GetRasterBand(i)
            break
    if alpha is not None:
        return not np.any(alpha.ReadAsArray(xoff, yoff, w, h))
    band = ds.GetRasterBand(1)
    nodata = band.GetNoDataValue()
    if nodata is not None:
        arr = band.ReadAsArray(xoff, yoff, w, h)
        if isinstance(nodata, float) and math.isnan(nodata):
            return bool(np.all(np.isnan(arr)))
        return bool(np.all(arr == nodata))
    return False


def split_into_tiles(mosaic_path, tiles_dir, basename, tile_w, tile_h,
                     skip_empty=True, compress="DEFLATE_FAST",
                     extra_metadata=None, workers=None,
                     progress=None, cancel=None):
    """Cut the mosaic into tiles on whole-pixel boundaries (lossless),
    using several threads (GDAL releases the GIL while reading/compressing)."""
    os.makedirs(tiles_dir, exist_ok=True)
    src = open_ds(mosaic_path)
    xsize, ysize = src.RasterXSize, src.RasterYSize
    gt = src.GetGeoTransform()
    dtype = src.GetRasterBand(1).DataType
    src = None
    opts = creation_options(compress, dtype, threads=False)

    jobs = []
    for row, yoff in enumerate(range(0, ysize, tile_h)):
        h = min(tile_h, ysize - yoff)
        for col, xoff in enumerate(range(0, xsize, tile_w)):
            jobs.append((row, col, xoff, yoff, min(tile_w, xsize - xoff), h))
    total = len(jobs)
    local = threading.local()
    lock = threading.Lock()
    handles = []

    def thread_ds():
        ds = getattr(local, "ds", None)
        if ds is None:
            ds = open_ds(mosaic_path)
            local.ds = ds
            with lock:
                handles.append(ds)
        return ds

    def work(job):
        if cancel is not None and cancel():
            return None
        row, col, xoff, yoff, w, h = job
        ds = thread_ds()
        if skip_empty and _window_is_empty(ds, xoff, yoff, w, h):
            return "empty"
        name = "%s_r%d_c%d.tif" % (basename, row, col)
        path = os.path.join(tiles_dir, name)
        t = gdal.Translate(path, ds, srcWin=[xoff, yoff, w, h],
                           format="GTiff", creationOptions=opts)
        if t is None:
            raise RuntimeError("Cannot write tile %s: %s" % (name, _last_error()))
        copy_metadata(ds, t, extra_metadata)
        t.FlushCache()
        t = None
        minx = gt[0] + xoff * gt[1]
        maxy = gt[3] + yoff * gt[5]
        return {"name": name, "row": row, "col": col, "width": w,
                "height": h,
                "extent": (minx, maxy + h * gt[5], minx + w * gt[1], maxy)}

    n_workers = max(1, int(workers or min(8, os.cpu_count() or 2)))
    records, done = [], 0
    pending = set()
    ex = ThreadPoolExecutor(max_workers=n_workers)
    try:
        futures = [ex.submit(work, j) for j in jobs]
        pending = set(futures)
        while pending:
            if cancel is not None and cancel():
                for f in pending:
                    f.cancel()
                raise Cancelled()
            finished, pending = wait(pending, timeout=0.2,
                                     return_when=FIRST_COMPLETED)
            for f in finished:
                res = f.result()                       # re-raises worker errors
                if isinstance(res, dict):
                    records.append(res)
                done += 1
            if progress is not None:
                progress(done / float(max(1, total)))
    finally:
        for f in pending:                      # error / cancel: drop queued tiles
            f.cancel()
        ex.shutdown(wait=True)
        with lock:
            handles[:] = []
    records.sort(key=lambda r: (r["row"], r["col"]))
    return records


def write_tile_index(records, index_path, srs_wkt, location_prefix="",
                     vendor="", acq_date="", layer_name="tileindex"):
    """GeoPackage with one polygon per tile.

    ``location`` is stored RELATIVE to the GeoPackage's folder
    (location_prefix + tile name) so the whole output folder can be moved,
    zipped or shared.
    """
    drv = ogr.GetDriverByName("GPKG")
    if os.path.exists(index_path):
        drv.DeleteDataSource(index_path)
    ds = drv.CreateDataSource(index_path)
    if ds is None:
        raise RuntimeError("Cannot create the tile index: %s" % _last_error())
    srs = osr.SpatialReference()
    srs.ImportFromWkt(srs_wkt)
    _traditional(srs)
    lyr = ds.CreateLayer(layer_name, srs, ogr.wkbPolygon)
    f_loc = ogr.FieldDefn("location", ogr.OFTString)
    f_loc.SetWidth(255)
    lyr.CreateField(f_loc)
    for fname, ftype in (("name", ogr.OFTString), ("row", ogr.OFTInteger),
                         ("col", ogr.OFTInteger), ("width", ogr.OFTInteger),
                         ("height", ogr.OFTInteger), ("vendor", ogr.OFTString),
                         ("acq_date", ogr.OFTString)):
        lyr.CreateField(ogr.FieldDefn(fname, ftype))
    lyr.StartTransaction()
    for rec in records:
        minx, miny, maxx, maxy = rec["extent"]
        ring = ogr.Geometry(ogr.wkbLinearRing)
        for x, y in ((minx, miny), (maxx, miny), (maxx, maxy),
                     (minx, maxy), (minx, miny)):
            ring.AddPoint_2D(x, y)
        poly = ogr.Geometry(ogr.wkbPolygon)
        poly.AddGeometry(ring)
        feat = ogr.Feature(lyr.GetLayerDefn())
        feat.SetGeometry(poly)
        feat.SetField("location", location_prefix + rec["name"])
        feat.SetField("name", rec["name"])
        feat.SetField("row", rec["row"])
        feat.SetField("col", rec["col"])
        feat.SetField("width", rec["width"])
        feat.SetField("height", rec["height"])
        feat.SetField("vendor", vendor or "")
        feat.SetField("acq_date", acq_date or "")
        lyr.CreateFeature(feat)
    lyr.CommitTransaction()
    ds = None
    return len(records)
