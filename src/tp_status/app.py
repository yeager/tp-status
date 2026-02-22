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
    fetch_all_packages, PackageInfo,
    LANGUAGES, save_cache, load_cache, load_settings, save_settings,
)

LOCALEDIR = '/usr/share/locale'
gettext.bindtextdomain('tp-status', LOCALEDIR)
locale.bindtextdomain('tp-status', LOCALEDIR)
gettext.textdomain('tp-status')
_ = gettext.gettext

VERSION = "0.2.0"
APP_ID = "se.danielnylander.tp-status"

TP_BASE = "https://translationproject.org"


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
    .rank-gold { color: #f5c211; font-weight: bold; }
    .rank-silver { color: #a8a8a8; font-weight: bold; }
    .rank-bronze { color: #cd7f32; font-weight: bold; }
    .leaderboard-row { padding: 4px 12px; }
    .view-switcher { margin: 0 8px; }
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
        self.filter_text = ""
        self.filter_status = "all"
        self.sort_by = "name"
        self.sort_reverse = False
        self._loading = False

        # Load settings
        settings = load_settings()
        self.selected_lang = settings.get("language", _get_system_lang())
        self.sort_by = settings.get("sort_by", "name")
        self.sort_reverse = settings.get("sort_reverse", False)
        self.filter_status = settings.get("filter_status", "all")

    def _save_prefs(self):
        save_settings({
            "language": self.selected_lang,
            "sort_by": self.sort_by,
            "sort_reverse": self.sort_reverse,
            "filter_status": self.filter_status,
        })

    def do_activate(self):
        _setup_css()
        self.win = Adw.ApplicationWindow(application=self, default_width=1050, default_height=750)
        self.win.set_title(_("Translation Project — Domain Status"))

        # Header bar
        header = Adw.HeaderBar()

        # Language selector with "All languages" option
        lang_list = [("all", _("All Languages"))] + sorted(LANGUAGES.items(), key=lambda x: x[1])
        lang_names = [name for _code, name in lang_list]
        self._lang_codes = [code for code, _name in lang_list]
        lang_model = Gtk.StringList.new(lang_names)
        self.lang_dropdown = Gtk.DropDown(model=lang_model, tooltip_text=_("Language"))
        if self.selected_lang in self._lang_codes:
            self.lang_dropdown.set_selected(self._lang_codes.index(self.selected_lang))
        self.lang_dropdown.connect("notify::selected", self._on_lang_changed)
        header.pack_start(self.lang_dropdown)

        # Refresh button
        refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text=_("Refresh data from TP"))
        refresh_btn.connect("clicked", self._on_refresh)
        header.pack_start(refresh_btn)

        # Right side: TP link, settings, menu
        tp_btn = Gtk.Button(icon_name="web-browser-symbolic", tooltip_text=_("Open translationproject.org"))
        tp_btn.connect("clicked", lambda _b: webbrowser.open(TP_BASE))
        header.pack_end(tp_btn)

        menu = Gio.Menu()
        menu.append(_("Preferences"), "win.preferences")
        menu.append(_("Keyboard Shortcuts"), "win.shortcuts")
        menu.append(_("About"), "win.about")
        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu)
        header.pack_end(menu_button)

        # Actions
        for name, cb in [("about", self._show_about), ("shortcuts", self._show_shortcuts), ("preferences", self._show_preferences)]:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", cb)
            self.win.add_action(action)

        # Main layout with view stack
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        main_box.append(header)

        # View switcher: Domains / Leaderboard
        self.view_stack = Adw.ViewStack()
        self.view_stack.add_titled(self._build_domains_view(), "domains", _("Domains"))
        self.view_stack.add_titled(self._build_leaderboard_view(), "leaderboard", _("Leaderboard"))

        switcher = Adw.ViewSwitcher(stack=self.view_stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        switcher.add_css_class("view-switcher")
        main_box.append(switcher)
        main_box.append(self.view_stack)

        self.win.set_content(main_box)
        self._setup_shortcuts()
        self._show_welcome()
        self.win.present()

        # Load data
        cached = load_cache()
        if cached:
            self.packages = cached
            self._apply_filter()
            self._update_leaderboard()
            self.status_label.set_label(_("Loaded from cache. Press refresh to update."))
        else:
            self._on_refresh(None)

    def _build_domains_view(self):
        """Build the main domains list view."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        # Toolbar
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
                          margin_start=12, margin_end=12, margin_top=8, margin_bottom=4)

        self.search_entry = Gtk.SearchEntry(placeholder_text=_("Search domains…"), hexpand=True)
        self.search_entry.connect("search-changed", self._on_search_changed)
        toolbar.append(self.search_entry)

        # Filter
        filter_items = [_("All"), _("Fully Translated"), _("Partially Translated"), _("Not Translated"), _("Outdated")]
        filter_model = Gtk.StringList.new(filter_items)
        self.filter_dropdown = Gtk.DropDown(model=filter_model, tooltip_text=_("Filter"))
        filters = ["all", "translated", "partial", "untranslated", "outdated"]
        if self.filter_status in filters:
            self.filter_dropdown.set_selected(filters.index(self.filter_status))
        self.filter_dropdown.connect("notify::selected", self._on_filter_changed)
        toolbar.append(self.filter_dropdown)

        # Sort
        sort_items = [_("Name"), _("Completion %"), _("Total Strings"), _("Translated Strings"), _("Translator")]
        sort_model = Gtk.StringList.new(sort_items)
        self.sort_dropdown = Gtk.DropDown(model=sort_model, tooltip_text=_("Sort by"))
        sorts = ["name", "pct", "strings", "translated", "translator"]
        if self.sort_by in sorts:
            self.sort_dropdown.set_selected(sorts.index(self.sort_by))
        self.sort_dropdown.connect("notify::selected", self._on_sort_changed)
        toolbar.append(self.sort_dropdown)

        # Reverse sort toggle
        self.reverse_btn = Gtk.ToggleButton(icon_name="view-sort-descending-symbolic",
                                            tooltip_text=_("Reverse sort order"), active=self.sort_reverse)
        self.reverse_btn.connect("toggled", self._on_reverse_toggled)
        toolbar.append(self.reverse_btn)

        box.append(toolbar)

        # Stats bar
        stats_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12,
                            margin_start=12, margin_end=12, margin_top=4, margin_bottom=8, homogeneous=True)
        self._stat_total = self._make_stat_card(_("Domains"), "0")
        self._stat_translated = self._make_stat_card(_("Fully Translated"), "0")
        self._stat_partial = self._make_stat_card(_("Partial"), "0")
        self._stat_missing = self._make_stat_card(_("Not Translated"), "0")
        self._stat_avg = self._make_stat_card(_("Average"), "0%")
        stats_bar.append(self._stat_total[0])
        stats_bar.append(self._stat_translated[0])
        stats_bar.append(self._stat_partial[0])
        stats_bar.append(self._stat_missing[0])
        stats_bar.append(self._stat_avg[0])
        box.append(stats_bar)

        # Progress bar
        self.progress_bar = Gtk.ProgressBar(show_text=True, visible=False, margin_start=12, margin_end=12)
        box.append(self.progress_bar)

        # Status bar
        self.status_label = Gtk.Label(label="", xalign=0, margin_start=12, margin_end=12, margin_bottom=4)
        self.status_label.add_css_class("dim-label")
        box.append(self.status_label)

        # Domain list
        scrolled = Gtk.ScrolledWindow(vexpand=True)
        self.list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4,
                                margin_start=12, margin_end=12, margin_bottom=12)
        scrolled.set_child(self.list_box)
        box.append(scrolled)

        return box

    def _build_leaderboard_view(self):
        """Build the language leaderboard view."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_start=12, margin_end=12, margin_top=12)

        title = Gtk.Label(label=_("Language Leaderboard"), xalign=0)
        title.add_css_class("title-2")
        box.append(title)

        subtitle = Gtk.Label(label=_("Ranked by number of fully translated domains"), xalign=0, margin_bottom=12)
        subtitle.add_css_class("dim-label")
        box.append(subtitle)

        scrolled = Gtk.ScrolledWindow(vexpand=True)
        self.leaderboard_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        scrolled.set_child(self.leaderboard_box)
        box.append(scrolled)

        return box

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
            elif keyval == Gdk.KEY_f:
                self.search_entry.grab_focus()
                return True
            elif keyval == Gdk.KEY_e:
                self._export_csv()
                return True
            elif keyval == Gdk.KEY_slash:
                self._show_shortcuts(None)
                return True
        if keyval == Gdk.KEY_F5:
            self._on_refresh(None)
            return True
        return False

    def _on_lang_changed(self, dropdown, _pspec):
        idx = dropdown.get_selected()
        if 0 <= idx < len(self._lang_codes):
            self.selected_lang = self._lang_codes[idx]
            self._save_prefs()
            self._apply_filter()

    def _on_search_changed(self, entry):
        self.filter_text = entry.get_text().lower()
        self._apply_filter()

    def _on_filter_changed(self, dropdown, _pspec):
        filters = ["all", "translated", "partial", "untranslated", "outdated"]
        idx = dropdown.get_selected()
        self.filter_status = filters[idx] if idx < len(filters) else "all"
        self._save_prefs()
        self._apply_filter()

    def _on_sort_changed(self, dropdown, _pspec):
        sorts = ["name", "pct", "strings", "translated", "translator"]
        idx = dropdown.get_selected()
        self.sort_by = sorts[idx] if idx < len(sorts) else "name"
        self._save_prefs()
        self._apply_filter()

    def _on_reverse_toggled(self, btn):
        self.sort_reverse = btn.get_active()
        icon = "view-sort-ascending-symbolic" if self.sort_reverse else "view-sort-descending-symbolic"
        btn.set_icon_name(icon)
        self._save_prefs()
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
        self.status_label.set_label(_("Updated at %s — %d domains") % (now, len(packages)))
        self._apply_filter()
        self._update_leaderboard()

    def _on_data_error(self, error):
        self._loading = False
        self.progress_bar.set_visible(False)
        self.status_label.set_label(_("Error: %s") % error)

    def _get_lang_stats(self, pkg, lang):
        """Get stats for a package. If lang='all', aggregate across all languages."""
        if lang == "all":
            # Show: how many languages have translated this domain
            total_langs = len(pkg.translations)
            full_langs = sum(1 for t in pkg.translations.values() if t.get("pct", 0) >= 100)
            pct = round(full_langs / total_langs * 100, 1) if total_langs > 0 else 0
            return {
                "translated": full_langs,
                "total": total_langs,
                "pct": pct,
                "translator": f"{total_langs} {_('languages')}",
                "version": "",
            }
        tr = pkg.translations.get(lang)
        if tr:
            return tr
        return {"translated": 0, "total": pkg.total_strings, "pct": 0, "translator": "", "version": ""}

    def _apply_filter(self):
        lang = self.selected_lang
        filtered = []

        for pkg in self.packages:
            if self.filter_text and self.filter_text not in pkg.name.lower():
                continue

            stats = self._get_lang_stats(pkg, lang)
            pct = stats["pct"]

            if self.filter_status == "translated" and pct < 100:
                continue
            elif self.filter_status == "partial" and not (0 < pct < 100):
                continue
            elif self.filter_status == "untranslated" and pct > 0:
                continue
            elif self.filter_status == "outdated":
                if lang == "all":
                    continue
                tr = pkg.translations.get(lang)
                if not tr or tr.get("pct", 0) >= 100:
                    continue

            filtered.append(pkg)

        # Sort
        def sort_key(p):
            stats = self._get_lang_stats(p, lang)
            if self.sort_by == "pct":
                return stats["pct"]
            elif self.sort_by == "strings":
                return p.total_strings
            elif self.sort_by == "translated":
                return stats["translated"]
            elif self.sort_by == "translator":
                return stats.get("translator", "").lower()
            return p.name.lower()

        reverse = self.sort_reverse
        if self.sort_by in ("pct", "strings", "translated"):
            reverse = not reverse  # Default descending for numeric
        filtered.sort(key=sort_key, reverse=reverse)

        self.filtered_packages = filtered
        self._update_stats()
        self._rebuild_list()

    def _update_stats(self):
        lang = self.selected_lang
        total = len(self.packages)

        if lang == "all":
            # Global stats: average completion across all languages
            all_pcts = []
            for p in self.packages:
                for tr in p.translations.values():
                    all_pcts.append(tr.get("pct", 0))
            avg = round(sum(all_pcts) / len(all_pcts), 1) if all_pcts else 0
            full = sum(1 for p in self.packages
                       if all(t.get("pct", 0) >= 100 for t in p.translations.values()) and p.translations)
            partial = total - full
            missing = 0
        else:
            full = sum(1 for p in self.packages if p.translations.get(lang, {}).get("pct", 0) >= 100)
            partial = sum(1 for p in self.packages if 0 < p.translations.get(lang, {}).get("pct", 0) < 100)
            missing = total - full - partial
            pcts = [p.translations.get(lang, {}).get("pct", 0) for p in self.packages]
            avg = round(sum(pcts) / len(pcts), 1) if pcts else 0

        self._stat_total[1].set_label(str(total))
        self._stat_translated[1].set_label(str(full))
        self._stat_partial[1].set_label(str(partial))
        self._stat_missing[1].set_label(str(missing))
        self._stat_avg[1].set_label(f"{avg}%")

    def _rebuild_list(self):
        child = self.list_box.get_first_child()
        while child:
            next_child = child.get_next_sibling()
            self.list_box.remove(child)
            child = next_child

        lang = self.selected_lang

        for pkg in self.filtered_packages:
            stats = self._get_lang_stats(pkg, lang)
            pct = stats["pct"]
            translated = stats["translated"]
            total = stats["total"]
            version = stats.get("version", "")
            translator = stats.get("translator", "")

            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            row.add_css_class("card")
            row.set_margin_top(2)
            row.set_margin_bottom(2)

            # Domain name + details
            name_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True,
                               margin_start=12, margin_top=8, margin_bottom=8)
            name_label = Gtk.Label(label=pkg.name, xalign=0)
            name_label.add_css_class("pkg-title")
            name_box.append(name_label)

            detail_parts = []
            if pkg.latest_version:
                detail_parts.append(f"v{pkg.latest_version}")
            if pkg.total_strings > 0 and lang != "all":
                detail_parts.append(f"{pkg.total_strings} {_('strings')}")
            if translator and lang != "all":
                detail_parts.append(translator)
            if version and version != pkg.latest_version and lang != "all":
                detail_parts.append(f"({_('translated')}: v{version})")
            if detail_parts:
                detail_label = Gtk.Label(label=" — ".join(detail_parts), xalign=0)
                detail_label.add_css_class("dim-label")
                detail_label.set_ellipsize(Pango.EllipsizeMode.END)
                name_box.append(detail_label)

            row.append(name_box)

            # Stats
            if lang == "all":
                stats_str = f"{translated}/{total} {_('langs')}"
            else:
                stats_str = f"{translated}/{total}"
            stats_label = Gtk.Label(label=stats_str, margin_end=4)
            stats_label.add_css_class("dim-label")
            row.append(stats_label)

            # Percentage badge
            pct_label = Gtk.Label(label=f"{pct:.0f}%", margin_end=8)
            pct_label.add_css_class(_heatmap_class(pct))
            pct_label.set_width_chars(5)
            row.append(pct_label)

            # TP link button
            link_btn = Gtk.Button(icon_name="external-link-symbolic",
                                  valign=Gtk.Align.CENTER, margin_end=8,
                                  tooltip_text=_("Open on TP"))
            link_btn.add_css_class("flat")
            link_btn.connect("clicked", self._on_pkg_link, pkg.name)
            row.append(link_btn)

            # Click row to open
            gesture = Gtk.GestureClick()
            gesture.connect("released", self._on_pkg_click, pkg.name)
            row.add_controller(gesture)
            row.set_cursor(Gdk.Cursor.new_from_name("pointer"))

            self.list_box.append(row)

    def _update_leaderboard(self):
        """Build language leaderboard ranked by fully translated domains."""
        child = self.leaderboard_box.get_first_child()
        while child:
            next_child = child.get_next_sibling()
            self.leaderboard_box.remove(child)
            child = next_child

        if not self.packages:
            return

        # Count full translations per language
        lang_stats = {}
        for code, name in LANGUAGES.items():
            full = sum(1 for p in self.packages if p.translations.get(code, {}).get("pct", 0) >= 100)
            partial = sum(1 for p in self.packages if 0 < p.translations.get(code, {}).get("pct", 0) < 100)
            total_pct = [p.translations.get(code, {}).get("pct", 0) for p in self.packages]
            avg = round(sum(total_pct) / len(total_pct), 1) if total_pct else 0
            lang_stats[code] = {"name": name, "full": full, "partial": partial, "avg": avg}

        # Sort by full translations descending
        ranked = sorted(lang_stats.items(), key=lambda x: (x[1]["full"], x[1]["avg"]), reverse=True)

        # Header row
        hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, margin_bottom=4)
        hdr.append(Gtk.Label(label="#", width_chars=4, xalign=1))
        hdr.append(Gtk.Label(label=_("Language"), hexpand=True, xalign=0))
        hdr.append(Gtk.Label(label=_("100%"), width_chars=6, xalign=1))
        hdr.append(Gtk.Label(label=_("Partial"), width_chars=8, xalign=1))
        hdr.append(Gtk.Label(label=_("Average"), width_chars=8, xalign=1))
        for child_w in [hdr.get_first_child()]:
            pass
        self.leaderboard_box.append(hdr)
        self.leaderboard_box.append(Gtk.Separator())

        total_domains = len(self.packages)

        for rank, (code, stats) in enumerate(ranked, 1):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row.add_css_class("leaderboard-row")
            if rank <= 3:
                row.add_css_class("card")

            # Rank
            rank_label = Gtk.Label(label=str(rank), width_chars=4, xalign=1)
            if rank == 1:
                rank_label.add_css_class("rank-gold")
            elif rank == 2:
                rank_label.add_css_class("rank-silver")
            elif rank == 3:
                rank_label.add_css_class("rank-bronze")
            row.append(rank_label)

            # Language name + code
            lang_label = Gtk.Label(label=f"{stats['name']} ({code})", hexpand=True, xalign=0)
            row.append(lang_label)

            # Full count
            full_label = Gtk.Label(label=str(stats["full"]), width_chars=6, xalign=1)
            full_label.add_css_class(_heatmap_class(stats["full"] / total_domains * 100 if total_domains else 0))
            row.append(full_label)

            # Partial
            partial_label = Gtk.Label(label=str(stats["partial"]), width_chars=8, xalign=1)
            row.append(partial_label)

            # Average
            avg_label = Gtk.Label(label=f"{stats['avg']}%", width_chars=8, xalign=1)
            row.append(avg_label)

            # Click to select language
            gesture = Gtk.GestureClick()
            gesture.connect("released", self._on_leaderboard_click, code)
            row.add_controller(gesture)
            row.set_cursor(Gdk.Cursor.new_from_name("pointer"))

            self.leaderboard_box.append(row)

    def _on_leaderboard_click(self, gesture, n_press, x, y, lang_code):
        """Switch to domain view filtered by this language."""
        if lang_code in self._lang_codes:
            self.selected_lang = lang_code
            self.lang_dropdown.set_selected(self._lang_codes.index(lang_code))
            self.view_stack.set_visible_child_name("domains")
            self._apply_filter()

    def _on_pkg_click(self, gesture, n_press, x, y, pkg_name):
        webbrowser.open(f"{TP_BASE}/domain/{pkg_name}.html")

    def _on_pkg_link(self, btn, pkg_name):
        webbrowser.open(f"{TP_BASE}/domain/{pkg_name}.html")

    def _export_csv(self):
        lang = self.selected_lang
        lines = ["Domain,Version,Translated,Total,Percentage,Translator"]
        for pkg in self.filtered_packages:
            stats = self._get_lang_stats(pkg, lang)
            lines.append(f"{pkg.name},{pkg.latest_version},{stats.get('translated', 0)},{stats.get('total', pkg.total_strings)},{stats.get('pct', 0)},{stats.get('translator', '')}")

        dialog = Gtk.FileDialog(title=_("Export CSV"))
        dialog.set_initial_name(f"tp-domains-{lang}.csv")
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

    def _show_preferences(self, *_args):
        dialog = Adw.PreferencesDialog(title=_("Preferences"))

        # General page
        page = Adw.PreferencesPage(title=_("General"), icon_name="preferences-system-symbolic")

        # Language group
        lang_group = Adw.PreferencesGroup(title=_("Default Language"))
        lang_row = Adw.ComboRow(title=_("Language"), subtitle=_("Default language to show on startup"))
        lang_list = sorted(LANGUAGES.items(), key=lambda x: x[1])
        lang_model = Gtk.StringList.new([f"{code} — {name}" for code, name in lang_list])
        lang_row.set_model(lang_model)
        lang_codes = [code for code, _name in lang_list]
        if self.selected_lang in lang_codes:
            lang_row.set_selected(lang_codes.index(self.selected_lang))
        lang_row.connect("notify::selected", self._on_pref_lang_changed, lang_codes)
        lang_group.add(lang_row)
        page.add(lang_group)

        # Links group
        links_group = Adw.PreferencesGroup(title=_("Links"))
        tp_row = Adw.ActionRow(title=_("Translation Project"), subtitle="translationproject.org", activatable=True)
        tp_row.add_suffix(Gtk.Image(icon_name="external-link-symbolic"))
        tp_row.connect("activated", lambda _r: webbrowser.open(TP_BASE))
        links_group.add(tp_row)

        team_row = Adw.ActionRow(title=_("Your Team Page"),
                                 subtitle=f"translationproject.org/team/{self.selected_lang}.html",
                                 activatable=True)
        team_row.add_suffix(Gtk.Image(icon_name="external-link-symbolic"))
        team_row.connect("activated", lambda _r: webbrowser.open(f"{TP_BASE}/team/{self.selected_lang}.html"))
        links_group.add(team_row)

        matrix_row = Adw.ActionRow(title=_("Translation Matrix"), subtitle=_("Overview of all languages × domains"),
                                   activatable=True)
        matrix_row.add_suffix(Gtk.Image(icon_name="external-link-symbolic"))
        matrix_row.connect("activated", lambda _r: webbrowser.open(f"{TP_BASE}/extra/matrix.html"))
        links_group.add(matrix_row)

        page.add(links_group)
        dialog.add(page)
        dialog.present(self.win)

    def _on_pref_lang_changed(self, row, _pspec, lang_codes):
        idx = row.get_selected()
        if 0 <= idx < len(lang_codes):
            self.selected_lang = lang_codes[idx]
            if self.selected_lang in self._lang_codes:
                self.lang_dropdown.set_selected(self._lang_codes.index(self.selected_lang))
            self._save_prefs()
            self._apply_filter()

    def _show_about(self, *_args):
        about = Adw.AboutDialog(
            application_name=_("Translation Project Status"),
            application_icon=APP_ID,
            version=VERSION,
            developer_name="Daniel Nylander",
            website="https://github.com/yeager/tp-status",
            issue_url="https://github.com/yeager/tp-status/issues",
            license_type=Gtk.License.GPL_3_0,
            comments=_("View translation statistics for domains on translationproject.org.\n\n"
                       "TP calls them 'domains' — each domain is a software package "
                       "with translatable strings."),
        )
        about.present(self.win)

    def _show_shortcuts(self, *_args):
        shortcuts = Adw.Dialog(title=_("Keyboard Shortcuts"))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                      margin_start=20, margin_end=20, margin_top=20, margin_bottom=20)
        for key, desc in [
            ("Ctrl+Q", _("Quit")),
            ("Ctrl+F", _("Search")),
            ("Ctrl+E", _("Export CSV")),
            ("Ctrl+/", _("Keyboard Shortcuts")),
            ("F5", _("Refresh")),
        ]:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            row.append(Gtk.Label(label=key, width_chars=10, xalign=0))
            row.append(Gtk.Label(label=desc, xalign=0, hexpand=True))
            box.append(row)
        shortcuts.set_child(box)
        shortcuts.present(self.win)

    def _show_welcome(self):
        config_dir = os.path.join(os.environ.get("XDG_CONFIG_HOME",
                                  os.path.expanduser("~/.config")), "tp-status")
        os.makedirs(config_dir, exist_ok=True)
        welcome_file = os.path.join(config_dir, "welcome.json")
        if os.path.exists(welcome_file):
            return

        dialog = Adw.Dialog(title=_("Welcome"))
        page = Adw.StatusPage(
            icon_name="accessories-dictionary-symbolic",
            title=_("Translation Project Status"),
            description=_("View and track translation progress for GNU and free software "
                         "domains on translationproject.org.\n\n"
                         "• Browse 148+ domains with translation stats\n"
                         "• Filter by language and completion status\n"
                         "• Language leaderboard — see which teams lead\n"
                         "• Sort by name, completion, strings, translator\n"
                         "• Reverse sort order with one click\n"
                         "• Export data to CSV\n"
                         "• Click any domain to open on TP website"),
        )
        close_btn = Gtk.Button(label=_("Get Started"), halign=Gtk.Align.CENTER)
        close_btn.add_css_class("suggested-action")
        close_btn.add_css_class("pill")
        close_btn.connect("clicked", lambda b: dialog.close())
        page.set_child(close_btn)
        dialog.set_child(page)
        dialog.set_content_width(480)
        dialog.set_content_height(450)
        dialog.present(self.win)

        with open(welcome_file, "w") as f:
            json.dump({"shown": True}, f)


def main():
    app = TPStatusApp()
    app.run(sys.argv)


if __name__ == "__main__":
    main()
