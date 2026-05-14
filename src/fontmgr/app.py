"""GTK font browser, installer, and catalog generator for Linux."""

from __future__ import annotations

import datetime as dt
import shutil
import subprocess
from functools import lru_cache
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from xml.sax.saxutils import escape

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



def css_quote(value: str) -> str:
    """Escape a string for use as a quoted GTK CSS value."""

    return value.replace("\\", "\\\\").replace('"', '\\"')


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


def apply_font_css(widget: Gtk.Widget, family: str, size_pt: int) -> None:
    """Apply per-widget font styling with GTK CSS instead of deprecated APIs."""

    family = css_quote(family)
    apply_css(
        widget,
        f'* {{ font-family: "{family}"; font-size: {size_pt}pt; }} '
        f'text {{ font-family: "{family}"; font-size: {size_pt}pt; }}',
    )

def is_font_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in FONT_EXTENSIONS


def file_kind(path: Path) -> str:
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


def font_identity(path: Path) -> tuple[str, str, str]:
    """Best-effort family/name/style extraction from a font file name."""

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
    return family, stem, style


def discover_fonts(folder: Path, recursive: bool = False) -> list[FontRecord]:
    paths: Iterable[Path]
    paths = folder.rglob("*") if recursive else folder.iterdir() if folder.exists() else []
    records: list[FontRecord] = []
    for path in sorted((p for p in paths if is_font_file(p)), key=lambda p: p.name.lower()):
        family, name, style = font_identity(path)
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
                name=name,
                family=family,
                style=style,
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


def create_outline_svg(record: FontRecord, text: str, width: int, height: int, font_size: int, fill: str = "#111111") -> str:
    """Build an SVG preview whose glyphs are converted to path outlines."""

    try:
        from fontTools.pens.svgPathPen import SVGPathPen

        _font, glyph_set, cmap, hmtx, units_per_em = load_outline_font(str(record.path))
        scale = font_size / units_per_em
        baseline = min(height - 8, int(height * 0.78))
        x_units = 0
        paths: list[str] = []
        for character in text:
            glyph_name = cmap.get(ord(character))
            if glyph_name and glyph_name in glyph_set:
                pen = SVGPathPen(glyph_set)
                glyph_set[glyph_name].draw(pen)
                commands = pen.getCommands()
                if commands:
                    paths.append(f'<path d="{commands}" transform="translate({x_units} 0)"/>')
            x_units += glyph_advance(glyph_name, hmtx, units_per_em)
        outlined = "".join(paths)
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
            f'<defs><clipPath id="previewClip"><rect x="0" y="0" width="{width}" height="{height}"/></clipPath></defs>'
            f'<g clip-path="url(#previewClip)" fill="{fill}" transform="translate(0 {baseline}) scale({scale} -{scale})">{outlined}</g>'
            '</svg>'
        )
    except Exception:
        safe_text = escape(text)
        safe_family = escape(record.family, {'"': '&quot;'})
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
            f'<text x="0" y="{height - 8}" font-family="{safe_family}" font-size="{font_size}" fill="{fill}">{safe_text}</text>'
            '</svg>'
        )


def svg_to_pixbuf(svg: str, width: int, height: int) -> GdkPixbuf.Pixbuf | None:
    """Render SVG bytes into a pixbuf for display in GTK widgets."""

    try:
        loader = GdkPixbuf.PixbufLoader.new_with_type("svg")
        loader.set_size(width, height)
        loader.write(svg.encode())
        loader.close()
        return loader.get_pixbuf()
    except Exception:
        return None


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


class FontTile(Gtk.EventBox):
    """Selectable compact preview tile for a font."""

    WIDTH = 250
    HEIGHT = 80
    PREVIEW_WIDTH = 232
    PREVIEW_HEIGHT = 46

    def __init__(self, record: FontRecord, on_select, on_open):
        super().__init__()
        self.record = record
        self.selected = False
        self.on_select = on_select
        self.on_open = on_open
        self.set_visible_window(True)
        self.set_size_request(self.WIDTH, self.HEIGHT)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin=8)
        name = Gtk.Label(label=record.name, xalign=0)
        name.set_ellipsize(Pango.EllipsizeMode.END)
        self.preview = Gtk.Image()
        self.preview.set_size_request(self.PREVIEW_WIDTH, self.PREVIEW_HEIGHT)
        box.pack_start(name, False, False, 0)
        box.pack_start(self.preview, False, False, 0)
        self.add(box)
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
        fill = "#0f3d91" if self.selected else "#111111"
        svg = create_outline_svg(self.record, self.record.name, self.PREVIEW_WIDTH, self.PREVIEW_HEIGHT, 38, fill)
        pixbuf = svg_to_pixbuf(svg, self.PREVIEW_WIDTH, self.PREVIEW_HEIGHT)
        if pixbuf is not None:
            self.preview.set_from_pixbuf(pixbuf)
            self.preview_loaded = True

    def update_style(self) -> None:
        css = (
            "background: #dbeafe; color: #0f3d91; border: 2px solid #2563eb;"
            if self.selected
            else "background: #ffffff; color: #111111; border: 1px solid #111111;"
        )
        apply_css(self, f"eventbox {{ {css} }}")


class FontPane(Gtk.Box):
    """Shared Browse Fonts/My Fonts layout."""

    def __init__(self, app: "FontManagerWindow", mode: str, autoload: bool = True):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin=16)
        self.app = app
        self.mode = mode
        self.records: list[FontRecord] = []
        self.selected_records: list[FontRecord] = []
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
        top.pack_start(heading, True, True, 0)
        top.pack_start(self.show_fonts, False, False, 8)
        top.pack_start(self.show_files, False, False, 0)
        self.pack_start(top, False, False, 0)

        content = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        content.set_wide_handle(True)
        self.sidebar = self._build_sidebar()
        content.pack1(self.sidebar, resize=False, shrink=False)
        self.stack = Gtk.Stack()
        self.flowbox = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, min_children_per_line=3, max_children_per_line=3)
        self.flowbox.set_homogeneous(True)
        self.flow_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.ALWAYS, vscrollbar_policy=Gtk.PolicyType.AUTOMATIC)
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
                ("Uninstall Font", lambda _b: self.app.uninstall_selected()),
                ("Add Font to Group", lambda _b: self.app.add_selected_to_group()),
                ("Remove from Group", lambda _b: self.app.remove_selected_from_group()),
            ):
                button = Gtk.Button(label=label)
                button.connect("clicked", cb)
                buttons.pack_start(button, False, False, 0)
        else:
            recent = Gtk.Button(label="Recent folders ▾")
            buttons.pack_start(recent, False, False, 0)
        self.pack_start(buttons, False, False, 0)

    def _build_sidebar(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=12)
        box.set_size_request(230, -1)
        if self.mode == "browse":
            box.pack_start(Gtk.Label(label="FOLDERS", xalign=0), False, False, 4)
            chooser = Gtk.FileChooserWidget(action=Gtk.FileChooserAction.SELECT_FOLDER)
            chooser.set_current_folder(str(Path.cwd()))
            chooser.connect("selection-changed", self._folder_changed)
            box.pack_start(chooser, True, True, 0)
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

    def _folder_changed(self, chooser: Gtk.FileChooserWidget) -> None:
        filename = chooser.get_filename()
        if filename:
            self.current_folder = Path(filename)
            self.refresh()

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
        if self.mode == "browse":
            self.records = discover_fonts(self.current_folder)
        else:
            scope = getattr(self, "current_scope", "all")
            group = getattr(self, "current_group", None)
            if scope == "group" and group:
                self.records = discover_fonts(group)
            elif scope in {"user", "system"}:
                self.records = discover_installed_fonts(scope)
            else:
                self.records = discover_installed_fonts("all")
        self.selected_records = []
        self._populate_fonts()
        self._populate_files()

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

    def _file_selection_changed(self, selection: Gtk.TreeSelection) -> None:
        _model, paths = selection.get_selected_rows()
        self.selected_records = [self.file_store[path][6] for path in paths]

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

    def find(self, query: str) -> None:
        query = query.casefold()
        matches = [r for r in self.records if query in r.name.casefold() or query in r.family.casefold()]
        if matches:
            self.selected_records = [matches[0]]
            self._sync_tile_selection()

    def new_group(self, _button=None) -> None:
        dialog = Gtk.Dialog(title="New font group", transient_for=self.app, flags=Gtk.DialogFlags.MODAL)
        dialog.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL, Gtk.STOCK_OK, Gtk.ResponseType.OK)
        entry = Gtk.Entry(placeholder_text="Group name")
        dialog.get_content_area().pack_start(entry, True, True, 12)
        dialog.show_all()
        if dialog.run() == Gtk.ResponseType.OK and entry.get_text().strip():
            (USER_FONT_DIR / entry.get_text().strip()).mkdir(parents=True, exist_ok=True)
            self._load_group_buttons()
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


class FontManagerWindow(Gtk.ApplicationWindow):
    """Main application window."""

    def __init__(self, app: Gtk.Application):
        super().__init__(application=app, title="enBox Font Manager")
        self.set_default_size(1100, 760)
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(root)
        root.pack_start(self._menu_bar(), False, False, 0)
        root.pack_start(self._toolbar(), False, False, 0)
        self.notebook = Gtk.Notebook()
        self.browse_pane = FontPane(self, "browse")
        self.my_pane = FontPane(self, "my", autoload=False)
        self.notebook.append_page(self.browse_pane, Gtk.Label(label="Browse Fonts"))
        self.notebook.append_page(self.my_pane, Gtk.Label(label="My Fonts"))
        self.notebook.connect("switch-page", self._tab_switched)
        root.pack_start(self.notebook, True, True, 0)
        self.show_all()

    def _tab_switched(self, _notebook: Gtk.Notebook, _page: Gtk.Widget, page_num: int) -> None:
        (self.browse_pane if page_num == 0 else self.my_pane).ensure_loaded()

    @property
    def active_pane(self) -> FontPane:
        return self.browse_pane if self.notebook.get_current_page() == 0 else self.my_pane

    def _menu_bar(self) -> Gtk.MenuBar:
        menubar = Gtk.MenuBar()
        menus = {
            "File": [("Install", self.install_selected), ("Uninstall", self.uninstall_selected), ("Print Listing", self.print_listing), ("Exit", lambda *_: self.close())],
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
            toolbar.insert(button, -1)
        spacer = Gtk.SeparatorToolItem()
        spacer.set_expand(True)
        spacer.set_draw(False)
        toolbar.insert(spacer, -1)
        search_item = Gtk.ToolItem()
        self.search_entry = Gtk.SearchEntry(placeholder_text="Search")
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
        USER_FONT_DIR.mkdir(parents=True, exist_ok=True)
        for record in self.selected_records():
            shutil.copy2(record.path, USER_FONT_DIR / record.path.name)
        self._fc_cache()
        self.refresh_all()

    def uninstall_selected(self, *_args) -> None:
        for record in self.selected_records():
            try:
                record.path.relative_to(USER_FONT_DIR)
            except ValueError:
                continue
            record.path.unlink(missing_ok=True)
        self._fc_cache()
        self.refresh_all()

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
            for record in self.selected_records():
                destination = target / record.path.name
                if record.installed_scope == "system":
                    shutil.copy2(record.path, destination)
                else:
                    shutil.move(str(record.path), destination)
            self._fc_cache()
            self.refresh_all()
        dialog.destroy()

    def remove_selected_from_group(self) -> None:
        USER_FONT_DIR.mkdir(parents=True, exist_ok=True)
        for record in self.selected_records():
            if record.path.parent != USER_FONT_DIR:
                shutil.move(str(record.path), USER_FONT_DIR / record.path.name)
        self._fc_cache()
        self.refresh_all()

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
            self.active_pane.find(entry.get_text())
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
        dialog = Gtk.AboutDialog(transient_for=self, modal=True, program_name="enBox Font Manager", version="0.1.0", comments="Browse, view, install, uninstall, group, and print Linux fonts.")
        dialog.run()
        dialog.destroy()

    @staticmethod
    def _fc_cache() -> None:
        try:
            subprocess.run(["fc-cache", "-f", str(USER_FONT_DIR)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            pass


class FontViewDialog(Gtk.Dialog):
    def __init__(self, parent: Gtk.Window, record: FontRecord):
        super().__init__(title="View font", transient_for=parent, flags=Gtk.DialogFlags.MODAL)
        self.record = record
        self.add_buttons(Gtk.STOCK_CLOSE, Gtk.ResponseType.CLOSE)
        self.set_default_size(420, 520)
        area = self.get_content_area()
        details = Gtk.Grid(column_spacing=24, row_spacing=8, margin=16)
        rows = [("Font name", record.name), ("File name", str(record.path)), ("Font type", record.kind), ("Installation status", "This font is currently installed." if record.installed else "This font is not currently installed.")]
        for row, (label, value) in enumerate(rows):
            details.attach(Gtk.Label(label=label, xalign=0), 0, row, 1, 1)
            details.attach(Gtk.Label(label=value, xalign=0), 1, row, 1, 1)
        area.pack_start(details, False, False, 0)
        self.preview = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, margin=16)
        self.preview.get_buffer().set_text("The quick brown\nfox jumps over\nthe lazy dog")
        apply_font_css(self.preview, record.family, 42)
        scroll = Gtk.ScrolledWindow(min_content_height=240)
        scroll.add(self.preview)
        area.pack_start(scroll, True, True, 0)
        size_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, margin=16)
        size_box.pack_start(Gtk.Label(label="Font size:"), False, False, 0)
        combo = Gtk.ComboBoxText()
        for size in range(8, 73, 2):
            combo.append_text(str(size))
        combo.set_active((42 - 8) // 2)
        combo.connect("changed", self._size_changed)
        size_box.pack_start(combo, False, False, 0)
        area.pack_start(size_box, False, False, 0)

    def _size_changed(self, combo: Gtk.ComboBoxText) -> None:
        size = combo.get_active_text() or "42"
        apply_font_css(self.preview, self.record.family, int(size))

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

    draw_text("Font Catalog", "Sans Bold 24", 32, y)
    y += 52
    for family in sorted({r.family for r in records}):
        family_records = [r for r in records if r.family == family]
        if y > height - 120:
            surface.show_page()
            y = 36
        draw_text(family, "Sans 14", 32, y)
        ctx.move_to(120, y + 10)
        ctx.line_to(width - 32, y + 10)
        ctx.stroke()
        y += 24
        for record in family_records:
            if y > height - 80:
                surface.show_page()
                y = 36
            ctx.set_source_rgb(0, 0, 0)
            draw_outline_text(ctx, record, SAMPLE_TEXT, 32, y + 18, 18, max_width=width - 64)
            y += 24
            draw_text(f"{record.style} ({record.path})", "Monospace 7", 32, y)
            y += 18
        y += 16
    surface.finish()


class FontManagerApplication(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="com.enbox.FontManager", flags=Gio.ApplicationFlags.FLAGS_NONE)

    def do_activate(self):
        window = self.props.active_window or FontManagerWindow(self)
        window.present()


def main(argv: list[str] | None = None) -> int:
    app = FontManagerApplication()
    return app.run(argv or sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
