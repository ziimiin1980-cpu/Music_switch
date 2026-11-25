# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec.

Notes (see docs/freezing.md in pywinrt):
* winrt-runtime registers its own PyInstaller hook, which collects every
  installed ``winrt-*`` projection package plus the ``_table.pywinrt`` files.
  Those tables must stay real files, so use --onedir (``COLLECT``), never a
  zip archive.
* The UI is Qt Widgets only.  QML/Quick, WebEngine, Multimedia, Charts and the
  other add-on modules are explicitly excluded: they are installed by the
  PySide6 meta-package but never imported, and each of them adds tens of MB.
  PyInstaller's PySide6 hook still copies every Qt DLL and plugin it finds, so
  the TOC is filtered below down to QtCore/QtGui/QtWidgets, the Windows
  platform plugin and the Windows 11 style.
"""

from pathlib import Path

root = Path(SPECPATH)

a = Analysis(
    [str(root / "app.py")],
    pathex=[str(root)],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # Qt add-ons (PySide6 meta-package installs all of them)
        "PySide6.QtQml",
        "PySide6.QtQuick",
        "PySide6.QtQuickWidgets",
        "PySide6.QtQuickControls2",
        "PySide6.QtQuickTest",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebEngineQuick",
        "PySide6.QtWebChannel",
        "PySide6.QtWebSockets",
        "PySide6.QtNetwork",
        "PySide6.QtMultimedia",
        "PySide6.QtMultimediaWidgets",
        "PySide6.QtSpatialAudio",
        "PySide6.Qt3DCore",
        "PySide6.Qt3DRender",
        "PySide6.Qt3DInput",
        "PySide6.Qt3DLogic",
        "PySide6.Qt3DAnimation",
        "PySide6.Qt3DExtras",
        "PySide6.QtCharts",
        "PySide6.QtDataVisualization",
        "PySide6.QtGraphs",
        "PySide6.QtPdf",
        "PySide6.QtPdfWidgets",
        "PySide6.QtSql",
        "PySide6.QtTest",
        "PySide6.QtBluetooth",
        "PySide6.QtPositioning",
        "PySide6.QtLocation",
        "PySide6.QtSerialPort",
        "PySide6.QtSensors",
        "PySide6.QtNfc",
        "PySide6.QtRemoteObjects",
        "PySide6.QtScxml",
        "PySide6.QtStateMachine",
        "PySide6.QtTextToSpeech",
        "PySide6.QtHelp",
        "PySide6.QtUiTools",
        "PySide6.QtDesigner",
        "PySide6.QtOpenGL",
        "PySide6.QtOpenGLWidgets",
        "PySide6.QtSvg",
        "PySide6.QtSvgWidgets",
        # Pillow AVIF codec: never used for SMTC artwork (PNG/JPEG), 8 MB
        "PIL.AvifImagePlugin",
        "PIL._avif",
        # stdlib / test leftovers
        "tkinter",
        "unittest",
        "pydoc",
    ],
    noarchive=False,
)

# The PySide6 hook copies all Qt DLLs and plugins regardless of ``excludes``.
# A pure Qt Widgets app only needs QtCore/QtGui/QtWidgets, platforms/qwindows
# and the Windows 11 style; the rest is 50+ MB of dead weight.
_UNUSED_DLLS = {
    "qt6qml.dll",
    "qt6qmlmeta.dll",
    "qt6qmlmodels.dll",
    "qt6qmlworkerscript.dll",
    "qt6quick.dll",
    "qt6pdf.dll",
    "qt6network.dll",
    "qt6opengl.dll",
    "qt6svg.dll",
    "qt6virtualkeyboard.dll",
    "opengl32sw.dll",
}
_KEPT_PLUGINS = {
    "pyside6/plugins/platforms/qwindows.dll",
    "pyside6/plugins/styles/qmodernwindowsstyle.dll",
}


def _keep(entry) -> bool:
    dest = entry[0].replace("\\", "/").lower()
    name = dest.rsplit("/", 1)[-1]
    if name in _UNUSED_DLLS or name.startswith("_avif."):
        return False
    if "/plugins/" in dest:
        return dest in _KEPT_PLUGINS
    if "/translations/" in dest:
        return False
    return True


a.binaries = [entry for entry in a.binaries if _keep(entry)]
a.datas = [entry for entry in a.datas if _keep(entry)]

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Pereklych",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(root / "build" / "app.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Pereklych",
)
