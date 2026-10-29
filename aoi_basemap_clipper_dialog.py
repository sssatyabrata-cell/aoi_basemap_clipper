# -*- coding: utf-8 -*-
"""
AOI Basemap Clipper dialog (QGIS side).

All heavy lifting lives in ``clip_core`` (pure GDAL) and ``xyz_tools``. This
file only:
  * collects the options,
  * builds the AOI cutline in the right CRS,
  * downloads (fast path) or renders online layers on an exact pixel grid,
  * calls the core, swaps finished files into place and loads the tile index.
"""

import math
import os
import re
import shutil
import tempfile
import threading
import time

import numpy as np

from qgis.PyQt.QtCore import QSize, QUrl
from qgis.PyQt.QtGui import QColor, QImage, QPainter
from qgis.PyQt.QtNetwork import QNetworkRequest
from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QSpinBox, QCheckBox, QFileDialog, QTextEdit, QProgressBar,
    QMessageBox, QGroupBox, QApplication, QComboBox, QDoubleSpinBox
)
from qgis.core import (
    Qgis, QgsProject, QgsMapSettings, QgsMapRendererParallelJob, QgsRectangle,
    QgsCoordinateTransform, QgsCoordinateReferenceSystem, QgsGeometry,
    QgsVectorLayer, QgsMapLayerProxyModel, QgsSettings, QgsUnitTypes,
    QgsBlockingNetworkRequest, QgsWkbTypes
)
from qgis.gui import QgsMapLayerComboBox

from . import clip_core as core
from . import xyz_tools

STYLE_DIR = os.path.join(os.path.dirname(__file__), "styles")
TILE_INDEX_STYLE = os.path.join(STYLE_DIR, "tile_index_style.qml")
SETTINGS_PREFIX = "AOIBasemapClipper/"

MODE_AUTO, MODE_ZOOM, MODE_WIDTH, MODE_CANVAS, MODE_GROUND = 0, 1, 2, 3, 4
RENDER_BLOCK = 2048
DEFAULT_XYZ_ZOOM = 18


# ------------------------------------------------- Qt5 / Qt6 compatibility
def _scoped(cls, scope, name):
    holder = getattr(cls, scope, None)
    if holder is not None and hasattr(holder, name):
        return getattr(holder, name)
    return getattr(cls, name)


MB_YES = _scoped(QMessageBox, "StandardButton", "Yes")
MB_NO = _scoped(QMessageBox, "StandardButton", "No")
HTTP_STATUS = _scoped(QNetworkRequest, "Attribute", "HttpStatusCodeAttribute")
NET_OK = _scoped(QgsBlockingNetworkRequest, "ErrorCode", "NoError")


def _layer_filter(name):
    flt = getattr(Qgis, "LayerFilter", None)
    if flt is not None and hasattr(flt, name):
        return getattr(flt, name)
    return getattr(QgsMapLayerProxyModel, name)


def _set_filter(combo, name):
    try:
        combo.setFilters(_layer_filter(name))
    except TypeError:
        combo.setFilters(getattr(QgsMapLayerProxyModel, name))


def _polygon_type():
    gt = getattr(Qgis, "GeometryType", None)
    if gt is not None and hasattr(gt, "Polygon"):
        return gt.Polygon
    return QgsWkbTypes.PolygonGeometry


def _rgba_format():
    return _scoped(QImage, "Format", "Format_RGBA8888")


def _meters_factor(crs):
    """Factor converting metres to the CRS's linear map unit (feet CRSs!)."""
    try:
        metre = Qgis.DistanceUnit.Meters
    except AttributeError:
        metre = QgsUnitTypes.DistanceMeters
    return QgsUnitTypes.fromUnitToUnitFactor(metre, crs.mapUnits())


def qimage_to_rgba(image):
    """QImage -> uint8 array (h, w, 4) RGBA (straight alpha)."""
    if image.isNull():
        raise ValueError("Empty image")
    w, h = image.width(), image.height()
    image = image.convertToFormat(_rgba_format())
    ptr = image.constBits()
    n_bytes = image.sizeInBytes() if hasattr(image, "sizeInBytes") else image.byteCount()
    ptr.setsize(n_bytes)
    bpl = image.bytesPerLine()
    arr = np.frombuffer(ptr, dtype=np.uint8).reshape((h, bpl))[:, :w * 4]
    return arr.reshape((h, w, 4)).copy()


def _fmt_time(seconds):
    seconds = int(round(seconds))
    if seconds < 60:
        return "%ds" % seconds
    m, s = divmod(seconds, 60)
    if m < 60:
        return "%dm %02ds" % (m, s)
    h, m = divmod(m, 60)
    return "%dh %02dm" % (h, m)


class AOIBasemapClipperDialog(QDialog):

    def __init__(self, iface, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self._cancelled = False
        self._running = False
        self._close_after_run = False
        self._temp_paths = []
        self.setWindowTitle("AOI Basemap Clipper")
        self.setMinimumWidth(580)
        self._build_ui()
        self._load_settings()
        self._update_source_info()

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        layout = QVBoxLayout(self)

        # ---- Input ------------------------------------------------------
        box_in = QGroupBox("Input")
        form = QFormLayout(box_in)

        self.aoi_combo = QgsMapLayerComboBox()
        _set_filter(self.aoi_combo, "PolygonLayer")
        form.addRow("AOI (polygon layer):", self.aoi_combo)

        self.selected_only_chk = QCheckBox("Selected features only")
        form.addRow("", self.selected_only_chk)

        self.image_combo = QgsMapLayerComboBox()
        _set_filter(self.image_combo, "RasterLayer")
        self.image_combo.layerChanged.connect(self._update_source_info)
        form.addRow("Image / basemap:", self.image_combo)

        self.info_lbl = QLabel("")
        self.info_lbl.setWordWrap(True)
        self.info_lbl.setStyleSheet("color: gray;")
        form.addRow("", self.info_lbl)
        layout.addWidget(box_in)

        # ---- Resolution ---------------------------------------------------
        box_res = QGroupBox("Resolution")
        rform = QFormLayout(box_res)
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Auto - keep full native resolution (recommended)", MODE_AUTO)
        self.mode_combo.addItem("XYZ zoom level (Web-Mercator layers)", MODE_ZOOM)
        self.mode_combo.addItem("Fixed output width (px)", MODE_WIDTH)
        self.mode_combo.addItem("Match current map canvas zoom (what you see now)", MODE_CANVAS)
        self.mode_combo.addItem("Custom ground pixel size (m/px) - e.g. 0.15 for 6-inch imagery", MODE_GROUND)
        self.mode_combo.currentIndexChanged.connect(self._update_source_info)
        rform.addRow("Mode:", self.mode_combo)

        self.zoom_spin = QSpinBox()
        self.zoom_spin.setRange(0, 24)
        self.zoom_spin.setValue(DEFAULT_XYZ_ZOOM)
        rform.addRow("Zoom level:", self.zoom_spin)

        self.ground_spin = QDoubleSpinBox()
        self.ground_spin.setDecimals(4)
        self.ground_spin.setRange(0.001, 1000.0)
        self.ground_spin.setSingleStep(0.01)
        self.ground_spin.setValue(0.1524)
        self.ground_spin.setSuffix(" m/px")
        rform.addRow("Ground pixel size:", self.ground_spin)

        self.width_spin = QSpinBox()
        self.width_spin.setRange(256, 400000)
        self.width_spin.setSingleStep(256)
        self.width_spin.setValue(8192)
        self.width_spin.setSuffix(" px")
        rform.addRow("Output width:", self.width_spin)

        self.reproject_chk = QCheckBox(
            "Reproject to EPSG:4326 (resamples the pixels - leave OFF to keep them untouched)")
        self.reproject_chk.setChecked(False)
        rform.addRow("", self.reproject_chk)

        self.resample_combo = QComboBox()
        for label, key in (("Auto (recommended: nearest for palette / class rasters)", "auto"),
                           ("Cubic (sharpest)", "cubic"), ("Bilinear", "bilinear"),
                           ("Lanczos", "lanczos"), ("Nearest", "near")):
            self.resample_combo.addItem(label, key)
        rform.addRow("Resampling (only when reprojecting / resizing):", self.resample_combo)
        layout.addWidget(box_res)

        # ---- Performance ----------------------------------------------------
        box_perf = QGroupBox("Speed")
        pform = QFormLayout(box_perf)
        self.direct_chk = QCheckBox(
            "Download XYZ tiles directly in parallel (fastest; falls back to QGIS rendering)")
        self.direct_chk.setChecked(True)
        pform.addRow("", self.direct_chk)
        cpu = os.cpu_count() or 4
        self.dl_threads_spin = QSpinBox()
        self.dl_threads_spin.setRange(1, 64)
        self.dl_threads_spin.setValue(16)
        self.dl_threads_spin.setToolTip("Simultaneous tile downloads. Lower it if the server "
                                        "throttles you (HTTP 429 / 403 errors).")
        pform.addRow("Download threads:", self.dl_threads_spin)
        self.parallel_spin = QSpinBox()
        self.parallel_spin.setRange(1, 16)
        self.parallel_spin.setValue(6)
        self.parallel_spin.setToolTip("Blocks rendered at the same time for WMS / other layers "
                                      "that cannot be downloaded tile by tile.")
        pform.addRow("Parallel render requests (WMS / other):", self.parallel_spin)
        self.workers_spin = QSpinBox()
        self.workers_spin.setRange(1, 64)
        self.workers_spin.setValue(max(2, min(8, cpu)))
        self.workers_spin.setToolTip("Threads used to cut and compress the output tiles.")
        pform.addRow("Tiling threads:", self.workers_spin)
        layout.addWidget(box_perf)

        # ---- Output ---------------------------------------------------------
        box_out = QGroupBox("Output")
        oform = QFormLayout(box_out)
        out_row = QHBoxLayout()
        self.output_edit = QLineEdit()
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse_output)
        out_row.addWidget(self.output_edit)
        out_row.addWidget(browse_btn)
        oform.addRow("Save to folder:", out_row)

        self.basename_edit = QLineEdit("clip")
        oform.addRow("Output name (prefix):", self.basename_edit)

        tile_row = QHBoxLayout()
        self.tile_w_spin = QSpinBox()
        self.tile_w_spin.setRange(64, 40000)
        self.tile_w_spin.setValue(1024)
        self.tile_w_spin.setSuffix(" px")
        self.tile_h_spin = QSpinBox()
        self.tile_h_spin.setRange(64, 40000)
        self.tile_h_spin.setValue(1024)
        self.tile_h_spin.setSuffix(" px")
        tile_row.addWidget(QLabel("W:"))
        tile_row.addWidget(self.tile_w_spin)
        tile_row.addWidget(QLabel("H:"))
        tile_row.addWidget(self.tile_h_spin)
        oform.addRow("Tile size:", tile_row)

        self.compress_combo = QComboBox()
        self.compress_combo.addItem("Deflate - fast (lossless, default)", "DEFLATE_FAST")
        self.compress_combo.addItem("Deflate - normal (lossless, smaller)", "DEFLATE")
        if core.zstd_available():
            self.compress_combo.addItem("ZSTD (lossless, fastest + small; needs modern GDAL to read)", "ZSTD")
        self.compress_combo.addItem("LZW (lossless)", "LZW")
        self.compress_combo.addItem("None (fastest write, largest files)", "NONE")
        oform.addRow("Compression:", self.compress_combo)

        self.vendor_edit = QLineEdit()
        self.vendor_edit.setPlaceholderText("optional - used by the tile index 'Load Image' action")
        oform.addRow("Vendor:", self.vendor_edit)
        self.date_edit = QLineEdit()
        self.date_edit.setPlaceholderText("optional - YYYY-MM-DD")
        oform.addRow("Acquisition date:", self.date_edit)

        self.skip_empty_chk = QCheckBox("Skip empty (fully transparent) tiles")
        self.skip_empty_chk.setChecked(True)
        oform.addRow("", self.skip_empty_chk)
        self.keep_mosaic_chk = QCheckBox("Keep the full clipped mosaic (before tiling)")
        self.keep_mosaic_chk.setChecked(True)
        oform.addRow("", self.keep_mosaic_chk)
        self.add_to_project_chk = QCheckBox("Add tile index to project when finished")
        self.add_to_project_chk.setChecked(True)
        oform.addRow("", self.add_to_project_chk)
        self.actions_chk = QCheckBox(
            "Embed one-click 'Load Image' / 'Remove Image' actions in the tile index "
            "(small Python snippets that run when you click them)")
        self.actions_chk.setChecked(True)
        oform.addRow("", self.actions_chk)
        layout.addWidget(box_out)

        # ---- Run / cancel / progress ---------------------------------------
        btn_row = QHBoxLayout()
        self.run_btn = QPushButton("Run")
        self.run_btn.clicked.connect(self._run)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.clicked.connect(self._request_cancel)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.setToolTip("Clear the folder, name, vendor and date fields, the log and the progress bar")
        self.clear_btn.clicked.connect(self._clear_fields)
        btn_row.addWidget(self.run_btn)
        btn_row.addWidget(self.cancel_btn)
        btn_row.addWidget(self.clear_btn)
        layout.addLayout(btn_row)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setFixedHeight(150)
        layout.addWidget(self.log_box)

    # ------------------------------------------------------------ settings
    def _load_settings(self):
        s = QgsSettings()
        self.output_edit.setText(s.value(SETTINGS_PREFIX + "folder", "", type=str))
        self.basename_edit.setText(s.value(SETTINGS_PREFIX + "basename", "clip", type=str))
        self.mode_combo.setCurrentIndex(s.value(SETTINGS_PREFIX + "mode", 0, type=int))
        self.zoom_spin.setValue(s.value(SETTINGS_PREFIX + "zoom", DEFAULT_XYZ_ZOOM, type=int))
        self.width_spin.setValue(s.value(SETTINGS_PREFIX + "width", 8192, type=int))
        self.ground_spin.setValue(s.value(SETTINGS_PREFIX + "ground", 0.1524, type=float))
        self.parallel_spin.setValue(s.value(SETTINGS_PREFIX + "parallel", 6, type=int))
        self.dl_threads_spin.setValue(s.value(SETTINGS_PREFIX + "dl_threads", 16, type=int))
        self.workers_spin.setValue(s.value(SETTINGS_PREFIX + "workers", self.workers_spin.value(), type=int))
        self.tile_w_spin.setValue(s.value(SETTINGS_PREFIX + "tile_w", 1024, type=int))
        self.tile_h_spin.setValue(s.value(SETTINGS_PREFIX + "tile_h", 1024, type=int))
        # new key: older versions defaulted to "reproject ON" which silently resampled
        self.reproject_chk.setChecked(s.value(SETTINGS_PREFIX + "reproject_v2", False, type=bool))
        self.selected_only_chk.setChecked(s.value(SETTINGS_PREFIX + "selected_only", False, type=bool))
        self.skip_empty_chk.setChecked(s.value(SETTINGS_PREFIX + "skip_empty", True, type=bool))
        self.keep_mosaic_chk.setChecked(s.value(SETTINGS_PREFIX + "keep_mosaic", True, type=bool))
        self.add_to_project_chk.setChecked(s.value(SETTINGS_PREFIX + "add_to_project", True, type=bool))
        self.direct_chk.setChecked(s.value(SETTINGS_PREFIX + "direct", True, type=bool))
        self.actions_chk.setChecked(s.value(SETTINGS_PREFIX + "actions", True, type=bool))
        for combo, key in ((self.resample_combo, "resample"), (self.compress_combo, "compress")):
            idx = combo.findData(s.value(SETTINGS_PREFIX + key, "", type=str))
            if idx >= 0:
                combo.setCurrentIndex(idx)

    def _save_settings(self):
        s = QgsSettings()
        s.setValue(SETTINGS_PREFIX + "folder", self.output_edit.text().strip())
        s.setValue(SETTINGS_PREFIX + "basename", self.basename_edit.text().strip())
        s.setValue(SETTINGS_PREFIX + "mode", self.mode_combo.currentIndex())
        s.setValue(SETTINGS_PREFIX + "zoom", self.zoom_spin.value())
        s.setValue(SETTINGS_PREFIX + "width", self.width_spin.value())
        s.setValue(SETTINGS_PREFIX + "ground", self.ground_spin.value())
        s.setValue(SETTINGS_PREFIX + "parallel", self.parallel_spin.value())
        s.setValue(SETTINGS_PREFIX + "dl_threads", self.dl_threads_spin.value())
        s.setValue(SETTINGS_PREFIX + "workers", self.workers_spin.value())
        s.setValue(SETTINGS_PREFIX + "tile_w", self.tile_w_spin.value())
        s.setValue(SETTINGS_PREFIX + "tile_h", self.tile_h_spin.value())
        s.setValue(SETTINGS_PREFIX + "reproject_v2", self.reproject_chk.isChecked())
        s.setValue(SETTINGS_PREFIX + "selected_only", self.selected_only_chk.isChecked())
        s.setValue(SETTINGS_PREFIX + "skip_empty", self.skip_empty_chk.isChecked())
        s.setValue(SETTINGS_PREFIX + "keep_mosaic", self.keep_mosaic_chk.isChecked())
        s.setValue(SETTINGS_PREFIX + "add_to_project", self.add_to_project_chk.isChecked())
        s.setValue(SETTINGS_PREFIX + "direct", self.direct_chk.isChecked())
        s.setValue(SETTINGS_PREFIX + "actions", self.actions_chk.isChecked())
        s.setValue(SETTINGS_PREFIX + "resample", self.resample_combo.currentData())
        s.setValue(SETTINGS_PREFIX + "compress", self.compress_combo.currentData())

    # ------------------------------------------------------------- helpers
    def _browse_output(self):
        folder = QFileDialog.getExistingDirectory(self, "Select output folder",
                                                  self.output_edit.text())
        if folder:
            self.output_edit.setText(folder)

    def _clear_fields(self):
        """Empty every manually typed field, the log and the progress bar."""
        if self._running:
            return
        self.output_edit.clear()
        self.basename_edit.clear()
        self.vendor_edit.clear()
        self.date_edit.clear()
        self.log_box.clear()
        self.progress.setValue(0)
        s = QgsSettings()
        s.setValue(SETTINGS_PREFIX + "folder", "")
        s.setValue(SETTINGS_PREFIX + "basename", "")

    def _log(self, msg):
        self.log_box.append(msg)
        QApplication.processEvents()

    def _request_cancel(self):
        self._cancelled = True
        self._log("Cancelling…")

    def _is_cancelled(self):
        QApplication.processEvents()
        return self._cancelled

    def _stage(self, lo, hi):
        def cb(frac):
            self.progress.setValue(int(lo + (hi - lo) * max(0.0, min(1.0, frac))))
            QApplication.processEvents()
        return cb

    def _threaded(self, lo, hi, func, *args, **kwargs):
        """Run a pure-GDAL / network function in a worker thread.

        The worker only writes plain Python values; this (GUI) thread polls
        them, updates the progress bar and keeps the window responsive, so Qt
        is never touched from a GDAL or network thread."""
        state = {"frac": 0.0, "result": None, "error": None}

        def progress(frac):
            state["frac"] = frac

        def cancel():
            return self._cancelled

        def work():
            try:
                state["result"] = func(*args, progress=progress, cancel=cancel, **kwargs)
            except BaseException as exc:        # re-raised on the GUI thread
                state["error"] = exc

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        while thread.is_alive():
            frac = max(0.0, min(1.0, state["frac"]))
            self.progress.setValue(int(lo + (hi - lo) * frac))
            QApplication.processEvents()
            thread.join(0.05)
        if state["error"] is not None:
            raise state["error"]
        return state["result"]

    @staticmethod
    def _gdal_source(layer):
        """Return a GDAL-openable source (file, /vsi path, subdataset) for a
        GDAL-provider raster, else None (online / rendered layers)."""
        if layer is None or layer.providerType() != "gdal":
            return None
        path = layer.dataProvider().dataSourceUri().split("|")[0]
        if os.path.isfile(path):
            return path
        if path.lower().startswith(("/vsi", "gpkg:", "netcdf:", "hdf4_eos:",
                                    "hdf5:", "http://", "https://")):
            return path
        return None

    @staticmethod
    def _is_xyz(layer):
        return (layer is not None and layer.providerType() == "wms"
                and "type=xyz" in layer.source().lower())

    @staticmethod
    def _xyz_zmax(layer):
        parsed = xyz_tools.parse_xyz_source(layer.source())
        if parsed:
            return parsed["zmax"]
        m = re.search(r"zmax=(\d+)", layer.source())
        return int(m.group(1)) if m else DEFAULT_XYZ_ZOOM

    @staticmethod
    def _is_webmercator(layer):
        return layer is not None and layer.crs().authid() == "EPSG:3857"

    def _update_source_info(self, *_):
        layer = self.image_combo.currentLayer()
        mode = self.mode_combo.currentData()
        zoom_ok = self._is_webmercator(layer)
        model_item = self.mode_combo.model().item(MODE_ZOOM)
        if model_item is not None:
            model_item.setEnabled(zoom_ok)
        if mode == MODE_ZOOM and not zoom_ok:
            self.mode_combo.setCurrentIndex(MODE_AUTO)
            mode = MODE_AUTO
        self.zoom_spin.setEnabled(mode == MODE_ZOOM)
        self.width_spin.setEnabled(mode == MODE_WIDTH)
        self.ground_spin.setEnabled(mode == MODE_GROUND)

        if layer is None:
            self.info_lbl.setText("No raster layer selected.")
            return
        tos = ("Make sure the provider's terms of use allow saving offline copies "
               "of this imagery.")
        if self._gdal_source(layer):
            self.info_lbl.setText(
                "GDAL raster: the AOI is cut straight from the source on its own pixel grid "
                "(bit-identical pixels, native resolution and bit depth) unless you reproject "
                "or resize. Native pixel size: %.6g x %.6g layer-CRS units, %d band(s)."
                % (layer.rasterUnitsPerPixelX(), layer.rasterUnitsPerPixelY(), layer.bandCount()))
        elif self._is_xyz(layer):
            z = self._xyz_zmax(layer)
            self.info_lbl.setText(
                "Online XYZ tiles: only tiles that touch the AOI are fetched, in parallel, and "
                "written on the tile grid (Auto = zoom %d, the layer's max zoom). One output "
                "pixel = one tile pixel; no resampling unless you reproject or resize. %s" % (z, tos))
        else:
            self.info_lbl.setText(
                "Rendered layer (WMS / other). Tip: the data has a real resolution (e.g. 6-inch "
                "imagery = 0.15 m/px). Rendering finer than that only makes the job slower and "
                "bigger - use 'Custom ground pixel size' to match it. %s" % tos)

    # ---------------------------------------------------------------- run
    def _output_paths(self, out_folder, basename, reproject):
        mosaic_name = "%s_%s.tif" % (basename, "4326" if reproject else "clipped")
        return {
            "mosaic": os.path.join(out_folder, mosaic_name),
            "mosaic_tmp": os.path.join(out_folder, ".%s.tmp.tif" % mosaic_name[:-4]),
            "tiles": os.path.join(out_folder, "%s_tiles" % basename),
            "tiles_tmp": os.path.join(out_folder, ".%s_tiles.tmp" % basename),
            "index": os.path.join(out_folder, "%s_tileindex.gpkg" % basename),
            "index_tmp": os.path.join(out_folder, ".%s_tileindex.tmp.gpkg" % basename),
        }

    @staticmethod
    def _norm(p):
        return os.path.normcase(os.path.normpath(p))

    def _project_layers_using(self, paths, dirs):
        """Project layers whose file is one of ``paths`` or inside ``dirs``."""
        wanted = {self._norm(p) for p in paths}
        dirs = [self._norm(d) + os.sep for d in dirs]
        hits = []
        for lyr in QgsProject.instance().mapLayers().values():
            src = lyr.source().split("|")[0]
            if not src:
                continue
            n = self._norm(src)
            if n in wanted or any(n.startswith(d) for d in dirs):
                hits.append(lyr)
        return hits

    def _run(self):
        aoi_layer = self.aoi_combo.currentLayer()
        image_layer = self.image_combo.currentLayer()
        out_folder = self.output_edit.text().strip()
        basename = re.sub(r"[^\w\-]+", "_", self.basename_edit.text().strip()) or "clip"

        if aoi_layer is None:
            QMessageBox.warning(self, "AOI Basemap Clipper", "Please choose an AOI polygon layer.")
            return
        if image_layer is None:
            QMessageBox.warning(self, "AOI Basemap Clipper", "Please choose an image/basemap layer.")
            return
        if not out_folder:
            QMessageBox.warning(self, "AOI Basemap Clipper", "Please choose an output folder.")
            return
        if self.selected_only_chk.isChecked() and aoi_layer.selectedFeatureCount() == 0:
            QMessageBox.warning(self, "AOI Basemap Clipper",
                                "'Selected features only' is ticked but nothing is selected.")
            return

        try:
            os.makedirs(out_folder, exist_ok=True)
        except OSError as exc:
            QMessageBox.critical(self, "AOI Basemap Clipper", "Cannot create the output folder:\n%s" % exc)
            return

        reproject = self.reproject_chk.isChecked()
        paths = self._output_paths(out_folder, basename, reproject)

        # ---- never overwrite silently ---------------------------------------
        existing = [p for p in (paths["mosaic"] if self.keep_mosaic_chk.isChecked() else None,
                                paths["tiles"], paths["index"]) if p and os.path.exists(p)]
        if existing:
            ans = QMessageBox.question(
                self, "AOI Basemap Clipper",
                "These outputs already exist and will be replaced:\n\n%s\n\n"
                "Layers using them are removed from the project first. Continue?"
                % "\n".join(existing))
            if ans != MB_YES:
                return
            for lyr in self._project_layers_using(
                    [paths["mosaic"], paths["index"]], [paths["tiles"]]):
                QgsProject.instance().removeMapLayer(lyr.id())
            QApplication.processEvents()

        self._save_settings()
        self._cancelled = False
        self._running = True
        self._set_inputs_enabled(False)
        self.progress.setValue(0)
        self.log_box.clear()

        tmp_dir = None
        t0 = time.perf_counter()
        try:
            tmp_dir = tempfile.mkdtemp(prefix=".aoi_clip_", dir=out_folder)
            self._temp_paths = [paths["mosaic_tmp"], paths["tiles_tmp"], paths["index_tmp"]]
            self._cleanup_temp()                # leftovers of a crashed run
            summary = self._process(aoi_layer, image_layer, out_folder, basename,
                                    tmp_dir, paths, reproject)
            self.progress.setValue(100)
            elapsed = _fmt_time(time.perf_counter() - t0)
            self._log("Finished in %s." % elapsed)
            QMessageBox.information(
                self, "AOI Basemap Clipper",
                "Done in %s.\n%s\nOutput written to:\n%s" % (elapsed, summary, out_folder))
        except core.Cancelled:
            self._log("Cancelled by user - partial files removed.")
        except Exception as exc:
            self._log("ERROR: %s" % exc)
            QMessageBox.critical(self, "AOI Basemap Clipper", "Failed:\n%s" % exc)
        finally:
            if tmp_dir:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            self._cleanup_temp()
            self._running = False
            self._set_inputs_enabled(True)
            if self._close_after_run:
                self._close_after_run = False
                self.close()

    def _set_inputs_enabled(self, enabled):
        """Lock every option while a job runs (the pipeline re-reads some
        widgets late, so editing them mid-run could change the result)."""
        for box in self.findChildren(QGroupBox):
            box.setEnabled(enabled)
        self.run_btn.setEnabled(enabled)
        self.cancel_btn.setEnabled(not enabled)
        self.clear_btn.setEnabled(enabled)

    def _cleanup_temp(self):
        for p in self._temp_paths:
            try:
                if os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
                elif os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass

    @staticmethod
    def _replace_file(tmp, final):
        for _ in range(8):
            try:
                os.replace(tmp, final)
                return
            except PermissionError:
                time.sleep(0.4)
                QApplication.processEvents()
        raise RuntimeError("Could not write %s - is it open in another program or "
                           "still loaded in QGIS? Close it and run again." % final)

    # ---------------------------------------------------------- pipeline
    def _process(self, aoi_layer, image_layer, out_folder, basename, tmp_dir,
                 paths, reproject):
        mode = self.mode_combo.currentData()
        compress = self.compress_combo.currentData()
        resample = self.resample_combo.currentData()
        keep_mosaic = self.keep_mosaic_chk.isChecked()
        file_path = self._gdal_source(image_layer)

        # ---- 1. source CRS ------------------------------------------------
        src_crs = image_layer.crs()
        probe = None
        if file_path:
            try:
                probe = core.open_ds(file_path)
            except ValueError as exc:
                self._log("Cannot read the source with GDAL (%s) - rendering it instead." % exc)
                file_path = None
        if probe is not None:
            wkt = probe.GetProjection()
            if wkt:
                crs = QgsCoordinateReferenceSystem()
                crs.createFromWkt(wkt)
                if crs.isValid():
                    src_crs = crs
        if not src_crs.isValid():
            src_crs = self.iface.mapCanvas().mapSettings().destinationCrs()
            self._log("Layer has no CRS - using the canvas CRS (%s)." % src_crs.authid())
        dst_crs = QgsCoordinateReferenceSystem("EPSG:4326") if reproject else src_crs

        # ---- 2. AOI cutline -------------------------------------------------
        self._log("Preparing AOI cutline…")
        mask_path = os.path.join(tmp_dir, "aoi_mask.gpkg")
        ext_src, ext_dst = self._build_mask(aoi_layer, src_crs, dst_crs, mask_path)

        # ---- 3. source raster on disk -----------------------------------------
        res_scale = 1.0
        t_stage = time.perf_counter()
        if file_path:
            src_path = file_path
            self._log("Source: GDAL raster - clipping directly from %s" % os.path.basename(file_path))
            if mode != MODE_AUTO:
                native = abs(probe.GetGeoTransform()[1])
                target = self._target_pixel_size(mode, ext_src, src_crs)
                res_scale = target / native
                self._log("Resampling to pixel size %.6g (x%.3f of native)." % (target, res_scale))
                if target > 0:
                    self._confirm_size(
                        max(1, int(ext_src.width() / target)),
                        max(1, int(ext_src.height() / target)),
                        target, src_crs, ext_src, paths["mosaic_tmp"])
            probe = None
            self.progress.setValue(5)
            render_weight = 5
        else:
            src_path = os.path.join(tmp_dir, "render.tif")
            self._fetch_online(image_layer, src_crs, mode, ext_src, mask_path, src_path)
            render_weight = 45
            self._log("Download / render stage: %s." % _fmt_time(time.perf_counter() - t_stage))

        # ---- 4. clip (+ optional reproject) in ONE warp ------------------------
        meta = {}
        if self.vendor_edit.text().strip():
            meta["vendor"] = re.sub(r"\s+", "_", self.vendor_edit.text().strip())
        date_txt = self.date_edit.text().strip()
        if date_txt:
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_txt):
                self._log("Note: acquisition date should look like YYYY-MM-DD - written as typed.")
            meta["acquisitionTime"] = date_txt

        self._log("Clipping to AOI%s…" % (" and reprojecting to EPSG:4326" if reproject else ""))
        t_stage = time.perf_counter()
        info = self._threaded(
            render_weight, 75, core.clip_to_aoi,
            src_path, mask_path, "aoi_mask",
            (ext_src.xMinimum(), ext_src.yMinimum(), ext_src.xMaximum(), ext_src.yMaximum()),
            paths["mosaic_tmp"],
            dst_srs_wkt=dst_crs.toWkt() if reproject else None,
            extent_dst=(ext_dst.xMinimum(), ext_dst.yMinimum(),
                        ext_dst.xMaximum(), ext_dst.yMaximum()),
            res_scale=res_scale, resample=resample,
            compress=compress if keep_mosaic else "NONE",
            src_srs_override=src_crs.toWkt(), extra_metadata=meta)
        note = ""
        if info["exact"]:
            note = (" (palette expanded to RGBA)" if info["palette_expanded"]
                    else " (bit-identical to source pixels)")
        elif info["reprojected"] or res_scale != 1.0:
            note = " (resampled: %s)" % info["resample"]
        self._log("Clipped mosaic: %d x %d px, %d bands, pixel size %.8g%s - %s."
                  % (info["width"], info["height"], info["bands"], info["pixel_x"], note,
                     _fmt_time(time.perf_counter() - t_stage)))

        # ---- 5. tiles -------------------------------------------------------------
        self._log("Splitting into tiles…")
        t_stage = time.perf_counter()
        records = self._threaded(
            75, 95, core.split_into_tiles,
            paths["mosaic_tmp"], paths["tiles_tmp"], basename,
            self.tile_w_spin.value(), self.tile_h_spin.value(),
            skip_empty=self.skip_empty_chk.isChecked(), compress=compress,
            workers=self.workers_spin.value())
        if not records:
            raise RuntimeError("No tiles were produced (the clipped image is empty).")
        self._log("%d tiles written in %s." % (len(records), _fmt_time(time.perf_counter() - t_stage)))

        # ---- 6. tile index -----------------------------------------------------------
        self._log("Writing tile index (%d tiles)…" % len(records))
        mosaic_ds = core.open_ds(paths["mosaic_tmp"])
        srs_wkt = mosaic_ds.GetProjection()
        mosaic_ds = None
        core.write_tile_index(records, paths["index_tmp"], srs_wkt,
                              location_prefix="%s_tiles/" % basename,
                              vendor=meta.get("vendor", ""),
                              acq_date=meta.get("acquisitionTime", ""))

        # ---- 7. swap finished files into place (atomic per file) --------------
        self._commit(paths, basename, keep_mosaic)
        self._apply_tile_index_style(paths["index"])
        self.progress.setValue(98)
        return "%d tiles, mosaic %d x %d px." % (len(records), info["width"], info["height"])

    def _commit(self, paths, basename, keep_mosaic):
        if keep_mosaic:
            self._replace_file(paths["mosaic_tmp"], paths["mosaic"])
        else:
            try:
                os.remove(paths["mosaic_tmp"])
            except OSError:
                pass
        # tiles: remove only OUR previous tiles, then move the new ones in
        tiles, tmp = paths["tiles"], paths["tiles_tmp"]
        os.makedirs(tiles, exist_ok=True)
        pattern = re.compile(r"^%s_r\d+_c\d+\.tif(\.aux\.xml)?$" % re.escape(basename))
        for name in os.listdir(tiles):
            if pattern.match(name):
                try:
                    os.remove(os.path.join(tiles, name))
                except OSError as exc:
                    raise RuntimeError("Cannot replace old tile %s: %s" % (name, exc))
        for name in os.listdir(tmp):
            self._replace_file(os.path.join(tmp, name), os.path.join(tiles, name))
        shutil.rmtree(tmp, ignore_errors=True)
        self._replace_file(paths["index_tmp"], paths["index"])

    # ------------------------------------------------------------- AOI mask
    def _build_mask(self, aoi_layer, mask_crs, dst_crs, mask_path):
        """Write the AOI (valid polygons, in ``mask_crs``) as a GPKG cutline.
        Long edges are densified before reprojection so they follow the true
        curved shape. Returns (extent in mask CRS, extent in dst CRS)."""
        ctx = QgsProject.instance().transformContext()
        needs_transform = aoi_layer.crs() != mask_crs
        to_mask = QgsCoordinateTransform(aoi_layer.crs(), mask_crs, ctx) if needs_transform else None
        same_dst = (dst_crs == mask_crs)
        to_dst = None if same_dst else QgsCoordinateTransform(mask_crs, dst_crs, ctx)
        poly_type = _polygon_type()

        if self.selected_only_chk.isChecked():
            features = aoi_layer.getSelectedFeatures()
        else:
            features = aoi_layer.getFeatures()

        wkbs = []
        ext_m = QgsRectangle()
        ext_m.setMinimal()
        ext_d = QgsRectangle()
        ext_d.setMinimal()
        skipped = 0
        for feat in features:
            geom = feat.geometry()
            if geom is None or geom.isNull() or geom.isEmpty():
                skipped += 1
                continue
            try:
                g_mask = QgsGeometry(geom)
                if needs_transform:
                    bb = g_mask.boundingBox()
                    step = max(bb.width(), bb.height()) / 400.0
                    if step > 0:
                        dense = g_mask.densifyByDistance(step)
                        if dense is not None and not dense.isNull():
                            g_mask = dense
                    g_mask.transform(to_mask)
            except Exception:
                skipped += 1
                continue
            if not g_mask.isGeosValid():
                g_mask = g_mask.makeValid()
            parts = [g for g in g_mask.asGeometryCollection()
                     if g.type() == poly_type and not g.isEmpty()]
            if not parts:
                skipped += 1
                continue
            for part in parts:
                bb = part.boundingBox()
                if not all(math.isfinite(v) for v in
                           (bb.xMinimum(), bb.yMinimum(), bb.xMaximum(), bb.yMaximum())):
                    continue
                wkbs.append(bytes(part.asWkb()))
                ext_m.combineExtentWith(bb)
                if same_dst:
                    ext_d.combineExtentWith(bb)
                else:
                    try:
                        pd = QgsGeometry(part)
                        pd.transform(to_dst)
                        ext_d.combineExtentWith(pd.boundingBox())
                    except Exception:
                        pass
        if skipped:
            self._log("Note: %d AOI feature(s) were empty/invalid and skipped." % skipped)
        if not wkbs:
            raise RuntimeError("The AOI layer has no usable polygon geometry.")
        if not same_dst and (ext_d.isNull() or not ext_d.isFinite()):
            raise RuntimeError("The AOI could not be transformed to %s." % dst_crs.authid())
        core.write_mask(mask_path, mask_crs.toWkt(), wkbs)
        return ext_m, (ext_m if same_dst else ext_d)

    # ----------------------------------------------- online layers (download)
    def _fetch_online(self, image_layer, src_crs, mode, ext_src, mask_path, src_path):
        """Produce ``src_path`` (RGBA GeoTIFF, exact grid) from an online layer:
        direct parallel tile download if possible, else QGIS rendering."""
        parsed = None
        if (self.direct_chk.isChecked() and self._is_xyz(image_layer)
                and src_crs.authid() == "EPSG:3857"):
            parsed = xyz_tools.parse_xyz_source(image_layer.source())
            if parsed is None:
                self._log("This XYZ URL uses placeholders that cannot be downloaded "
                          "directly - using the QGIS renderer.")
        px0, origin, zoom = self._render_grid(image_layer, src_crs, mode, ext_src)

        if parsed is not None and zoom is not None:
            try:
                self._download_xyz(parsed, zoom, ext_src, mask_path, src_path, src_crs)
                return
            except core.Cancelled:
                raise
            except Exception as exc:
                self._log("Direct download not possible (%s) - using the QGIS renderer." % exc)

        px = px0
        x0, y_top, w, h = core.snap_extent(
            ext_src.xMinimum(), ext_src.yMinimum(), ext_src.xMaximum(), ext_src.yMaximum(),
            px, px, origin[0], origin[1])
        self._log("Layer: provider '%s', CRS %s." % (image_layer.providerType(), src_crs.authid()))
        self._log("Resolution: %s." % self._describe_pixel(px, src_crs, ext_src))
        self._confirm_size(w, h, px, src_crs, ext_src, src_path)

        # render a private clone: immune to the user editing / deleting the
        # layer while we work, and without its opacity / brightness settings
        render_layer = self._neutral_clone(image_layer)
        res = core.render_to_geotiff(
            None, src_path, src_crs.toWkt(), x0, y_top, px, px, w, h, block=RENDER_BLOCK,
            progress=self._stage(0, 45), cancel=self._is_cancelled,
            render_batch=self._make_batch_renderer(render_layer, src_crs),
            batch_size=self.parallel_spin.value(),
            block_filter=core.make_block_filter(mask_path, "aoi_mask"))
        self._log("Rendered %d of %d blocks (%d outside the AOI were skipped)."
                  % (res["blocks"] - res["skipped"], res["blocks"], res["skipped"]))

    def _confirm_size(self, w, h, px, crs, extent, near_path):
        mpix = w * h / 1.0e6
        self._log("Size: %.0f megapixels (%d x %d px)." % (mpix, w, h))
        try:
            free = shutil.disk_usage(os.path.dirname(near_path)).free
            need = w * h * 4 * 1.2
            if need > free:
                raise RuntimeError(
                    "Not enough free disk space: the temporary image needs about %.1f GB "
                    "but only %.1f GB are free in the output folder."
                    % (need / 1e9, free / 1e9))
        except OSError:
            pass
        if mpix > 150:
            ans = QMessageBox.question(
                self, "AOI Basemap Clipper",
                "This will produce %.0f megapixels (%d x %d px) at %s.\n\n"
                "If the source data is coarser than this (6-inch imagery is about 0.15 m/px), "
                "the extra pixels add no detail - only time and disk space. Use 'Custom ground "
                "pixel size' to match the data's real resolution.\n\nContinue?"
                % (mpix, w, h, self._describe_pixel(px, crs, extent)))
            if ans != MB_YES:
                raise core.Cancelled()

    def _make_fetcher(self, parsed):
        """Thread-safe tile fetcher built on QGIS' network stack (proxy,
        authentication configs, cache and SSL settings all keep working)."""
        authcfg = parsed.get("authcfg") or ""
        headers = parsed.get("headers") or {}

        def fetch(url):
            req = QNetworkRequest(QUrl(url))
            for k, v in headers.items():
                req.setRawHeader(k.encode("utf-8"), v.encode("utf-8"))
            blocking = QgsBlockingNetworkRequest()
            if authcfg:
                blocking.setAuthCfg(authcfg)
            err = blocking.get(req, False)
            reply = blocking.reply()
            status = reply.attribute(HTTP_STATUS)
            if status in (204, 404, 410):
                return None                         # no such tile -> transparent
            if err != NET_OK:
                raise IOError("HTTP %s %s" % (status if status else "-", blocking.errorMessage()))
            return bytes(reply.content())

        return fetch

    @staticmethod
    def _decode_tile(raw):
        return qimage_to_rgba(QImage.fromData(raw))

    def _probe_xyz(self, parsed, zoom, extent, progress=None, cancel=None):
        """Download one real tile to learn the tile size (256 / 512 px)."""
        fetch = self._make_fetcher(parsed)
        cx = (extent.xMinimum() + extent.xMaximum()) / 2.0
        cy = (extent.yMinimum() + extent.yMaximum()) / 2.0
        cands = [(cx, cy), (extent.xMinimum(), extent.yMinimum()),
                 (extent.xMaximum(), extent.yMaximum()),
                 (extent.xMinimum(), extent.yMaximum()),
                 (extent.xMaximum(), extent.yMinimum())]
        last = "no tile answered"
        for x, y in cands:
            tx, ty = xyz_tools.tile_for_point(x, y, zoom)
            try:
                raw = fetch(xyz_tools.format_url(parsed["url"], zoom, tx, ty))
                if raw:
                    arr = self._decode_tile(raw)
                    if arr.shape[0] == arr.shape[1]:
                        return int(arr.shape[0])
                    last = "non-square tiles"
            except Exception as exc:
                last = str(exc)
        raise RuntimeError(last)

    def _download_xyz(self, parsed, zoom, ext_src, mask_path, src_path, src_crs):
        zmax, zmin = parsed["zmax"], parsed["zmin"]
        if zoom > zmax:
            self._log("Note: zoom %d is above the layer's max zoom %d - using %d." % (zoom, zmax, zmax))
            zoom = zmax
        if zoom < zmin:
            zoom = zmin
        tile_px = self._threaded(0, 1, self._probe_xyz, parsed, zoom, ext_src)
        px = xyz_tools.zoom_pixel_size(zoom, tile_px)
        x0, y_top, w, h = core.snap_extent(
            ext_src.xMinimum(), ext_src.yMinimum(), ext_src.xMaximum(), ext_src.yMaximum(),
            px, px, -xyz_tools.WEB_MERCATOR_ORIGIN, xyz_tools.WEB_MERCATOR_ORIGIN)
        self._log("Direct XYZ download at zoom %d (%d px tiles)." % (zoom, tile_px))
        self._log("Resolution: %s." % self._describe_pixel(px, src_crs, ext_src))
        self._confirm_size(w, h, px, src_crs, ext_src, src_path)
        stats = self._threaded(
            0, 45, core.fetch_xyz_to_geotiff,
            parsed["url"], zoom, tile_px, src_path, src_crs.toWkt(),
            x0, y_top, px, w, h, self._make_fetcher(parsed), self._decode_tile,
            block_filter=core.make_block_filter(mask_path, "aoi_mask"),
            workers=self.dl_threads_spin.value())
        self._log("Downloaded %d tiles (%d outside the AOI were never requested%s)."
                  % (stats["tiles"] - stats["missing"], stats["outside_aoi"],
                     ", %d did not exist on the server" % stats["missing"] if stats["missing"] else ""))

    @staticmethod
    def _neutral_clone(layer):
        """Clone a raster layer and reset opacity / blending / colour filters so
        the exported pixels are the source's, not what the project styling
        happens to show."""
        lyr = layer.clone()
        steps = [
            lambda: lyr.setOpacity(1.0),
            lambda: lyr.renderer().setOpacity(1.0),
            lambda: lyr.setBlendMode(_scoped(QPainter, "CompositionMode", "CompositionMode_SourceOver")),
            lambda: (lyr.brightnessFilter().setBrightness(0),
                     lyr.brightnessFilter().setContrast(0),
                     lyr.brightnessFilter().setGamma(1.0)),
            lambda: (lyr.hueSaturationFilter().setSaturation(0),
                     lyr.hueSaturationFilter().setColorizeOn(False)),
        ]
        for step in steps:
            try:
                step()
            except Exception:
                pass
        return lyr

    # ------------------------------------------------------- render helpers
    def _canvas_pixel_size(self, crs):
        """Map units per screen pixel of the current canvas, expressed in `crs`."""
        canvas = self.iface.mapCanvas()
        canvas_crs = canvas.mapSettings().destinationCrs()
        ext = canvas.extent()
        width_px = max(1, canvas.mapSettings().outputSize().width())
        if canvas_crs != crs:
            tr = QgsCoordinateTransform(canvas_crs, crs, QgsProject.instance().transformContext())
            ext = tr.transformBoundingBox(ext)
        return ext.width() / float(width_px)

    @staticmethod
    def _tile_snapped_pixel(px):
        """Round a Web-Mercator pixel size to the next finer XYZ zoom level."""
        z = int(math.ceil(math.log(xyz_tools.WEB_MERCATOR_RES0 / px, 2) - 0.01))
        z = max(0, min(25, z))
        return core.zoom_pixel_size(z), z

    @staticmethod
    def _mercator_lat(extent):
        return math.degrees(2 * math.atan(math.exp(extent.center().y() / 6378137.0)) - math.pi / 2)

    def _describe_pixel(self, px, crs, extent):
        """Human readable resolution: ground metres per pixel (+ zoom for 3857)."""
        if crs.authid() == "EPSG:3857":
            ground = px * math.cos(math.radians(self._mercator_lat(extent)))
            zoom = math.log(xyz_tools.WEB_MERCATOR_RES0 / px, 2)
            return "%.4g m/px on the ground (~XYZ zoom %.1f)" % (ground, zoom)
        if crs.isGeographic():
            return "%.4g m/px (north-south) on the ground" % (px * 111320.0)
        factor = _meters_factor(crs)
        return "%.4g m/px on the ground" % (px / factor if factor else px)

    def _ground_to_units(self, ground, crs, extent):
        """Convert metres on the ground to pixel size in `crs` map units
        (handles Web-Mercator latitude stretch and feet-based CRSs)."""
        if crs.authid() == "EPSG:3857":
            return ground / math.cos(math.radians(self._mercator_lat(extent)))
        if crs.isGeographic():
            return ground / 111320.0
        return ground * _meters_factor(crs)

    @staticmethod
    def _name_resolution(layer):
        """Ground resolution (m/px) hinted by the layer name / source, e.g.
        '6in' -> 0.1524 m, '30cm' -> 0.30 m. None if nothing is found."""
        text = "%s %s" % (layer.name(), layer.source())
        m = re.search(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(in|inch|cm)(?![a-z])", text, re.IGNORECASE)
        if not m:
            return None
        value = float(m.group(1))
        metres = value * (0.0254 if m.group(2).lower().startswith("in") else 0.01)
        return metres if 0.01 <= metres <= 10 else None

    def _target_pixel_size(self, mode, extent, crs):
        """Pixel size (in source-CRS units) requested by the resolution mode."""
        if mode == MODE_ZOOM:
            return core.zoom_pixel_size(self.zoom_spin.value())
        if mode == MODE_WIDTH:
            return extent.width() / float(self.width_spin.value())
        if mode == MODE_CANVAS:
            return self._canvas_pixel_size(crs)
        if mode == MODE_GROUND:
            return self._ground_to_units(self.ground_spin.value(), crs, extent)
        raise ValueError("No explicit pixel size in auto mode.")

    def _render_grid(self, layer, crs, mode, extent):
        """Choose (pixel size, grid origin, xyz_zoom). ``xyz_zoom`` is set only
        when the grid is exactly an XYZ tile level (direct download possible)."""
        webm = crs.authid() == "EPSG:3857"
        tile_origin = (-xyz_tools.WEB_MERCATOR_ORIGIN, xyz_tools.WEB_MERCATOR_ORIGIN)
        top_left = (extent.xMinimum(), extent.yMaximum())
        xyz = webm and self._is_xyz(layer)

        if mode == MODE_ZOOM and webm:
            z = self.zoom_spin.value()
            return core.zoom_pixel_size(z), tile_origin, (z if xyz else None)
        if mode == MODE_WIDTH:
            return extent.width() / float(self.width_spin.value()), top_left, None
        if mode in (MODE_CANVAS, MODE_GROUND):
            if mode == MODE_CANVAS:
                px = self._canvas_pixel_size(crs)
            else:
                px = self._ground_to_units(self.ground_spin.value(), crs, extent)
            if xyz:
                px, z = self._tile_snapped_pixel(px)
                self._log("Matched to XYZ level %d." % z)
                return px, tile_origin, z
            return px, top_left, None

        # --- auto ---
        if xyz:
            z = self._xyz_zmax(layer)
            self._log("XYZ layer detected, using its max zoom (%d)." % z)
            return core.zoom_pixel_size(z), tile_origin, z

        hinted = self._name_resolution(layer)
        if hinted:
            self._log("Auto: layer name says %.4g m/px (e.g. '6in' = 0.1524 m) - rendering at the "
                      "data's native resolution." % hinted)
            return self._ground_to_units(hinted, crs, extent), top_left, None

        candidates = []
        native = layer.rasterUnitsPerPixelX()
        if native and native > 0:
            candidates.append(native)
        try:
            candidates.append(self._canvas_pixel_size(crs))
        except Exception:
            pass
        if not candidates:
            self._log("No resolution information - using a fixed width of 8192 px.")
            return extent.width() / 8192.0, top_left, None
        self._log("Auto: using the finer of provider / canvas resolution.")
        return min(candidates), top_left, None

    @staticmethod
    def _image_to_array(image, w, h):
        if image.isNull() or image.width() != w or image.height() != h:
            raise RuntimeError("QGIS returned an unexpected render size (%dx%d, wanted %dx%d)."
                               % (image.width(), image.height(), w, h))
        return qimage_to_rgba(image)

    def _make_batch_renderer(self, layer, crs):
        """Return render_batch(specs) -> [RGBA arrays].

        Each spec is (xmin, ymin, xmax, ymax, w, h) with an extent that is
        exactly w*px by h*py, so QgsMapSettings never adjusts it. All jobs of
        a batch are started first and then collected, so network requests of
        online layers overlap."""
        transform_ctx = QgsProject.instance().transformContext()

        def make_job(spec):
            xmin, ymin, xmax, ymax, w, h = spec
            settings = QgsMapSettings()
            settings.setLayers([layer])
            settings.setDestinationCrs(crs)
            settings.setTransformContext(transform_ctx)
            settings.setBackgroundColor(QColor(0, 0, 0, 0))
            settings.setOutputSize(QSize(w, h))
            settings.setExtent(QgsRectangle(xmin, ymin, xmax, ymax))
            settings.setOutputDpi(96)
            for flag in (getattr(getattr(Qgis, "MapSettingsFlag", None), "Antialiasing", None),
                         getattr(QgsMapSettings, "Antialiasing", None)):
                if flag is not None:
                    try:
                        settings.setFlag(flag, False)
                        break
                    except Exception:
                        pass
            job = QgsMapRendererParallelJob(settings)
            job.start()
            return settings, job

        def render_batch(specs):
            started = [make_job(sp) for sp in specs]      # keep settings alive
            arrays = []
            for spec, (settings, job) in zip(specs, started):
                job.waitForFinished()
                arrays.append(self._image_to_array(job.renderedImage(), spec[4], spec[5]))
            return arrays

        return render_batch

    # ------------------------------------------------------------ styling
    def _apply_tile_index_style(self, index_path):
        """Embed the bundled style in the GeoPackage and optionally add the
        tile index to the project."""
        vlayer = QgsVectorLayer("%s|layername=tileindex" % index_path, "tileindex", "ogr")
        if not vlayer.isValid():
            self._log("Note: could not open tile index to apply style.")
            return
        style_file = self._style_file()
        if style_file:
            vlayer.loadNamedStyle(style_file)
            try:
                vlayer.saveStyleToDatabase("default", "AOI Basemap Clipper tile index style", True, "")
            except Exception as exc:
                self._log("Note: could not embed style in GeoPackage (%s)." % exc)
        else:
            self._log("Note: bundled tile index style not found, skipping styling.")
        if style_file and style_file != TILE_INDEX_STYLE:
            try:
                os.remove(style_file)           # temp copy without actions
            except OSError:
                pass
        vlayer.triggerRepaint()
        if self.add_to_project_chk.isChecked():
            QgsProject.instance().addMapLayer(vlayer)

    def _style_file(self):
        """Bundled QML; without the Python actions if the user unticked them."""
        if not os.path.exists(TILE_INDEX_STYLE):
            return None
        if self.actions_chk.isChecked():
            return TILE_INDEX_STYLE
        with open(TILE_INDEX_STYLE, encoding="utf-8") as fh:
            text = fh.read()
        text = re.sub(r"<attributeactions>.*?</attributeactions>",
                      "<attributeactions/>", text, flags=re.S)
        fd, path = tempfile.mkstemp(suffix=".qml", prefix="aoi_style_")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def reject(self):
        # Esc / window-manager close while a job runs: stop it, then close
        if self._running:
            self._cancelled = True
            self._close_after_run = True
            self._log("Stopping… the window closes when the job has stopped.")
            return
        super().reject()

    def closeEvent(self, event):
        if self._running:
            self._cancelled = True
            self._close_after_run = True
            self._log("Stopping… the window closes when the job has stopped.")
            event.ignore()
            return
        super().closeEvent(event)
