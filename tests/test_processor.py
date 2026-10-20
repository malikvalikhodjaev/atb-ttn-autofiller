import csv
import io
import unittest
import zipfile
from datetime import date
from pathlib import Path
from xml.etree import ElementTree as ET

from invoice_processor import (
    MAX_UPLOAD_BYTES, HEADERS, ProcessingError, export_csv, export_selected_xlsx,
    import_invoices, normalize_date, validate_rows,
)

ROOT = Path(__file__).resolve().parents[1]
ID_A = "a123456789abcdef0123456789abcdef"
ID_B = "b123456789abcdef0123456789abcdef"
MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def row(source_row=2, **values):
    return {"source_row": source_row, "values": {field: values.get(field, "") for field in ("id", "number", "date", "tin", "comment")}, "source_issues": {}}


def make_xlsx(sheets, *, date1904=False, shared_strings=None):
    """Construct bounded XML fixtures to exercise independent XLSX variants."""
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        wb = ET.Element("workbook", {"xmlns": MAIN, "xmlns:r": REL})
        ET.SubElement(wb, "workbookPr", {"date1904": "1" if date1904 else "0"})
        sheet_list = ET.SubElement(wb, "sheets")
        relations = ET.Element("Relationships", {"xmlns": "http://schemas.openxmlformats.org/package/2006/relationships"})
        for number, (name, rows) in enumerate(sheets, 1):
            ET.SubElement(sheet_list, "sheet", {"name": name, "sheetId": str(number), "r:id": f"rId{number}"})
            ET.SubElement(relations, "Relationship", {"Id": f"rId{number}", "Target": f"worksheets/sheet{number}.xml", "Type": REL + "/worksheet"})
            worksheet = ET.Element("worksheet", {"xmlns": MAIN})
            data = ET.SubElement(worksheet, "sheetData")
            for source_number, values in enumerate(rows, 1):
                xml_row = ET.SubElement(data, "row", {"r": str(source_number)})
                for column, value in enumerate(values):
                    if value is None:
                        continue
                    spec = value if isinstance(value, dict) else {"type": "inlineStr", "value": str(value)}
                    cell = ET.SubElement(xml_row, "c", {"r": f"{chr(65 + column)}{source_number}", "t": spec["type"], "s": str(spec.get("style", 0))})
                    if spec["type"] == "inlineStr":
                        ET.SubElement(ET.SubElement(cell, "is"), "t").text = spec["value"]
                    elif spec["type"] == "formula":
                        cell.set("t", "n")
                        ET.SubElement(cell, "f").text = spec["value"]
                        ET.SubElement(cell, "v").text = spec.get("cached", "0")
                    else:
                        ET.SubElement(cell, "v").text = spec["value"]
            archive.writestr(f"xl/worksheets/sheet{number}.xml", ET.tostring(worksheet))
        archive.writestr("xl/workbook.xml", ET.tostring(wb))
        archive.writestr("xl/_rels/workbook.xml.rels", ET.tostring(relations))
        archive.writestr("xl/styles.xml", f'<styleSheet xmlns="{MAIN}"><cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="14"/></cellXfs></styleSheet>')
        if shared_strings is not None:
            shared = ET.Element("sst", {"xmlns": MAIN})
            for value in shared_strings:
                ET.SubElement(ET.SubElement(shared, "si"), "t").text = value
            archive.writestr("xl/sharedStrings.xml", ET.tostring(shared))
    return output.getvalue()


class ValidationTests(unittest.TestCase):
    def test_id_only_and_didox_link(self):
        data = validate_rows([row(id=ID_A.upper()), row(3, id=f"https://didox.uz/documents/{ID_B}")])
        self.assertEqual(data["summary"]["ready"], 2)
        self.assertEqual(data["rows"][0]["normalized"]["id"], ID_A)
        self.assertFalse(any(item["ttn_created"] for item in data["rows"]))

    def test_number_date_tin_and_leading_zeroes(self):
        data = validate_rows([row(number="00078/11", date="12.10.2026", tin="012345678")])
        self.assertEqual(data["summary"]["ready"], 1)
        self.assertEqual(data["rows"][0]["normalized"]["number"], "00078/11")
        self.assertEqual(data["rows"][0]["normalized"]["tin"], "012345678")
        self.assertEqual(data["rows"][0]["normalized"]["date"], "2026-10-12")

    def test_missing_and_invalid_fields(self):
        data = validate_rows([row(number="78", date="31.02.2026", tin="123"), row(3, id="broken", number="78", date="12.10.2026", tin="310196503")])
        self.assertEqual(data["summary"]["invalid"], 2)
        self.assertIn("date", data["rows"][0]["field_errors"])
        self.assertIn("id", data["rows"][1]["field_errors"])

    def test_an_id_does_not_hide_invalid_supplied_date(self):
        data = validate_rows([row(id=ID_A, date="tomorrow")])
        self.assertEqual(data["summary"]["invalid"], 1)

    def test_duplicate_id(self):
        data = validate_rows([row(id=ID_A), row(5, id=ID_A.upper())])
        self.assertEqual(data["summary"]["ready"], 1)
        self.assertEqual(data["rows"][1]["duplicate_of"], 2)

    def test_id_is_preferred_over_equivalent_fallback_row(self):
        fields = dict(number="78/11", date="12.10.2026", tin="310196503")
        data = validate_rows([row(**fields), row(6, id=ID_A, **fields)])
        self.assertEqual(data["rows"][0]["status"], "duplicate")
        self.assertEqual(data["rows"][0]["duplicate_of"], 6)
        self.assertEqual(data["rows"][1]["status"], "ready")

    def test_same_id_conflicting_dates_blocks_both(self):
        data = validate_rows([row(id=ID_A, date="12.10.2026"), row(3, id=ID_A, date="13.10.2026")])
        self.assertEqual(data["summary"]["conflict"], 2)
        self.assertEqual(data["summary"]["ready"], 0)

    def test_same_fallback_key_with_different_ids_blocks_both(self):
        fields = dict(number="78/11", date="12.10.2026", tin="310196503")
        data = validate_rows([row(id=ID_A, **fields), row(3, id=ID_B, **fields)])
        self.assertEqual(data["summary"]["conflict"], 2)

    def test_invalid_first_occurrence_does_not_hide_valid_later_row(self):
        data = validate_rows([row(id=ID_A, date="bad"), row(3, id=ID_A)])
        self.assertEqual([item["status"] for item in data["rows"]], ["invalid", "ready"])

    def test_record_ids_survive_empty_row_removal(self):
        first = validate_rows([row(id=ID_A), row(3, id=ID_B)])
        first["rows"][0]["values"] = {field: "" for field in first["rows"][0]["values"]}
        second = validate_rows(first["rows"])
        self.assertEqual(second["rows"][0]["record_id"], "row-3")

    def test_other_hosts_and_multiple_ids_are_rejected(self):
        for value in (f"https://didox.uz.evil.example/{ID_A}", f"https://didox.uz/{ID_A}/{ID_B}"):
            self.assertEqual(validate_rows([row(id=value)])["summary"]["invalid"], 1)

    def test_only_real_calendar_dates(self):
        self.assertEqual(normalize_date("29.02.2024"), "2024-02-29")
        with self.assertRaises(ValueError):
            normalize_date("29.02.2026")


class FileTests(unittest.TestCase):
    def test_actual_template_is_empty_and_notes_are_ignored(self):
        data = (ROOT / "templates/sf_upload_template.xlsx").read_bytes()
        result = import_invoices(data, "template.xlsx")
        self.assertEqual(result["summary"]["total"], 0)
        self.assertEqual(result["source"]["sheet"], "Список СФ")

    def test_numeric_excel_date(self):
        serial = str((date(2026, 10, 12) - date(1899, 12, 30)).days)
        blob = make_xlsx([("Список СФ", [HEADERS, ["", "00078/11", {"type": "n", "value": serial, "style": 1}, "012345678", ""]])])
        result = import_invoices(blob, "list.xlsx")
        self.assertEqual(result["summary"]["ready"], 1)
        self.assertEqual(result["rows"][0]["normalized"]["date"], "2026-10-12")

    def test_date1904_and_iso_cells(self):
        blob = make_xlsx([("Data", [HEADERS, ["", "1", {"type": "n", "value": "0"}, "310196503", ""], ["", "2", {"type": "d", "value": "2026-10-12T00:00:00Z"}, "310196503", ""]])], date1904=True)
        result = import_invoices(blob, "list.xlsx")
        self.assertEqual(result["rows"][0]["normalized"]["date"], "1904-01-01")
        self.assertEqual(result["rows"][1]["normalized"]["date"], "2026-10-12")

    def test_fake_excel_leap_day_and_fractional_dates_are_invalid(self):
        for serial in ("60", "46307.5"):
            blob = make_xlsx([("Data", [HEADERS, ["", "1", {"type": "n", "value": serial}, "310196503", ""]])])
            self.assertEqual(import_invoices(blob, "list.xlsx")["summary"]["invalid"], 1)

    def test_shared_strings_and_negative_shared_index(self):
        blob = make_xlsx([("Data", [["ID СФ"], [{"type": "s", "value": "0"}]])], shared_strings=[ID_A])
        self.assertEqual(import_invoices(blob, "list.xlsx")["summary"]["ready"], 1)
        broken = make_xlsx([("Data", [["ID СФ"], [{"type": "s", "value": "-1"}]])], shared_strings=[ID_A])
        with self.assertRaises(ProcessingError):
            import_invoices(broken, "list.xlsx")

    def test_formula_cache_is_not_used_as_invoice_number(self):
        blob = make_xlsx([("Data", [HEADERS, ["", {"type": "formula", "value": "1+1", "cached": "2"}, "12.10.2026", "310196503", ""]])])
        result = import_invoices(blob, "list.xlsx")
        self.assertEqual(result["summary"]["invalid"], 1)
        self.assertIn("формулу", result["rows"][0]["field_errors"]["number"])

    def test_numeric_id_and_number_accidentally_converted_to_date(self):
        blob = make_xlsx([("Data", [HEADERS, [{"type": "n", "value": "12345678901234567890123456789012"}, "", "", "", ""], [ID_B, {"type": "n", "value": "46307", "style": 1}, "", "", ""]])])
        result = import_invoices(blob, "list.xlsx")
        self.assertEqual(result["summary"]["invalid"], 2)

    def test_multiple_lists_require_explicit_choice(self):
        blob = make_xlsx([("A", [["ID СФ"], [ID_A]]), ("B", [["ID СФ"], [ID_B]])])
        with self.assertRaises(ProcessingError) as raised:
            import_invoices(blob, "list.xlsx")
        self.assertEqual(raised.exception.sheets, ["A", "B"])
        result = import_invoices(blob, "list.xlsx", "B")
        self.assertEqual(result["rows"][0]["normalized"]["id"], ID_B)

    def test_csv_encodings_delimiters_and_multiline_comments(self):
        stream = io.StringIO(newline="")
        writer = csv.writer(stream, delimiter=";")
        writer.writerow(HEADERS)
        writer.writerow(["", "00078/11", "12.10.2026", "012345678", "Комментарий; с разделителем\nи переносом"])
        for encoding in ("utf-8-sig", "cp1251"):
            result = import_invoices(stream.getvalue().encode(encoding), "list.csv")
            self.assertEqual(result["summary"]["ready"], 1)
            self.assertEqual(result["rows"][0]["source_row"], 2)
            self.assertIn("\n", result["rows"][0]["values"]["comment"])

    def test_bad_archive_header_and_size_are_rejected(self):
        for content, name in ((b"not a zip", "list.xlsx"), (b"old", "list.xls"), (b"", "list.csv"), (b"a" * (MAX_UPLOAD_BYTES + 1), "list.csv")):
            with self.assertRaises(ProcessingError):
                import_invoices(content, name)

    def test_selected_xlsx_roundtrip_preserves_text_and_actual_dates(self):
        values = [row(number="00078/11", date="12.10.2026", tin="012345678", comment='Текст "как есть"'), row(3, id=ID_B, number="+42", comment="<script>literal</script>")]
        template = (ROOT / "templates/sf_upload_template.xlsx").read_bytes()
        output = export_selected_xlsx(values, ["row-2", "row-3"], template)
        result = import_invoices(output, "selected.xlsx")
        self.assertEqual(result["summary"]["ready"], 2)
        self.assertEqual(result["rows"][0]["normalized"]["number"], "00078/11")
        self.assertEqual(result["rows"][0]["normalized"]["date"], "2026-10-12")
        self.assertEqual(result["rows"][0]["normalized"]["tin"], "012345678")
        self.assertEqual(result["rows"][1]["normalized"]["number"], "+42")
        self.assertEqual(result["rows"][1]["normalized"]["comment"], "<script>literal</script>")

    def test_export_does_not_include_invalid_or_duplicate_rows(self):
        values = [row(id=ID_A), row(3, id=ID_A), row(4, number="broken")]
        template = (ROOT / "templates/sf_upload_template.xlsx").read_bytes()
        output = export_selected_xlsx(values, ["row-2", "row-3", "row-4"], template)
        self.assertEqual(import_invoices(output, "selected.xlsx")["summary"]["total"], 1)

    def test_export_extends_table_past_template_rows(self):
        values = [row(index + 2, id=f"{index + 1:032x}") for index in range(125)]
        template = (ROOT / "templates/sf_upload_template.xlsx").read_bytes()
        output = export_selected_xlsx(values, [f"row-{index + 2}" for index in range(125)], template)
        self.assertEqual(import_invoices(output, "selected.xlsx")["summary"]["ready"], 125)

    def test_report_does_not_execute_spreadsheet_formulas(self):
        output = export_csv([row(id=ID_A, comment="=1+1")], report=True)
        rows = list(csv.reader(io.StringIO(output.decode("utf-8-sig")), delimiter=";"))
        self.assertEqual(rows[1][6], "'=1+1")
        self.assertEqual(rows[1][-1], "Не создавалась")


if __name__ == "__main__":
    unittest.main()
