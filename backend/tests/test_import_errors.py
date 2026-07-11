"""M7: malformed uploads must produce friendly 422s, never 500s."""

import io

from openpyxl import Workbook

from tests.test_security import make_client as _make_client


def make_client():
    client = _make_client()
    client.headers.update({"X-Budget-App": "1"})  # CSRF header (H2)
    return client


def upload(client, name, payload: bytes):
    return client.post("/api/imports", files={"file": (name, payload)})


def test_corrupt_pdf_rejected_with_message():
    r = upload(make_client(), "statement.pdf", b"%PDF-1.7\nthis is not really a pdf")
    assert r.status_code == 422
    assert "PDF" in r.json()["detail"]


def test_truncated_pdf_rejected():
    r = upload(make_client(), "statement.pdf", b"%PDF-")
    assert r.status_code == 422


def test_latin1_csv_falls_back_and_imports():
    csv_latin1 = (
        "Date,Description,Amount\n09/07/2026,CAF\xc9 NERO,4.60\n".encode("latin-1")
    )
    r = upload(make_client(), "amex.csv", csv_latin1)
    assert r.status_code == 200
    assert r.json()["new"] == 1


def test_undecodable_binary_rejected_not_500():
    # UTF-16 survives the latin-1 fallback as NUL-ridden garbage; it must
    # come back as a clean 422 from importer detection, not a 500.
    payload = "Date,Description,Amount\n09/07/2026,TESCO,4.60\n".encode("utf-16")
    r = upload(make_client(), "weird.csv", payload)
    assert r.status_code == 422


def test_corrupt_xlsx_rejected_with_message():
    r = upload(make_client(), "sheet.xlsx", b"PK\x03\x04not really a zip archive")
    assert r.status_code == 422
    assert "Excel" in r.json()["detail"]


def test_empty_xlsx_rejected():
    buf = io.BytesIO()
    Workbook().save(buf)  # valid workbook, one empty sheet
    r = upload(make_client(), "empty.xlsx", buf.getvalue())
    assert r.status_code == 422
