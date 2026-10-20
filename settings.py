"""Local deployment settings; does not implement remote authentication."""

from __future__ import annotations

import json
from pathlib import Path
import re
import threading
from urllib.parse import urlsplit


DEFAULT_SETTINGS = {
    "schema_version": 2,
    "didox_url": "https://didox.uz/",
    "smartup_url": "",
    "company_tin": "",
    "loading_address": "",
    "didox_api_url": "https://api-partners.didox.uz",
    "smartup_api_url": "https://smartup.online",
    "smartup_project_code": "trade",
    "smartup_filial_id": "",
    "smartup_filial_code": "",
    "loading_point": {},
    "drivers": [],
    "destinations": {},
    "distances": {},
}
_LOCK = threading.RLock()


class SettingsError(ValueError):
    pass


def validate_settings(value):
    if not isinstance(value, dict) or set(value) - set(DEFAULT_SETTINGS):
        raise SettingsError("Настройки содержат неизвестные поля. Используйте поля из окна настройки.")
    if type(value.get("schema_version", 1)) is not int or value.get("schema_version", 1) not in (1, 2):
        raise SettingsError("Неподдерживаемая версия настроек.")
    result = json.loads(json.dumps(DEFAULT_SETTINGS))
    for field, label, maximum in (
        ("didox_url", "Ссылка Didox", 2000), ("smartup_url", "Ссылка Smartup", 2000),
        ("company_tin", "ИНН организации", 14), ("loading_address", "Адрес погрузки", 500),
        ("didox_api_url", "API Didox", 2000), ("smartup_api_url", "API Smartup", 2000),
        ("smartup_project_code", "project_code", 100), ("smartup_filial_id", "filial_id", 100),
        ("smartup_filial_code", "filial_code", 100),
    ):
        item = value.get(field, result[field])
        if not isinstance(item, str) or len(item) > maximum or re.search(r"[\x00-\x1f\x7f]", item):
            raise SettingsError(f"Проверьте поле «{label}».")
        result[field] = item.strip()
    for field, label in (("didox_url", "Ссылка Didox"), ("smartup_url", "Ссылка Smartup"),
                         ("didox_api_url", "API Didox"), ("smartup_api_url", "API Smartup")):
        text = result[field]
        if not text:
            if field != "smartup_url":
                raise SettingsError(f"Заполните «{label}».")
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
    for name, expected in (("loading_point", dict), ("drivers", list), ("destinations", dict), ("distances", dict)):
        item = value.get(name, result[name])
        if not isinstance(item, expected) or len(item) > 2000:
            raise SettingsError(f"Проверьте справочник «{name}».")
        result[name] = item
    try:
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise SettingsError("Некорректный JSON справочников.") from exc
    if len(encoded.encode("utf-8")) > 2 * 1024 * 1024:
        raise SettingsError("Настройки и справочники должны занимать до 2 МБ.")
    for item in result["drivers"]:
        if not isinstance(item, dict) or set(item) - {"van_code", "vehicle_number", "vehicle_model", "driver_name", "driver_pinfl"}:
            raise SettingsError("У водителя разрешены «van_code», «vehicle_number», «vehicle_model», «driver_name», «driver_pinfl».")
        if any(not isinstance(text, str) or len(text) > 300 for text in item.values()):
            raise SettingsError("Реквизиты водителя должны быть текстом до 300 символов.")
        if item.get("driver_pinfl") and not re.fullmatch(r"[0-9]{14}", item["driver_pinfl"]):
            raise SettingsError("«driver_pinfl» должен содержать 14 цифр.")
    return result


def load_settings(path: Path):
    with _LOCK:
        if not path.exists():
            return validate_settings({})
        try:
            if path.stat().st_size > 2 * 1024 * 1024:
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
