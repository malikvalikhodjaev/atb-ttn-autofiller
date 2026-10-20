"""Run a clearly labelled, loopback-only API demonstration with three invoices."""

from pathlib import Path
import argparse
import sys
import uuid
import webbrowser

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from app import make_server
from settings import save_settings
from test_integration import CREDENTIALS, configured_settings, start_gateway


def main():
    parser = argparse.ArgumentParser(description="Local demonstration only; no real Didox, Smartup or E-IMZO")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--api-port", type=int, default=8767)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    gateway, thread = start_gateway(args.api_port)
    configuration = ROOT / "local-data" / "demo" / uuid.uuid4().hex / "config.local.json"
    configuration.parent.mkdir(parents=True, exist_ok=True)
    save_settings(configuration, configured_settings(gateway.base))
    server = make_server(args.port, configuration)
    try:
        status = server.workflow.connect(CREDENTIALS)
        if not status["can_create_ttn"]:
            raise RuntimeError("Local demonstration connection failed")
        url = f"http://127.0.0.1:{server.server_address[1]}/"
        print("LOCAL API DEMONSTRATION ONLY: " + url, flush=True)
        print("Upload examples/integration_one.csv or examples/integration_three.csv. Real E-IMZO is disabled in this local demonstration.", flush=True)
        if not args.no_browser:
            webbrowser.open(url)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        gateway.shutdown(); gateway.server_close(); thread.join(timeout=2)


if __name__ == "__main__":
    main()
