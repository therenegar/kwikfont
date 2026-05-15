# Kwik Font

Kwik Font is a GTK utility for browsing and managing fonts on the Linux desktop.

### Browse fonts
<img width="1154" height="885" alt="Browse" src="https://github.com/user-attachments/assets/7847111c-b719-40c1-ad3f-84f2be0127f3" />

### Manage fonts
<img width="1154" height="885" alt="Manage" src="https://github.com/user-attachments/assets/8c530f46-f19a-4f92-9937-51fe66c17953" />

### Create PDF specimens
<img width="2479" height="3508" alt="font-catalog" src="https://github.com/user-attachments/assets/2a5fa63b-7e80-4b0c-b161-7529534ce31e" />


## Requirements

On Debian, install pre-requisites

```bash
sudo apt install python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-gdkpixbuf-2.0 python3-cairo fontconfig
```

## Run from source

```bash
./kwik-font
```

Alternatively, run the module directly:

```bash
PYTHONPATH=src python3 -m kwikfont.app
```

## Build a Debian package

Kwik Font includes a lightweight package builder that creates an installable `.deb` with the application code, launcher, desktop entry, and Debian dependency metadata. The script only requires standard Debian packaging tools such as `dpkg-deb`; it does not require `debhelper`.

1. Install build-time tooling:

   ```bash
   sudo apt update
   sudo apt install dpkg-dev python3
   ```

2. Build the package from the repository root:

   ```bash
   ./scripts/build-deb.sh
   ```

3. Install the generated package:

   ```bash
   sudo apt install ./dist/kwik-font_0.1.0_all.deb
   ```

4. Start Kwik Font from your application launcher, or run:

   ```bash
   kwik-font
   ```

The package declares runtime dependencies on `python3`, GTK/PyGObject bindings, Cairo, FontTools, and Fontconfig. If you install the package with `apt`, missing dependencies are resolved automatically from your configured repositories. If you install with `dpkg -i`, run `sudo apt -f install` afterwards to resolve any missing dependencies.

## Notes

- User font operations write to `~/.fonts`.
- System fonts are listed from `/usr/share/fonts`; uninstalling system fonts is intentionally skipped to avoid requiring elevated privileges or deleting distribution-managed files.
- Font previews and PDF specimens use `fonttools` to convert font glyphs into outlines so they do not depend on the font already being installed.
- Font family, style, and display names are read from OpenType/TrueType/Type 1 metadata, with filename parsing used only as a fallback for malformed or unsupported files.
