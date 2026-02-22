"""Scrape translation statistics from translationproject.org."""

import re
import urllib.request
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

BASE_URL = "https://translationproject.org"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "tp-status"

LANGUAGES = {
    "sv": "Swedish", "da": "Danish", "de": "German", "es": "Spanish",
    "fi": "Finnish", "fr": "French", "it": "Italian", "nb": "Norwegian Bokmål",
    "nl": "Dutch", "pl": "Polish", "pt_BR": "Brazilian Portuguese",
    "cs": "Czech", "hu": "Hungarian", "ja": "Japanese", "ko": "Korean",
    "ru": "Russian", "uk": "Ukrainian", "zh_CN": "Chinese (Simplified)",
    "zh_TW": "Chinese (Traditional)", "bg": "Bulgarian", "ca": "Catalan",
    "el": "Greek", "eo": "Esperanto", "et": "Estonian", "eu": "Basque",
    "ga": "Irish", "gl": "Galician", "hr": "Croatian", "id": "Indonesian",
    "lt": "Lithuanian", "lv": "Latvian", "ms": "Malay", "ro": "Romanian",
    "sk": "Slovak", "sl": "Slovenian", "sr": "Serbian", "tr": "Turkish",
    "vi": "Vietnamese",
}


@dataclass
class PackageInfo:
    name: str
    latest_version: str = ""
    total_strings: int = 0
    translations: dict = field(default_factory=dict)  # lang -> {version, translated, total, pct, translator}


def _fetch(url: str) -> str:
    """Fetch URL content."""
    req = urllib.request.Request(url, headers={"User-Agent": "tp-status/0.1"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def fetch_package_list() -> list[str]:
    """Get list of all packages from the domain index."""
    html = _fetch(f"{BASE_URL}/domain/index.html")
    return re.findall(r'<a href="([^"]+)\.html"[^>]*>([^<]+)</a>', html)


def fetch_package_stats(pkg_name: str) -> PackageInfo:
    """Fetch translation stats for a single package."""
    info = PackageInfo(name=pkg_name)
    try:
        html = _fetch(f"{BASE_URL}/domain/{pkg_name}.html")
    except Exception:
        return info

    # Parse the table rows
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL)
    
    current_lang = None
    current_code = None
    
    for row in rows:
        cells = re.findall(r'<td[^>]*>(.*?)</td>', row, re.DOTALL)
        clean = [re.sub(r'<[^>]+>', '', c).strip() for c in cells]
        
        if not clean:
            continue
        
        # Language row: has lang name, code, version, translator, stats
        if len(clean) >= 5:
            lang_name = clean[0]
            lang_code = clean[1]
            version = clean[2]
            translator = clean[3]
            stats_str = clean[4]
            
            if '/' in stats_str:
                try:
                    parts = stats_str.split('/')
                    translated = int(parts[0].strip())
                    total = int(parts[1].strip())
                    pct = round(translated / total * 100, 1) if total > 0 else 0
                    info.translations[lang_code] = {
                        "version": version,
                        "translated": translated,
                        "total": total,
                        "pct": pct,
                        "translator": translator,
                    }
                    if total > info.total_strings:
                        info.total_strings = total
                        info.latest_version = version
                    current_lang = lang_code
                except (ValueError, IndexError):
                    pass
        
        # Continuation row (same language, newer version): version, translator, stats
        elif len(clean) >= 3 and '/' in clean[-1] and current_lang:
            version = clean[0] if len(clean) >= 3 else ""
            translator = clean[-2] if len(clean) >= 3 else ""
            stats_str = clean[-1]
            
            if '/' in stats_str:
                try:
                    parts = stats_str.split('/')
                    translated = int(parts[0].strip())
                    total = int(parts[1].strip())
                    pct = round(translated / total * 100, 1) if total > 0 else 0
                    # Update to latest version
                    info.translations[current_lang] = {
                        "version": version,
                        "translated": translated,
                        "total": total,
                        "pct": pct,
                        "translator": translator,
                    }
                    if total > info.total_strings:
                        info.total_strings = total
                        info.latest_version = version
                except (ValueError, IndexError):
                    pass
    
    return info


def fetch_all_packages(progress_cb=None) -> list[PackageInfo]:
    """Fetch stats for all packages."""
    pkg_list = fetch_package_list()
    results = []
    for i, (slug, name) in enumerate(pkg_list):
        if progress_cb:
            progress_cb(i + 1, len(pkg_list), name)
        info = fetch_package_stats(slug)
        if info.total_strings > 0:
            results.append(info)
    results.sort(key=lambda p: p.name.lower())
    return results


def save_cache(packages: list[PackageInfo]):
    """Save package data to cache."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    data = []
    for p in packages:
        data.append({
            "name": p.name,
            "latest_version": p.latest_version,
            "total_strings": p.total_strings,
            "translations": p.translations,
        })
    (CACHE_DIR / "packages.json").write_text(json.dumps(data, indent=2))


def load_settings() -> dict:
    """Load user settings."""
    settings_file = CACHE_DIR / "settings.json"
    if settings_file.exists():
        try:
            return json.loads(settings_file.read_text())
        except Exception:
            pass
    return {}


def save_settings(settings: dict):
    """Save user settings."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / "settings.json").write_text(json.dumps(settings, indent=2))


def load_cache() -> list[PackageInfo] | None:
    """Load cached package data."""
    cache_file = CACHE_DIR / "packages.json"
    if not cache_file.exists():
        return None
    try:
        data = json.loads(cache_file.read_text())
        return [PackageInfo(
            name=d["name"],
            latest_version=d.get("latest_version", ""),
            total_strings=d.get("total_strings", 0),
            translations=d.get("translations", {}),
        ) for d in data]
    except Exception:
        return None
