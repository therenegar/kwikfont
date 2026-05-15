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
cd src
python -m fontmgr.app
```

## Notes

- User font operations write to `~/.fonts`.
- System fonts are listed from `/usr/share/fonts`; uninstalling system fonts is intentionally skipped to avoid requiring elevated privileges or deleting distribution-managed files.
- Font previews and PDF specimens use `fonttools` to convert font glyphs into outlines so they do not depend on the font already being installed.
- Font family, style, and display names are read from OpenType/TrueType/Type 1 metadata, with filename parsing used only as a fallback for malformed or unsupported files.
