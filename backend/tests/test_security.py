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
