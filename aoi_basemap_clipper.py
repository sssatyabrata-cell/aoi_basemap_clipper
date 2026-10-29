# -*- coding: utf-8 -*-
import os

from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtGui import QIcon


class AOIBasemapClipper:
    """QGIS plugin wrapper: adds a toolbar/menu button that opens the
    AOI Basemap Clipper dialog."""

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.actions = []
        self.menu = "&AOI Basemap Clipper"
        self.toolbar = self.iface.addToolBar("AOI Basemap Clipper")
        self.toolbar.setObjectName("AOIBasemapClipperToolbar")
        self.dlg = None

    def initGui(self):
        icon_path = os.path.join(self.plugin_dir, "icon.png")
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()
        action = QAction(icon, "AOI Basemap Clipper", self.iface.mainWindow())
        action.triggered.connect(self.run)
        action.setEnabled(True)
        self.toolbar.addAction(action)
        self.iface.addPluginToRasterMenu(self.menu, action)
        self.actions.append(action)

    def unload(self):
        for action in self.actions:
            self.iface.removePluginRasterMenu(self.menu, action)
            if self.toolbar is not None:
                self.toolbar.removeAction(action)
        self.actions = []
        if self.dlg is not None:
            self.dlg.close()
            self.dlg.deleteLater()
            self.dlg = None
        if self.toolbar is not None:
            self.iface.mainWindow().removeToolBar(self.toolbar)
            self.toolbar.deleteLater()
            self.toolbar = None

    def run(self):
        # Imported here so QGIS starts fast and any dependency problem only
        # surfaces when the tool is actually used.
        from .aoi_basemap_clipper_dialog import AOIBasemapClipperDialog

        if self.dlg is None:
            # parented to the QGIS main window: stays on top of it, closes with it
            self.dlg = AOIBasemapClipperDialog(self.iface, self.iface.mainWindow())
        self.dlg.show()
        self.dlg.raise_()
        self.dlg.activateWindow()
