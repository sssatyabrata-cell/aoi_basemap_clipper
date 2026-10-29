# -*- coding: utf-8 -*-
"""
XYZ (slippy-map) helpers - pure Python, no GDAL / QGIS imports.

Used by the "direct tile download" fast path: instead of asking QGIS to
*render* an XYZ basemap (slow: one map render + one resample per block), the
plugin parses the layer's tile URL and downloads the tiles itself, many in
parallel, writing every tile straight into a GeoTIFF on the tile grid.
"""

import math
import re
from urllib.parse import unquote

WEB_MERCATOR_ORIGIN = 20037508.342789244
WEB_MERCATOR_WORLD = 2.0 * WEB_MERCATOR_ORIGIN
WEB_MERCATOR_RES0 = 156543.03392804097      # m/px of a 256 px tile at z = 0

_SUPPORTED_PLACEHOLDERS = {"x", "y", "z", "-y"}


def zoom_pixel_size(zoom, tile_px=256):
    """Pixel size (EPSG:3857 units) at ``zoom`` for tiles of ``tile_px`` px."""
    return WEB_MERCATOR_WORLD / (float(tile_px) * (2 ** int(zoom)))


def _parse_query(source):
    """Like urllib's parse_qs, but a literal '+' stays '+' (parse_qs turns it
    into a space, which corrupts tile URLs that contain one)."""
    out = {}
    for part in source.split("&"):
        if not part:
            continue
        key, _, val = part.partition("=")
        out.setdefault(unquote(key), []).append(unquote(val))
    return out


def parse_xyz_source(source):
    """Parse a QGIS XYZ layer source string.

    Returns a dict ``{url, zmin, zmax, authcfg, headers}`` or ``None`` when
    the layer cannot be downloaded directly (unsupported placeholders such as
    {q} or {s}, non-http URLs, ...). ``None`` makes the caller fall back to
    the (slower) QGIS renderer, so behaviour is never worse than before.
    """
    if not source:
        return None
    q = _parse_query(source)

    def first(key, default=None):
        vals = q.get(key)
        return vals[0] if vals else default

    if (first("type") or "").lower() != "xyz":
        return None
    url = first("url")
    if not url or not url.lower().startswith(("http://", "https://")):
        return None
    placeholders = set(re.findall(r"\{([^{}]*)\}", url))
    if not placeholders <= _SUPPORTED_PLACEHOLDERS:
        return None
    if "x" not in placeholders or "z" not in placeholders:
        return None
    if "y" not in placeholders and "-y" not in placeholders:
        return None

    def as_int(key, default):
        try:
            return int(first(key, default))
        except (TypeError, ValueError):
            return default

    headers = {}
    for key, vals in q.items():
        if key.lower().startswith("http-header:") and vals:
            headers[key.split(":", 1)[1]] = vals[0]
    if first("referer"):
        headers.setdefault("Referer", first("referer"))

    return {
        "url": url,
        "zmin": as_int("zmin", 0),
        "zmax": as_int("zmax", 18),
        "authcfg": first("authcfg", "") or "",
        "headers": headers,
    }


def format_url(template, z, x, y):
    """Fill {z}/{x}/{y}/{-y} (TMS) in a tile URL template."""
    n = 2 ** int(z)
    return (template.replace("{z}", str(int(z)))
            .replace("{x}", str(int(x)))
            .replace("{-y}", str(n - 1 - int(y)))
            .replace("{y}", str(int(y))))


def tile_for_point(x, y, zoom):
    """Tile (tx, ty) that contains the EPSG:3857 point (x, y)."""
    n = 2 ** int(zoom)
    size = WEB_MERCATOR_WORLD / n
    tx = int(math.floor((x + WEB_MERCATOR_ORIGIN) / size))
    ty = int(math.floor((WEB_MERCATOR_ORIGIN - y) / size))
    return max(0, min(n - 1, tx)), max(0, min(n - 1, ty))


def tile_bounds(tx, ty, zoom):
    """(xmin, ymin, xmax, ymax) of a tile in EPSG:3857."""
    size = WEB_MERCATOR_WORLD / (2 ** int(zoom))
    xmin = -WEB_MERCATOR_ORIGIN + tx * size
    ymax = WEB_MERCATOR_ORIGIN - ty * size
    return xmin, ymax - size, xmin + size, ymax


def iter_tile_plan(c0, r0, width, height, tile_px, zoom):
    """Describe which tiles cover the pixel window and where they go.

    (c0, r0) is the window's top-left pixel measured from the world's
    top-left corner at ``zoom`` (i.e. on the tile grid). Yields tuples
    ``(tx, ty, dst_x, dst_y, src_x, src_y, w, h)``:
    copy ``w x h`` pixels starting at (src_x, src_y) of tile (tx, ty) to
    (dst_x, dst_y) of the output. Tiles outside the world are skipped.
    """
    n = 2 ** int(zoom)
    tx0, tx1 = c0 // tile_px, (c0 + width - 1) // tile_px
    ty0, ty1 = r0 // tile_px, (r0 + height - 1) // tile_px
    for ty in range(ty0, ty1 + 1):
        if ty < 0 or ty >= n:
            continue
        top = ty * tile_px
        ys, ye = max(r0, top), min(r0 + height, top + tile_px)
        for tx in range(tx0, tx1 + 1):
            if tx < 0 or tx >= n:
                continue
            left = tx * tile_px
            xs, xe = max(c0, left), min(c0 + width, left + tile_px)
            yield (tx, ty, xs - c0, ys - r0, xs - left, ys - top,
                   xe - xs, ye - ys)
