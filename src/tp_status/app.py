"""Translation Project Status Viewer — GTK4/Adwaita app."""

import gettext
import json
import locale
import os
import sys
import threading
import webbrowser
from datetime import datetime

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Gio, Pango, Gdk

from .scraper import (
    fetch_all_packages, fetch_package_list, PackageInfo,
    LANGUAGES, save_cache, load_cache,
)

LOCALEDIR = '/usr/share/locale'
gettext.bindtextdomain('tp-status', LOCALEDIR)
locale.bindtextdomain('tp-status', LOCALEDIR)
gettext.textdomain('tp-status')
_ = gettext.gettext

VERSION = "0.1.0"
APP_ID = "se.danielnylander.tp-status"


def _setup_css():
    css = b"""
    .heatmap-green { background-color: #26a269; color: white; border-radius: 6px; padding: 2px 8px; }
    .heatmap-yellow { background-color: #e5a50a; color: white; border-radius: 6px; padding: 2px 8px; }
    .heatmap-orange { background-color: #ff7800; color: white; border-radius: 6px; padding: 2px 8px; }
    .heatmap-red { background-color: #c01c28; color: white; border-radius: 6px; padding: 2px 8px; }
    .heatmap-gray { background-color: #77767b; color: white; border-radius: 6px; padding: 2px 8px; }
    .stat-card { background-color: @card_bg_color; border-radius: 12px; padding: 16px; }
    .stat-number { font-size: 24px; font-weight: bold; }
    .pkg-title { font-size: 14px; font-weight: bold; }
    """
    provider = Gtk.CssProvider()
    provider.load_from_data(css)
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def _heatmap_class(pct):
    if pct >= 100: return "heatmap-green"
    elif pct >= 75: return "heatmap-yellow"
    elif pct >= 50: return "heatmap-orange"
    elif pct > 0: return "heatmap-red"
    return "heatmap-gray"


def _get_system_lang():
    try:
        loc = locale.getlocale()[0]
        if loc:
            code = loc.split("_")[0]
            if code in LANGUAGES:
                return code
            if loc in LANGUAGES:
                return loc
    except Exception:
        pass
    return "sv"


class TPStatusApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.packages = []
        self.filtered_packages = []
        self.selected_lang = _get_system_lang()
        self.filter_text = ""
        self.filter_status = "all"  # all, translated, untranslated, outdated
        self.sort_by = "name"  # name, pct, strings
        self._loading = False

    def do_activate(self):
        _setup_css()
        self.win = Adw.ApplicationWindow(application=self, default_width=1000, default_height=700)
        self.win.set_title(_("Translation Project Status"))

        # Header bar
        header = Adw.HeaderBar()

        # Language selector
        lang_button = Gtk.DropDown()
        lang_list = sorted(LANGUAGES.items(), key=lambda x: x[1])
        lang_names = [f"{code} — {name}" for code, name in lang_list]
        lang_model = Gtk.StringList.new(lang_names)
        lang_button.set_model(lang_model)
        # Find current lang index
        lang_codes = [code for code, _name in lang_list]
        if self.selected_lang in lang_codes:
            lang_button.set_selected(lang_codes.index(self.selected_lang))
        lang_button.connect("notify::selected", self._on_lang_changed, lang_codes)
        header.pack_start(lang_button)

        # Refresh button
        refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text=_("Refresh data"))
        refresh_btn.connect("clicked", self._on_refresh)
        header.pack_start(refresh_btn)

        # Menu
        menu = Gio.Menu()
        menu.append(_("Keyboard Shortcuts"), "win.shortcuts")
        menu.append(_("About"), "win.about")
        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        header.pack_end(menu_button)

        # Actions
        about_action = Gio.SimpleAction.new("about", None)
        about_action.connect("activate", self._show_about)
        self.win.add_action(about_action)

        shortcuts_action = Gio.SimpleAction.new("shortcuts", None)
        shortcuts_action.connect("activate", self._show_shortcuts)
        self.win.add_action(shortcuts_action)

        # Main layout
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        main_box.append(header)

        # Toolbar
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, margin_start=12, margin_end=12, margin_top=8, margin_bottom=4)

        # Search
        self.search_entry = Gtk.SearchEntry(placeholder_text=_("Search packages…"), hexpand=True)
        self.search_entry.connect("search-changed", self._on_search_changed)
        toolbar.append(self.search_entry)

        # Filter dropdown
        filter_model = Gtk.StringList.new([_("All"), _("Translated"), _("Untranslated"), _("Outdated")])
        self.filter_dropdown = Gtk.DropDown(model=filter_model, tooltip_text=_("Filter"))
        self.filter_dropdown.connect("notify::selected", self._on_filter_changed)
        toolbar.append(self.filter_dropdown)

        # Sort dropdown
        sort_model = Gtk.StringList.new([_("Name"), _("Completion %"), _("Strings")])
        self.sort_dropdown = Gtk.DropDown(model=sort_model, tooltip_text=_("Sort"))
        self.sort_dropdown.connect("notify::selected", self._on_sort_changed)
        toolbar.append(self.sort_dropdown)

        main_box.append(toolbar)

        # Stats bar
        self.stats_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12, margin_start=12, margin_end=12, margin_top=4, margin_bottom=8, homogeneous=True)
        self._stat_total = self._make_stat_card(_("Packages"), "0")
        self._stat_translated = self._make_stat_card(_("Fully Translated"), "0")
        self._stat_partial = self._make_stat_card(_("Partially Translated"), "0")
        self._stat_missing = self._make_stat_card(_("Not Translated"), "0")
        self.stats_bar.append(self._stat_total[0])
        self.stats_bar.append(self._stat_translated[0])
        self.stats_bar.append(self._stat_partial[0])
        self.stats_bar.append(self._stat_missing[0])
        main_box.append(self.stats_bar)

        # Progress bar (shown during loading)
        self.progress_bar = Gtk.ProgressBar(show_text=True, visible=False, margin_start=12, margin_end=12)
        main_box.append(self.progress_bar)

        # Status bar
        self.status_label = Gtk.Label(label="", xalign=0, margin_start=12, margin_end=12, margin_bottom=4)
        self.status_label.add_css_class("dim-label")
        main_box.append(self.status_label)

        # Scrollable list
        scrolled = Gtk.ScrolledWindow(vexpand=True)
        self.list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_start=12, margin_end=12, margin_bottom=12)
        scrolled.set_child(self.list_box)
        main_box.append(scrolled)

        self.win.set_content(main_box)

        # Keyboard shortcuts
        self._setup_shortcuts()

        # Show welcome dialog on first run
        self._show_welcome()

        self.win.present()

        # Load data
        cached = load_cache()
        if cached:
            self.packages = cached
            self._apply_filter()
            self.status_label.set_label(_("Loaded from cache. Press refresh to update."))
        else:
            self._on_refresh(None)

    def _make_stat_card(self, title, value):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.add_css_class("stat-card")
        val_label = Gtk.Label(label=value)
        val_label.add_css_class("stat-number")
        title_label = Gtk.Label(label=title)
        title_label.add_css_class("dim-label")
        box.append(val_label)
        box.append(title_label)
        return box, val_label

    def _setup_shortcuts(self):
        ctrl = Gtk.EventControllerKey()
        ctrl.connect("key-pressed", self._on_key_pressed)
        self.win.add_controller(ctrl)

    def _on_key_pressed(self, ctrl, keyval, keycode, state):
        mod = state & Gtk.accelerator_get_default_mod_mask()
        if mod == Gdk.ModifierType.CONTROL_MASK:
            if keyval == Gdk.KEY_q:
                self.quit()
                return True
            elif keyval == Gdk.KEY_f or keyval == Gdk.KEY_F5 - Gdk.KEY_F5 + Gdk.KEY_f:
                self.search_entry.grab_focus()
                return True
            elif keyval == Gdk.KEY_e:
                self._export_csv()
                return True
        if keyval == Gdk.KEY_F5:
            self._on_refresh(None)
            return True
        return False

    def _on_lang_changed(self, dropdown, _pspec, lang_codes):
        idx = dropdown.get_selected()
        if 0 <= idx < len(lang_codes):
            self.selected_lang = lang_codes[idx]
            self._apply_filter()

    def _on_search_changed(self, entry):
        self.filter_text = entry.get_text().lower()
        self._apply_filter()

    def _on_filter_changed(self, dropdown, _pspec):
        filters = ["all", "translated", "untranslated", "outdated"]
        idx = dropdown.get_selected()
        self.filter_status = filters[idx] if idx < len(filters) else "all"
        self._apply_filter()

    def _on_sort_changed(self, dropdown, _pspec):
        sorts = ["name", "pct", "strings"]
        idx = dropdown.get_selected()
        self.sort_by = sorts[idx] if idx < len(sorts) else "name"
        self._apply_filter()

    def _on_refresh(self, _btn):
        if self._loading:
            return
        self._loading = True
        self.progress_bar.set_visible(True)
        self.progress_bar.set_fraction(0)
        self.progress_bar.set_text(_("Loading…"))
        self.status_label.set_label(_("Fetching data from translationproject.org…"))

        def worker():
            def progress(i, total, name):
                GLib.idle_add(lambda: (
                    self.progress_bar.set_fraction(i / total),
                    self.progress_bar.set_text(f"{i}/{total} — {name}"),
                ))
            try:
                packages = fetch_all_packages(progress_cb=progress)
                save_cache(packages)
                GLib.idle_add(self._on_data_loaded, packages)
            except Exception as e:
                GLib.idle_add(self._on_data_error, str(e))

        threading.Thread(target=worker, daemon=True).start()

    def _on_data_loaded(self, packages):
        self.packages = packages
        self._loading = False
        self.progress_bar.set_visible(False)
        now = datetime.now().strftime("%H:%M")
        self.status_label.set_label(_("Updated at %s — %d packages") % (now, len(packages)))
        self._apply_filter()

    def _on_data_error(self, error):
        self._loading = False
        self.progress_bar.set_visible(False)
        self.status_label.set_label(_("Error: %s") % error)

    def _apply_filter(self):
        lang = self.selected_lang
        filtered = []

        for pkg in self.packages:
            # Text filter
            if self.filter_text and self.filter_text not in pkg.name.lower():
                continue

            tr = pkg.translations.get(lang)
            pct = tr["pct"] if tr else 0

            # Status filter
            if self.filter_status == "translated" and pct < 100:
                continue
            elif self.filter_status == "untranslated" and pct > 0:
                continue
            elif self.filter_status == "outdated" and (not tr or pct >= 100):
                continue

            filtered.append(pkg)

        # Sort
        if self.sort_by == "pct":
            filtered.sort(key=lambda p: p.translations.get(lang, {}).get("pct", 0), reverse=True)
        elif self.sort_by == "strings":
            filtered.sort(key=lambda p: p.total_strings, reverse=True)
        else:
            filtered.sort(key=lambda p: p.name.lower())

        self.filtered_packages = filtered
        self._update_stats()
        self._rebuild_list()

    def _update_stats(self):
        lang = self.selected_lang
        total = len(self.packages)
        full = sum(1 for p in self.packages if p.translations.get(lang, {}).get("pct", 0) >= 100)
        partial = sum(1 for p in self.packages if 0 < p.translations.get(lang, {}).get("pct", 0) < 100)
        missing = total - full - partial

        self._stat_total[1].set_label(str(total))
        self._stat_translated[1].set_label(str(full))
        self._stat_partial[1].set_label(str(partial))
        self._stat_missing[1].set_label(str(missing))

    def _rebuild_list(self):
        # Clear list
        child = self.list_box.get_first_child()
        while child:
            next_child = child.get_next_sibling()
            self.list_box.remove(child)
            child = next_child

        lang = self.selected_lang

        for pkg in self.filtered_packages:
            tr = pkg.translations.get(lang)
            pct = tr["pct"] if tr else 0
            translated = tr["translated"] if tr else 0
            total = tr["total"] if tr else pkg.total_strings
            version = tr["version"] if tr else ""
            translator = tr["translator"] if tr else ""

            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            row.add_css_class("card")
            row.set_margin_top(2)
            row.set_margin_bottom(2)

            # Package name + version
            name_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, margin_start=12, margin_top=8, margin_bottom=8)
            name_label = Gtk.Label(label=pkg.name, xalign=0)
            name_label.add_css_class("pkg-title")
            name_box.append(name_label)

            detail = f"v{pkg.latest_version}" if pkg.latest_version else ""
            if translator:
                detail += f" — {translator}"
            if version and version != pkg.latest_version:
                detail += f" ({_('translated')}: v{version})"
            if detail:
                detail_label = Gtk.Label(label=detail, xalign=0)
                detail_label.add_css_class("dim-label")
                detail_label.set_ellipsize(Pango.EllipsizeMode.END)
                name_box.append(detail_label)

            row.append(name_box)

            # Stats
            stats_str = f"{translated}/{total}"
            stats_label = Gtk.Label(label=stats_str, margin_end=4)
            stats_label.add_css_class("dim-label")
            row.append(stats_label)

            # Percentage badge
            pct_label = Gtk.Label(label=f"{pct:.0f}%", margin_end=8)
            pct_label.add_css_class(_heatmap_class(pct))
            pct_label.set_width_chars(5)
            row.append(pct_label)

            # Click to open in browser
            gesture = Gtk.GestureClick()
            gesture.connect("released", self._on_pkg_click, pkg.name)
            row.add_controller(gesture)
            row.set_cursor(Gdk.Cursor.new_from_name("pointer"))

            self.list_box.append(row)

    def _on_pkg_click(self, gesture, n_press, x, y, pkg_name):
        webbrowser.open(f"https://translationproject.org/domain/{pkg_name}.html")

    def _export_csv(self):
        lang = self.selected_lang
        lines = ["Package,Version,Translated,Total,Percentage,Translator"]
        for pkg in self.filtered_packages:
            tr = pkg.translations.get(lang, {})
            lines.append(f"{pkg.name},{pkg.latest_version},{tr.get('translated', 0)},{tr.get('total', pkg.total_strings)},{tr.get('pct', 0)},{tr.get('translator', '')}")

        dialog = Gtk.FileDialog(title=_("Export CSV"))
        dialog.set_initial_name(f"tp-status-{lang}.csv")
        dialog.save(self.win, None, self._on_export_done, "\n".join(lines))

    def _on_export_done(self, dialog, result, csv_data):
        try:
            f = dialog.save_finish(result)
            if f:
                with open(f.get_path(), "w") as fh:
                    fh.write(csv_data)
                self.status_label.set_label(_("Exported to %s") % f.get_path())
        except Exception:
            pass

    def _show_about(self, *_args):
        about = Adw.AboutDialog(
            application_name=_("Translation Project Status"),
            application_icon=APP_ID,
            version=VERSION,
            developer_name="Daniel Nylander",
            website="https://github.com/yeager/tp-status",
            issue_url="https://github.com/yeager/tp-status/issues",
            license_type=Gtk.License.GPL_3_0,
            comments=_("View translation statistics from translationproject.org"),
        )
        about.present(self.win)

    def _show_shortcuts(self, *_args):
        shortcuts = Adw.Dialog(title=_("Keyboard Shortcuts"))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=20, margin_end=20, margin_top=20, margin_bottom=20)
        for key, desc in [
            ("Ctrl+Q", _("Quit")),
            ("Ctrl+F", _("Search")),
            ("Ctrl+E", _("Export CSV")),
            ("F5", _("Refresh")),
        ]:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            row.append(Gtk.Label(label=key, width_chars=10, xalign=0))
            row.append(Gtk.Label(label=desc, xalign=0, hexpand=True))
            box.append(row)
        shortcuts.set_child(box)
        shortcuts.present(self.win)

    def _show_welcome(self):
        config_dir = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "tp-status")
        os.makedirs(config_dir, exist_ok=True)
        welcome_file = os.path.join(config_dir, "welcome.json")
        if os.path.exists(welcome_file):
            return

        dialog = Adw.Dialog(title=_("Welcome"))
        page = Adw.StatusPage(
            icon_name="accessories-dictionary-symbolic",
            title=_("Translation Project Status"),
            description=_("View and track translation progress for GNU and free software packages on translationproject.org.\n\n"
                         "• Browse 148+ packages with translation stats\n"
                         "• Filter by language, completion status\n"
                         "• Color-coded completion percentages\n"
                         "• Export data to CSV\n"
                         "• Click any package to open on TP website"),
        )
        close_btn = Gtk.Button(label=_("Get Started"), halign=Gtk.Align.CENTER)
        close_btn.add_css_class("suggested-action")
        close_btn.add_css_class("pill")
        close_btn.connect("clicked", lambda b: dialog.close())
        page.set_child(close_btn)
        dialog.set_child(page)
        dialog.set_content_width(450)
        dialog.set_content_height(400)
        dialog.present(self.win)

        with open(welcome_file, "w") as f:
            json.dump({"shown": True}, f)


def main():
    app = TPStatusApp()
    app.run(sys.argv)


if __name__ == "__main__":
    main()
