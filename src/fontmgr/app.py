"""Kwik Font GTK font browser, installer, and catalog generator for Linux."""

from __future__ import annotations

import datetime as dt
import re
import shutil
import subprocess
from functools import lru_cache
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import cairo
import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango, PangoCairo  # noqa: E402

FONT_EXTENSIONS = {".ttf", ".otf", ".ttc", ".otc", ".woff", ".woff2", ".pfb", ".pfa"}
USER_FONT_DIR = Path.home() / ".fonts"
SYSTEM_FONT_DIR = Path("/usr/share/fonts")
SAMPLE_TEXT = "AaBbCcDdEeFfGgHhIiJjKkLlMmNnOoPpQqRrSsTtUuVvWwXxYyZz"
APP_NAME = "Kwik Font"
APP_ID = "com.kwikfont.KwikFont"


@dataclass(frozen=True)
class FontRecord:
    """Metadata for one font file shown by the application."""

    path: Path
    name: str
    family: str
    style: str
    kind: str
    size: int
    modified: str
    installed_scope: str | None = None

    @property
    def installed(self) -> bool:
        return self.installed_scope is not None

    @property
    def display_path(self) -> str:
        try:
            return str(self.path.relative_to(Path.home())).replace(str(Path.home()), "~")
        except ValueError:
            return str(self.path)


@dataclass(frozen=True)
class FontIdentity:
    """Names read from a font file's own metadata."""

    family: str
    name: str
    style: str


def apply_css(widget: Gtk.Widget, css: str) -> None:
    """Attach replacement CSS to a single widget without accumulating providers."""

    context = widget.get_style_context()
    previous = getattr(widget, "_fontmgr_css_provider", None)
    if previous is not None:
        context.remove_provider(previous)
    provider = Gtk.CssProvider()
    provider.load_from_data(css.encode())
    context.add_provider(provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    setattr(widget, "_fontmgr_css_provider", provider)



def is_font_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in FONT_EXTENSIONS


def extension_file_kind(path: Path) -> str:
    return {
        ".ttf": "TrueType",
        ".ttc": "TrueType Collection",
        ".otf": "OpenType",
        ".otc": "OpenType Collection",
        ".woff": "Web Open Font",
        ".woff2": "Web Open Font 2",
        ".pfb": "PostScript Type 1",
        ".pfa": "PostScript Type 1",
    }.get(path.suffix.lower(), path.suffix.upper().lstrip("."))


def file_kind(path: Path) -> str:
    """Determine font technology from the font file itself when possible."""

    if path.suffix.lower() in {".pfb", ".pfa"}:
        return "PostScript Type 1"
    try:
        from fontTools.ttLib import TTCollection, TTFont

        if path.suffix.lower() in {".ttc", ".otc"}:
            collection = TTCollection(str(path))
            font = collection.fonts[0]
            collection_suffix = " Collection"
        else:
            font = TTFont(str(path), lazy=True)
            collection_suffix = ""
        if getattr(font, "flavor", None) == "woff":
            return "Web Open Font"
        if getattr(font, "flavor", None) == "woff2":
            return "Web Open Font 2"
        if "CFF " in font or "CFF2" in font:
            return f"OpenType/CFF{collection_suffix}"
        if "glyf" in font:
            return f"TrueType{collection_suffix}"
        return f"OpenType{collection_suffix}"
    except Exception:
        return extension_file_kind(path)


def clean_font_name(value: str | None) -> str | None:
    """Normalize a raw name-table or Type 1 metadata string."""

    if value is None:
        return None
    cleaned = " ".join(value.replace("\x00", " ").strip().split())
    if not cleaned or set(cleaned) <= {"-"}:
        return None
    return cleaned


def select_name_record(names: list[str | None]) -> str | None:
    """Return the first usable font metadata name from a priority-ordered list."""

    for name in names:
        cleaned = clean_font_name(name)
        if cleaned:
            return cleaned
    return None


def opentype_name(font, *name_ids: int) -> str | None:
    """Read a preferred English Unicode name from an OpenType/TrueType name table."""

    if "name" not in font:
        return None
    name_table = font["name"]
    records = [record for record in name_table.names if record.nameID in name_ids]
    english_records = [
        record
        for record in records
        if (record.platformID == 3 and record.langID in {0x0409, 0}) or (record.platformID == 1 and record.langID == 0)
    ]
    unicode_records = [record for record in records if record.isUnicode()]
    for record in english_records + unicode_records + records:
        try:
            cleaned = clean_font_name(record.toUnicode())
        except Exception:
            cleaned = clean_font_name(str(record))
        if cleaned:
            return cleaned
    return None


def opentype_identity(path: Path) -> FontIdentity:
    """Read font names from OpenType/TrueType metadata."""

    from fontTools.ttLib import TTCollection, TTFont

    if path.suffix.lower() in {".ttc", ".otc"}:
        collection = TTCollection(str(path))
        font = collection.fonts[0]
    else:
        font = TTFont(str(path), lazy=True)
    family = opentype_name(font, 16, 1)
    style = opentype_name(font, 17, 2) or "Regular"
    full_name = opentype_name(font, 4)
    postscript_name = opentype_name(font, 6)
    name = select_name_record([full_name, postscript_name, f"{family} {style}" if family else None, family, path.stem])
    family = select_name_record([family, name, path.stem]) or path.stem
    style = select_name_record([style, "Regular"]) or "Regular"
    name = name or family
    return FontIdentity(family=family, name=name, style=style)


def parse_type1_string_metadata(text: str, key: str) -> str | None:
    """Extract a simple Type 1 dictionary value."""

    patterns = [
        rf"/{re.escape(key)}\s*\((.*?)\)",
        rf"/{re.escape(key)}\s*/([^\s{{}}\[\]()/]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.DOTALL)
        if match:
            return clean_font_name(match.group(1).replace("\\(", "(").replace("\\)", ")"))
    return None


def type1_identity(path: Path) -> FontIdentity:
    """Read names from Type 1 font dictionaries."""

    text = path.read_bytes()[:262_144].decode("latin-1", errors="ignore")
    family = parse_type1_string_metadata(text, "FamilyName")
    style = parse_type1_string_metadata(text, "Weight") or "Regular"
    full_name = parse_type1_string_metadata(text, "FullName")
    font_name = parse_type1_string_metadata(text, "FontName")
    name = select_name_record([full_name, font_name, f"{family} {style}" if family else None, path.stem])
    family = select_name_record([family, name, path.stem]) or path.stem
    style = select_name_record([style, "Regular"]) or "Regular"
    return FontIdentity(family=family, name=name or family, style=style)


def filename_identity(path: Path) -> FontIdentity:
    """Fallback identity when a file cannot be parsed as a supported font."""

    stem = path.stem.replace("_", " ")
    parts = stem.replace("-", " ").split()
    style_words = {
        "thin",
        "extralight",
        "ultralight",
        "light",
        "regular",
        "book",
        "medium",
        "semibold",
        "demibold",
        "bold",
        "extrabold",
        "ultrabold",
        "black",
        "heavy",
        "italic",
        "oblique",
        "condensed",
        "expanded",
        "narrow",
    }
    style = "Regular"
    if parts and parts[-1].lower() in style_words:
        style = parts[-1].title()
        family = " ".join(parts[:-1]) or stem
    else:
        family = " ".join(parts) or stem
    return FontIdentity(family=family, name=stem, style=style)


def font_identity(path: Path) -> FontIdentity:
    """Read font identity from OpenType/TrueType/Type 1 metadata."""

    try:
        if path.suffix.lower() in {".pfb", ".pfa"}:
            return type1_identity(path)
        return opentype_identity(path)
    except Exception:
        return filename_identity(path)


def discover_fonts(folder: Path, recursive: bool = False) -> list[FontRecord]:
    paths: Iterable[Path]
    paths = folder.rglob("*") if recursive else folder.iterdir() if folder.exists() else []
    records: list[FontRecord] = []
    for path in sorted((p for p in paths if is_font_file(p)), key=lambda p: p.name.lower()):
        identity = font_identity(path)
        stat = path.stat()
        scope = None
        try:
            path.relative_to(USER_FONT_DIR)
            scope = "user"
        except ValueError:
            try:
                path.relative_to(SYSTEM_FONT_DIR)
                scope = "system"
            except ValueError:
                scope = None
        records.append(
            FontRecord(
                path=path,
                name=identity.name,
                family=identity.family,
                style=identity.style,
                kind=file_kind(path),
                size=stat.st_size,
                modified=dt.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d"),
                installed_scope=scope,
            )
        )
    return records


def discover_installed_fonts(scope: str = "all") -> list[FontRecord]:
    """Discover installed fonts from the known user/system font roots only."""

    if scope == "user":
        return discover_fonts(USER_FONT_DIR, recursive=True)
    if scope == "system":
        return discover_fonts(SYSTEM_FONT_DIR, recursive=True)
    return discover_fonts(USER_FONT_DIR, recursive=True) + discover_fonts(SYSTEM_FONT_DIR, recursive=True)


def human_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


@lru_cache(maxsize=64)
def load_outline_font(font_path: str):
    """Load a fontTools font and reusable tables for outline rendering."""

    from fontTools.ttLib import TTFont

    font = TTFont(font_path)
    glyph_set = font.getGlyphSet()
    cmap = font.getBestCmap() or {}
    hmtx = font["hmtx"].metrics if "hmtx" in font else {}
    units_per_em = font["head"].unitsPerEm if "head" in font else 1000
    return font, glyph_set, cmap, hmtx, units_per_em


def glyph_advance(glyph_name: str | None, hmtx: dict, units_per_em: int) -> int:
    if glyph_name and glyph_name in hmtx:
        return hmtx[glyph_name][0]
    return int(units_per_em * 0.35 if glyph_name is None else units_per_em * 0.6)


class CairoOutlinePen:
    """fontTools pen that replays glyph contours into a Cairo context."""

    def __init__(self, ctx: cairo.Context):
        from fontTools.pens.basePen import BasePen

        class _Pen(BasePen):
            def __init__(self, glyph_set):
                super().__init__(glyph_set)
                self.current_point = (0.0, 0.0)

            def _moveTo(self, point):
                self.current_point = point
                ctx.move_to(*point)

            def _lineTo(self, point):
                self.current_point = point
                ctx.line_to(*point)

            def _curveToOne(self, point1, point2, point3):
                self.current_point = point3
                ctx.curve_to(point1[0], point1[1], point2[0], point2[1], point3[0], point3[1])

            def _qCurveToOne(self, point1, point2):
                point0 = self.current_point
                curve1 = (point0[0] + (2.0 / 3.0) * (point1[0] - point0[0]), point0[1] + (2.0 / 3.0) * (point1[1] - point0[1]))
                curve2 = (point2[0] + (2.0 / 3.0) * (point1[0] - point2[0]), point2[1] + (2.0 / 3.0) * (point1[1] - point2[1]))
                self._curveToOne(curve1, curve2, point2)

            def _closePath(self):
                ctx.close_path()

        self.pen_class = _Pen

    def for_glyph_set(self, glyph_set):
        return self.pen_class(glyph_set)


def draw_outline_text(ctx: cairo.Context, record: FontRecord, text: str, x: float, baseline: float, font_size: float, max_width: float | None = None) -> None:
    """Draw text as filled font outlines on a Cairo context."""

    try:
        _font, glyph_set, cmap, hmtx, units_per_em = load_outline_font(str(record.path))
    except Exception:
        ctx.move_to(x, baseline)
        ctx.show_text(text)
        return

    scale = font_size / units_per_em
    pen_x = 0.0
    pen_factory = CairoOutlinePen(ctx)
    for character in text:
        glyph_name = cmap.get(ord(character))
        advance = glyph_advance(glyph_name, hmtx, units_per_em)
        if max_width is not None and pen_x * scale > max_width:
            break
        if glyph_name and glyph_name in glyph_set:
            ctx.save()
            ctx.translate(x + pen_x * scale, baseline)
            ctx.scale(scale, -scale)
            glyph_set[glyph_name].draw(pen_factory.for_glyph_set(glyph_set))
            ctx.fill()
            ctx.restore()
        pen_x += advance


def create_outline_pixbuf(record: FontRecord, text: str, width: int, height: int, font_size: int, fill: tuple[float, float, float]) -> GdkPixbuf.Pixbuf | None:
    """Render a font preview pixbuf using the same Cairo outlines as PDF output."""

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    ctx = cairo.Context(surface)
    ctx.set_operator(cairo.OPERATOR_CLEAR)
    ctx.paint()
    ctx.set_operator(cairo.OPERATOR_OVER)
    ctx.set_source_rgb(*fill)
    draw_outline_text(ctx, record, text, 0, min(height - 8, int(height * 0.78)), font_size, max_width=width)
    surface.flush()
    return Gdk.pixbuf_get_from_surface(surface, 0, 0, width, height)


def create_wrapped_outline_pixbuf(record: FontRecord, text: str, width: int, height: int, font_size: int, fill: tuple[float, float, float]) -> GdkPixbuf.Pixbuf | None:
    """Render wrapped preview text onto an opaque white pixbuf."""

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    ctx = cairo.Context(surface)
    ctx.set_source_rgb(1.0, 1.0, 1.0)
    ctx.paint()
    ctx.set_source_rgb(*fill)
    line_height = int(font_size * 1.35)
    baseline = font_size + 10
    current_line = ""

    def draw_line(line: str, y: int) -> None:
        draw_outline_text(ctx, record, line, 0, y, font_size, max_width=width)

    for word in text.split():
        candidate = f"{current_line} {word}".strip()
        if current_line and len(candidate) * font_size * 0.55 > width:
            draw_line(current_line, baseline)
            baseline += line_height
            current_line = word
            if baseline > height - 8:
                break
        else:
            current_line = candidate
    if current_line and baseline <= height - 8:
        draw_line(current_line, baseline)
    surface.flush()
    return Gdk.pixbuf_get_from_surface(surface, 0, 0, width, height)


class FontTile(Gtk.EventBox):
    """Selectable fixed-size preview tile for a font."""

    WIDTH = 240
    HEIGHT = 96
    MARGIN = 8
    PREVIEW_WIDTH = WIDTH - (MARGIN * 2)
    PREVIEW_HEIGHT = 50

    def __init__(self, record: FontRecord, on_select, on_open):
        super().__init__()
        self.record = record
        self.selected = False
        self.on_select = on_select
        self.on_open = on_open
        self.set_visible_window(True)
        self.set_size_request(self.WIDTH, self.HEIGHT)
        self.set_halign(Gtk.Align.START)
        self.set_valign(Gtk.Align.START)
        self.set_hexpand(False)
        self.set_vexpand(False)
        self.style_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin=self.MARGIN)
        self.style_box.set_size_request(self.PREVIEW_WIDTH, self.HEIGHT - (self.MARGIN * 2))
        self.style_box.get_style_context().add_class("font-tile")
        name = Gtk.Label(label=record.name, xalign=0)
        name.set_halign(Gtk.Align.START)
        name.set_ellipsize(Pango.EllipsizeMode.END)
        name.set_width_chars(1)
        self.preview = Gtk.Image()
        self.preview.set_halign(Gtk.Align.START)
        self.preview.set_size_request(self.PREVIEW_WIDTH, self.PREVIEW_HEIGHT)
        self.style_box.pack_start(name, False, False, 0)
        self.style_box.pack_start(self.preview, False, False, 0)
        self.add(self.style_box)
        self.connect("button-press-event", self._clicked)
        self.preview_loaded = False
        self.update_style()

    def _clicked(self, _widget, event):
        if event.type == Gdk.EventType._2BUTTON_PRESS:
            self.on_select(self.record, False)
            self.on_open(self.record)
        else:
            self.on_select(self.record, bool(event.state & Gdk.ModifierType.CONTROL_MASK))
        return True

    def set_selected(self, selected: bool) -> None:
        if self.selected == selected:
            return
        self.selected = selected
        self.update_style()
        if self.preview_loaded:
            self.update_preview()

    def ensure_preview(self) -> None:
        if self.preview_loaded:
            return
        self.update_preview()

    def update_preview(self) -> None:
        fill = (1.0, 1.0, 1.0) if self.selected else (0.0, 0.0, 0.0)
        pixbuf = create_outline_pixbuf(self.record, self.record.name, self.PREVIEW_WIDTH, self.PREVIEW_HEIGHT, 38, fill)
        if pixbuf is not None:
            self.preview.set_from_pixbuf(pixbuf)
            self.preview_loaded = True

    def update_style(self) -> None:
        css = (
            "background-color: #111111; color: #ffffff; border: 2px solid #111111;"
            if self.selected
            else "background-color: #ffffff; color: #111111; border: 1px solid #111111;"
        )
        apply_css(self.style_box, f".font-tile {{ {css} }} .font-tile * {{ color: inherit; }}")


class FontPane(Gtk.Box):
    """Shared Browse Fonts/My Fonts layout."""

    def __init__(self, app: "FontManagerWindow", mode: str, autoload: bool = True):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin=16)
        self.app = app
        self.mode = mode
        self.all_records: list[FontRecord] = []
        self.records: list[FontRecord] = []
        self.selected_records: list[FontRecord] = []
        self.search_query = ""
        self.recent_folders: list[Path] = []
        self.loaded = False
        self.lazy_preview_source_id = 0
        self.current_folder = Path.cwd()
        self.show_fonts = Gtk.RadioButton.new_with_label_from_widget(None, "Show fonts")
        self.show_files = Gtk.RadioButton.new_with_label_from_widget(self.show_fonts, "Show files")
        self.show_fonts.set_active(True)
        self.show_fonts.connect("toggled", lambda _b: self.refresh())
        self._build()
        if autoload:
            self.refresh()

    def _build(self) -> None:
        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        heading = Gtk.Label(label="Browse Fonts" if self.mode == "browse" else "My Fonts", xalign=0)
        heading.get_style_context().add_class("title-1")
        apply_css(heading, "label { font-size: 28pt; }")
        top.pack_start(heading, True, True, 0)
        top.pack_start(self.show_fonts, False, False, 8)
        top.pack_start(self.show_files, False, False, 0)
        self.pack_start(top, False, False, 0)

        content = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        content.set_wide_handle(True)
        self.sidebar = self._build_sidebar()
        content.pack1(self.sidebar, resize=False, shrink=False)
        self.stack = Gtk.Stack()
        self.flowbox = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, min_children_per_line=1, max_children_per_line=100)
        self.flowbox.set_homogeneous(False)
        self.flowbox.set_valign(Gtk.Align.START)
        self.flow_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vscrollbar_policy=Gtk.PolicyType.AUTOMATIC)
        self.flow_scroll.add(self.flowbox)
        self.flow_scroll.connect("size-allocate", lambda *_args: self.queue_visible_preview_update())
        self.flowbox.connect("size-allocate", lambda *_args: self.queue_visible_preview_update())
        self.flow_scroll.get_hadjustment().connect("value-changed", lambda *_args: self.queue_visible_preview_update())
        self.flow_scroll.get_vadjustment().connect("value-changed", lambda *_args: self.queue_visible_preview_update())
        self.file_store = Gtk.ListStore(str, str, str, str, str, str, object)
        self.file_view = Gtk.TreeView(model=self.file_store)
        for idx, title in enumerate(("Name", "Family", "Type", "File Name", "Size", "Date")):
            renderer = Gtk.CellRendererText()
            column = Gtk.TreeViewColumn(title, renderer, text=idx)
            column.set_sort_column_id(idx)
            column.set_resizable(True)
            self.file_view.append_column(column)
        self.file_view.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        self.file_view.get_selection().connect("changed", self._file_selection_changed)
        self.file_view.connect("row-activated", self._file_row_activated)
        file_scroll = Gtk.ScrolledWindow(vscrollbar_policy=Gtk.PolicyType.ALWAYS)
        file_scroll.add(self.file_view)
        self.stack.add_named(self.flow_scroll, "fonts")
        self.stack.add_named(file_scroll, "files")
        content.pack2(self.stack, resize=True, shrink=False)
        self.pack_start(content, True, True, 0)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        if self.mode == "my":
            for label, cb in (
                ("New Group", self.new_group),
                ("Delete Group", self.delete_group),
                ("Add Font to Group", lambda _b: self.app.add_selected_to_group()),
                ("Remove from Group", lambda _b: self.app.remove_selected_from_group()),
            ):
                button = Gtk.Button(label=label)
                button.connect("clicked", cb)
                buttons.pack_start(button, False, False, 0)
            buttons.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 0)
            uninstall_button = Gtk.Button(label="Uninstall Font")
            uninstall_button.connect("clicked", lambda _button: self.app.uninstall_selected())
            self.app.uninstall_actions.append(uninstall_button)
            buttons.pack_start(uninstall_button, False, False, 0)
        else:
            self.recent_button = Gtk.MenuButton()
            self.recent_button.set_label("Recent Folders")
            self.recent_menu = Gtk.Menu()
            self.recent_button.set_popup(self.recent_menu)
            self._refresh_recent_menu()
            buttons.pack_start(self.recent_button, False, False, 0)
            buttons.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 0)
            install_button = Gtk.Button(label="Install Font")
            install_button.connect("clicked", lambda _button: self.app.install_selected())
            self.app.install_actions.append(install_button)
            buttons.pack_start(install_button, False, False, 0)
        self.pack_start(buttons, False, False, 0)

    def _build_sidebar(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=8)
        box.set_size_request(170, -1)
        if self.mode == "browse":
            box.pack_start(Gtk.Label(label="FOLDERS", xalign=0), False, False, 4)
            box.pack_start(self._build_folder_tree(), True, True, 0)
        else:
            box.pack_start(Gtk.Label(label="MY INSTALLED FONTS", xalign=0), False, False, 4)
            for label, key in (("All fonts", "all"), ("User fonts", "user"), ("System fonts", "system")):
                button = Gtk.RadioButton.new_with_label_from_widget(getattr(self, "_scope_group", None), label)
                self._scope_group = button
                button.connect("toggled", self._my_scope_changed, key)
                if key == "all":
                    button.set_active(True)
                box.pack_start(button, False, False, 0)
            box.pack_start(Gtk.Label(label="MY FONT GROUPS", xalign=0), False, False, 18)
            self.groups_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            box.pack_start(self.groups_box, False, False, 0)
            self._load_group_buttons()
        return box

    def _build_folder_tree(self) -> Gtk.Widget:
        self.folder_store = Gtk.TreeStore(str, str, bool)
        self.folder_tree = Gtk.TreeView(model=self.folder_store, headers_visible=False)
        renderer = Gtk.CellRendererText(ellipsize=Pango.EllipsizeMode.END)
        column = Gtk.TreeViewColumn("Folder", renderer, text=0)
        self.folder_tree.append_column(column)
        root_iter = self.folder_store.append(None, ["/", "/", False])
        self._add_folder_placeholder(root_iter)
        self.folder_tree.connect("row-expanded", self._folder_row_expanded)
        selection = self.folder_tree.get_selection()
        selection.connect("changed", self._folder_selection_changed)
        self.loading_folder_tree = True
        self._select_folder_path(self.current_folder)
        self.loading_folder_tree = False
        scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vscrollbar_policy=Gtk.PolicyType.AUTOMATIC)
        scroll.add(self.folder_tree)
        return scroll

    def _add_folder_placeholder(self, parent_iter: Gtk.TreeIter) -> None:
        self.folder_store.append(parent_iter, ["", "", True])

    def _folder_row_expanded(self, _tree: Gtk.TreeView, tree_iter: Gtk.TreeIter, _path: Gtk.TreePath) -> None:
        self._populate_folder_iter(tree_iter)

    def _populate_folder_iter(self, tree_iter: Gtk.TreeIter) -> None:
        if self.folder_store[tree_iter][2]:
            return
        while True:
            child = self.folder_store.iter_children(tree_iter)
            if child is None:
                break
            self.folder_store.remove(child)
        folder = Path(self.folder_store[tree_iter][1])
        for child_folder in self._child_folders(folder):
            child_iter = self.folder_store.append(tree_iter, [child_folder.name or str(child_folder), str(child_folder), False])
            self._add_folder_placeholder(child_iter)
        self.folder_store[tree_iter][2] = True

    @staticmethod
    def _child_folders(folder: Path) -> list[Path]:
        try:
            return sorted((child for child in folder.iterdir() if child.is_dir()), key=lambda child: child.name.casefold())
        except OSError:
            return []

    def _find_child_folder_iter(self, parent_iter: Gtk.TreeIter, folder: Path) -> Gtk.TreeIter | None:
        child = self.folder_store.iter_children(parent_iter)
        folder_text = str(folder)
        while child is not None:
            if self.folder_store[child][1] == folder_text:
                return child
            child = self.folder_store.iter_next(child)
        return None

    def _select_folder_path(self, folder: Path) -> None:
        try:
            folder = folder.resolve()
        except OSError:
            folder = Path.cwd()
        root_iter = self.folder_store.get_iter_first()
        if root_iter is None:
            return
        current_iter = root_iter
        self._populate_folder_iter(current_iter)
        self.folder_tree.expand_row(self.folder_store.get_path(current_iter), False)
        for index in range(1, len(folder.parts) + 1):
            part_path = Path(*folder.parts[:index])
            if part_path == Path("/"):
                continue
            next_iter = self._find_child_folder_iter(current_iter, part_path)
            if next_iter is None:
                break
            current_iter = next_iter
            self._populate_folder_iter(current_iter)
            self.folder_tree.expand_row(self.folder_store.get_path(current_iter), False)
        tree_path = self.folder_store.get_path(current_iter)
        self.folder_tree.get_selection().select_path(tree_path)
        self.folder_tree.scroll_to_cell(tree_path, None, True, 0.5, 0.0)

    def _load_group_buttons(self) -> None:
        if not hasattr(self, "groups_box"):
            return
        for child in self.groups_box.get_children():
            self.groups_box.remove(child)
        USER_FONT_DIR.mkdir(parents=True, exist_ok=True)
        for folder in sorted(p for p in USER_FONT_DIR.iterdir() if p.is_dir()):
            button = Gtk.RadioButton.new_with_label_from_widget(self._scope_group, folder.name)
            button.connect("toggled", self._group_changed, folder)
            self.groups_box.pack_start(button, False, False, 0)
        self.groups_box.show_all()

    def _folder_selection_changed(self, selection: Gtk.TreeSelection) -> None:
        model, tree_iter = selection.get_selected()
        if tree_iter is None:
            return
        folder = model[tree_iter][1]
        if folder:
            self.current_folder = Path(folder)
            if not getattr(self, "loading_folder_tree", False):
                self.add_recent_folder(self.current_folder)
                self.refresh()

    def add_recent_folder(self, folder: Path) -> None:
        if self.mode != "browse":
            return
        try:
            folder = folder.resolve()
        except OSError:
            pass
        self.recent_folders = [existing for existing in self.recent_folders if existing != folder]
        self.recent_folders.insert(0, folder)
        del self.recent_folders[10:]
        self._refresh_recent_menu()

    def _refresh_recent_menu(self) -> None:
        if not hasattr(self, "recent_menu"):
            return
        for child in self.recent_menu.get_children():
            self.recent_menu.remove(child)
        if not self.recent_folders:
            item = Gtk.MenuItem(label="No recent folders")
            item.set_sensitive(False)
            self.recent_menu.append(item)
        else:
            for folder in self.recent_folders:
                item = Gtk.MenuItem(label=str(folder))
                item.connect("activate", self._recent_folder_selected, folder)
                self.recent_menu.append(item)
        self.recent_menu.show_all()

    def _recent_folder_selected(self, _item: Gtk.MenuItem, folder: Path) -> None:
        self.add_recent_folder(folder)
        self._select_folder_path(folder)

    def _my_scope_changed(self, button: Gtk.RadioButton, key: str) -> None:
        if button.get_active():
            self.current_scope = key
            self.current_group = None
            self.refresh()

    def _group_changed(self, button: Gtk.RadioButton, folder: Path) -> None:
        if button.get_active():
            self.current_group = folder
            self.current_scope = "group"
            self.refresh()

    def ensure_loaded(self) -> None:
        if not self.loaded:
            self.refresh()

    def refresh(self) -> None:
        self.loaded = True
        if self.show_fonts.get_active():
            self.stack.set_visible_child_name("fonts")
        else:
            self.stack.set_visible_child_name("files")
        self.app.begin_loading(self)
        try:
            if self.mode == "browse":
                self.all_records = discover_fonts(self.current_folder, recursive=True)
            else:
                scope = getattr(self, "current_scope", "all")
                group = getattr(self, "current_group", None)
                if scope == "group" and group:
                    self.all_records = discover_fonts(group)
                elif scope in {"user", "system"}:
                    self.all_records = discover_installed_fonts(scope)
                else:
                    self.all_records = discover_installed_fonts("all")
            self.apply_filter()
        finally:
            self.app.end_loading(self)

    def apply_filter(self) -> None:
        query = self.search_query.casefold().strip()
        if query:
            self.records = [record for record in self.all_records if self._record_matches(record, query)]
        else:
            self.records = list(self.all_records)
        self.selected_records = [record for record in self.selected_records if record in self.records]
        self._populate_fonts()
        self._populate_files()
        self._sync_tile_selection()

    @staticmethod
    def _record_matches(record: FontRecord, query: str) -> bool:
        fields = (record.name, record.family, record.style, record.kind, str(record.path))
        return any(query in field.casefold() for field in fields)

    def _populate_fonts(self) -> None:
        for child in self.flowbox.get_children():
            self.flowbox.remove(child)
        for record in self.records:
            self.flowbox.add(FontTile(record, self._tile_selected, self.app.open_font_record))
        self.flowbox.show_all()
        self.queue_visible_preview_update()

    def queue_visible_preview_update(self) -> None:
        if self.lazy_preview_source_id:
            return
        self.lazy_preview_source_id = GLib.idle_add(self.render_visible_previews)

    def render_visible_previews(self) -> bool:
        self.lazy_preview_source_id = 0
        if not self.show_fonts.get_active() or not self.flowbox.get_realized():
            return False
        hadjustment = self.flow_scroll.get_hadjustment()
        vadjustment = self.flow_scroll.get_vadjustment()
        viewport_left = hadjustment.get_value()
        viewport_right = viewport_left + hadjustment.get_page_size()
        viewport_top = vadjustment.get_value()
        viewport_bottom = viewport_top + vadjustment.get_page_size()
        preload_margin = FontTile.HEIGHT * 2
        for child in self.flowbox.get_children():
            tile = child.get_child()
            if not isinstance(tile, FontTile) or tile.preview_loaded:
                continue
            allocation = child.get_allocation()
            child_left = allocation.x
            child_right = allocation.x + allocation.width
            child_top = allocation.y
            child_bottom = allocation.y + allocation.height
            horizontally_visible = child_right >= viewport_left and child_left <= viewport_right
            vertically_near = child_bottom >= viewport_top - preload_margin and child_top <= viewport_bottom + preload_margin
            if horizontally_visible and vertically_near:
                tile.ensure_preview()
        return False

    def _populate_files(self) -> None:
        self.file_store.clear()
        for record in self.records:
            self.file_store.append([record.name, record.family, record.kind, str(record.path), human_size(record.size), record.modified, record])

    def _tile_selected(self, record: FontRecord, additive: bool) -> None:
        if not additive:
            self.selected_records = [record]
        elif record in self.selected_records:
            self.selected_records.remove(record)
        else:
            self.selected_records.append(record)
        for child in self.flowbox.get_children():
            tile = child.get_child() if isinstance(child, Gtk.FlowBoxChild) else child
            if isinstance(tile, FontTile):
                tile.set_selected(tile.record in self.selected_records)
        self.app.update_status_for_pane(self)

    def _file_selection_changed(self, selection: Gtk.TreeSelection) -> None:
        _model, paths = selection.get_selected_rows()
        self.selected_records = [self.file_store[path][6] for path in paths]
        self.app.update_status_for_pane(self)

    def _file_row_activated(self, _view: Gtk.TreeView, path: Gtk.TreePath, _column: Gtk.TreeViewColumn) -> None:
        record = self.file_store[path][6]
        self.selected_records = [record]
        self.app.open_font_record(record)

    def select_all(self) -> None:
        self.selected_records = list(self.records)
        self.file_view.get_selection().select_all()
        self._sync_tile_selection()

    def invert_selection(self) -> None:
        self.selected_records = [r for r in self.records if r not in self.selected_records]
        self.file_view.get_selection().unselect_all()
        for idx, record in enumerate(self.records):
            if record in self.selected_records:
                self.file_view.get_selection().select_path(Gtk.TreePath(idx))
        self._sync_tile_selection()

    def _sync_tile_selection(self) -> None:
        for child in self.flowbox.get_children():
            tile = child.get_child() if isinstance(child, Gtk.FlowBoxChild) else child
            if isinstance(tile, FontTile):
                tile.set_selected(tile.record in self.selected_records)
        self.app.update_status_for_pane(self)

    def find(self, query: str) -> None:
        self.search_query = query
        self.apply_filter()

    def new_group(self, _button=None) -> None:
        dialog = Gtk.Dialog(title="New font group", transient_for=self.app, flags=Gtk.DialogFlags.MODAL)
        dialog.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_OK, Gtk.ResponseType.OK)
        entry = Gtk.Entry(placeholder_text="Group name")
        dialog.get_content_area().pack_start(entry, True, True, 12)
        dialog.show_all()
        if dialog.run() == Gtk.ResponseType.OK and entry.get_text().strip():
            (USER_FONT_DIR / entry.get_text().strip()).mkdir(parents=True, exist_ok=True)
            self._load_group_buttons()
            self.app.update_status_for_pane(self, "Group created")
        dialog.destroy()

    def delete_group(self, _button=None) -> None:
        group = getattr(self, "current_group", None)
        if not group:
            return
        dialog = Gtk.MessageDialog(transient_for=self.app, flags=Gtk.DialogFlags.MODAL, message_type=Gtk.MessageType.WARNING, buttons=Gtk.ButtonsType.OK_CANCEL, text=f"Delete font group '{group.name}'?")
        response = dialog.run()
        dialog.destroy()
        if response == Gtk.ResponseType.OK:
            shutil.rmtree(group)
            self.current_group = None
            self.current_scope = "all"
            self._load_group_buttons()
            self.refresh()
            self.app.update_status_for_pane(self, "Group deleted")


class FontManagerWindow(Gtk.ApplicationWindow):
    """Main application window."""

    def __init__(self, app: Gtk.Application):
        super().__init__(application=app, title=APP_NAME)
        self.set_default_size(1100, 760)
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(root)
        self.status_message = "Ready"
        self.install_actions: list[Gtk.Widget] = []
        self.uninstall_actions: list[Gtk.Widget] = []
        self.statusbar = Gtk.Statusbar()
        self.status_context_id = self.statusbar.get_context_id("fontmgr")
        self.loading_spinner = Gtk.Spinner()
        self.status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.status_box.pack_start(self.loading_spinner, False, False, 6)
        self.status_box.pack_start(self.statusbar, True, True, 0)
        root.pack_start(self._menu_bar(), False, False, 0)
        root.pack_start(self._toolbar(), False, False, 0)
        self.notebook = Gtk.Notebook()
        self.browse_pane = FontPane(self, "browse")
        self.my_pane = FontPane(self, "my", autoload=False)
        self.notebook.append_page(self.browse_pane, Gtk.Label(label="Browse Fonts"))
        self.notebook.append_page(self.my_pane, Gtk.Label(label="My Fonts"))
        self.notebook.connect("switch-page", self._tab_switched)
        root.pack_start(self.notebook, True, True, 0)
        root.pack_start(self.status_box, False, False, 0)
        self.update_status_for_pane(self.browse_pane)
        self.show_all()

    def _tab_switched(self, _notebook: Gtk.Notebook, _page: Gtk.Widget, page_num: int) -> None:
        pane = self.browse_pane if page_num == 0 else self.my_pane
        pane.ensure_loaded()
        pane.find(self.search_entry.get_text())
        self.update_status_for_pane(pane)

    @property
    def active_pane(self) -> FontPane:
        return self.browse_pane if self.notebook.get_current_page() == 0 else self.my_pane

    def update_status_for_pane(self, pane: FontPane, message: str | None = None) -> None:
        if message is not None:
            self.status_message = message
        if not hasattr(self, "statusbar"):
            return
        text = f"Fonts: {len(pane.records)}    Selected: {len(pane.selected_records)}    {self.status_message}"
        self.statusbar.pop(self.status_context_id)
        self.statusbar.push(self.status_context_id, text)
        self.update_action_sensitivity(pane)

    def update_action_sensitivity(self, pane: FontPane | None = None) -> None:
        pane = pane or self.active_pane
        selected = pane.selected_records
        can_install = bool(selected)
        can_uninstall = any(record.installed_scope == "user" for record in selected)
        for action in self.install_actions:
            action.set_sensitive(can_install)
        for action in self.uninstall_actions:
            action.set_sensitive(can_uninstall)

    def update_status(self, message: str | None = None) -> None:
        self.update_status_for_pane(self.active_pane, message)

    def begin_loading(self, pane: FontPane) -> None:
        if hasattr(self, "loading_spinner"):
            self.loading_spinner.start()
        self.update_status_for_pane(pane, "Loading fonts…")
        self._drain_events()

    def end_loading(self, pane: FontPane) -> None:
        if hasattr(self, "loading_spinner"):
            self.loading_spinner.stop()
        self.update_status_for_pane(pane, "Ready")

    def _menu_bar(self) -> Gtk.MenuBar:
        menubar = Gtk.MenuBar()
        menus = {
            "File": [("Install", self.install_selected), ("Uninstall", self.uninstall_selected), ("Refresh cache", self.refresh_cache), ("Print Listing", self.print_listing), ("Exit", lambda *_: self.close())],
            "Edit": [("Select All", lambda *_: self.active_pane.select_all()), ("Invert Selection", lambda *_: self.active_pane.invert_selection()), ("Find Font", self.find_font)],
            "View": [("View Font", self.view_font), ("Refresh Font List", self.refresh_all)],
            "Help": [("About", self.about)],
        }
        for title, items in menus.items():
            item = Gtk.MenuItem(label=title)
            menu = Gtk.Menu()
            for label, callback in items:
                child = Gtk.MenuItem(label=label)
                child.connect("activate", callback)
                if label == "Install":
                    self.install_actions.append(child)
                elif label == "Uninstall":
                    self.uninstall_actions.append(child)
                menu.append(child)
            item.set_submenu(menu)
            menubar.append(item)
        return menubar

    def _toolbar(self) -> Gtk.Toolbar:
        toolbar = Gtk.Toolbar()
        actions = [
            ("view-refresh", "Refresh", self.refresh_all),
            ("document-open", "View Font", self.view_font),
            ("document-print", "Print Listing", self.print_listing),
            ("list-add", "Install Font", self.install_selected),
            ("list-remove", "Uninstall Font", self.uninstall_selected),
        ]
        for icon, label, callback in actions:
            button = Gtk.ToolButton.new(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.LARGE_TOOLBAR), label)
            button.connect("clicked", callback)
            if label == "Install Font":
                self.install_actions.append(button)
            elif label == "Uninstall Font":
                self.uninstall_actions.append(button)
            toolbar.insert(button, -1)
        spacer = Gtk.SeparatorToolItem()
        spacer.set_expand(True)
        spacer.set_draw(False)
        toolbar.insert(spacer, -1)
        search_item = Gtk.ToolItem()
        self.search_entry = Gtk.SearchEntry(placeholder_text="Search")
        self.search_entry.connect("search-changed", lambda entry: self.active_pane.find(entry.get_text()))
        self.search_entry.connect("activate", lambda entry: self.active_pane.find(entry.get_text()))
        search_item.add(self.search_entry)
        toolbar.insert(search_item, -1)
        return toolbar

    def selected_records(self) -> list[FontRecord]:
        return self.active_pane.selected_records

    def refresh_all(self, *_args) -> None:
        self.active_pane.refresh()
        if self.browse_pane.loaded and self.active_pane is not self.browse_pane:
            self.browse_pane.refresh()
        if self.my_pane.loaded and self.active_pane is not self.my_pane:
            self.my_pane.refresh()

    def install_selected(self, *_args) -> None:
        records = list(self.selected_records())
        if not records:
            self.update_status("No font selected")
            return
        USER_FONT_DIR.mkdir(parents=True, exist_ok=True)
        for record in records:
            shutil.copy2(record.path, USER_FONT_DIR / record.path.name)
        self.refresh_all()
        self.update_status("Font installed" if len(records) == 1 else f"{len(records)} fonts installed")

    def uninstall_selected(self, *_args) -> None:
        deleted = 0
        for record in self.selected_records():
            try:
                record.path.relative_to(USER_FONT_DIR)
            except ValueError:
                continue
            record.path.unlink(missing_ok=True)
            deleted += 1
        self.refresh_all()
        if deleted:
            self.update_status("Font uninstalled" if deleted == 1 else f"{deleted} fonts uninstalled")
        else:
            self.update_status("No user font selected")

    def add_selected_to_group(self) -> None:
        groups = sorted(p for p in USER_FONT_DIR.glob("*") if p.is_dir())
        if not groups:
            self.my_pane.new_group()
            groups = sorted(p for p in USER_FONT_DIR.glob("*") if p.is_dir())
        dialog = Gtk.Dialog(title="Add font to group", transient_for=self, flags=Gtk.DialogFlags.MODAL)
        dialog.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_OK, Gtk.ResponseType.OK)
        combo = Gtk.ComboBoxText()
        for group in groups:
            combo.append_text(group.name)
        combo.set_active(0)
        dialog.get_content_area().pack_start(combo, True, True, 12)
        dialog.show_all()
        if dialog.run() == Gtk.ResponseType.OK and combo.get_active_text():
            target = USER_FONT_DIR / combo.get_active_text()
            target.mkdir(parents=True, exist_ok=True)
            records = list(self.selected_records())
            if not records:
                self.update_status("No font selected")
                dialog.destroy()
                return
            for record in records:
                destination = target / record.path.name
                if record.installed_scope == "system":
                    shutil.copy2(record.path, destination)
                else:
                    shutil.move(str(record.path), destination)
            self.refresh_all()
            self.update_status("Font added to group" if len(records) == 1 else f"{len(records)} fonts added to group")
        dialog.destroy()

    def remove_selected_from_group(self) -> None:
        USER_FONT_DIR.mkdir(parents=True, exist_ok=True)
        moved = 0
        for record in self.selected_records():
            if record.path.parent != USER_FONT_DIR:
                shutil.move(str(record.path), USER_FONT_DIR / record.path.name)
                moved += 1
        self.refresh_all()
        if moved:
            self.update_status("Font removed from group" if moved == 1 else f"{moved} fonts removed from group")
        else:
            self.update_status("No grouped font selected")

    def open_font_record(self, record: FontRecord) -> None:
        FontViewDialog(self, record).run_dialog()

    def view_font(self, *_args) -> None:
        records = self.selected_records()
        if not records:
            return
        self.open_font_record(records[0])

    def find_font(self, *_args) -> None:
        dialog = Gtk.Dialog(title="Find Font", transient_for=self, flags=Gtk.DialogFlags.MODAL)
        dialog.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_FIND, Gtk.ResponseType.OK)
        entry = Gtk.Entry(placeholder_text="Font name")
        dialog.get_content_area().pack_start(entry, True, True, 12)
        dialog.show_all()
        if dialog.run() == Gtk.ResponseType.OK:
            query = entry.get_text()
            self.search_entry.set_text(query)
            self.active_pane.find(query)
        dialog.destroy()

    def print_listing(self, *_args) -> None:
        records = self.selected_records() or self.active_pane.records
        if not records:
            return
        dialog = Gtk.FileChooserDialog(title="Save Font Catalog", transient_for=self, action=Gtk.FileChooserAction.SAVE)
        dialog.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_SAVE, Gtk.ResponseType.OK)
        dialog.set_current_name("font-catalog.pdf")
        if dialog.run() == Gtk.ResponseType.OK:
            create_pdf_catalog(Path(dialog.get_filename()), records)
        dialog.destroy()

    def about(self, *_args) -> None:
        dialog = Gtk.AboutDialog(transient_for=self, modal=True, program_name=APP_NAME, version="0.1.0", comments="Browse, view, install, uninstall, group, and print Linux fonts.")
        dialog.run()
        dialog.destroy()

    @staticmethod
    def _drain_events() -> None:
        while Gtk.events_pending():
            Gtk.main_iteration_do(False)

    def refresh_cache(self, *_args) -> None:
        self.update_status("Refreshing font cache…")
        self._drain_events()
        self._fc_cache()
        self.update_status("Font cache refreshed")

    @staticmethod
    def _fc_cache() -> None:
        try:
            subprocess.run(["fc-cache", "-f", "-v"], check=False)
        except FileNotFoundError:
            pass


class FontViewDialog(Gtk.Dialog):
    PREVIEW_WIDTH = 648
    PREVIEW_HEIGHT = 240
    DEFAULT_PREVIEW_TEXT = "The quick brown fox jumps over the lazy dog."

    def __init__(self, parent: Gtk.Window, record: FontRecord):
        super().__init__(title="View font", transient_for=parent, flags=Gtk.DialogFlags.MODAL)
        self.record = record
        self.add_buttons(Gtk.STOCK_CLOSE, Gtk.ResponseType.CLOSE)
        self.set_default_size(680, 560)
        self.set_size_request(680, 560)
        self.set_resizable(False)
        area = self.get_content_area()
        details = Gtk.Grid(column_spacing=24, row_spacing=8, margin=16)
        rows = [("Font name", record.name), ("File name", str(record.path)), ("Font type", record.kind), ("Installation status", "This font is currently installed." if record.installed else "This font is not currently installed.")]
        for row, (label, value) in enumerate(rows):
            details.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
            value_label = Gtk.Label(label=value, xalign=0)
            value_label.set_line_wrap(True)
            value_label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
            value_label.set_max_width_chars(70)
            details.attach(value_label, 1, row, 1, 1)
        area.pack_start(details, False, False, 0)

        preview_frame = Gtk.EventBox(margin_left=16, margin_right=16, margin_top=8, margin_bottom=8)
        apply_css(preview_frame, "eventbox { background: #ffffff; border: 1px solid #111111; }")
        self.preview = Gtk.Image()
        self.preview.set_halign(Gtk.Align.START)
        self.preview.set_valign(Gtk.Align.CENTER)
        self.preview.set_size_request(self.PREVIEW_WIDTH, self.PREVIEW_HEIGHT)
        preview_frame.add(self.preview)
        area.pack_start(preview_frame, True, True, 0)

        self.preview_entry = Gtk.Entry(margin_left=16, margin_right=16)
        self.preview_entry.set_hexpand(True)
        self.preview_entry.set_text(self.DEFAULT_PREVIEW_TEXT)
        self.preview_entry.connect("changed", lambda _entry: self.update_preview())
        area.pack_start(self.preview_entry, False, False, 0)

        size_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, margin=16)
        self.size_scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 8, 72, 1)
        self.size_scale.set_value(42)
        self.size_scale.set_digits(0)
        self.size_scale.set_hexpand(True)
        self.size_scale.connect("value-changed", lambda _scale: self.update_preview())
        size_box.pack_start(self.size_scale, True, True, 0)
        area.pack_start(size_box, False, False, 0)
        self.update_preview()

    def update_preview(self) -> None:
        size = int(self.size_scale.get_value())
        text = self.preview_entry.get_text() or self.DEFAULT_PREVIEW_TEXT
        pixbuf = create_wrapped_outline_pixbuf(self.record, text, self.PREVIEW_WIDTH, self.PREVIEW_HEIGHT, size, (0.0, 0.0, 0.0))
        if pixbuf is not None:
            self.preview.set_from_pixbuf(pixbuf)

    def run_dialog(self) -> None:
        self.show_all()
        self.run()
        self.destroy()


def create_pdf_catalog(filename: Path, records: list[FontRecord]) -> None:
    width, height = 595, 842
    surface = cairo.PDFSurface(str(filename), width, height)
    ctx = cairo.Context(surface)
    pango_ctx = PangoCairo.create_context(ctx)
    layout = Pango.Layout.new(pango_ctx)
    y = 36

    def draw_text(text: str, font: str, x: int, y_pos: int) -> int:
        ctx.move_to(x, y_pos)
        layout.set_text(text, -1)
        layout.set_font_description(Pango.FontDescription(font))
        PangoCairo.show_layout(ctx, layout)
        _ink, logical = layout.get_pixel_extents()
        return logical.height

    draw_text("Font Catalog", "Helvetica 24", 30, y)
    y += 52
    for family in sorted({r.family for r in records}):
        family_records = [r for r in records if r.family == family]
        if y > height - 120:
            surface.show_page()
            y = 36
        ctx.set_source_rgb(0, 0, 0)
        ctx.set_line_width(1)
        ctx.move_to(32, y + 10.5)
        ctx.line_to(width - 32, y + 10.5)
        ctx.stroke()
        layout.set_text(family, -1)
        layout.set_font_description(Pango.FontDescription("Helvetica 14"))
        _ink, logical = layout.get_pixel_extents()
        ctx.set_source_rgb(1, 1, 1)
        ctx.rectangle(28, y - 2, logical.width + 8, logical.height + 4)
        ctx.fill()
        ctx.set_source_rgb(0, 0, 0)
        ctx.move_to(32, y)
        PangoCairo.show_layout(ctx, layout)
        y += 24
        for record in family_records:
            if y > height - 80:
                surface.show_page()
                y = 36
            ctx.set_source_rgb(0, 0, 0)
            draw_outline_text(ctx, record, SAMPLE_TEXT, 32, y + 18, 18, max_width=width - 64)
            y += 24
            draw_text(f"{record.style} ({record.path.name})", "Monospace 7", 32, y)
            y += 18
        y += 16
    surface.finish()


class FontManagerApplication(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)

    def do_activate(self):
        window = self.props.active_window or FontManagerWindow(self)
        window.present()


def main(argv: list[str] | None = None) -> int:
    app = FontManagerApplication()
    return app.run(argv or sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
