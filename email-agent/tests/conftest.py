import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The schema fixture below drops every table, so the suite must never be able
# to reach a real database. setdefault would have handed it any inherited
# DATABASE_URL — running pytest in a shell that exported production's would
# have dropped it. The name is forced, and rejected unless it ends in _test.
_TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg2://localhost:5432/email_agent_test",
)
_db_name = _TEST_DATABASE_URL.rsplit("/", 1)[-1].split("?")[0]
if not _db_name.endswith("_test"):
    raise RuntimeError(
        f"refusing to run the test suite against database {_db_name!r}: "
        "the tests drop every table, so the name must end in '_test'. "
        "Set TEST_DATABASE_URL to a throwaway database."
    )
os.environ["DATABASE_URL"] = _TEST_DATABASE_URL
os.environ.setdefault("JWT_SECRET", "test-signing-key")
os.environ.setdefault("INTERNAL_SECRET_KEY", "test-internal-secret")
os.environ.setdefault("NYLAS_WEBHOOK_SECRET", "test-webhook-secret")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-telegram-token")
os.environ.setdefault("PRICE_TOLERANCE_CENTS", "0")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import main  # noqa: E402
from database import models  # noqa: E402
from database.connection import SessionLocal, engine, session_dependency  # noqa: E402
from services import events  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def schema():
    models.Base.metadata.drop_all(engine)
    models.Base.metadata.create_all(engine)
    yield
    models.Base.metadata.drop_all(engine)


@pytest.fixture
def session(schema):
    s = SessionLocal()
    for table in reversed(models.Base.metadata.sorted_tables):
        s.execute(table.delete())
    s.commit()
    try:
        yield s
        s.commit()
    finally:
        s.close()


@pytest.fixture
def account(session):
    acct = models.EmailAccount(
        company_id=1,
        nylas_grant_id="grant-1",
        email_address="dispatch@shipluxellc.com",
    )
    session.add(acct)
    session.commit()
    return acct


@pytest.fixture
def load_snapshot():
    return {
        "id": "load-uuid-1",
        "load_id": 55012,
        "pick_up_at": "Chicago, IL",
        "pick_up_zip": "60601",
        "pick_up_latitude": 41.8781,
        "pick_up_longitude": -87.6298,
        "pick_up_date": "2026-08-01T09:00:00",
        "deliver_to": "Detroit, MI",
        "deliver_zip": "48201",
        "delivery_date": "2026-08-02T14:00:00",
        "miles": 283,
        "vehicle_type": "Large Straight",
        "suggested_truck": "Large Straight",
        "pieces": 4,
        "weight": 6200,
        "dims": [96, 48, 60],
        "contact_name": "Acme Logistics",
        "contact_email": "broker@acme-logistics.com",
    }


@pytest.fixture
def negotiation(session, load_snapshot):
    n = models.Negotiation(
        company_id=1,
        load_uuid="load-uuid-1",
        load_snapshot=load_snapshot,
        driver_id=42,
        driver_amount=2400.0,
        driver_telegram_group_id="-1001234567890",
        dispatcher_user_id=7,
        bid_amount=3200.0,
        broker_email="broker@acme-logistics.com",
        broker_name="Acme Logistics",
        nylas_thread_id="thread-1",
        subject="Bid — Chicago, IL to Detroit, MI",
        status=models.BID_SENT,
    )
    session.add(n)
    session.commit()
    return n


@pytest.fixture
def company_profile():
    return {
        "id": 1,
        "name": "ShipLuxe LLC",
        "mc": "846834",
        "address": "10921 Reed Hartman Highway STE 323, Cincinnati, OH 45242",
        "email": "operation@shipluxellc.com",
        "phone_number": "630-426-3362",
        "logo_url": "https://cdn.example.com/logo.png",
        "bid_validity_minutes": 15,
    }


@pytest.fixture
def driver_profile():
    return {
        "id": 42,
        "full_name": "John Doe",
        "telegram_group_id": "-1001234567890",
        "current_latitude": 41.9,
        "current_longitude": -87.7,
        "vehicle": {
            "vehicle_type": "Large Straight",
            "length": 288,
            "width": 96,
            "height": 96,
            "payload": 12000,
            "ramps": "Yes",
            "equipments": [{"name": "Liftgate"}, {"name": "Pallet Jack"}],
        },
    }


@pytest.fixture
def client(session, monkeypatch):
    def override():
        yield session

    main.app.dependency_overrides[session_dependency] = override
    monkeypatch.setattr(events.hub, "publish", lambda e: None)
    with TestClient(main.app) as c:
        yield c
    main.app.dependency_overrides.clear()
