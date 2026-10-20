"""Create a source distribution from an explicit, reviewed file list."""

from pathlib import Path
import hashlib
import zipfile


ROOT = Path(__file__).resolve().parents[1]
RELEASE = "ttn-processor-0.1.1"
FILES = (
    ".gitignore", "README.md", "README.txt", "RUNBOOK.txt", "HANDOFF.md", "INTEGRATION.md",
    "examples/README.md", "examples/sf_demo.csv",
    "app.py", "invoice_processor.py", "settings.py", "start.cmd", "check.cmd",
    "config.example.json", "web/index.html", "web/styles.css", "web/app.js",
    "templates/sf_upload_template.xlsx", "tests/test_processor.py",
    "tests/test_http.py", "tests/test_settings.py", "tools/package_release.py",
)


def build_release(output_dir=None):
    output_dir = Path(output_dir) if output_dir is not None else ROOT / "dist"
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / (RELEASE + ".zip")
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in FILES:
            data = (ROOT / name).read_bytes()
            info = zipfile.ZipInfo(RELEASE + "/" + name, date_time=(2026, 10, 20, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    with zipfile.ZipFile(destination) as archive:
        if archive.testzip() is not None or len(archive.namelist()) != len(FILES):
            raise RuntimeError("Release archive verification failed")
        for name in FILES:
            if archive.read(RELEASE + "/" + name) != (ROOT / name).read_bytes():
                raise RuntimeError("Release file mismatch: " + name)
    checksum = hashlib.sha256(destination.read_bytes()).hexdigest()
    destination.with_suffix(".zip.sha256").write_text(checksum + "  " + destination.name + "\n", encoding="ascii")
    return destination


if __name__ == "__main__":
    print(build_release())
