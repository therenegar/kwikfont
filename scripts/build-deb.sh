#!/usr/bin/env bash
set -euo pipefail

repo_root="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

pkg_name="kwik-font"
version="$(python3 - <<'PY'
from pathlib import Path
import re
text = Path('pyproject.toml').read_text(encoding='utf-8')
match = re.search(r'^version\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
if not match:
    raise SystemExit('Unable to read project.version from pyproject.toml')
print(match.group(1))
PY
)"
arch="all"
build_root="$repo_root/build/deb/${pkg_name}_${version}_${arch}"
out_dir="$repo_root/dist"

mkdir -p "$out_dir"
rm -rf "$build_root"
mkdir -p \
  "$build_root/DEBIAN" \
  "$build_root/usr/bin" \
  "$build_root/usr/lib/$pkg_name" \
  "$build_root/usr/share/applications" \
  "$build_root/usr/share/doc/$pkg_name"

cp -a src/kwikfont "$build_root/usr/lib/$pkg_name/"
find "$build_root/usr/lib/$pkg_name" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$build_root/usr/lib/$pkg_name" -type f -name "*.py[co]" -delete
install -m 0644 data/com.kwikfont.KwikFont.desktop \
  "$build_root/usr/share/applications/com.kwikfont.KwikFont.desktop"
install -m 0644 README.md "$build_root/usr/share/doc/$pkg_name/README.md"
install -m 0644 packaging/debian/copyright "$build_root/usr/share/doc/$pkg_name/copyright"

cat > "$build_root/usr/bin/kwik-font" <<'WRAPPER'
#!/bin/sh
set -eu
export PYTHONPATH="/usr/lib/kwik-font${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m kwikfont.app "$@"
WRAPPER
chmod 0755 "$build_root/usr/bin/kwik-font"

installed_size="$(du -sk "$build_root" | awk '{print $1}')"
cat > "$build_root/DEBIAN/control" <<CONTROL
Package: $pkg_name
Version: $version
Section: graphics
Priority: optional
Architecture: $arch
Maintainer: Kwik Font <maintainers@kwikfont.local>
Installed-Size: $installed_size
Depends: python3 (>= 3.10), python3-gi, python3-gi-cairo, gir1.2-gtk-3.0, gir1.2-gdkpixbuf-2.0, python3-cairo, python3-fonttools, fontconfig
Description: GTK font browser and manager for Linux
 Kwik Font is a GTK utility for browsing installed and local fonts,
 installing user fonts, uninstalling user-managed fonts, and generating
 printable PDF font catalog/specimen sheets.
CONTROL

dpkg-deb --build --root-owner-group "$build_root" "$out_dir/${pkg_name}_${version}_${arch}.deb"

echo "Built $out_dir/${pkg_name}_${version}_${arch}.deb"
