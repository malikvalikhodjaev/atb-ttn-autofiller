"""Start the local TTN invoice-list processor with Python 3.10+."""

from __future__ import annotations

import argparse
import json
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from invoice_processor import MAX_UPLOAD_BYTES, ProcessingError, export_csv, export_selected_xlsx, import_invoices, validate_rows
from settings import SettingsError, load_settings, save_settings


ROOT = Path(__file__).resolve().parent
VERSION = "0.1.1"
STATIC = {
    "/": (ROOT / "web" / "index.html", "text/html; charset=utf-8"),
    "/app.js": (ROOT / "web" / "app.js", "text/javascript; charset=utf-8"),
    "/styles.css": (ROOT / "web" / "styles.css", "text/css; charset=utf-8"),
    "/api/template": (ROOT / "templates" / "sf_upload_template.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
}


class Handler(BaseHTTPRequestHandler):
    server_version = "TTNLocal/" + VERSION

    def log_message(self, format, *args):
        return

    def _send(self, status, data, content_type="application/json; charset=utf-8", filename=None):
        if isinstance(data, dict):
            data = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(data)

    def _allowed(self):
        port = self.server.server_address[1]
        allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if self.headers.get("Host", "") not in allowed_hosts:
            self._send(403, {"error": "Недопустимый адрес запроса."})
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {"http://" + host for host in allowed_hosts}:
            self._send(403, {"error": "Запрос разрешён только из локального окна обработчика."})
            return False
        return True

    def do_GET(self):
        if not self._allowed():
            return
        route = urlsplit(self.path).path
        if route == "/api/status":
            self._send(200, {"version": VERSION, "mode": "local", "didox_connected": False, "smartup_connected": False, "can_create_ttn": False})
        elif route == "/api/settings":
            try:
                self._send(200, {"settings": load_settings(self.server.settings_path)})
            except SettingsError as exc:
                self._send(400, {"error": str(exc)})
        elif route == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
        elif route in STATIC:
            file_path, content_type = STATIC[route]
            if not file_path.is_file():
                self._send(404, {"error": "Файл приложения не найден. Распакуйте весь архив обработчика."})
                return
            self._send(200, file_path.read_bytes(), content_type, "sf_upload_template.xlsx" if route == "/api/template" else None)
        else:
            self._send(404, {"error": "Страница не найдена."})

    def do_POST(self):
        if not self._allowed():
            return
        try:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ProcessingError("Некорректный размер запроса.") from exc
            if not 0 < length <= MAX_UPLOAD_BYTES:
                raise ProcessingError("Выберите непустой файл или список размером до 15 МБ.")
            self.connection.settimeout(15)
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise ProcessingError("Загрузка файла не завершена. Повторите её.")
            route = urlsplit(self.path)
            if route.path == "/api/import":
                params = parse_qs(route.query)
                filename = params.get("filename", [""])[0]
                sheet = params.get("sheet", [None])[0]
                self._send(200, import_invoices(raw, filename, sheet))
            elif route.path in ("/api/validate", "/api/export", "/api/settings"):
                try:
                    payload = json.loads(raw.decode("utf-8"), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
                except (ValueError, UnicodeDecodeError) as exc:
                    raise ProcessingError("Некорректные данные списка.") from exc
                if not isinstance(payload, dict):
                    raise ProcessingError("Некорректный формат запроса.")
                if route.path == "/api/settings":
                    self._send(200, {"settings": save_settings(self.server.settings_path, payload.get("settings"))})
                elif route.path == "/api/validate":
                    self._send(200, validate_rows(payload.get("rows")))
                else:
                    kind = payload.get("kind")
                    if kind not in ("report", "list"):
                        raise ProcessingError("Неизвестный вид выгрузки.")
                    selected = payload.get("selected_ids", [])
                    if not isinstance(selected, list) or any(not isinstance(item, str) for item in selected):
                        raise ProcessingError("Некорректный список выбранных строк.")
                    if kind == "report":
                        content = export_csv(payload.get("rows"), report=True)
                        self._send(200, content, "text/csv; charset=utf-8", "sf_validation_report.csv")
                    else:
                        template_path = ROOT / "templates" / "sf_upload_template.xlsx"
                        if not template_path.is_file():
                            raise ProcessingError("Шаблон не найден. Распакуйте весь архив обработчика.")
                        content = export_selected_xlsx(payload.get("rows"), selected, template_path.read_bytes())
                        self._send(200, content, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "sf_selected_list.xlsx")
            else:
                self._send(404, {"error": "Действие не найдено."})
        except (ProcessingError, SettingsError) as exc:
            self._send(400, {"error": str(exc), "sheets": getattr(exc, "sheets", [])})
        except (TimeoutError, ConnectionError):
            return
        except Exception:
            self._send(500, {"error": "Не удалось обработать файл. Проверьте формат и повторите загрузку."})


def make_server(port=8765, settings_path=None):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.settings_path = Path(settings_path) if settings_path is not None else ROOT / "config.local.json"
    return server


def check_installation(settings_path):
    failures = []
    if sys.version_info < (3, 10):
        failures.append("Нужен Python версии 3.10 или новее.")
    else:
        print(f"OK: Python {sys.version_info.major}.{sys.version_info.minor}")
    for path, _ in STATIC.values():
        if not path.is_file():
            failures.append("Нет файла: " + str(path.relative_to(ROOT)))
    if not failures:
        try:
            import_invoices((ROOT / "templates" / "sf_upload_template.xlsx").read_bytes(), "template.xlsx")
            print("OK: файлы интерфейса и шаблон Excel")
        except ProcessingError as exc:
            failures.append(str(exc))
    try:
        current = load_settings(settings_path)
        print("OK: локальные настройки" if settings_path.exists() else "OK: настройки можно заполнить в окне программы")
        print("Smartup: ссылка задана" if current["smartup_url"] else "Smartup: ссылка пока не задана")
    except SettingsError as exc:
        failures.append(str(exc))
    for failure in failures:
        print("ОШИБКА: " + failure)
    print("Интеграция Didox/Smartup и создание ТТН ещё требуют реализации.")
    return 1 if failures else 0


def main():
    parser = argparse.ArgumentParser(description="Local TTN invoice-list processor")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--check", action="store_true", help="Check local files and settings without starting a server")
    parser.add_argument("--config", type=Path, default=ROOT / "config.local.json")
    args = parser.parse_args()
    installation_result = check_installation(args.config)
    if args.check or installation_result:
        return installation_result
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")
    try:
        server = make_server(args.port, args.config)
    except OSError:
        if args.port != 8765:
            raise
        server = make_server(0, args.config)
    address = f"http://127.0.0.1:{server.server_address[1]}"
    print(f"TTN processor {VERSION}: {address}", flush=True)
    print("Local mode. Press Ctrl+C to stop.", flush=True)
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, args=(address,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    sys.exit(main())
