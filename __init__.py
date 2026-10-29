# -*- coding: utf-8 -*-
"""
AOI Basemap Clipper
--------------------
Entry point required by QGIS. QGIS calls classFactory(iface) to load the
plugin when it starts (or when the user enables it in the Plugin Manager).
"""


def classFactory(iface):
    from .aoi_basemap_clipper import AOIBasemapClipper
    return AOIBasemapClipper(iface)
