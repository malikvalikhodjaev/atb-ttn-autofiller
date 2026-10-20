"""Invoice to waybill preparation, durable per-invoice state, and explicit writes."""

from __future__ import annotations

import base64
from contextlib import contextmanager
import csv
import io
from datetime import date, timedelta
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
import unicodedata
import uuid
from urllib.parse import urlsplit

from connectors import DidoxClient, SmartupClient, IntegrationError, field, decimal_value, document_data, json_bytes
from invoice_processor import validate_rows
from settings import load_settings, save_settings


def text(value):
    return "" if value is None else str(value).strip()


def normalized(value):
    return re.sub(r"[^\w]", "", unicodedata.normalize("NFKC", text(value)).casefold())


def point(value, fallback_address=""):
    value = value if isinstance(value, dict) else {}
    return {"regionId": field(value, "regionId"), "regionName": field(value, "regionName", ""),
        "districtCode": field(value, "districtCode"), "districtName": field(value, "districtName", ""),
        "mahallaId": field(value, "mahallaId"), "mahallaName": field(value, "mahallaName"),
        "address": field(value, "address", fallback_address), "longitude": field(value, "longitude"), "latitude": field(value, "latitude")}


def invoice_summary(invoice_id, response, settings, expected):
    invoice, metadata = document_data(response)
    doc = field(invoice, "FacturaDoc", {})
    products = field(field(invoice, "ProductList", {}), "Products", [])
    if not isinstance(products, list) or not products or not field(doc, "FacturaNo"):
        raise IntegrationError("Документ не является поддерживаемой товарной СФ.")
    company = text(field(invoice, "SellerTin"))
    if company != settings["company_tin"]:
        raise IntegrationError("Поставщик СФ не совпадает с «ИНН организации».")
    summary = {"id": invoice_id, "number": text(field(doc, "FacturaNo")), "date": text(field(doc, "FacturaDate"))[:10],
        "tin": text(field(invoice, "BuyerTin")), "invoice": invoice, "metadata": metadata}
    if not re.fullmatch(r"(?:[0-9]{9}|[0-9]{14})", summary["tin"]):
        raise IntegrationError("В СФ нет корректного ИНН/ПИНФЛ покупателя.")
    try:
        date.fromisoformat(summary["date"])
    except ValueError:
        raise IntegrationError("В СФ отсутствует корректная дата.") from None
    for key in ("number", "date", "tin"):
        if expected.get(key) and expected[key] != summary[key]:
            raise IntegrationError(f"Реквизит «{key}» в найденной СФ отличается от загруженного списка.")
    summary["total_without_vat"] = sum((decimal_value(field(item, "DeliverySum"), "Стоимость поставки") for item in products), Decimal(0))
    summary["total_with_vat"] = sum((decimal_value(field(item, "DeliverySumWithVat"), "Стоимость с НДС") for item in products), Decimal(0))
    if field(invoice, "HasCommittent") or field(field(invoice, "ProductList", {}), "HasCommittent"):
        raise IntegrationError("СФ с комитентом требует отдельной проверки; автоматический перенос не выполняется.")
    linked = field(invoice, "WaybillLocalIds", []) or field(field(invoice, "ProductList", {}), "WaybillLocalIds", [])
    related = field(field(response, "data", response), "relatedDocuments", [])
    if linked or any(text(field(item, "docType", field(item, "type"))) in ("041", "41") for item in related if isinstance(item, dict)):
        raise IntegrationError("У СФ уже указана связанная ТТН. Проверьте существующие документы перед созданием новой.")
    return summary


def select_driver(order, settings):
    van = text(field(order, "van_code"))
    name = text(field(order, "expeditor_name"))
    candidates = [item for item in settings["drivers"] if van and normalized(van) in (
        normalized(item.get("van_code")), normalized(item.get("vehicle_number")))]
    exact = [item for item in candidates if normalized(item.get("driver_name")) == normalized(name)]
    driver = exact[0] if len(exact) == 1 else candidates[0] if len(candidates) == 1 else {}
    return {"vehicle_number": driver.get("vehicle_number", van), "vehicle_model": driver.get("vehicle_model", ""),
        "driver_name": name, "driver_pinfl": driver.get("driver_pinfl", "")}, (
        "ФИО Smartup отличается от справочника водителей." if driver and normalized(driver.get("driver_name")) != normalized(name) else
        "В справочнике несколько водителей этой машины; выберите правильного." if len(candidates) > 1 and not exact else "")


def build_waybill(summary, order, settings):
    invoice = summary["invoice"]
    seller, buyer = field(invoice, "Seller", {}), field(invoice, "Buyer", {})
    consignor = {"TinOrPinfl": settings["company_tin"], "Name": text(field(seller, "Name")),
                 "BranchCode": text(field(seller, "BranchCode")), "BranchName": text(field(seller, "BranchName"))}
    consignee = {"TinOrPinfl": summary["tin"], "Name": text(field(buyer, "Name")),
                 "BranchCode": text(field(buyer, "BranchCode")), "BranchName": text(field(buyer, "BranchName"))}
    driver, driver_warning = select_driver(order, settings)
    loading = point(settings["loading_point"], settings["loading_address"])
    destination = settings["destinations"].get(summary["tin"], field(buyer, "DeliveryPoint", buyer))
    unloading = point(destination, text(field(buyer, "Address")))
    distance_key = f"{loading['regionId']}:{loading['districtCode']}:{loading['address']}|{unloading['regionId']}:{unloading['districtCode']}"
    distance = settings["distances"].get(distance_key, "")
    products = []
    source_products = field(field(invoice, "ProductList", {}), "Products", [])
    for index, item in enumerate(source_products, 1):
        gross, net = field(item, "WeightBrutto"), field(item, "WeightNetto")
        if len(source_products) == 1:
            if gross in (None, ""):
                gross = field(order, "total_weight_brutto")
            if net in (None, ""):
                net = field(order, "total_weight_netto")
        products.append({"ordNo": index, "CommittentTinOrPinfl": "", "CommittentName": "",
            "CatalogCode": text(field(item, "CatalogCode")), "CatalogName": text(field(item, "CatalogName")),
            "ProductName": text(field(item, "Name")), "PackageCode": text(field(item, "PackageCode")),
            "PackageName": text(field(item, "PackageName")), "Amount": text(field(item, "Count")),
            "Price": text(field(item, "Summa")), "DeliverySum": text(field(item, "DeliverySum")),
            "WeightBrutto": None if gross in (None, "") else text(gross), "WeightNetto": None if net in (None, "") else text(net)})
    gross_values = [item["WeightBrutto"] for item in products]
    total_gross = str(sum((decimal_value(value, "Масса брутто") for value in gross_values), Decimal(0))) if all(value is not None for value in gross_values) else None
    payload = {"WaybillLocalType": 0, "DeliveryType": 2,
        "WaybillDoc": {"WaybillNo": summary["number"], "WaybillDate": summary["date"]},
        "ContractDoc": {"ContractNo": text(field(field(invoice, "ContractDoc", {}), "ContractNo")),
                        "ContractDate": text(field(field(invoice, "ContractDoc", {}), "ContractDate"))[:10]},
        "OldWaybillDoc": None, "HasCommittent": False, "SingleSidedType": 0,
        "Consignor": consignor, "Consignee": consignee, "FreightForwarder": dict(consignor), "Carrier": dict(consignor),
        "Client": None, "Payer": None, "TransportType": 1,
        "Roadway": {"OtherCarOwners": None, "Truck": {"RegNo": driver["vehicle_number"], "Model": driver["vehicle_model"]},
            "Trailer": None, "Carriages": [], "Driver": {"pinfl": driver["driver_pinfl"], "FullName": driver["driver_name"]},
            "ProductGroups": [{"LoadingPoint": loading, "LoadingTrustee": None, "UnloadingPoint": unloading,
                "UnloadingTrustee": None, "UnloadingEmpowerment": None,
                "ProductInfo": {"TotalDeliverySum": str(summary["total_without_vat"]), "TotalWeightBrutto": total_gross, "products": products}}]},
        "Railway": None, "ResponsiblePerson": {"Pinfl": driver["driver_pinfl"], "FullName": driver["driver_name"]},
        "ResponsibleCompany": None, "TotalDistance": text(distance), "DeliveryCost": "0", "TotalDeliveryCost": "0"}
    warnings = ["Проверьте официальный способ привязки ТТН к существующей СФ: текущая связь сохраняется в локальном журнале.",
                "Проверьте единицы массы; автоматического преобразования веса нет."]
    if driver_warning:
        warnings.append(driver_warning)
    return payload, warnings


def validate_waybill(payload, summary):
    errors = []
    if not isinstance(payload, dict):
        return ["JSON ТТН должен быть объектом."]
    def required(value, label):
        if value in (None, ""):
            errors.append(f"Заполните «{label}».")
    def number(value, label, *, positive=False):
        try:
            amount = decimal_value(value, label)
            if amount < 0 or positive and amount == 0:
                raise IntegrationError(f"«{label}» должен быть {'положительным' if positive else 'неотрицательным'}.")
            return amount
        except IntegrationError as exc:
            errors.append(str(exc))
            return None
    for part, expected_tin in (("Consignor", field(summary["invoice"], "SellerTin")), ("Consignee", summary["tin"]),
                               ("FreightForwarder", field(summary["invoice"], "SellerTin")), ("Carrier", field(summary["invoice"], "SellerTin"))):
        block = field(payload, part, {})
        if text(field(block, "TinOrPinfl")) != text(expected_tin):
            errors.append(f"«{part}.TinOrPinfl» должен совпадать с исходной СФ.")
        required(field(block, "Name"), part + ".Name")
        source_name = text(field(field(summary["invoice"], "Buyer" if part == "Consignee" else "Seller", {}), "Name"))
        if source_name and text(field(block, "Name")) != source_name:
            errors.append(f"«{part}.Name» должен совпадать с СФ.")
    doc = field(payload, "WaybillDoc", {})
    if text(field(doc, "WaybillNo")) != summary["number"] or text(field(doc, "WaybillDate")) != summary["date"]:
        errors.append("Номер и дата ТТН должны совпадать с выбранной СФ.")
    contract = field(payload, "ContractDoc", {})
    required(field(contract, "ContractNo"), "Номер договора")
    try:
        date.fromisoformat(text(field(contract, "ContractDate")))
    except ValueError:
        errors.append("Заполните существующую «Дату договора».")
    source_contract = field(summary["invoice"], "ContractDoc", {})
    for key in ("ContractNo", "ContractDate"):
        source = text(field(source_contract, key))
        expected = source[:10] if key == "ContractDate" else source
        if source and text(field(contract, key)) != expected:
            errors.append(f"«{key}» должен совпадать с СФ.")
    road = field(payload, "Roadway", {})
    for key in ("RegNo", "Model"):
        required(field(field(road, "Truck", {}), key), "Truck." + key)
    driver = field(road, "Driver", {})
    pinfl = text(field(driver, "pinfl"))
    if not re.fullmatch(r"[0-9]{14}", pinfl):
        errors.append("ПИНФЛ водителя должен содержать 14 цифр.")
    required(field(driver, "FullName"), "ФИО водителя")
    responsible = field(payload, "ResponsiblePerson", {})
    if text(field(responsible, "Pinfl")) != pinfl or text(field(responsible, "FullName")) != text(field(driver, "FullName")):
        errors.append("Ответственное лицо должно совпадать с водителем.")
    groups = field(road, "ProductGroups", [])
    if not isinstance(groups, list) or len(groups) != 1 or not isinstance(groups[0], dict):
        return errors + ["Для одной СФ нужна одна группа груза."]
    group = groups[0]
    for point_key in ("LoadingPoint", "UnloadingPoint"):
        address = field(group, point_key, {})
        for key in ("regionId", "regionName", "districtCode", "districtName", "address"):
            required(field(address, key), point_key + "." + key)
        for key in ("regionId", "districtCode"):
            try:
                if int(field(address, key)) <= 0:
                    raise ValueError
            except (ValueError, TypeError):
                errors.append(f"«{point_key}.{key}» должен быть положительным целым кодом из справочника.")
    info = field(group, "ProductInfo", {})
    products = field(info, "products", [])
    originals = field(field(summary["invoice"], "ProductList", {}), "Products", [])
    if not isinstance(products, list) or len(products) != len(originals):
        return errors + ["Количество товарных строк отличается от СФ."]
    total = Decimal(0)
    gross_total = Decimal(0)
    for index, (item, original) in enumerate(zip(products, originals), 1):
        if not isinstance(item, dict):
            errors.append(f"Товар «{index}» должен быть объектом.")
            continue
        for key in ("ProductName", "CatalogCode", "CatalogName", "PackageCode", "PackageName"):
            required(field(item, key), f"Товар {index}: {key}")
        for dest, source in (("CatalogCode", "CatalogCode"), ("ProductName", "Name")):
            if text(field(item, dest)) != text(field(original, source)):
                errors.append(f"Товар «{index}»: «{dest}» должен совпадать с СФ.")
        for dest, source in (("Amount", "Count"), ("Price", "Summa"), ("DeliverySum", "DeliverySum")):
            actual = number(field(item, dest), f"Товар {index}: {dest}")
            if actual is not None and actual != decimal_value(field(original, source), source):
                errors.append(f"Товар «{index}»: «{dest}» отличается от СФ.")
            if dest == "DeliverySum" and actual is not None:
                total += actual
        gross = number(field(item, "WeightBrutto"), f"Товар {index}: брутто", positive=True)
        net = number(field(item, "WeightNetto"), f"Товар {index}: нетто", positive=True)
        if gross is not None:
            gross_total += gross
        if gross is not None and net is not None and net > gross:
            errors.append(f"Товар «{index}»: нетто больше брутто.")
    stated = number(field(info, "TotalDeliverySum"), "Общая стоимость груза")
    if stated != summary["total_without_vat"] or total != summary["total_without_vat"]:
        errors.append("Стоимость груза без НДС должна точно совпадать со СФ.")
    stated_gross = number(field(info, "TotalWeightBrutto"), "Общая масса брутто", positive=True)
    if stated_gross is not None and stated_gross != gross_total:
        errors.append("Общая масса брутто не совпадает с суммой товарных строк.")
    number(field(payload, "TotalDistance"), "Расстояние", positive=True)
    for key in ("DeliveryCost", "TotalDeliveryCost"):
        if number(field(payload, key), key) != Decimal(0):
            errors.append(f"«{key}» должен быть равен нулю.")
    if (field(payload, "TransportType") != 1 or field(payload, "HasCommittent") is not False or field(payload, "SingleSidedType") != 0
            or field(payload, "WaybillLocalType") != 0 or field(payload, "DeliveryType") != 2):
        errors.append("Поддерживается автомобильная двусторонняя ТТН без комитента.")
    return list(dict.fromkeys(errors))


class Ledger:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def connection(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, company TEXT NOT NULL, invoice_id TEXT NOT NULL, status TEXT NOT NULL, waybill_id TEXT, details TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', updated REAL NOT NULL, UNIQUE(company,invoice_id))")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def get(self, company, *, job_id=None, invoice_id=None):
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE company=? AND " + ("id=?" if job_id else "invoice_id=?"),
                                     (company, job_id or invoice_id)).fetchone()
        return dict(row) if row else None

    def save(self, company, invoice_id, details):
        existing = self.get(company, invoice_id=invoice_id)
        if existing and existing["status"] not in ("ready", "needs_review"):
            return existing
        identity = existing["id"] if existing else uuid.uuid4().hex
        status = "needs_review" if details["errors"] else "ready"
        with self.connection() as connection:
            connection.execute("INSERT INTO jobs(id,company,invoice_id,status,details,updated) VALUES(?,?,?,?,?,?) ON CONFLICT(company,invoice_id) DO UPDATE SET status=excluded.status,details=excluded.details,error='',updated=excluded.updated WHERE jobs.status IN ('ready','needs_review')",
                               (identity, company, invoice_id, status, json_bytes(details).decode("utf-8"), time.time()))
        return self.get(company, job_id=identity)

    def update(self, identity, *, status, error="", waybill_id=None):
        with self.connection() as connection:
            connection.execute("UPDATE jobs SET status=?,error=?,waybill_id=COALESCE(?,waybill_id),updated=? WHERE id=?", (status, error, waybill_id, time.time(), identity))

    def claim(self, identity, before, after):
        with self.connection() as connection:
            result = connection.execute("UPDATE jobs SET status=?,error='',updated=? WHERE id=? AND status=?", (after, time.time(), identity, before))
            return result.rowcount == 1

    def list(self, company):
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM jobs WHERE company=? ORDER BY updated DESC", (company,)).fetchall()
        return [dict(row) for row in rows]


class Workflow:
    def __init__(self, settings_path, *, ledger_path=None, didox_factory=DidoxClient, smartup_factory=SmartupClient):
        self.settings_path = Path(settings_path)
        self.ledger = Ledger(ledger_path or self.settings_path.parent / "local-data" / "workflow.sqlite3")
        self.didox_factory, self.smartup_factory = didox_factory, smartup_factory
        self.didox = self.smartup = None
        self.lock = threading.RLock()
        self.challenges = {}
        self.connection_config = None

    def settings(self):
        return load_settings(self.settings_path)

    def config_key(self, settings):
        return tuple(settings[name] for name in ("company_tin", "didox_api_url", "smartup_api_url", "smartup_project_code", "smartup_filial_id", "smartup_filial_code"))

    def status(self):
        with self.lock:
            if self.connection_config != self.config_key(self.settings()):
                self.didox = self.smartup = None
            settings = self.settings()
            local = all(urlsplit(settings[key]).hostname in ("127.0.0.1", "localhost", "::1") for key in ("didox_api_url", "smartup_api_url"))
            return {"didox_connected": self.didox is not None, "smartup_connected": self.smartup is not None, "api_environment": "loopback" if local else "remote",
                    "can_create_ttn": self.didox is not None and self.smartup is not None}

    def settings_stamp(self):
        return hashlib.sha256(json_bytes(self.settings())).hexdigest()

    def disconnect(self):
        with self.lock:
            self.didox = self.smartup = None
            self.challenges.clear()
        return self.status()

    def connect(self, credentials):
        if not isinstance(credentials, dict) or any(not isinstance(value, str) or len(value) > 100000 or '\r' in value or '\n' in value for value in credentials.values()):
            raise IntegrationError("Некорректные данные подключения.")
        settings = self.settings()
        errors = {}
        with self.lock:
            self.connection_config = self.config_key(settings)
            self.didox = self.smartup = None
            try:
                client = self.didox_factory(settings["didox_api_url"], settings["company_tin"], credentials.get("didox_partner_token", ""), user_token=credentials.get("didox_user_token", ""))
                client.login(password=credentials.get("didox_password", ""), login_tax_id=credentials.get("didox_login_tax_id", ""), signature=credentials.get("didox_signature", ""))
                self.didox = client
            except IntegrationError as exc:
                errors["didox"] = str(exc)
            try:
                client = self.smartup_factory(settings["smartup_api_url"], credentials.get("smartup_username", ""), credentials.get("smartup_password", ""),
                    settings["smartup_project_code"], settings["smartup_filial_id"], settings["smartup_filial_code"])
                client.orders(date.today().isoformat(), refresh=True)
                self.smartup = client
            except IntegrationError as exc:
                errors["smartup"] = str(exc)
        return {**self.status(), "errors": errors}

    def require(self, both=True):
        self.status()
        if self.didox is None or both and self.smartup is None:
            raise IntegrationError("Выполните проверку подключения к нужным системам.")

    def public_job(self, row):
        details = json.loads(row["details"], parse_float=Decimal)
        summary = details["summary"]
        return {"job_id": row["id"], "invoice_id": row["invoice_id"], "number": summary["number"], "date": summary["date"],
            "tin": summary["tin"], "status": row["status"], "waybill_id": row["waybill_id"], "error": row["error"],
            "errors": details["errors"], "warnings": details["warnings"], "payload": details["payload"],
            "order_id": text(field(details["order"], "deal_id")), "total_without_vat": text(summary["total_without_vat"]),
            "total_with_vat": text(summary["total_with_vat"]), "updated": row["updated"]}

    def jobs(self):
        with self.lock:
            return [self.public_job(row) for row in self.ledger.list(self.settings()["company_tin"])]

    def search_invoices(self, filters):
        if not isinstance(filters, dict):
            raise IntegrationError("Заполните фильтры списка СФ.")
        with self.lock:
            self.require(False)
            result = self.didox.list_invoices(filters.get("date_from"), filters.get("date_to"),
                partner=filters.get("partner", ""), status=filters.get("status", "3"), page=filters.get("page", 1))
            result["rows"] = validate_rows([{"source_row": index + 2, "values": item}
                for index, item in enumerate(result.pop("invoices"))])["rows"]
            return result

    def driver_candidates(self, date_from, date_to):
        try:
            start, end = date.fromisoformat(date_from), date.fromisoformat(date_to)
        except (ValueError, TypeError):
            raise IntegrationError("Укажите существующие «Даты заказов Smartup с/по».") from None
        if start > end or (end - start).days > 6:
            raise IntegrationError("Для списка водителей выберите от одного до семи дней.")
        with self.lock:
            self.status()
            if self.smartup is None:
                raise IntegrationError("Выполните вход в Smartup.")
            settings = self.settings()
            records = {}
            day = start
            while day <= end:
                for order in self.smartup.orders(day.isoformat(), refresh=True):
                    if text(field(order, "delivery_date"))[:10] not in (day.isoformat(), day.strftime("%d.%m.%Y")) or text(field(order, "status")) != "A":
                        continue
                    name, van = text(field(order, "expeditor_name")), text(field(order, "van_code"))
                    if not name or not van:
                        continue
                    key = (normalized(van), normalized(name))
                    known = [item for item in settings["drivers"] if normalized(item.get("driver_name")) == key[1]
                        and key[0] in (normalized(item.get("van_code")), normalized(item.get("vehicle_number")))]
                    source = known[0] if len(known) == 1 else {}
                    records[key] = {"van_code": van, "driver_name": name, "vehicle_number": source.get("vehicle_number", ""),
                        "vehicle_model": source.get("vehicle_model", ""), "driver_pinfl": source.get("driver_pinfl", "")}
                    if len(records) > 2000:
                        raise IntegrationError("Слишком много водителей; сузьте период.")
                day += timedelta(days=1)
            return {"drivers": sorted(records.values(), key=lambda item: (item["driver_name"], item["van_code"])),
                "note": "Из архивных заказов Smartup получены ФИО и код автомобиля. Госномер, модель и ПИНФЛ нужно проверить и дополнить; сохранённые ранее значения подставлены из местного справочника."}

    def save_drivers(self, drivers):
        if not isinstance(drivers, list) or not 1 <= len(drivers) <= 2000:
            raise IntegrationError("Получите список водителей и заполните его перед сохранением.")
        for item in drivers:
            if (not isinstance(item, dict) or any(not isinstance(item.get(key), str) or not item[key].strip()
                    for key in ("van_code", "vehicle_number", "vehicle_model", "driver_name", "driver_pinfl"))
                    or not re.fullmatch(r"[0-9]{14}", item["driver_pinfl"])):
                raise IntegrationError("У каждого водителя заполните «госномер», «модель» и «ПИНФЛ» из 14 цифр.")
        keys = [(normalized(item["van_code"]), normalized(item["driver_name"])) for item in drivers]
        if len(set(keys)) != len(keys):
            raise IntegrationError("Уберите повторяющихся водителей одной машины.")
        with self.lock:
            settings = self.settings()
            settings["drivers"] = [item for item in settings["drivers"] if (normalized(item.get("van_code")), normalized(item.get("driver_name"))) not in keys] + drivers
            saved = save_settings(self.settings_path, settings)
            return {"settings": saved, "count": len(drivers)}

    def prepare(self, rows, selected_ids, *, order_id=""):
        if not isinstance(selected_ids, list) or any(not isinstance(item, str) for item in selected_ids):
            raise IntegrationError("Некорректный список выбранных СФ.")
        checked = validate_rows(rows)
        selected = set(selected_ids)
        eligible = [row for row in checked["rows"] if row["record_id"] in selected and row["status"] == "ready"]
        if not eligible or len(eligible) > 50:
            raise IntegrationError("Выберите от одной до 50 проверенных СФ.")
        results = []
        with self.lock:
            self.require()
            settings = self.settings()
            self.smartup.cache.clear()
            for row in eligible:
                try:
                    invoice_id, response = self.didox.find_invoice(row["normalized"])
                    existing = self.ledger.get(settings["company_tin"], invoice_id=invoice_id)
                    if existing and existing["status"] not in ("ready", "needs_review"):
                        results.append({"record_id": row["record_id"], "job": self.public_job(existing)})
                        continue
                    summary = invoice_summary(invoice_id, response, settings, row["normalized"])
                    matches = self.smartup.matches(summary["tin"], summary["date"], summary["total_with_vat"])
                    if order_id:
                        matches = [item for item in matches if text(field(item, "deal_id")) == order_id]
                    if len(matches) != 1:
                        results.append({"record_id": row["record_id"], "error": "Нет точного заказа Smartup." if not matches else "Несколько заказов: выберите нужный.",
                            "candidates": [{"order_id": text(field(item, "deal_id")), "number": text(field(item, "delivery_number")), "driver": text(field(item, "expeditor_name")), "vehicle": text(field(item, "van_code"))} for item in matches]})
                        continue
                    payload, warnings = build_waybill(summary, matches[0], settings)
                    details = {"summary": summary, "order": matches[0], "payload": payload, "warnings": warnings,
                               "errors": validate_waybill(payload, summary), "settings_stamp": self.settings_stamp()}
                    saved = self.ledger.save(settings["company_tin"], invoice_id, details)
                    results.append({"record_id": row["record_id"], "job": self.public_job(saved)})
                except (IntegrationError, ValueError, TypeError, KeyError) as exc:
                    results.append({"record_id": row["record_id"], "error": str(exc) if isinstance(exc, IntegrationError) else "Не удалось разобрать данные документа; проверьте ответ API."})
        return results

    def get_job(self, identity):
        if not isinstance(identity, str):
            raise IntegrationError("Выберите подготовленную ТТН.")
        row = self.ledger.get(self.settings()["company_tin"], job_id=identity)
        if row is None:
            raise IntegrationError("ТТН не найдена в журнале текущей организации.")
        return row

    def edit(self, identity, payload):
        with self.lock:
            row = self.get_job(identity)
            if row["status"] not in ("ready", "needs_review"):
                raise IntegrationError("После отправки запроса создания локальный JSON менять нельзя; проверьте документ в Didox.")
            details = json.loads(row["details"], parse_float=Decimal)
            details["payload"] = payload
            details["errors"] = validate_waybill(payload, details["summary"])
            details["settings_stamp"] = self.settings_stamp()
            saved = self.ledger.save(row["company"], row["invoice_id"], details)
            return self.public_job(saved)

    def save_references(self, identity):
        with self.lock:
            row = self.get_job(identity)
            if row["status"] not in ("ready", "draft"):
                raise IntegrationError("Для сохранения справочников выберите проверенную ТТН.")
            details = json.loads(row["details"], parse_float=Decimal)
            payload, summary = details["payload"], details["summary"]
            errors = validate_waybill(payload, summary)
            if errors:
                raise IntegrationError("Сначала исправьте поля ТТН: " + " ".join(errors))
            settings = self.settings()
            group = field(field(payload, "Roadway", {}), "ProductGroups")[0]
            loading, unloading = field(group, "LoadingPoint"), field(group, "UnloadingPoint")
            settings["destinations"][summary["tin"]] = unloading
            key = f"{field(loading, 'regionId')}:{field(loading, 'districtCode')}:{field(loading, 'address')}|{field(unloading, 'regionId')}:{field(unloading, 'districtCode')}"
            settings["distances"][key] = text(field(payload, "TotalDistance"))
            road = field(payload, "Roadway")
            driver = {"van_code": text(field(details["order"], "van_code")), "vehicle_number": text(field(field(road, "Truck"), "RegNo")),
                "vehicle_model": text(field(field(road, "Truck"), "Model")), "driver_name": text(field(field(road, "Driver"), "FullName")),
                "driver_pinfl": text(field(field(road, "Driver"), "pinfl"))}
            settings["drivers"] = [item for item in settings["drivers"] if (normalized(item.get("vehicle_number")), normalized(item.get("driver_name"))) !=
                (normalized(driver["vehicle_number"]), normalized(driver["driver_name"]))] + [driver]
            # Coordinates may have been decoded as Decimal; settings store JSON-native geometry.
            settings = json.loads(json_bytes(settings))
            saved_settings = save_settings(self.settings_path, settings)
            if row["status"] == "ready":
                details["settings_stamp"] = self.settings_stamp()
                self.ledger.save(row["company"], row["invoice_id"], details)
            return {"settings": saved_settings, "job": self.public_job(self.get_job(identity))}

    def create(self, identities, *, reviewed=False):
        if reviewed is not True:
            raise IntegrationError("Подтвердите проверку данных и способа привязки ТТН к СФ.")
        if not isinstance(identities, list) or not 1 <= len(identities) <= 50 or any(not isinstance(item, str) for item in identities):
            raise IntegrationError("Выберите от одной до 50 ТТН.")
        results = []
        with self.lock:
            self.require()
            for identity in dict.fromkeys(identities):
                try:
                    row = self.get_job(identity)
                    if row["status"] in ("draft", "signed", "remote_document"):
                        results.append(self.public_job(row))
                        continue
                    if row["status"] != "ready":
                        raise IntegrationError("Исправьте поля или проверьте неопределённый результат в Didox перед повтором.")
                    details = json.loads(row["details"], parse_float=Decimal)
                    if details.get("settings_stamp") != self.settings_stamp():
                        raise IntegrationError("Настройки изменились. Подготовьте ТТН заново или проверьте и сохраните её JSON.")
                    errors = validate_waybill(details["payload"], details["summary"])
                    if errors:
                        raise IntegrationError(" ".join(errors))
                    source = self.didox.get_document(row["invoice_id"])
                    current = invoice_summary(row["invoice_id"], source, self.settings(), details["summary"])
                    if json_bytes(current["invoice"]) != json_bytes(details["summary"]["invoice"]):
                        raise IntegrationError("Исходная СФ изменилась. Подготовьте ТТН заново.")
                    if not self.ledger.claim(identity, "ready", "creating"):
                        raise IntegrationError("Запрос создания уже выполняется в другом окне.")
                    try:
                        waybill_id = self.didox.create_waybill(details["payload"])
                    except IntegrationError as exc:
                        self.ledger.update(identity, status="uncertain" if exc.uncertain else "ready", error=str(exc))
                        raise
                    self.ledger.update(identity, status="draft", waybill_id=waybill_id)
                    results.append(self.public_job(self.get_job(identity)))
                except IntegrationError as exc:
                    results.append({"job_id": identity, "error": str(exc)})
        return results

    def reconcile(self, identity, waybill_id):
        with self.lock:
            self.require(False)
            row = self.get_job(identity)
            if row["status"] not in ("creating", "uncertain", "signing", "sign_uncertain"):
                raise IntegrationError("Сверка нужна только для операции с неопределённым результатом.")
            details = json.loads(row["details"], parse_float=Decimal)
            response = self.didox.get_document(waybill_id)
            remote, metadata = document_data(response)
            errors = validate_waybill(remote, details["summary"])
            if errors:
                raise IntegrationError("Найденная ТТН не совпадает с СФ: " + " ".join(errors))
            self.ledger.update(identity, status="draft" if text(field(metadata, "status")) == "0" else "remote_document", waybill_id=waybill_id.lower())
            return self.public_job(self.get_job(identity))

    def confirm_absent(self, identity, checked):
        if checked is not True:
            raise IntegrationError("Сначала проверьте отсутствие документа в Didox.")
        with self.lock:
            row = self.get_job(identity)
            if row["status"] not in ("creating", "uncertain") or row["waybill_id"]:
                raise IntegrationError("Возврат к созданию разрешён только для неизвестного результата без ID ТТН.")
            self.ledger.update(identity, status="ready", error="Пользователь подтвердил отсутствие ТТН в Didox перед повтором.")
            return self.public_job(self.get_job(identity))

    def signing_data(self, identity):
        with self.lock:
            self.require(False)
            row = self.get_job(identity)
            if row["status"] != "draft" or not row["waybill_id"]:
                raise IntegrationError("Для подписи выберите созданный черновик.")
            response = self.didox.get_document(row["waybill_id"])
            remote, metadata = document_data(response)
            if text(field(metadata, "status")) != "0":
                raise IntegrationError("Документ Didox уже не является черновиком; проверьте его статус.")
            details = json.loads(row["details"], parse_float=Decimal)
            errors = validate_waybill(remote, details["summary"])
            if errors:
                raise IntegrationError("Перед подписью исправьте ТТН в Didox: " + " ".join(errors))
            token = uuid.uuid4().hex
            encoded = base64.b64encode(json_bytes(remote)).decode("ascii")
            self.challenges[token] = {"job_id": identity, "data": encoded, "expires": time.time() + 300}
            return {"challenge": token, "data_base64": encoded, "waybill_id": row["waybill_id"]}

    def sign(self, identity, challenge, pkcs7, signature_hex):
        with self.lock:
            self.require(False)
            if not isinstance(challenge, str):
                raise IntegrationError("Подготовьте данные для подписи.")
            proof = self.challenges.pop(challenge, None)
            if not proof or proof["job_id"] != identity or proof["expires"] < time.time():
                raise IntegrationError("Подготовьте свежие данные для подписи.")
            row = self.get_job(identity)
            if row["status"] != "draft":
                raise IntegrationError("ТТН уже не находится в состоянии черновика.")
            current = self.didox.signing_data(row["waybill_id"])
            if current != proof["data"]:
                raise IntegrationError("ТТН изменилась после подготовки подписи; проверьте и повторите.")
            if not isinstance(pkcs7, str) or not isinstance(signature_hex, str) or not pkcs7 or not signature_hex or len(pkcs7) > 2000000 or len(signature_hex) > 100000:
                raise IntegrationError("E-IMZO не вернул подпись.")
            timestamp = self.didox.timestamp(pkcs7, signature_hex)
            if not self.ledger.claim(identity, "draft", "signing"):
                raise IntegrationError("Подписание уже выполняется в другом окне.")
            try:
                self.didox.sign(row["waybill_id"], timestamp)
            except IntegrationError as exc:
                self.ledger.update(identity, status="sign_uncertain" if exc.uncertain else "draft", error=str(exc))
                raise
            self.ledger.update(identity, status="signed")
            return self.public_job(self.get_job(identity))

    def pdf(self, identity):
        with self.lock:
            self.require(False)
            row = self.get_job(identity)
            if not row["waybill_id"]:
                raise IntegrationError("Для этой СФ ещё нет документа Didox.")
            return self.didox.pdf(row["waybill_id"])

    def catalogs(self):
        with self.lock:
            self.require(False)
            return {"regions": self.didox.call("GET", "/v1/regions/all"),
                    "districts": self.didox.call("GET", "/v1/districts/all"),
                    "warehouses": self.didox.call("GET", "/v1/profile/warehouses/" + self.settings()["company_tin"])}

    def auth_timestamp(self, partner_token, pkcs7, signature_hex):
        settings = self.settings()
        client = self.didox_factory(settings["didox_api_url"], settings["company_tin"], partner_token)
        return client.timestamp(pkcs7, signature_hex)

    def export_report(self):
        stream = io.StringIO(newline="")
        writer = csv.writer(stream, delimiter=";", lineterminator="\r\n")
        writer.writerow(("ID СФ", "Номер СФ", "Дата СФ", "Заказ Smartup", "Результат", "ID ТТН", "Ошибка", "Предупреждения"))
        for job in self.jobs():
            values = (job["invoice_id"], job["number"], job["date"], job["order_id"], job["status"], job["waybill_id"],
                      job["error"] or " ".join(job["errors"]), " ".join(job["warnings"]))
            writer.writerow([("'" + str(value)) if str(value or "").lstrip().startswith(("=", "+", "-", "@")) else str(value or "") for value in values])
        return stream.getvalue().encode("utf-8-sig")


def read_drivers(data):
    try:
        decoded = data.decode("utf-8-sig")
    except UnicodeError:
        decoded = data.decode("cp1251")
    try:
        delimiter = csv.Sniffer().sniff(decoded[:4096], delimiters=";,\t").delimiter
    except csv.Error:
        delimiter = ";"
    aliases = {"van_code": ("van_code", "кодsmartup"), "vehicle_number": ("vehicle_number", "госномер", "номермашины"),
               "vehicle_model": ("vehicle_model", "модель"), "driver_name": ("driver_name", "фио", "фиоводителя"),
               "driver_pinfl": ("driver_pinfl", "пинфл")}
    result = []
    reader = csv.DictReader(io.StringIO(decoded), delimiter=delimiter)
    headers = {normalized(name): name for name in reader.fieldnames or []}
    mapping = {name: next((headers[normalized(alias)] for alias in choices if normalized(alias) in headers), None) for name, choices in aliases.items()}
    if any(not mapping[key] for key in ("vehicle_number", "vehicle_model", "driver_name", "driver_pinfl")):
        raise IntegrationError("Нужны колонки «vehicle_number», «vehicle_model», «driver_name», «driver_pinfl»; «van_code» необязателен.")
    for row in reader:
        item = {key: text(row.get(header)) if header else "" for key, header in mapping.items()}
        if any(item.values()):
            result.append(item)
        if len(result) > 2000:
            raise IntegrationError("В справочнике допускается до 2000 водителей.")
    return result
