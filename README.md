# enBox Font Manager

enBox Font Manager is a standalone GTK utility for browsing, previewing, installing, uninstalling, grouping, and printing specimens for fonts on Linux. It is designed to be shipped with, or launched from, a directory of font files so the startup **Browse Fonts** tab immediately shows the fonts next to the executable.

## Features

- GTK desktop application with File, Edit, View, and Help menus.
- Toolbar actions for refresh, view, print listing, install, uninstall, and search.
- **Browse Fonts** tab for inspecting fonts in the launch directory or any selected filesystem folder.
- **My Fonts** tab for installed user fonts (`~/.fonts`), system fonts (`/usr/share/fonts`), and user-created font groups.
- Toggle between font preview tiles and sortable file metadata listings.
- Modal font viewer with font metadata, editable preview text, and size selector from 8 pt to 72 pt.
- User font installation and uninstallation with best-effort `fc-cache` refresh.
- Font groups backed by directories in `~/.fonts`.
- PDF font catalog/specimen generation for selected fonts, with preview text drawn from font outlines.

## Requirements

Install the native GTK/Python bindings supplied by your Linux distribution. On Debian or Ubuntu:

```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-gdkpixbuf-2.0 python3-cairo fontconfig
```

## Run from source

```bash
python -m fontmgr.app
```

When using the packaged console script:

```bash
enbox-font-manager
```

## Install from source

```bash
python -m pip install .
enbox-font-manager
```

## Notes

- User font operations write to `~/.fonts`.
- System fonts are listed from `/usr/share/fonts`; uninstalling system fonts is intentionally skipped to avoid requiring elevated privileges or deleting distribution-managed files.
- Font previews and PDF specimens use `fonttools` to convert font glyphs into outlines so they do not depend on the font already being installed.
- Font family, style, and display names are read from OpenType/TrueType/Type 1 metadata, with filename parsing used only as a fallback for malformed or unsupported files.
