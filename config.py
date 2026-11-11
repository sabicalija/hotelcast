"""
Persistent Configuration Management for HotelCast (`config.py`).

Stores and loads user settings in `~/.config/hotelcast/config.json`:
- `tv_ip` (default `""` — populated via mDNS scan or manual entry)
- `tv_port` (default `52266` — standard HappyCast / LeLink AirPlay port)
- `tv_name` (default `""` — optional room number or TV label)
- `activation_url` (default `""` — optional hotel portal URL or token)
- `last_directory` (default `""`)
- `preferred_audio_lang` (default `"eng"`, supports `"ger"` / `"deu"` / `"eng"`, etc.)
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from typing import Any, Dict, List


CONFIG_DIR = os.path.expanduser("~/.config/hotelcast")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")

LANGUAGE_SYNONYMS: Dict[str, set[str]] = {
    "ger": {"ger", "deu", "de", "german", "deutsch"},
    "deu": {"ger", "deu", "de", "german", "deutsch"},
    "de": {"ger", "deu", "de", "german", "deutsch"},
    "eng": {"eng", "en", "english"},
    "en": {"eng", "en", "english"},
    "fra": {"fra", "fre", "fr", "french"},
    "fre": {"fra", "fre", "fr", "french"},
    "spa": {"spa", "es", "spanish", "espanol"},
    "ita": {"ita", "it", "italian"},
    "jpn": {"jpn", "ja", "japanese"},
    "zho": {"zho", "chi", "zh", "cmn", "chinese"},
    "chi": {"zho", "chi", "zh", "cmn", "chinese"},
    "rus": {"rus", "ru", "russian"},
}


@dataclass
class HotelCastConfig:
    """Persistent configuration model stored in `~/.config/hotelcast/config.json`."""

    tv_ip: str = ""
    tv_port: int = 52266
    tv_name: str = ""
    activation_url: str = ""
    last_directory: str = ""
    preferred_audio_lang: str = "eng"

    @property
    def tv_address(self) -> str:
        ip = self.tv_ip.strip()
        if not ip:
            return ""
        return f"{ip}:{self.tv_port}"

    def _normalize(self) -> None:
        self.tv_ip = str(self.tv_ip or "").strip()
        try:
            port = int(self.tv_port)
            self.tv_port = port if (1 <= port <= 65535) else 52266
        except (TypeError, ValueError):
            self.tv_port = 52266
        self.tv_name = str(self.tv_name or "").strip()
        self.activation_url = str(self.activation_url or "").strip()
        self.last_directory = str(self.last_directory or "").strip()
        self.preferred_audio_lang = str(self.preferred_audio_lang or "eng").strip().lower() or "eng"

    def save(self, path: str = CONFIG_PATH) -> None:
        """Persist configuration atomically to `~/.config/hotelcast/config.json`."""
        self._normalize()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp_path = f"{path}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(asdict(self), f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, path)
        except OSError:
            pass

    @classmethod
    def load(cls, path: str = CONFIG_PATH) -> "HotelCastConfig":
        """Load configuration from `~/.config/hotelcast/config.json`, creating defaults if missing."""
        cfg = cls()
        if not os.path.isfile(path):
            cfg.save(path)
            return cfg

        try:
            with open(path, "r", encoding="utf-8") as f:
                data: Dict[str, Any] = json.load(f)
            if isinstance(data, dict):
                if "tv_ip" in data and data["tv_ip"] is not None:
                    cfg.tv_ip = str(data["tv_ip"]).strip()
                if data.get("tv_port"):
                    cfg.tv_port = int(data["tv_port"])
                if "tv_name" in data and data["tv_name"] is not None:
                    cfg.tv_name = str(data["tv_name"]).strip()
                if "activation_url" in data and data["activation_url"] is not None:
                    cfg.activation_url = str(data["activation_url"]).strip()
                if "last_directory" in data and data["last_directory"] is not None:
                    cfg.last_directory = str(data["last_directory"]).strip()
                if data.get("preferred_audio_lang"):
                    cfg.preferred_audio_lang = str(data["preferred_audio_lang"]).strip().lower()
        except (OSError, ValueError, json.JSONDecodeError):
            pass

        cfg._normalize()
        return cfg


def matches_preferred_language(track_lang: str, preferred_lang: str) -> bool:
    """
    Return True if `track_lang` matches `preferred_lang`, accounting for ISO 639-1/2
    equivalence (e.g., `ger` == `deu` == `de`, `eng` == `en`).
    """
    t = (track_lang or "").strip().lower()
    p = (preferred_lang or "").strip().lower()
    if not t or not p:
        return False
    if t == p:
        return True
    synonyms = LANGUAGE_SYNONYMS.get(p, {p})
    return t in synonyms


def select_preferred_audio_index(tracks: List[Any], preferred_lang: str) -> int:
    """
    Return the `audio_index` of the first track matching `preferred_lang`, or `0` if none match.
    """
    for idx, track in enumerate(tracks):
        lang = getattr(track, "language", "") or ""
        title = getattr(track, "title", "") or ""
        if matches_preferred_language(lang, preferred_lang) or matches_preferred_language(
            title, preferred_lang
        ):
            return getattr(track, "audio_index", idx)
    return 0
