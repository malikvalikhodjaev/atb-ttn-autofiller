import base64
import copy
from datetime import date
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import http.client
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, urlsplit

from connectors import IntegrationError, JsonTransport, json_bytes, field
from settings import DEFAULT_SETTINGS, save_settings
from workflow import Workflow, read_drivers


COMPANY = "123456789"
BUYER = "987654321"
DAY = "2026-10-20"
CREDENTIALS = {"didox_partner_token": "demo-partner-token", "didox_password": "demo-didox-password",
               "smartup_username": "demo-smartup-user", "smartup_password": "demo-smartup-password"}


def identity(index):
    return f"aaa{index:029x}"


def invoice(index):
    return {"FacturaDoc": {"FacturaNo": f"DEMO-{index}", "FacturaDate": DAY},
        "ContractDoc": {"ContractNo": "DEMO-CONTRACT", "ContractDate": "2026-01-01"},
        "SellerTin": COMPANY, "BuyerTin": BUYER,
        "Seller": {"Name": "DEMO SUPPLIER"}, "Buyer": {"Name": "DEMO CUSTOMER", "Address": "DEMO DELIVERY ADDRESS"},
        "ProductList": {"HasCommittent": False, "Products": [{"Name": "DEMO CARGO", "CatalogCode": "00102001001000000",
            "CatalogName": "DEMO CATALOG", "PackageCode": "100", "PackageName": "DEMO UNIT", "Count": "2",
            "Summa": str(50 * index), "DeliverySum": str(100 * index), "DeliverySumWithVat": str(112 * index),
            "WeightBrutto": "2", "WeightNetto": "1.8"}]}}


def order(index):
    return {"deal_id": str(index), "person_tin": BUYER, "delivery_date": "20.10.2026", "total_amount": str(112 * index),
        "expeditor_name": "DEMO DRIVER", "van_code": "DEMO-VAN", "delivery_number": f"DEMO-{index}", "status": "A"}


def configured_settings(base):
    value = copy.deepcopy(DEFAULT_SETTINGS)
    value.update({"company_tin": COMPANY, "didox_api_url": base, "smartup_api_url": base,
        "smartup_filial_id": "1", "loading_address": "DEMO LOADING ADDRESS",
        "loading_point": {"regionId": 26, "regionName": "DEMO REGION", "districtCode": 3, "districtName": "DEMO LOADING DISTRICT", "address": "DEMO LOADING ADDRESS"},
        "destinations": {BUYER: {"regionId": 26, "regionName": "DEMO REGION", "districtCode": 5, "districtName": "DEMO DELIVERY DISTRICT", "address": "DEMO DELIVERY ADDRESS"}},
        "drivers": [{"van_code": "DEMO-VAN", "vehicle_number": "DEMO-01", "vehicle_model": "DEMO MODEL", "driver_name": "DEMO DRIVER", "driver_pinfl": "10000000000000"}],
        "distances": {"26:3:DEMO LOADING ADDRESS|26:5": "6"}})
    return value


class Gateway(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, value, status=200):
        raw = json_bytes(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        route = urlsplit(self.path)
        self.server.requests.append(("GET", route.path, dict(self.headers), None))
        if route.path == "/redirect":
            self.send_response(307)
            self.send_header("Location", self.server.base + "/unexpected")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif route.path == "/precision":
            self.send({"value": Decimal("1234567890123456789.123456")})
        elif route.path == "/v1/profile":
            self.send({"tin": COMPANY, "name": "DEMO SUPPLIER"})
        elif route.path == "/v2/documents":
            query = {key: values[0] for key, values in parse_qs(route.query).items()}
            documents = []
            for key, value in self.server.invoices.items():
                doc = field(value, "FacturaDoc")
                item = {"doc_id": key, "usersTaxId": field(value, "SellerTin"), "name": field(doc, "FacturaNo"),
                    "doc_date": field(doc, "FacturaDate"), "partnerTin": field(value, "BuyerTin"), "partnerCompany": "DEMO CUSTOMER",
                    "doctype": "002", "owner": 1, "doc_status": self.server.statuses.get(key, 3)}
                if not self.server.ignore_filters:
                    if query.get("partner") and item["partnerTin"] != query["partner"]: continue
                    if query.get("name") and query["name"] not in item["name"]: continue
                    if query.get("status") and str(item["doc_status"]) != query["status"]: continue
                    if query.get("docDateFromCreated", "0000") > item["doc_date"]: continue
                    if query.get("docDateToCreated", "9999") < item["doc_date"]: continue
                documents.append(item)
            page, limit = int(query.get("page", 1)), int(query.get("limit", 100))
            start = (page - 1) * limit
            self.send({"data": documents[start:start + limit], "next_page_url": "v2/documents?page=" + str(page + 1) if start + limit < len(documents) else None})
        elif route.path.startswith("/v1/documents/view/"):
            raw = b"%PDF-1.4\n%%EOF"
            self.send_response(200); self.send_header("Content-Type", "application/pdf"); self.send_header("Content-Length", str(len(raw))); self.end_headers(); self.wfile.write(raw)
        elif route.path.startswith("/v1/documents/"):
            key = route.path.split("/")[-1]
            document = self.server.invoices.get(key) or self.server.drafts.get(key)
            if document is None:
                self.send({"message": "not found"}, 404)
            else:
                self.send({"data": {"json": document, "document": {"status": self.server.statuses.get(key, 1)}, "relatedDocuments": []}})
        elif route.path in ("/v1/regions/all", "/v1/districts/all"):
            self.send([])
        elif route.path.startswith("/v1/profile/warehouses/"):
            self.send([])
        else:
            self.send({"message": "unknown route"}, 404)

    def do_POST(self):
        route = urlsplit(self.path).path
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        self.server.requests.append(("POST", route, dict(self.headers), body))
        if route.startswith("/v1/auth/"):
            self.send({"token": "demo-user-token"})
        elif route.endswith("order$export"):
            expected = "Basic " + base64.b64encode((CREDENTIALS["smartup_username"] + ":" + CREDENTIALS["smartup_password"]).encode()).decode()
            if self.headers.get("Authorization") != expected or self.headers.get("filial_id") != "1":
                self.send({"message": "bad credentials"}, 401)
            else:
                self.send({"order": self.server.orders if body.get("delivery_date") == "20.10.2026" else []})
        elif route == "/v1/documents/041/create/ru":
            number = body["WaybillDoc"]["WaybillNo"]
            self.server.create_attempts += 1
            if number in self.server.fail_numbers:
                self.send({"data": {"message": "invalid district"}}, 422)
                return
            key = f"bbb{len(self.server.drafts) + 1:029x}"
            self.server.drafts[key] = body
            self.server.statuses[key] = 0
            if self.server.fail_after_create:
                self.server.fail_after_create = False
                self.send({"message": "temporary failure"}, 500)
            else:
                self.send({"_id": key, "pending_document": {"document_json": body}})
        elif route == "/v1/dsvs/timestamp":
            self.send({"timeStampTokenB64": "demo-timestamp-signature", "success": True})
        elif route.endswith("/sign"):
            key = route.split("/")[-2]
            self.server.statuses[key] = 110
            self.server.sign_attempts += 1
            self.send({"data": True})
        else:
            self.send({"message": "unknown route"}, 404)


def start_gateway(port=0):
    server = ThreadingHTTPServer(("127.0.0.1", port), Gateway)
    server.base = f"http://127.0.0.1:{server.server_address[1]}"
    server.invoices = {identity(index): invoice(index) for index in range(1, 4)}
    server.orders = [order(index) for index in range(1, 4)]
    server.drafts, server.statuses, server.requests = {}, {}, []
    server.create_attempts = server.sign_attempts = 0
    server.fail_numbers = set()
    server.fail_after_create = False
    server.ignore_filters = False
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.server, self.thread = start_gateway()
        self.path = Path(self.temporary.name) / "config.local.json"
        save_settings(self.path, configured_settings(self.server.base))
        self.workflow = Workflow(self.path)
        connected = self.workflow.connect(CREDENTIALS)
        self.assertTrue(connected["can_create_ttn"], connected)

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=2)
        self.temporary.cleanup()

    def rows(self, count=1):
        return [{"source_row": index + 1, "values": {"id": identity(index)}, "source_issues": {}} for index in range(1, count + 1)]

    def prepare(self, count=1):
        return self.workflow.prepare(self.rows(count), [f"row-{index + 1}" for index in range(1, count + 1)])

    def test_single_invoice_creates_one_draft_and_retry_reuses_it(self):
        prepared = self.prepare()[0]["job"]
        self.assertEqual(prepared["status"], "ready", prepared["errors"])
        created = self.workflow.create([prepared["job_id"]], reviewed=True)[0]
        self.assertEqual(created["status"], "draft")
        self.assertEqual(self.server.create_attempts, 1)
        self.assertEqual(self.workflow.create([prepared["job_id"]], reviewed=True)[0]["waybill_id"], created["waybill_id"])
        self.assertEqual(self.prepare()[0]["job"]["waybill_id"], created["waybill_id"])
        self.assertEqual(self.server.create_attempts, 1)
        self.assertTrue(self.workflow.pdf(prepared["job_id"]).startswith(b"%PDF-"))

    def test_three_invoice_batch_keeps_partial_results_and_continues(self):
        jobs = [item["job"] for item in self.prepare(3)]
        self.server.fail_numbers.add("DEMO-2")
        result = self.workflow.create([job["job_id"] for job in jobs], reviewed=True)
        self.assertEqual([item.get("status", "error") for item in result], ["draft", "error", "draft"])
        self.assertEqual(len(self.server.drafts), 2)
        self.server.fail_numbers.clear()
        resumed = self.workflow.create([job["job_id"] for job in jobs], reviewed=True)
        self.assertTrue(all(item["status"] == "draft" for item in resumed))
        self.assertEqual(len(self.server.drafts), 3)

    def test_zero_matches_never_create_a_document(self):
        self.server.orders = []
        result = self.prepare()[0]
        self.assertIn("error", result)
        self.assertEqual(self.server.create_attempts, 0)

    def test_ambiguous_orders_require_an_explicit_selection(self):
        second = order(1); second["deal_id"] = "duplicate"
        self.server.orders.append(second)
        result = self.prepare()[0]
        self.assertEqual(len(result["candidates"]), 2)
        chosen = self.workflow.prepare(self.rows(), ["row-2"], order_id="duplicate")[0]
        self.assertEqual(chosen["job"]["order_id"], "duplicate")
        self.assertEqual(self.server.create_attempts, 0)

    def test_missing_weights_require_review_and_can_be_filled(self):
        item = self.server.invoices[identity(1)]["ProductList"]["Products"][0]
        item.pop("WeightBrutto"); item.pop("WeightNetto")
        prepared = self.prepare()[0]["job"]
        self.assertEqual(prepared["status"], "needs_review")
        payload = prepared["payload"]
        info = payload["Roadway"]["ProductGroups"][0]["ProductInfo"]
        info["products"][0]["WeightBrutto"] = "2"
        info["products"][0]["WeightNetto"] = "1.8"
        info["TotalWeightBrutto"] = "2"
        self.assertEqual(self.workflow.edit(prepared["job_id"], payload)["status"], "ready")

    def test_editing_invoice_amounts_is_blocked(self):
        job = self.prepare()[0]["job"]
        payload = job["payload"]
        payload["Roadway"]["ProductGroups"][0]["ProductInfo"]["products"][0]["DeliverySum"] = "999"
        edited = self.workflow.edit(job["job_id"], payload)
        self.assertEqual(edited["status"], "needs_review")
        self.assertIn("error", self.workflow.create([job["job_id"]], reviewed=True)[0])
        self.assertEqual(self.server.create_attempts, 0)

    def test_uncertain_create_is_not_repeated_and_can_be_reconciled(self):
        job = self.prepare()[0]["job"]
        self.server.fail_after_create = True
        result = self.workflow.create([job["job_id"]], reviewed=True)
        self.assertIn("error", result[0])
        self.assertEqual(self.workflow.get_job(job["job_id"])["status"], "uncertain")
        self.workflow.create([job["job_id"]], reviewed=True)
        self.assertEqual(self.server.create_attempts, 1)
        remote_id = next(iter(self.server.drafts))
        reconciled = self.workflow.reconcile(job["job_id"], remote_id)
        self.assertEqual(reconciled["status"], "draft")
        self.assertEqual(self.workflow.create([job["job_id"]], reviewed=True)[0]["waybill_id"], remote_id)

    def test_journal_survives_restart_without_duplicate_creation(self):
        job = self.prepare()[0]["job"]
        self.workflow.create([job["job_id"]], reviewed=True)
        self.workflow = Workflow(self.path)
        self.workflow.connect(CREDENTIALS)
        self.assertEqual(self.prepare()[0]["job"]["status"], "draft")
        self.assertEqual(self.server.create_attempts, 1)

    def test_review_is_required_before_remote_writes(self):
        job = self.prepare()[0]["job"]
        with self.assertRaises(IntegrationError):
            self.workflow.create([job["job_id"]])
        self.assertEqual(self.server.create_attempts, 0)

    def test_signing_flow_is_separate_from_draft_creation(self):
        job = self.prepare()[0]["job"]
        self.workflow.create([job["job_id"]], reviewed=True)
        self.assertEqual(self.server.sign_attempts, 0)
        proof = self.workflow.signing_data(job["job_id"])
        signed = self.workflow.sign(job["job_id"], proof["challenge"], "demo-pkcs7", "demo-hex")
        self.assertEqual(signed["status"], "signed")
        self.assertEqual(self.server.sign_attempts, 1)
        with self.assertRaises(IntegrationError):
            self.workflow.sign(job["job_id"], proof["challenge"], "demo-pkcs7", "demo-hex")

    def test_document_change_after_signature_preparation_is_rejected(self):
        job = self.prepare()[0]["job"]
        created = self.workflow.create([job["job_id"]], reviewed=True)[0]
        proof = self.workflow.signing_data(job["job_id"])
        self.server.drafts[created["waybill_id"]]["TotalDistance"] = "7"
        with self.assertRaises(IntegrationError):
            self.workflow.sign(job["job_id"], proof["challenge"], "demo-pkcs7", "demo-hex")
        self.assertEqual(self.server.sign_attempts, 0)

    def test_decimal_precision_and_redirect_rejection(self):
        transport = JsonTransport(self.server.base, label="test")
        self.assertEqual(transport.request("GET", "/precision")["value"], Decimal("1234567890123456789.123456"))
        self.assertIn(b"1234567890123456789.123456", json_bytes({"value": Decimal("1234567890123456789.123456")}))
        with self.assertRaises(IntegrationError):
            transport.request("GET", "/redirect", headers={"Authorization": "test-secret"})
        self.assertFalse(any(path == "/unexpected" for _, path, _, _ in self.server.requests))

    def test_settings_change_invalidates_connections_and_secret_is_not_saved(self):
        saved = self.path.read_text(encoding="utf-8")
        for value in CREDENTIALS.values():
            self.assertNotIn(value, saved)
        settings = configured_settings(self.server.base); settings["smartup_filial_id"] = "2"
        save_settings(self.path, settings)
        self.assertFalse(self.workflow.status()["can_create_ttn"])

    def test_lookup_by_number_date_and_tin_checks_exact_invoice(self):
        row = {"source_row": 2, "values": {"number": "DEMO-2", "date": DAY, "tin": BUYER}}
        result = self.workflow.prepare([row], ["row-2"])[0]
        self.assertEqual(result["job"]["invoice_id"], identity(2))

    def test_driver_csv_and_no_smartup_import_requests(self):
        source = 'vehicle_number;vehicle_model;driver_name;driver_pinfl\nDEMO-01;DEMO MODEL;DEMO DRIVER;10000000000000\n'.encode('utf-8-sig')
        self.assertEqual(read_drivers(source)[0]["driver_pinfl"], "10000000000000")
        self.prepare(3)
        smartup = [(method, path) for method, path, _, _ in self.server.requests if "/b/" in path]
        self.assertTrue(smartup)
        self.assertTrue(all(path.endswith("order$export") for _, path in smartup))

    def test_lowercase_didox_document_and_saved_references(self):
        def lower(value):
            if isinstance(value, dict): return {key.lower(): lower(item) for key, item in value.items()}
            if isinstance(value, list): return [lower(item) for item in value]
            return value
        self.server.invoices[identity(1)] = lower(self.server.invoices[identity(1)])
        job = self.prepare()[0]["job"]
        self.assertEqual(job["status"], "ready", job["errors"])
        saved = self.workflow.save_references(job["job_id"])
        self.assertEqual(saved["settings"]["destinations"][BUYER]["address"], "DEMO DELIVERY ADDRESS")
        self.assertEqual(saved["job"]["status"], "ready")

    def test_partial_connection_does_not_enable_remote_creation(self):
        credentials = dict(CREDENTIALS, smartup_password="incorrect")
        status = self.workflow.connect(credentials)
        self.assertTrue(status["didox_connected"])
        self.assertFalse(status["smartup_connected"])
        self.assertFalse(status["can_create_ttn"])
        with self.assertRaises(IntegrationError): self.prepare()

    def test_didox_filtered_list_checks_status_dates_buyer_and_company(self):
        filters = {"date_from": DAY, "date_to": DAY, "partner": BUYER, "status": "3"}
        self.assertEqual(len(self.workflow.search_invoices(filters)["rows"]), 3)
        self.server.statuses[identity(2)] = 0
        self.server.invoices[identity(3)]["SellerTin"] = "111111111"
        self.server.ignore_filters = True
        result = self.workflow.search_invoices(filters)
        self.assertEqual([row["normalized"]["id"] for row in result["rows"]], [identity(1)])
        self.assertEqual(self.server.create_attempts, 0)
        query_request = [item for item in self.server.requests if item[1] == "/v2/documents"][-1]
        self.assertEqual(query_request[2]["Partner-Authorization"], CREDENTIALS["didox_partner_token"])

    def test_didox_list_has_explicit_pagination(self):
        self.server.invoices = {identity(index): invoice(index) for index in range(1, 56)}
        filters = {"date_from": DAY, "date_to": DAY}
        first = self.workflow.search_invoices(filters)
        second = self.workflow.search_invoices(dict(filters, page=2))
        self.assertEqual(len(first["rows"]), 50)
        self.assertTrue(first["has_more"])
        self.assertEqual(len(second["rows"]), 5)
        self.assertFalse(second["has_more"])
        self.assertNotEqual(first["rows"][0]["normalized"]["id"], second["rows"][0]["normalized"]["id"])

    def test_smartup_driver_list_requires_missing_details_and_preserves_references(self):
        settings = configured_settings(self.server.base); settings["drivers"] = []
        save_settings(self.path, settings)
        result = self.workflow.driver_candidates(DAY, DAY)
        self.assertEqual(len(result["drivers"]), 1)
        driver = result["drivers"][0]
        self.assertEqual(driver["van_code"], "DEMO-VAN")
        self.assertEqual(driver["driver_pinfl"], "")
        self.assertEqual(driver["vehicle_number"], "")
        with self.assertRaises(IntegrationError): self.workflow.save_drivers(result["drivers"])
        driver.update(vehicle_number="DEMO-01", vehicle_model="DEMO MODEL", driver_pinfl="10000000000000")
        self.assertEqual(self.workflow.save_drivers([driver])["count"], 1)
        again = self.workflow.driver_candidates(DAY, DAY)
        self.assertEqual(again["drivers"][0]["driver_pinfl"], "10000000000000")
        self.assertEqual(self.server.create_attempts, 0)

    def test_search_and_driver_period_validation_precedes_remote_requests(self):
        count = len(self.server.requests)
        for filters in ({"date_from": DAY, "date_to": "2026-01-01"}, {"date_from": DAY, "date_to": DAY, "status": "wrong"},
                        {"date_from": DAY, "date_to": DAY, "partner": "123"}, {"date_from": DAY, "date_to": DAY, "page": 41}):
            with self.assertRaises(IntegrationError): self.workflow.search_invoices(filters)
        with self.assertRaises(IntegrationError): self.workflow.driver_candidates("2026-10-01", DAY)
        self.assertEqual(len(self.server.requests), count)

    def test_browser_api_supports_three_invoice_batch_and_report(self):
        from app import make_server
        app = make_server(0, self.path, workflow=self.workflow)
        thread = threading.Thread(target=app.serve_forever, daemon=True); thread.start()
        def request(method, route, data=None):
            connection = http.client.HTTPConnection('127.0.0.1', app.server_address[1], timeout=10)
            try:
                connection.request(method, route, body=json_bytes(data) if data is not None else None, headers={'Content-Type':'application/json'})
                response=connection.getresponse(); raw=response.read()
                self.assertEqual(response.status, 200, raw)
                return raw
            finally: connection.close()
        try:
            status=json.loads(request('GET','/api/status')); self.assertTrue(status['can_create_ttn'])
            search=json.loads(request('POST','/api/integration/search-invoices',{'filters':{'date_from':DAY,'date_to':DAY}}))
            self.assertEqual(len(search['rows']),3)
            drivers=json.loads(request('POST','/api/integration/driver-candidates',{'date_from':DAY,'date_to':DAY}))
            self.assertEqual(len(drivers['drivers']),1)
            saved=json.loads(request('POST','/api/integration/save-drivers',{'drivers':drivers['drivers']}))
            self.assertEqual(saved['count'],1)
            from invoice_processor import import_invoices
            self.assertEqual(len(import_invoices(request('GET','/api/example'),'sf_example_DEMO.xlsx')['rows']),3)
            data=json.loads(request('POST','/api/integration/prepare',{'rows':self.rows(3),'selected_ids':['row-2','row-3','row-4']}))
            ids=[item['job']['job_id'] for item in data['results']]
            created=json.loads(request('POST','/api/integration/create',{'job_ids':ids,'reviewed':True}))
            self.assertEqual([item['status'] for item in created['results']],['draft']*3)
            self.assertEqual(len(json.loads(request('GET','/api/integration/jobs'))['jobs']),3)
            report=request('GET','/api/integration/report').decode('utf-8-sig')
            self.assertEqual(len(report.splitlines()),4)
            self.assertTrue(request('GET','/api/integration/pdf?job_id='+ids[0]).startswith(b'%PDF-'))
            for secret in CREDENTIALS.values(): self.assertNotIn(secret,report)
        finally:
            app.shutdown(); app.server_close(); thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
