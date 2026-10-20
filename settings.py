"""Local deployment settings; does not implement remote authentication."""

from __future__ import annotations

import json
from pathlib import Path
import re
import threading
from urllib.parse import urlsplit


DEFAULT_SETTINGS = {
    "schema_version": 1,
    "didox_url": "https://didox.uz/",
    "smartup_url": "",
    "company_tin": "",
    "loading_address": "",
}
_LOCK = threading.RLock()


class SettingsError(ValueError):
    pass


def validate_settings(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULT_SETTINGS):
        raise SettingsError("Настройки содержат неизвестные поля. Используйте поля из окна настройки.")
    if type(value.get("schema_version", 1)) is not int or value.get("schema_version", 1) != 1:
        raise SettingsError("Неподдерживаемая версия настроек.")
    result = dict(DEFAULT_SETTINGS)
    for field, label, maximum in (
        ("didox_url", "Ссылка Didox", 2000), ("smartup_url", "Ссылка Smartup", 2000),
        ("company_tin", "ИНН организации", 14), ("loading_address", "Адрес погрузки", 500),
    ):
        item = value.get(field, result[field])
        if not isinstance(item, str) or len(item) > maximum or re.search(r"[\x00-\x1f\x7f]", item):
            raise SettingsError(f"Проверьте поле «{label}».")
        result[field] = item.strip()
    for field, label in (("didox_url", "Ссылка Didox"), ("smartup_url", "Ссылка Smartup")):
        text = result[field]
        if not text:
            if field == "didox_url":
                raise SettingsError("Заполните «Ссылку Didox».")
            continue
        try:
            url = urlsplit(text)
            port = url.port
            if (url.scheme not in ("http", "https") or not url.hostname or url.username is not None
                    or url.password is not None or url.query or url.fragment
                    or re.search(r"[\s\\]", text) or port == 0):
                raise ValueError
        except ValueError as exc:
            raise SettingsError(f"«{label}»: укажите адрес HTTP/HTTPS без логина, пароля и параметров запроса.") from exc
    if result["company_tin"] and not re.fullmatch(r"[0-9]{9}", result["company_tin"]):
        raise SettingsError("«ИНН организации» должен содержать 9 цифр.")
    return result


def load_settings(path: Path):
    with _LOCK:
        if not path.exists():
            return dict(DEFAULT_SETTINGS)
        try:
            if path.stat().st_size > 16384:
                raise SettingsError("Файл «config.local.json» слишком большой.")
            return validate_settings(json.loads(path.read_text(encoding="utf-8-sig")))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise SettingsError("Не удалось прочитать «config.local.json». Исправьте настройки и сохраните их заново.") from exc


def save_settings(path: Path, value):
    settings = validate_settings(value)
    temporary = path.with_name(path.name + ".tmp")
    with _LOCK:
        try:
            temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(path)
        except OSError as exc:
            raise SettingsError("Не удалось сохранить настройки. Распакуйте программу в папку с правом записи.") from exc
    return settings
