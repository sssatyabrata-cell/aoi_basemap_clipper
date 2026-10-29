# AOI Basemap Clipper (v2.0.0)

Clip a local raster or an online basemap (XYZ / WMS) to an AOI polygon at full
native resolution, optionally reproject to EPSG:4326, split into tiles and
build a portable GeoPackage tile index. Requires QGIS 3.22 or newer.

## Why it is fast
| Stage | What was slow | What it does now |
|---|---|---|
| Online XYZ | One QGIS map render per 2048 px block, then a second pass | Parses the tile URL and downloads tiles itself, 16 in parallel (configurable), through QGIS' network stack (proxy / auth / cache keep working). Only tiles that touch the AOI polygon are requested. Falls back to the renderer automatically. |
| Temp image | Compressed, dense | Uncompressed, sparse, tile-aligned GeoTIFF, written with one call per tile |
| Clip | Single thread | One multi-threaded `gdal.Warp` (warp + compression threads) |
| Compression | Deflate level 6 | Deflate level 1 by default (lossless), optional ZSTD |
| Tiling | Sequential | Parallel workers, each with its own dataset handle |
| Temp location | System temp drive | Inside the output folder (same disk: no copying, no full C: drive) |

## Behaviour you can rely on
* **Reprojection is OFF by default.** Local rasters are then cut on their own pixel grid (bit-identical).
  XYZ layers are written on the tile grid, one output pixel = one tile pixel.
* Palette / class rasters are expanded to RGBA so the area outside the AOI is really transparent;
  they are never interpolated. "Auto" resampling uses nearest neighbour for class data.
* The tile index stores **relative** paths: move or zip the whole output folder freely.
* Existing outputs are never overwritten silently; partial files are removed on cancel / error;
  finished files are swapped in only at the end.
* No global GDAL state is changed (`gdal.UseExceptions()` is not called).

## Tile index actions
The index layer carries **Load Image** and **Remove Image** actions. They are small Python
snippets stored in the layer style. Untick "Embed one-click actions" if you do not want any code
inside the GeoPackage style.

## Terms of use
Many tile providers (Google, Esri, Bing, ...) forbid bulk offline copies. Check your licence.
Lower "Download threads" if a server answers with HTTP 429 / 403.

## Tests
`python -m unittest discover -s tests` - the tile-math tests need nothing; the GDAL tests run inside
QGIS' Python (or any GDAL + numpy environment) and are skipped otherwise.

## Packaging notes
`metadata.txt` has `tracker`, `repository` and `homepage` pointing to the GitHub repo; check them:
fill them in before uploading to the official QGIS plugin repository. `LICENSE` is GPL-2.0
(the licence the official repository expects); change it if you prefer another GPL-compatible one.
