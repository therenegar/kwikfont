"""GTK font browser, installer, and catalog generator for Linux."""

from __future__ import annotations

import datetime as dt
import shutil
import subprocess
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
from gi.repository import Gdk, Gio, Gtk, Pango, PangoCairo  # noqa: E402

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


def human_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


class FontTile(Gtk.EventBox):
    """Selectable preview tile for a font."""

    def __init__(self, record: FontRecord, on_select):
        super().__init__()
        self.record = record
        self.selected = False
        self.on_select = on_select
        self.set_visible_window(True)
        self.set_size_request(260, 100)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin=10)
        name = Gtk.Label(label=record.name, xalign=0)
        preview = Gtk.Label(label=record.name, xalign=0)
        preview.set_ellipsize(Pango.EllipsizeMode.END)
        preview.override_font(Pango.FontDescription(f"{record.family} 40"))
        box.pack_start(name, False, False, 0)
        box.pack_start(preview, True, True, 0)
        self.add(box)
        self.connect("button-press-event", self._clicked)
        self.update_style()

    def _clicked(self, _widget, event):
        self.on_select(self.record, bool(event.state & Gdk.ModifierType.CONTROL_MASK))
        return True

    def set_selected(self, selected: bool) -> None:
        self.selected = selected
        self.update_style()

    def update_style(self) -> None:
        css = "background: #111; color: #fff; border: 1px solid #111;" if self.selected else "background: #fff; color: #111; border: 1px solid #999;"
        provider = Gtk.CssProvider()
        provider.load_from_data(f"eventbox {{ {css} }}".encode())
        self.get_style_context().add_provider(provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


class FontPane(Gtk.Box):
    """Shared Browse Fonts/My Fonts layout."""

    def __init__(self, app: "FontManagerWindow", mode: str):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin=16)
        self.app = app
        self.mode = mode
        self.records: list[FontRecord] = []
        self.selected_records: list[FontRecord] = []
        self.current_folder = Path.cwd()
        self.show_fonts = Gtk.RadioButton.new_with_label_from_widget(None, "Show fonts")
        self.show_files = Gtk.RadioButton.new_with_label_from_widget(self.show_fonts, "Show files")
        self.show_fonts.set_active(True)
        self.show_fonts.connect("toggled", lambda _b: self.refresh())
        self._build()
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
        flow_scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.ALWAYS, vscrollbar_policy=Gtk.PolicyType.AUTOMATIC)
        flow_scroll.add(self.flowbox)
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
        self.stack.add_named(flow_scroll, "fonts")
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

    def refresh(self) -> None:
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
            elif scope == "user":
                self.records = discover_fonts(USER_FONT_DIR, recursive=True)
            elif scope == "system":
                self.records = discover_fonts(SYSTEM_FONT_DIR, recursive=True)
            else:
                self.records = discover_fonts(USER_FONT_DIR, recursive=True) + discover_fonts(SYSTEM_FONT_DIR, recursive=True)
        self.selected_records = []
        self._populate_fonts()
        self._populate_files()

    def _populate_fonts(self) -> None:
        for child in self.flowbox.get_children():
            self.flowbox.remove(child)
        for record in self.records:
            self.flowbox.add(FontTile(record, self._tile_selected))
        self.flowbox.show_all()

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
        self.my_pane = FontPane(self, "my")
        self.notebook.append_page(self.browse_pane, Gtk.Label(label="Browse Fonts"))
        self.notebook.append_page(self.my_pane, Gtk.Label(label="My Fonts"))
        root.pack_start(self.notebook, True, True, 0)
        self.show_all()

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
        self.browse_pane.refresh()
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

    def view_font(self, *_args) -> None:
        records = self.selected_records()
        if not records:
            return
        FontViewDialog(self, records[0]).run_dialog()

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
        self.preview.override_font(Pango.FontDescription(f"{record.family} 42"))
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
        self.preview.override_font(Pango.FontDescription(f"{self.record.family} {size}"))

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
            draw_text(SAMPLE_TEXT, f"{record.family} 16", 32, y)
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
