"""Local invoice-list import and validation. No external service calls."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import posixpath
import re
import unicodedata
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import PurePosixPath, PureWindowsPath
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET


MAX_UPLOAD_BYTES = 15 * 1024 * 1024
MAX_EXPANDED_BYTES = 64 * 1024 * 1024
MAX_ROWS = 5000
MAX_FIELD_LENGTH = 4000
FIELDS = ("id", "number", "date", "tin", "comment")
HEADERS = ("ID СФ", "Номер СФ", "Дата СФ", "ИНН клиента", "Комментарий")
LABELS = dict(zip(FIELDS, HEADERS))
STATUS_LABELS = {
    "ready": "Список проверен",
    "invalid": "Нужно исправить",
    "duplicate": "Повтор",
    "conflict": "Противоречие в списке",
}
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
REL_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
ID_PATTERN = re.compile(r"^[0-9a-fA-F]{32}$")
UUID_PATTERN = re.compile(r"^[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$")
URL_ID_PATTERN = re.compile(
    r"(?<![0-9a-fA-F])(?:[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}|[0-9a-fA-F]{32})(?![0-9a-fA-F])"
)


class ProcessingError(ValueError):
    def __init__(self, message: str, *, sheets: list[str] | None = None):
        super().__init__(message)
        self.sheets = sheets or []


def _header_key(value: str) -> str:
    return re.sub(r"[^a-zа-я0-9]", "", value.casefold().replace("ё", "е"))


_ALIASES = {
    "id": ("ID СФ", "ID счета-фактуры", "ID документа", "invoice id", "ид сф"),
    "number": ("Номер СФ", "Номер счета-фактуры", "№ СФ", "invoice number"),
    "date": ("Дата СФ", "Дата счета-фактуры", "invoice date"),
    "tin": ("ИНН клиента", "ИНН покупателя", "ИНН контрагента", "ИНН", "buyer tin"),
    "comment": ("Комментарий", "Примечание", "comment"),
}
HEADER_LOOKUP = {_header_key(alias): field for field, aliases in _ALIASES.items() for alias in aliases}


def normalize_id(value: str) -> str:
    candidate = value.strip()
    if "://" in candidate:
        parsed = urlsplit(candidate)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not (host == "didox.uz" or host.endswith(".didox.uz")):
            raise ValueError("В «ID СФ» допустим ID документа или HTTPS-ссылка на Didox.")
        found = {part.replace("-", "").lower() for part in URL_ID_PATTERN.findall(unquote(parsed.path + "?" + parsed.query + "#" + parsed.fragment))}
        if len(found) != 1:
            raise ValueError("В ссылке Didox должен быть один ID документа. Вставьте сам ID.")
        return found.pop()
    if UUID_PATTERN.fullmatch(candidate):
        candidate = candidate.replace("-", "")
    if not ID_PATTERN.fullmatch(candidate):
        raise ValueError("«ID СФ» должен содержать 32 шестнадцатеричных символа.")
    return candidate.lower()


def normalize_date(value: str) -> str:
    value = value.strip()
    for pattern, order in ((r"(\d{4})-(\d{2})-(\d{2})", "ymd"), (r"(\d{1,2})[./](\d{1,2})[./](\d{4})", "dmy")):
        match = re.fullmatch(pattern, value)
        if match:
            parts = list(map(int, match.groups()))
            year, month, day = parts if order == "ymd" else parts[::-1]
            try:
                return date(year, month, day).isoformat()
            except ValueError:
                break
    raise ValueError("«Дата СФ»: укажите существующую дату в формате ДД.ММ.ГГГГ.")


def _excel_date(value: str, date1904: bool) -> str:
    try:
        serial = Decimal(value)
        if not serial.is_finite() or serial != serial.to_integral_value():
            raise ValueError
        days = int(serial)
        if days < (0 if date1904 else 1) or (not date1904 and days == 60):
            raise ValueError
        base = date(1904, 1, 1) if date1904 else date(1899, 12, 31)
        return (base + timedelta(days=days if date1904 or days < 60 else days - 1)).isoformat()
    except (ValueError, InvalidOperation, OverflowError):
        raise ValueError("Не удалось прочитать «Дату СФ» из числовой ячейки Excel.") from None


def _number_text(value: str) -> str:
    try:
        number = Decimal(value)
        if number.is_finite() and number == number.to_integral_value():
            return str(int(number))
    except InvalidOperation:
        pass
    return value


def _xml(archive: zipfile.ZipFile, name: str) -> ET.Element:
    try:
        payload = archive.read(name)
        if b"<!DOCTYPE" in payload.upper() or b"<!ENTITY" in payload.upper():
            raise ProcessingError("В XML файла обнаружены неподдерживаемые объявления.")
        return ET.fromstring(payload)
    except (KeyError, ET.ParseError, RuntimeError, OSError) as exc:
        raise ProcessingError("Не удалось прочитать структуру XLSX. Пересохраните файл в Excel.") from exc


def _mapping(cells: dict[int, tuple[str, str, int]]) -> dict[str, int] | None:
    result: dict[str, int] = {}
    duplicates = set()
    for column, (value, _, _) in cells.items():
        field = HEADER_LOOKUP.get(_header_key(value))
        if field:
            if field in result:
                duplicates.add(field)
            result[field] = column
    if "id" not in result and not {"number", "date", "tin"}.issubset(result):
        return None
    if duplicates:
        raise ProcessingError("Повторяются заголовки: " + ", ".join(f"«{LABELS[field]}»" for field in sorted(duplicates)) + ".")
    return result


def _column_number(reference: str) -> int:
    match = re.fullmatch(r"([A-Z]{1,3})[1-9][0-9]*", reference)
    if not match:
        raise ProcessingError("Некорректный адрес ячейки XLSX.")
    value = 0
    for character in match.group(1):
        value = value * 26 + ord(character) - ord("A") + 1
    if value > 16384:
        raise ProcessingError("Некорректный номер столбца XLSX.")
    return value - 1


def _read_xlsx(data: bytes, requested_sheet: str | None) -> tuple[list[dict], dict]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise ProcessingError("Файл не является корректным XLSX.") from exc
    with archive:
        entries = archive.infolist()
        if len(entries) > 4096 or sum(item.file_size for item in entries) > MAX_EXPANDED_BYTES:
            raise ProcessingError("XLSX слишком велик после распаковки. Оставьте только список СФ.")
        if len({item.filename for item in entries}) != len(entries):
            raise ProcessingError("В структуре XLSX повторяются файлы. Пересохраните документ.")
        if any(item.flag_bits & 1 for item in entries):
            raise ProcessingError("Зашифрованные XLSX не поддерживаются.")
        workbook = _xml(archive, "xl/workbook.xml")
        properties = workbook.find("m:workbookPr", NS)
        date1904 = properties is not None and properties.get("date1904", "0") in ("1", "true")
        relations = _xml(archive, "xl/_rels/workbook.xml.rels")
        targets = {node.get("Id"): node.get("Target", "") for node in relations if node.get("TargetMode") != "External"}
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            shared = ["".join(item.itertext()) if not list(item) else "".join(text.text or "" for text in item.iterfind(".//m:t", NS)) for item in _xml(archive, "xl/sharedStrings.xml").findall("m:si", NS)]
        date_styles = set()
        if "xl/styles.xml" in archive.namelist():
            styles = _xml(archive, "xl/styles.xml")
            custom = {int(node.get("numFmtId", "0")): node.get("formatCode", "") for node in styles.findall("m:numFmts/m:numFmt", NS)}
            for index, node in enumerate(styles.findall("m:cellXfs/m:xf", NS)):
                format_id = int(node.get("numFmtId", "0"))
                code = re.sub(r'"[^\"]*"|\\.|\[[^\]]*\]', "", custom.get(format_id, "")).lower()
                if format_id in set(range(14, 23)) | {45, 46, 47} or re.search(r"[dmyhs]", code):
                    date_styles.add(index)
        candidates = []
        sheets = workbook.findall("m:sheets/m:sheet", NS)
        if len(sheets) > 50:
            raise ProcessingError("В XLSX больше 50 листов. Сохраните список СФ отдельным файлом.")
        cell_count = 0
        for sheet in sheets:
            sheet_name = sheet.get("name", "Лист")
            target = targets.get(sheet.get(REL_ID), "")
            member = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join("xl", target))
            if not member.startswith("xl/") or not member.endswith(".xml"):
                raise ProcessingError("Неподдерживаемая ссылка на лист XLSX.")
            root = _xml(archive, member)
            parsed_rows = []
            for fallback_number, row in enumerate(root.findall("m:sheetData/m:row", NS), 1):
                source_row = int(row.get("r", str(fallback_number)))
                if not 1 <= source_row <= 1048576:
                    raise ProcessingError("Некорректный номер строки XLSX.")
                cells = {}
                for cell in row.findall("m:c", NS):
                    cell_count += 1
                    if cell_count > 200000:
                        raise ProcessingError("В XLSX слишком много ячеек. Сохраните список СФ отдельным файлом.")
                    column = _column_number(cell.get("r", ""))
                    if column in cells:
                        raise ProcessingError("В строке XLSX повторяется адрес ячейки.")
                    kind = cell.get("t", "n")
                    style = int(cell.get("s", "0"))
                    formula = cell.find("m:f", NS)
                    if formula is not None:
                        value, kind = "=" + (formula.text or ""), "formula"
                    elif kind == "inlineStr":
                        value = "".join(text.text or "" for text in cell.iterfind(".//m:t", NS))
                    else:
                        value = cell.findtext("m:v", "", NS)
                        if kind == "s":
                            try:
                                shared_index = int(value)
                                if not 0 <= shared_index < len(shared):
                                    raise IndexError
                                value = shared[shared_index]
                            except (ValueError, IndexError) as exc:
                                raise ProcessingError("Некорректная строка в структуре XLSX.") from exc
                    cells[column] = (value, kind, style)
                parsed_rows.append((source_row, cells))
            header = None
            for position, (source_row, cells) in enumerate(parsed_rows[:20]):
                mapping = _mapping(cells)
                if mapping:
                    header = position, source_row, mapping
                    break
            if header is None:
                continue
            position, header_row, mapping = header
            rows = []
            for source_row, cells in parsed_rows[position + 1:]:
                values, source_issues = {}, {}
                for field in FIELDS:
                    value, kind, style = cells.get(mapping.get(field, -1), ("", "text", 0))
                    if kind == "formula":
                        source_issues[field] = f"«{LABELS[field]}» содержит формулу. Замените её значением."
                    elif kind == "e":
                        source_issues[field] = f"Ошибка Excel в поле «{LABELS[field]}»."
                    elif kind == "d" and field == "date" and value:
                        try:
                            parsed_date = datetime.fromisoformat(value.replace("Z", "+00:00"))
                            if any((parsed_date.hour, parsed_date.minute, parsed_date.second, parsed_date.microsecond)):
                                raise ValueError
                            value = parsed_date.date().isoformat()
                        except ValueError:
                            source_issues[field] = "«Дата СФ» должна содержать существующую дату без времени."
                    elif kind == "n" and value:
                        if field == "date":
                            try:
                                value = _excel_date(value, date1904)
                            except ValueError as exc:
                                source_issues[field] = str(exc)
                        else:
                            value = _number_text(value)
                            if field == "id":
                                source_issues[field] = "«ID СФ» сохранён числом. Вставьте исходный ID как текст."
                            elif field in ("number", "tin") and style in date_styles:
                                source_issues[field] = f"Excel преобразовал «{LABELS[field]}» в дату. Вставьте исходное значение как текст."
                    values[field] = value
                if any(values.values()):
                    rows.append({"source_row": source_row, "values": values, "source_issues": source_issues})
                if len(rows) > MAX_ROWS:
                    raise ProcessingError(f"В одном списке допустимо не более {MAX_ROWS} СФ.")
            candidates.append({"name": sheet_name, "rows": rows, "header_row": header_row})
        if not candidates:
            raise ProcessingError("Не найдены заголовки списка. Нужен «ID СФ» или вместе «Номер СФ», «Дата СФ», «ИНН клиента» в первых 20 строках листа.")
        names = [candidate["name"] for candidate in candidates]
        if requested_sheet:
            chosen = next((candidate for candidate in candidates if candidate["name"] == requested_sheet), None)
            if chosen is None:
                raise ProcessingError("Указанный лист не найден среди листов со списком СФ.", sheets=names)
        else:
            chosen = next((candidate for candidate in candidates if candidate["name"] == "Список СФ"), None)
            if chosen is None and len(candidates) != 1:
                raise ProcessingError("В файле несколько списков. Выберите лист для обработки.", sheets=names)
            chosen = chosen or candidates[0]
        return chosen["rows"], {"sheet": chosen["name"], "sheet_names": names, "header_row": chosen["header_row"], "format": "xlsx"}


def _read_csv(data: bytes) -> tuple[list[dict], dict]:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = data.decode("cp1251")
        except UnicodeDecodeError as exc:
            raise ProcessingError("Не удалось определить кодировку CSV. Сохраните его в UTF-8.") from exc
    try:
        delimiter = csv.Sniffer().sniff(text[:8192], delimiters=";,\t").delimiter
    except csv.Error:
        delimiter = ";"
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    header = None
    rows = []
    try:
        for line_number, values in enumerate(reader, 1):
            if header is None:
                if line_number > 20:
                    break
                header = _mapping({index: (value, "text", 0) for index, value in enumerate(values)})
                if header:
                    header_row = line_number
                continue
            record = {field: values[index] if (index := header.get(field, -1)) >= 0 and index < len(values) else "" for field in FIELDS}
            if any(record.values()):
                rows.append({"source_row": line_number, "values": record, "source_issues": {}})
            if len(rows) > MAX_ROWS:
                raise ProcessingError(f"В одном списке допустимо не более {MAX_ROWS} СФ.")
    except csv.Error as exc:
        raise ProcessingError("Не удалось прочитать строки CSV.") from exc
    if header is None:
        raise ProcessingError("Не найдены заголовки списка СФ. Используйте шаблон Excel.")
    return rows, {"sheet": "CSV", "sheet_names": ["CSV"], "header_row": header_row, "format": "csv"}


def validate_rows(rows: list[dict]) -> dict:
    if not isinstance(rows, list) or len(rows) > MAX_ROWS:
        raise ProcessingError(f"Ожидается список не более {MAX_ROWS} строк.")
    result = []
    source_rows = set()
    for index, raw in enumerate(rows):
        if not isinstance(raw, dict) or not isinstance(raw.get("values"), dict):
            raise ProcessingError("Некорректный формат строки списка.")
        values = {}
        for field in FIELDS:
            value = raw["values"].get(field, "")
            if value is None:
                value = ""
            if not isinstance(value, str) or len(value) > MAX_FIELD_LENGTH:
                raise ProcessingError(f"В строке {index + 1} слишком длинное или некорректное поле «{LABELS[field]}».")
            values[field] = value.strip()
        if not any(values.values()):
            continue
        source_row = raw.get("source_row", index + 2)
        if not isinstance(source_row, int) or isinstance(source_row, bool) or not 1 <= source_row <= 1048576:
            raise ProcessingError("Некорректный номер исходной строки.")
        if source_row in source_rows:
            raise ProcessingError("В списке повторяется номер исходной строки.")
        source_rows.add(source_row)
        source_issues = raw.get("source_issues", {})
        if not isinstance(source_issues, dict):
            raise ProcessingError("Некорректные сведения о ячейках.")
        issues = {field: str(message)[:500] for field, message in source_issues.items() if field in FIELDS}
        for field, value in values.items():
            if value.startswith("="):
                issues[field] = f"«{LABELS[field]}» содержит формулу. Замените её значением."
        normalized = dict(values)
        if values["id"] and "id" not in issues:
            try:
                normalized["id"] = normalize_id(values["id"])
            except ValueError as exc:
                issues["id"] = str(exc)
        if values["date"] and "date" not in issues:
            try:
                normalized["date"] = normalize_date(values["date"])
            except ValueError as exc:
                issues["date"] = str(exc)
        if values["tin"]:
            normalized["tin"] = re.sub(r"\s", "", values["tin"])
            if not re.fullmatch(r"(?:[0-9]{9}|[0-9]{14})", normalized["tin"]):
                issues["tin"] = "«ИНН клиента» должен содержать 9 цифр; для ПИНФЛ допустимы 14 цифр."
        if len(values["number"]) > 120 or re.search(r"[\x00-\x1f]", values["number"]):
            issues["number"] = "Проверьте «Номер СФ»: максимум 120 символов, без переносов строк."
        if not values["id"]:
            for field in ("number", "date", "tin"):
                if not values[field]:
                    issues[field] = f"Без «ID СФ» обязательно поле «{LABELS[field]}»."
        result.append({
            "record_id": f"row-{source_row}", "source_row": source_row,
            "values": values, "normalized": normalized,
            "source_issues": source_issues, "field_errors": issues,
            "errors": list(issues.values()), "status": "invalid" if issues else "ready",
            "duplicate_of": None, "ttn_created": False,
        })

    parent = list(range(len(result)))

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    keys = {}
    for index, row in enumerate(result):
        if row["status"] != "ready":
            continue
        value = row["normalized"]
        row_keys = [("id", value["id"])] if value["id"] else []
        if all(value[field] for field in ("number", "date", "tin")):
            row_keys.append(("invoice", unicodedata.normalize("NFKC", value["number"]).casefold(), value["date"], value["tin"]))
        for key in row_keys:
            if key in keys:
                parent[find(index)] = find(keys[key])
            else:
                keys[key] = index
    groups = defaultdict(list)
    for index, row in enumerate(result):
        if row["status"] == "ready":
            groups[find(index)].append(row)
    for group in groups.values():
        if len(group) < 2:
            continue
        conflicts = []
        for field in ("id", "number", "date", "tin"):
            distinct = {unicodedata.normalize("NFKC", row["normalized"][field]).casefold() for row in group if row["normalized"][field]}
            if len(distinct) > 1:
                conflicts.append(f"«{LABELS[field]}»")
        if conflicts:
            message = "Связанные строки содержат разные значения: " + ", ".join(conflicts) + ". Проверьте список."
            for row in group:
                row["status"] = "conflict"
                row["errors"].append(message)
        else:
            representative = min(group, key=lambda row: (not bool(row["normalized"]["id"]), row["source_row"]))
            for row in group:
                if row is not representative:
                    row["status"] = "duplicate"
                    row["duplicate_of"] = representative["source_row"]
                    row["errors"].append(f"Повтор СФ из строки «{representative['source_row']}». В обработку попадёт одна запись.")
    summary = {status: sum(row["status"] == status for row in result) for status in STATUS_LABELS}
    summary["total"] = len(result)
    return {"rows": result, "summary": summary, "ttn_created": 0}


def import_invoices(data: bytes, filename: str, sheet_name: str | None = None) -> dict:
    if not data:
        raise ProcessingError("Выбран пустой файл.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ProcessingError("Максимальный размер файла — 15 МБ.")
    name = PureWindowsPath(PurePosixPath(filename).name).name
    suffix = PurePosixPath(name).suffix.lower()
    if suffix == ".xlsx":
        try:
            rows, metadata = _read_xlsx(data, sheet_name)
        except (ValueError, OverflowError, zipfile.BadZipFile) as exc:
            if isinstance(exc, ProcessingError):
                raise
            raise ProcessingError("Повреждённый или неподдерживаемый XLSX. Пересохраните файл.") from exc
    elif suffix == ".csv":
        rows, metadata = _read_csv(data)
    else:
        raise ProcessingError("Поддерживаются XLSX и CSV. Старый XLS сохраните в формате XLSX.")
    payload = validate_rows(rows)
    payload["source"] = {"file_name": name, "sha256": hashlib.sha256(data).hexdigest(), **metadata}
    return payload


def _csv_value(value) -> str:
    text = "" if value is None else str(value)
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text


def export_csv(rows: list[dict], *, report: bool, selected_ids: list[str] | None = None) -> bytes:
    checked = validate_rows(rows)
    selected = set(selected_ids or [])
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, delimiter=";", lineterminator="\r\n")
    if report:
        writer.writerow(("Строка Excel", "Результат", *HEADERS, "Пояснение", "ТТН"))
        for row in checked["rows"]:
            writer.writerow([_csv_value(value) for value in (
                row["source_row"], STATUS_LABELS[row["status"]],
                *(row["values"][field] for field in FIELDS), " ".join(row["errors"]), "Не создавалась",
            )])
    else:
        eligible = [row for row in checked["rows"] if row["status"] == "ready" and row["record_id"] in selected]
        if not eligible:
            raise ProcessingError("Выберите хотя бы одну проверенную СФ.")
        writer.writerow(HEADERS)
        for row in eligible:
            writer.writerow([_csv_value(row["normalized"][field]) for field in FIELDS])
    return stream.getvalue().encode("utf-8-sig")


def export_selected_xlsx(rows: list[dict], selected_ids: list[str], template: bytes) -> bytes:
    """Write a separate selected-list copy using the shipped blank workbook."""
    checked = validate_rows(rows)
    selected = set(selected_ids)
    eligible = [row for row in checked["rows"] if row["status"] == "ready" and row["record_id"] in selected]
    if not eligible:
        raise ProcessingError("Выберите хотя бы одну проверенную СФ.")
    namespace = NS["m"]
    tag = lambda name: "{" + namespace + "}" + name
    ET.register_namespace("", namespace)
    ET.register_namespace("r", "http://schemas.openxmlformats.org/officeDocument/2006/relationships")
    with zipfile.ZipFile(io.BytesIO(template)) as archive:
        workbook = _xml(archive, "xl/workbook.xml")
        properties = workbook.find("m:workbookPr", NS)
        date1904 = properties is not None and properties.get("date1904", "0") in ("1", "true")
        sheet = workbook.find("m:sheets/m:sheet", NS)
        if sheet is None:
            raise ProcessingError("В шаблоне нет листа списка СФ.")
        relationships = _xml(archive, "xl/_rels/workbook.xml.rels")
        target = next((node.get("Target", "") for node in relationships if node.get("Id") == sheet.get(REL_ID)), "")
        member = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join("xl", target))
        root = _xml(archive, member)
        sheet_data = root.find("m:sheetData", NS)
        if sheet_data is None:
            raise ProcessingError("В шаблоне отсутствует область данных.")
        existing = {int(row.get("r")): row for row in sheet_data.findall("m:row", NS)}
        formats = {}
        for cell in existing.get(2, ET.Element(tag("row"))).findall("m:c", NS):
            formats[_column_number(cell.get("r", ""))] = cell.get("s")
        for row_number, row in existing.items():
            if row_number > 1:
                for cell in list(row):
                    if cell.tag == tag("c") and _column_number(cell.get("r", "")) < 5:
                        row.remove(cell)
        for output_number, record in enumerate(eligible, 2):
            row = existing.get(output_number)
            if row is None:
                row = ET.SubElement(sheet_data, tag("row"), {"r": str(output_number), "ht": "23", "customHeight": "1"})
            for column, field in enumerate(FIELDS):
                attributes = {"r": f"{chr(ord('A') + column)}{output_number}"}
                if formats.get(column) is not None:
                    attributes["s"] = formats[column]
                value = record["normalized"][field]
                cell = ET.Element(tag("c"), attributes)
                numeric_date = False
                if field == "date" and value:
                    day = date.fromisoformat(value)
                    base = date(1904, 1, 1) if date1904 else date(1899, 12, 31)
                    serial = (day - base).days
                    if not date1904 and day >= date(1900, 3, 1):
                        serial += 1
                    if serial >= (0 if date1904 else 1):
                        ET.SubElement(cell, tag("v")).text = str(serial)
                        numeric_date = True
                if not numeric_date:
                    cell.set("t", "inlineStr")
                    text = ET.SubElement(ET.SubElement(cell, tag("is")), tag("t"))
                    text.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
                    text.text = value
                row.insert(column, cell)
        dimension = root.find("m:dimension", NS)
        if dimension is not None:
            dimension.set("ref", f"A1:G{max(101, len(eligible) + 1)}")
        replacements = {member: ET.tostring(root, encoding="utf-8", xml_declaration=True)}
        for name in archive.namelist():
            if name.startswith("xl/tables/") and name.endswith(".xml"):
                table = _xml(archive, name)
                if table.get("name") == "InvoicesToProcess":
                    reference = f"A1:E{len(eligible) + 1}"
                    table.set("ref", reference)
                    auto_filter = table.find("m:autoFilter", NS)
                    if auto_filter is not None:
                        auto_filter.set("ref", reference)
                    replacements[name] = ET.tostring(table, encoding="utf-8", xml_declaration=True)
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as destination:
            for item in archive.infolist():
                destination.writestr(item.filename, replacements.get(item.filename, archive.read(item.filename)))
        return output.getvalue()
