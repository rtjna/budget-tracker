from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.main as main
from app.db import Base


def make_client(base_url="http://localhost"):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine)

    def get_db():
        db = maker()
        try:
            yield db
        finally:
            db.close()

    main.app.dependency_overrides[main.get_db] = get_db
    return TestClient(main.app, base_url=base_url)


# --- H1: TrustedHostMiddleware (DNS rebinding mitigation) ---


def test_localhost_and_loopback_hosts_allowed():
    assert make_client("http://localhost").get("/api/health").status_code == 200
    assert make_client("http://127.0.0.1").get("/api/health").status_code == 200
    # Port suffix is stripped before matching.
    assert make_client("http://localhost:8000").get("/api/health").status_code == 200


def test_foreign_host_rejected():
    client = make_client()
    r = client.get("/api/health", headers={"host": "rebind.attacker.example"})
    assert r.status_code == 400


# --- H2: X-Budget-App custom header required on state-changing requests ---

CSRF_HEADER = {"X-Budget-App": "1"}


def test_post_without_header_rejected():
    client = make_client()
    body = {"account_id": 0, "date": "2026-07-10", "description": "Coffee", "amount": -3.5}
    r = client.post("/api/transactions", json=body)
    assert r.status_code == 403
    assert "X-Budget-App" in r.json()["detail"]


def test_multipart_upload_without_header_rejected():
    # /api/imports takes multipart, which a plain HTML form could submit.
    client = make_client()
    r = client.post("/api/imports", files={"file": ("x.csv", b"Date,Description,Amount\n")})
    assert r.status_code == 403


def test_patch_and_delete_without_header_rejected():
    client = make_client()
    assert client.patch("/api/transactions/1", json={"category_id": None}).status_code == 403
    assert client.delete("/api/transactions/1").status_code == 403


def test_post_with_header_works():
    client = make_client()
    body = {"account_id": 0, "date": "2026-07-10", "description": "Coffee", "amount": -3.5}
    r = client.post("/api/transactions", json=body, headers=CSRF_HEADER)
    assert r.status_code == 200


def test_get_without_header_works():
    client = make_client()
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/transactions").status_code == 200
