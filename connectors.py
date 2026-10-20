"""Documented Didox and Smartup JSON APIs, using only the standard library."""

from __future__ import annotations

import base64
from datetime import date
from decimal import Decimal, InvalidOperation
import json
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit, quote
from urllib.request import Request, HTTPRedirectHandler, build_opener


class IntegrationError(ValueError):
    def __init__(self, message, *, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


def field(obj, name, default=None):
    """Didox returns lowercase document keys as well as the schema's PascalCase."""
    if not isinstance(obj, dict):
        return default
    key = re.sub(r"[^a-z0-9]", "", name.lower())
    for candidate, value in obj.items():
        if re.sub(r"[^a-z0-9]", "", str(candidate).lower()) == key:
            return value
    return default


def decimal_value(value, label="сумма"):
    try:
        result = Decimal(str(value).replace(" ", "").replace("\u00a0", "").replace(",", "."))
        if not result.is_finite():
            raise InvalidOperation
        return result
    except (InvalidOperation, ValueError):
        raise IntegrationError(f"Не заполнено или некорректно поле «{label}».") from None


def json_bytes(value):
    def encode(item):
        if isinstance(item, Decimal):
            if not item.is_finite():
                raise ValueError("Non-finite decimal")
            return str(item)
        if isinstance(item, dict):
            return "{" + ",".join(json.dumps(str(key), ensure_ascii=False) + ":" + encode(child) for key, child in item.items()) + "}"
        if isinstance(item, (tuple, list)):
            return "[" + ",".join(encode(child) for child in item) + "]"
        return json.dumps(item, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    return encode(value).encode("utf-8")


def _unsupported(value):
    raise TypeError(type(value).__name__)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class JsonTransport:
    def __init__(self, base_url, *, label, timeout=30):
        parsed = urlsplit(base_url)
        try:
            port = parsed.port
        except ValueError:
            raise IntegrationError(f"Проверьте API-адрес «{label}».") from None
        if (parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1"))
                or not parsed.hostname or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or re.search(r"[\s\\]", base_url) or port == 0):
            raise IntegrationError(f"«{label}»: нужен HTTPS API-адрес без пароля, параметров и фрагмента.")
        self.base = base_url.rstrip("/")
        self.label = label
        self.timeout = timeout
        self.opener = build_opener(NoRedirect())

    def request(self, method, path, *, headers=None, body=None, query=None, write=False, secrets=()):
        if not path.startswith("/") or path.startswith("//"):
            raise IntegrationError("Некорректный путь API.")
        url = self.base + path + (("?" + urlencode(query)) if query else "")
        request = Request(url, data=json_bytes(body) if body is not None else None,
                          headers={"Accept": "application/json", "Content-Type": "application/json", **(headers or {})}, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(16 * 1024 * 1024 + 1)
            if len(raw) > 16 * 1024 * 1024:
                raise IntegrationError(f"«{self.label}»: ответ превышает 16 МБ; сузьте выборку.", uncertain=write)
            result = json.loads(raw.decode("utf-8-sig"), parse_float=Decimal)
            if not isinstance(result, (dict, list)):
                raise ValueError
            return result
        except HTTPError as exc:
            message = ""
            try:
                value = json.loads(exc.read(16384).decode("utf-8"))
                if isinstance(value, str):
                    message = value
                elif isinstance(value, dict):
                    detail = value.get("data", value)
                    message = str(detail.get("message", value.get("message", ""))) if isinstance(detail, dict) else ""
            except (ValueError, UnicodeError, OSError):
                pass
            for secret in secrets:
                if secret:
                    message = message.replace(str(secret), "[скрыто]")
            explanations = {401: "проверьте доступы или повторите вход", 403: "нет прав на операцию",
                            429: "лимит запросов; продолжите позже"}
            detail = message[:700] or explanations.get(exc.code, "проверьте данные и API-адрес")
            raise IntegrationError(f"«{self.label}»: HTTP {exc.code}, {detail}.", uncertain=write and exc.code >= 500) from None
        except (URLError, TimeoutError, ConnectionError, OSError):
            raise IntegrationError(f"«{self.label}»: соединение не завершено. Проверьте сеть и API-адрес.", uncertain=write) from None
        except (ValueError, UnicodeError):
            raise IntegrationError(f"«{self.label}»: ожидался JSON; проверьте API-адрес, а не ссылку на страницу входа.", uncertain=write) from None


def document_data(response):
    data = field(response, "data", response)
    value = field(data, "json")
    if value is None:
        value = field(field(data, "pending_document", {}), "document_json")
    if isinstance(value, str):
        try:
            value = json.loads(value, parse_float=Decimal)
        except ValueError:
            value = None
    if not isinstance(value, dict):
        raise IntegrationError("Didox не вернул JSON документа.")
    return value, field(data, "document", {})


class DidoxClient:
    def __init__(self, base_url, company_tin, partner_token, *, user_token="", transport=None):
        if not partner_token or not re.fullmatch(r"[0-9]{9}", company_tin):
            raise IntegrationError("Заполните «ИНН организации» и «Партнёрский токен Didox».")
        self.transport = transport or JsonTransport(base_url, label="Didox")
        self.company_tin = company_tin
        self.partner_token = partner_token
        self.user_token = user_token

    def call(self, method, path, *, body=None, query=None, write=False):
        result = self.transport.request(method, path, headers={"Partner-Authorization": self.partner_token,
            "user-key": self.user_token, "Accept-Language": "ru"}, body=body, query=query, write=write,
            secrets=(self.partner_token, self.user_token))
        detail = field(result, "data", result)
        if field(detail, "status") == "error" or field(result, "success") is False:
            raise IntegrationError("Didox отказал в операции. Проверьте документ и полномочия.")
        return result

    def login(self, *, password="", login_tax_id="", signature=""):
        tax_id = login_tax_id or self.company_tin
        if not re.fullmatch(r"(?:[0-9]{9}|[0-9]{14})", tax_id):
            raise IntegrationError("Проверьте «ИНН/ПИНФЛ для входа Didox».")
        if not self.user_token:
            if signature:
                path, body = f"/v1/auth/{tax_id}/token/ru", {"signature": signature}
            elif password:
                path, body = f"/v1/auth/{tax_id}/password/ru", {"password": password}
            else:
                raise IntegrationError("Введите «Пароль Didox», «Токен пользователя» либо выполните вход с ЭЦП.")
            response = self.transport.request("POST", path, headers={"Partner-Authorization": self.partner_token}, body=body,
                                              secrets=(self.partner_token, password, signature))
            self.user_token = str(field(response, "token", field(field(response, "data", {}), "token", "")))
            if not self.user_token:
                raise IntegrationError("Didox не вернул токен пользователя.")
        profile_response = self.call("GET", "/v1/profile")
        profile = field(profile_response, "data", profile_response)
        tin = str(field(profile, "tin", field(profile, "companyTaxId", "")))
        if tin != self.company_tin:
            response = self.call("POST", f"/v1/auth/company/{self.company_tin}/login/ru", body={})
            self.user_token = str(field(response, "token", field(field(response, "data", {}), "token", "")))
            if not self.user_token:
                raise IntegrationError("Не удалось войти в указанную компанию Didox.")
            profile_response = self.call("GET", "/v1/profile")
            profile = field(profile_response, "data", profile_response)
            if str(field(profile, "tin", field(profile, "companyTaxId", ""))) != self.company_tin:
                raise IntegrationError("Текущая организация Didox не совпадает с «ИНН организации».")
        return profile

    def get_document(self, document_id):
        if not isinstance(document_id, str) or not re.fullmatch(r"[a-fA-F0-9]{32}", document_id):
            raise IntegrationError("Ожидается ID документа Didox из 32 символов.")
        return self.call("GET", "/v1/documents/" + document_id, query={"owner": 1})

    def find_invoice(self, values):
        if values["id"]:
            return values["id"], self.get_document(values["id"])
        ids = set()
        for page in range(1, 21):
            response = self.call("GET", "/v2/documents", query={"page": page, "limit": 100, "owner": 1,
                "partner": values["tin"], "name": values["number"], "docDateFromCreated": values["date"], "docDateToCreated": values["date"]})
            items = field(response, "data", [])
            if not isinstance(items, list):
                raise IntegrationError("Didox вернул неизвестный формат списка СФ.")
            for item in items:
                identity = str(field(item, "doc_id", field(item, "id", field(item, "_id", ""))))
                if re.fullmatch(r"[a-fA-F0-9]{32}", identity):
                    ids.add(identity.lower())
            if not field(response, "next_page_url") and len(items) < 100:
                break
        else:
            raise IntegrationError("Слишком много совпадений Didox; укажите точный «ID СФ».")
        matched = []
        for identity in ids:
            response = self.get_document(identity)
            invoice, _ = document_data(response)
            doc = field(invoice, "FacturaDoc", {})
            if (str(field(doc, "FacturaNo", "")) == values["number"] and str(field(doc, "FacturaDate", ""))[:10] == values["date"]
                    and str(field(invoice, "BuyerTin", "")) == values["tin"]):
                matched.append((identity, response))
        if len(matched) != 1:
            raise IntegrationError("СФ не найдена или найдено несколько совпадений. Укажите точный «ID СФ».")
        return matched[0]

    def list_invoices(self, date_from, date_to, *, partner="", status="3", page=1):
        try:
            start, end = date.fromisoformat(date_from), date.fromisoformat(date_to)
        except (ValueError, TypeError):
            raise IntegrationError("Укажите существующие «Даты СФ с/по».") from None
        if start > end or (end - start).days > 366:
            raise IntegrationError("Период СФ должен быть от одного дня до года.")
        if not isinstance(partner, str) or partner and not re.fullmatch(r"(?:[0-9]{9}|[0-9]{14})", partner):
            raise IntegrationError("Проверьте «ИНН покупателя» либо оставьте его пустым.")
        allowed = {"0", "1", "2", "3", "4", "6", "8", "40", "60"}
        if not isinstance(status, str) or status not in allowed or type(page) is not int or not 1 <= page <= 40:
            raise IntegrationError("Выберите статус и страницу списка СФ от «1» до «40».")
        query = {"owner": 1, "doctype": "001,002,008", "status": status, "page": page, "limit": 50,
                 "docDateFromCreated": date_from, "docDateToCreated": date_to}
        if partner:
            query["partner"] = partner
        response = self.call("GET", "/v2/documents", query=query)
        items = field(response, "data", [])
        if not isinstance(items, list):
            raise IntegrationError("Didox вернул неизвестный формат списка СФ.")
        result = []
        for item in items:
            identity = str(field(item, "doc_id", field(item, "id", field(item, "_id", ""))))
            if not re.fullmatch(r"[a-fA-F0-9]{32}", identity):
                raise IntegrationError("В списке Didox отсутствует корректный «ID СФ».")
            doc_date = str(field(item, "doc_date", ""))[:10]
            buyer = str(field(item, "partnerTin", ""))
            if (str(field(item, "owner", "1")) != "1" or str(field(item, "doctype", "")) not in ("001", "002", "008")
                    or str(field(item, "doc_status", field(item, "status", ""))) != status
                    or not date_from <= doc_date <= date_to or partner and buyer != partner):
                continue
            if str(field(item, "usersTaxId", self.company_tin)) != self.company_tin:
                continue
            result.append({"id": identity.lower(), "number": str(field(item, "name", "")), "date": doc_date, "tin": buyer,
                "comment": f"Didox: {field(item, 'partnerCompany', '')}; статус «{status}»."})
        has_more = bool(field(response, "next_page_url")) or len(items) == 50
        return {"invoices": result, "page": page, "has_more": has_more,
                "limit_reached": page == 40 and has_more}

    def create_waybill(self, payload):
        response = self.call("POST", "/v1/documents/041/create/ru", body=payload, write=True)
        data = field(response, "data", response)
        identity = str(field(data, "_id", field(data, "id", "")))
        if not re.fullmatch(r"[a-fA-F0-9]{32}", identity):
            raise IntegrationError("Didox принял запрос, но не вернул распознаваемый ID ТТН. Проверьте черновики перед повтором.", uncertain=True)
        return identity.lower()

    def signing_data(self, document_id):
        response = self.get_document(document_id)
        data, _ = document_data(response)
        return base64.b64encode(json_bytes(data)).decode("ascii")

    def timestamp(self, pkcs7, signature_hex):
        response = self.call("POST", "/v1/dsvs/timestamp", body={"pkcs7": pkcs7, "signatureHex": signature_hex})
        value = field(response, "timeStampTokenB64", field(field(response, "data", {}), "timeStampTokenB64"))
        if not isinstance(value, str) or not value:
            raise IntegrationError("Didox не вернул подпись с timestamp.")
        return value

    def sign(self, document_id, signature):
        response = self.call("POST", f"/v1/documents/{document_id}/sign", body={"signature": signature}, write=True)
        if field(response, "data") is not True:
            raise IntegrationError("Didox не подтвердил подписание. Проверьте текущий статус документа.", uncertain=True)

    def pdf(self, document_id):
        if not isinstance(document_id, str) or not re.fullmatch(r"[a-fA-F0-9]{32}", document_id):
            raise IntegrationError("Некорректный ID документа.")
        request = Request(self.transport.base + f"/v1/documents/view/{document_id}/pdf/ru", headers={
            "Partner-Authorization": self.partner_token, "user-key": self.user_token, "Accept": "application/pdf"})
        try:
            with self.transport.opener.open(request, timeout=self.transport.timeout) as response:
                content = response.read(20 * 1024 * 1024 + 1)
            if not content.startswith(b"%PDF-") or len(content) > 20 * 1024 * 1024:
                raise IntegrationError("Didox не вернул PDF допустимого размера.")
            return content
        except (HTTPError, URLError, OSError) as exc:
            raise IntegrationError("PDF Didox недоступен. Проверьте сеанс и права просмотра.") from None


class SmartupClient:
    ORDER_PATH = "/b/trade/txs/tdeal/order$export"

    def __init__(self, base_url, username, password, project_code, filial_id, filial_code="", *, transport=None):
        if not all((username, password, project_code, filial_id)):
            raise IntegrationError("Заполните логин, пароль, «project_code» и «filial_id» Smartup.")
        if ":" in username:
            raise IntegrationError("Логин Smartup для Basic Auth не должен содержать двоеточие.")
        self.transport = transport or JsonTransport(base_url, label="Smartup")
        self.username, self.password = username, password
        self.project_code, self.filial_id, self.filial_code = project_code, filial_id, filial_code
        self.cache = {}

    def orders(self, delivery_date, *, refresh=False):
        if delivery_date in self.cache and not refresh:
            return self.cache[delivery_date]
        try:
            query_date = date.fromisoformat(delivery_date).strftime("%d.%m.%Y")
        except ValueError:
            raise IntegrationError("Проверьте «Дату СФ» для поиска Smartup.") from None
        authorization = "Basic " + base64.b64encode((self.username + ":" + self.password).encode("utf-8")).decode("ascii")
        body = {"delivery_date": query_date, "statuses": ["A"]}
        if self.filial_code:
            body["filial_code"] = self.filial_code
        response = self.transport.request("POST", self.ORDER_PATH, headers={"Authorization": authorization,
            "project_code": self.project_code, "filial_id": self.filial_id}, body=body,
            secrets=(self.username, self.password, authorization))
        if not isinstance(response, dict) or field(response, "errors") or field(response, "success") is False:
            raise IntegrationError("Smartup вернул ошибку экспорта. Проверьте права, организацию и глубину архива.")
        orders = field(response, "order")
        if not isinstance(orders, list):
            raise IntegrationError("Smartup не вернул массив «order». Проверьте API-адрес и доступ к архиву.")
        limits = field(response, "limits", {})
        if isinstance(limits, dict) and str(field(limits, "has_limit", "N")).upper() == "Y":
            count = field(limits, "object_count")
            if count and len(orders) >= int(count):
                raise IntegrationError("Выгрузка Smartup могла достигнуть лимита записей. Сузьте выборку у администратора.")
        self.cache[delivery_date] = orders
        return orders

    def matches(self, tin, delivery_date, total):
        results = []
        for order in self.orders(delivery_date):
            raw_date = str(field(order, "delivery_date", ""))
            parsed = None
            for pattern in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%y"):
                try:
                    from datetime import datetime
                    parsed = datetime.strptime(raw_date[:10], pattern).date().isoformat()
                    break
                except ValueError:
                    continue
            if str(field(order, "person_tin", "")).strip() != tin or parsed != delivery_date:
                continue
            amount = decimal_value(field(order, "total_amount"), "Smartup total_amount")
            if amount.quantize(Decimal("0.01")) == total.quantize(Decimal("0.01")):
                results.append(order)
        return results
