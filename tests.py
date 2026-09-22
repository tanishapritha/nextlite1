import os
import json
import sqlite3
import datetime
import pytest
from unittest.mock import MagicMock
from fastapi.testclient import TestClient

# Set environment variables for testing environment
os.environ["META_VERIFY_TOKEN"] = "nextlite-demo"
os.environ["META_GRAPH_API_VERSION"] = "v26.0"
os.environ["GLAZE_WHATSAPP_TOKEN"] = "glaze-test-whatsapp-token"
os.environ["GLAZE_CRM_TENANT_KEY"] = "glaze-test-crm-tenant-key"

import app

sent_messages = []


def mock_send_payload(payload, client_id=None):
    sent_messages.append(payload)
    return True


@pytest.fixture(autouse=True)
def setup_test_environment(tmp_path, monkeypatch):
    """
    Pytest fixture providing complete test isolation:
    - Creates a fresh temporary SQLite DB for every test
    - Mocks external network sending, Gemini, and CRM HTTP calls
    """
    db_file = tmp_path / "test_nextlite.db"
    monkeypatch.setattr(app, "DB_PATH", str(db_file))
    app.init_db()

    global sent_messages
    sent_messages = []

    # Mock outbound Meta payload sending
    monkeypatch.setattr(app, "_send_payload", mock_send_payload)
    monkeypatch.setattr(app.MetaCloudProvider, "_send_payload", mock_send_payload)

    yield


@pytest.fixture
def test_client():
    return TestClient(app.app)


TEST_PHONE = "+919999988888"


# ============================================================
# 1. APPLICATION IMPORT & ROUTE AUDIT
# ============================================================

def test_01_app_compilation_and_routes(test_client):
    r_root = test_client.get("/")
    r_health = test_client.get("/health")
    assert r_root.status_code == 200
    assert r_root.json().get("service") == "Glaze Dental Clinic WhatsApp AI"
    assert r_health.status_code == 200
    assert r_health.json().get("database") == "connected"


# ============================================================
# 2. WEBHOOK VERIFICATION
# ============================================================

def test_02_webhook_verification(test_client):
    # Valid verify token
    r_ok = test_client.get("/webhook?hub.mode=subscribe&hub.verify_token=nextlite-demo&hub.challenge=test_123")
    assert r_ok.status_code == 200
    assert r_ok.text == "test_123"

    # Invalid verify token
    r_bad = test_client.get("/webhook?hub.mode=subscribe&hub.verify_token=wrong-token&hub.challenge=test_123")
    assert r_bad.status_code == 403

    # Missing parameters
    r_missing = test_client.get("/webhook")
    assert r_missing.status_code == 403


# ============================================================
# 3. WEBHOOK PAYLOAD PARSING & UNHANDLED TYPES
# ============================================================

def test_03_webhook_payload_parsing(test_client):
    text_payload = {
        "object": "whatsapp_business_account",
        "entry": [{
            "changes": [{
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"phone_number_id": "1208541249018781"},
                    "messages": [{
                        "id": "wamid.TXT1",
                        "from": TEST_PHONE,
                        "type": "text",
                        "text": {"body": "hi"}
                    }]
                }
            }]
        }]
    }
    r_txt = test_client.post("/webhook", json=text_payload)
    assert r_txt.status_code == 200
    assert r_txt.json().get("processed") == 1

    unsupp_payload = {
        "object": "whatsapp_business_account",
        "entry": [{
            "changes": [{
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"phone_number_id": "1208541249018781"},
                    "messages": [{
                        "id": "wamid.STK1",
                        "from": TEST_PHONE,
                        "type": "sticker",
                        "sticker": {"id": "stk_999"}
                    }]
                }
            }]
        }]
    }
    r_stk = test_client.post("/webhook", json=unsupp_payload)
    assert r_stk.status_code == 200
    assert r_stk.json().get("processed") == 1


# ============================================================
# 4. META MESSAGE IDEMPOTENCY
# ============================================================

def test_04_webhook_idempotency(test_client):
    payload = {
        "object": "whatsapp_business_account",
        "entry": [{
            "changes": [{
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {"phone_number_id": "1208541249018781"},
                    "messages": [{
                        "id": "wamid.DUP123",
                        "from": TEST_PHONE,
                        "type": "text",
                        "text": {"body": "hi"}
                    }]
                }
            }]
        }]
    }

    sent_messages.clear()
    r1 = test_client.post("/webhook", json=payload)
    assert r1.status_code == 200
    assert r1.json().get("processed") == 1
    assert len(sent_messages) == 1

    sent_messages.clear()
    r2 = test_client.post("/webhook", json=payload)
    assert r2.status_code == 200
    assert r2.json().get("ignored_duplicates") == 1
    assert len(sent_messages) == 0  # Second delivery ignored, no duplicate message sent


# ============================================================
# 5. CRM GET SLOTS TESTS (Success, Empty, Timeout, 500, Headers)
# ============================================================

def test_05_crm_get_slots_http_integration(monkeypatch):
    captured_requests = []

    def mock_requests_get(url, params=None, headers=None, timeout=15):
        captured_requests.append({"url": url, "params": params, "headers": headers})

        class MockResponse:
            status_code = 200

            def json(self):
                return {"status": "success", "availableSlots": ["10:00 AM", "11:00 AM"]}

        return MockResponse()

    monkeypatch.setattr(app.requests, "get", mock_requests_get)

    slots = app.CRMClient.get_available_slots("glaze-dental", "2026-09-21")

    assert slots == ["10:00 AM", "11:00 AM"]
    assert len(captured_requests) == 1
    assert "https://vanifyai.online/api/v1/integrations/whatsapp/slots" in captured_requests[0]["url"]
    assert captured_requests[0]["params"]["date"] == "2026-09-21"
    assert captured_requests[0]["headers"]["X-Tenant-Key"] == os.environ["GLAZE_CRM_TENANT_KEY"]


def test_05b_crm_get_slots_empty_and_failures(monkeypatch):
    # Empty slots
    monkeypatch.setattr(app.requests, "get", lambda *a, **k: type("R", (), {"status_code": 200, "json": lambda s: {"availableSlots": []}})())
    slots_empty = app.CRMClient.get_available_slots("glaze-dental", "2026-09-21")
    assert slots_empty == []

    # Timeout
    def mock_timeout(*a, **k):
        raise app.requests.RequestException("Timeout")

    monkeypatch.setattr(app.requests, "get", mock_timeout)
    slots_timeout = app.CRMClient.get_available_slots("glaze-dental", "2026-09-21")
    assert slots_timeout is None

    # HTTP 500
    monkeypatch.setattr(app.requests, "get", lambda *a, **k: type("R", (), {"status_code": 500, "text": "Internal Error"})())
    slots_500 = app.CRMClient.get_available_slots("glaze-dental", "2026-09-21")
    assert slots_500 is None


# ============================================================
# 6. CRM BOOKING 201 SUCCESS & PAYLOAD / HEADER VERIFICATION
# ============================================================

def test_06_crm_booking_201_success(monkeypatch):
    captured_posts = []

    def mock_requests_post(url, headers=None, json=None, timeout=20):
        captured_posts.append({"url": url, "headers": headers, "json": json})

        class MockResponse:
            status_code = 201
            text = '{"appointmentId": "CRM-201-OK"}'

            def json(self):
                return {"appointmentId": "CRM-201-OK"}

        return MockResponse()

    monkeypatch.setattr(app.requests, "post", mock_requests_post)

    res = app.CRMClient.book_appointment(
        client_id="glaze-dental",
        customer_name="Rahul Sharma",
        customer_phone=TEST_PHONE,
        booking_date="2026-09-21",
        booking_time="10:00 AM"
    )

    assert res["status"] == "success"
    assert res["data"]["appointmentId"] == "CRM-201-OK"
    assert len(captured_posts) == 1    assert "https://vanifyai.online/api/v1/integrations/whatsapp/appointments/book" in captured_posts[0]["url"]
    assert captured_posts[0]["headers"]["X-Tenant-Key"] == os.environ["GLAZE_CRM_TENANT_KEY"]

    payload = captured_posts[0]["json"]
    assert payload["customerName"] == "Rahul Sharma"
    assert payload["customerPhone"] == TEST_PHONE
    assert payload["age"] == "24"
    assert payload["bookingDate"] == "2026-09-21"
    assert payload["bookingTime"] == "10:00 AM"
    assert payload["title"] == "WhatsApp Consultation"


# ============================================================
# 7. BOOKING CONFIRMATION STOP (NO WELCOME LOOP)
# ============================================================

def test_07_booking_confirmation_stop_no_welcome_loop(monkeypatch):
    app.reset_conversation(TEST_PHONE)

    monkeypatch.setattr(app, "get_available_slots", lambda c, d: ["10:00 AM"])
    monkeypatch.setattr(app.CRMClient, "get_available_slots", lambda c, d: ["10:00 AM"])
    monkeypatch.setattr(app, "book_appointment", lambda **kw: {"status": "success", "data": {"appointmentId": "CRM-777"}})
    monkeypatch.setattr(app.CRMClient, "book_appointment", lambda **kw: {"status": "success", "data": {"appointmentId": "CRM-777"}})

    mon_dt = datetime.date.today() + datetime.timedelta(days=(0 - datetime.date.today().weekday()) % 7 or 7)

    app.handle_user_message(TEST_PHONE, "interactive", "📅 Book appointment", action_id="book_appointment")
    app.handle_user_message(TEST_PHONE, "interactive", "Appointment", action_id="booking_service_0")
    app.handle_user_message(TEST_PHONE, "text", "Rahul Sharma")
    app.handle_user_message(TEST_PHONE, "text", "24")
    app.handle_user_message(TEST_PHONE, "interactive", "New patient", action_id="patient_new")
    app.handle_user_message(TEST_PHONE, "text", mon_dt.isoformat())
    app.handle_user_message(TEST_PHONE, "interactive", "10:00 AM", action_id="time_10:00 AM")

    sent_messages.clear()
    app.handle_user_message(TEST_PHONE, "interactive", "Confirm", action_id="confirm_booking")

    conv_done = app.get_conversation(TEST_PHONE)
    assert conv_done["state"] == "IDLE"
    assert len(sent_messages) == 1  # Confirmation ONLY
    assert "Your appointment has been confirmed" in sent_messages[0]["text"]["body"]
    assert "Welcome to Glaze" not in sent_messages[0]["text"]["body"]  # NO WELCOME MENU LOOP!


# ============================================================
# 8. CRM 409 CONFLICT HANDLING
# ============================================================

def test_08_crm_409_conflict_handling(monkeypatch):
    app.reset_conversation(TEST_PHONE)

    mon_dt = datetime.date.today() + datetime.timedelta(days=(0 - datetime.date.today().weekday()) % 7 or 7)

    monkeypatch.setattr(app, "get_available_slots", lambda c, d: ["10:00 AM", "12:00 PM"])
    monkeypatch.setattr(app.CRMClient, "get_available_slots", lambda c, d: ["10:00 AM", "12:00 PM"])

    app.handle_user_message(TEST_PHONE, "interactive", "📅 Book appointment", action_id="book_appointment")
    app.handle_user_message(TEST_PHONE, "interactive", "Appointment", action_id="booking_service_0")
    app.handle_user_message(TEST_PHONE, "text", "Rahul Sharma")
    app.handle_user_message(TEST_PHONE, "text", "24")
    app.handle_user_message(TEST_PHONE, "interactive", "New patient", action_id="patient_new")
    app.handle_user_message(TEST_PHONE, "text", mon_dt.isoformat())
    app.handle_user_message(TEST_PHONE, "interactive", "10:00 AM", action_id="time_10:00 AM")

    monkeypatch.setattr(app, "book_appointment", lambda **kw: {"status": "conflict", "data": {}})
    monkeypatch.setattr(app.CRMClient, "book_appointment", lambda **kw: {"status": "conflict", "data": {}})
    monkeypatch.setattr(app, "get_available_slots", lambda c, d: ["12:00 PM"])
    monkeypatch.setattr(app.CRMClient, "get_available_slots", lambda c, d: ["12:00 PM"])

    sent_messages.clear()
    app.handle_user_message(TEST_PHONE, "interactive", "Confirm", action_id="confirm_booking")

    conv_conflict = app.get_conversation(TEST_PHONE)
    assert conv_conflict["state"] == "BOOKING_TIME"
    assert len(sent_messages) >= 2
    assert "already booked" in sent_messages[0]["text"]["body"].lower()


# ============================================================
# 9. CRM OTHER FAILURES (401, 403, 429, 500) & NO CREDENTIAL LEAKAGE
# ============================================================

def test_09_crm_other_failures_no_leakage(monkeypatch):
    app.reset_conversation(TEST_PHONE)

    mon_dt = datetime.date.today() + datetime.timedelta(days=(0 - datetime.date.today().weekday()) % 7 or 7)

    monkeypatch.setattr(app, "get_available_slots", lambda c, d: ["10:00 AM"])
    monkeypatch.setattr(app.CRMClient, "get_available_slots", lambda c, d: ["10:00 AM"])

    app.handle_user_message(TEST_PHONE, "interactive", "📅 Book appointment", action_id="book_appointment")
    app.handle_user_message(TEST_PHONE, "interactive", "Appointment", action_id="booking_service_0")
    app.handle_user_message(TEST_PHONE, "text", "Rahul Sharma")
    app.handle_user_message(TEST_PHONE, "text", "24")
    app.handle_user_message(TEST_PHONE, "interactive", "New patient", action_id="patient_new")
    app.handle_user_message(TEST_PHONE, "text", mon_dt.isoformat())
    app.handle_user_message(TEST_PHONE, "interactive", "10:00 AM", action_id="time_10:00 AM")

    monkeypatch.setattr(app, "book_appointment", lambda **kw: {"status": "error", "data": {}})
    monkeypatch.setattr(app.CRMClient, "book_appointment", lambda **kw: {"status": "error", "data": {}})

    sent_messages.clear()
    app.handle_user_message(TEST_PHONE, "interactive", "Confirm", action_id="confirm_booking")

    conv_err = app.get_conversation(TEST_PHONE)
    assert conv_err["state"] == "IDLE"
    assert len(sent_messages) == 1
    text_out = sent_messages[0]["text"]["body"]
    assert "9822977740" in text_out
    assert "6b4b6128" not in text_out  # No tenant secret leakage
    assert "azure" not in text_out.lower()  # No internal CRM URL leakage


# ============================================================
# 10. DOUBLE BOOKING PROTECTION
# ============================================================

def test_10_double_booking_protection(monkeypatch):
    conn = app.get_db()
    conn.execute("DELETE FROM appointments")
    conn.commit()

    mon_dt = datetime.date.today() + datetime.timedelta(days=(0 - datetime.date.today().weekday()) % 7 or 7)

    monkeypatch.setattr(app, "get_available_slots", lambda c, d: ["10:00 AM"])
    monkeypatch.setattr(app.CRMClient, "get_available_slots", lambda c, d: ["10:00 AM"])
    monkeypatch.setattr(app, "book_appointment", lambda **kw: {"status": "success", "data": {"appointmentId": "APT-1"}})
    monkeypatch.setattr(app.CRMClient, "book_appointment", lambda **kw: {"status": "success", "data": {"appointmentId": "APT-1"}})

    app.reset_conversation("+911111111111")
    app.handle_user_message("+911111111111", "interactive", "📅 Book appointment", action_id="book_appointment")
    app.handle_user_message("+911111111111", "interactive", "Appointment", action_id="booking_service_0")
    app.handle_user_message("+911111111111", "text", "Patient One")
    app.handle_user_message("+911111111111", "text", "31")
    app.handle_user_message("+911111111111", "interactive", "New patient", action_id="patient_new")
    app.handle_user_message("+911111111111", "text", mon_dt.isoformat())
    app.handle_user_message("+911111111111", "interactive", "10:00 AM", action_id="time_10:00 AM")
    app.handle_user_message("+911111111111", "interactive", "Confirm", action_id="confirm_booking")

    # Second booking returns 409 Conflict
    monkeypatch.setattr(app, "book_appointment", lambda **kw: {"status": "conflict", "data": {}})
    monkeypatch.setattr(app.CRMClient, "book_appointment", lambda **kw: {"status": "conflict", "data": {}})

    app.reset_conversation("+912222222222")
    app.handle_user_message("+912222222222", "interactive", "📅 Book appointment", action_id="book_appointment")
    app.handle_user_message("+912222222222", "interactive", "Appointment", action_id="booking_service_0")
    app.handle_user_message("+912222222222", "text", "Patient Two")
    app.handle_user_message("+912222222222", "text", "32")
    app.handle_user_message("+912222222222", "interactive", "New patient", action_id="patient_new")
    app.handle_user_message("+912222222222", "text", mon_dt.isoformat())
    app.handle_user_message("+912222222222", "interactive", "10:00 AM", action_id="time_10:00 AM")
    app.handle_user_message("+912222222222", "interactive", "Confirm", action_id="confirm_booking")

    row_count = conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0]
    conn.close()
    assert row_count == 1  # Only 1 successful local appointment saved


# ============================================================
# 11. CRM SUCCESS + WHATSAPP OUTBOUND FAILURE PROTECTION
# ============================================================

def test_11_crm_success_whatsapp_failure_protection(monkeypatch):
    conn = app.get_db()
    conn.execute("DELETE FROM appointments")
    conn.commit()

    mon_dt = datetime.date.today() + datetime.timedelta(days=(0 - datetime.date.today().weekday()) % 7 or 7)

    monkeypatch.setattr(app, "get_available_slots", lambda c, d: ["10:00 AM"])
    monkeypatch.setattr(app.CRMClient, "get_available_slots", lambda c, d: ["10:00 AM"])

    book_call_count = 0

    def mock_book(**kw):
        nonlocal book_call_count
        book_call_count += 1
        return {"status": "success", "data": {"appointmentId": "APT-99"}}

    monkeypatch.setattr(app, "book_appointment", mock_book)
    monkeypatch.setattr(app.CRMClient, "book_appointment", mock_book)
    monkeypatch.setattr(app, "_send_payload", lambda p, client_id=None: False)

    app.reset_conversation(TEST_PHONE)
    app.handle_user_message(TEST_PHONE, "interactive", "📅 Book appointment", action_id="book_appointment")
    app.handle_user_message(TEST_PHONE, "interactive", "Appointment", action_id="booking_service_0")
    app.handle_user_message(TEST_PHONE, "text", "Test Patient")
    app.handle_user_message(TEST_PHONE, "text", "29")
    app.handle_user_message(TEST_PHONE, "interactive", "New patient", action_id="patient_new")
    app.handle_user_message(TEST_PHONE, "text", mon_dt.isoformat())
    app.handle_user_message(TEST_PHONE, "interactive", "10:00 AM", action_id="time_10:00 AM")
    app.handle_user_message(TEST_PHONE, "interactive", "Confirm", action_id="confirm_booking")

    row_count = conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0]
    conn.close()
    assert book_call_count == 1
    assert row_count == 1  # Appointment remains saved; no duplicate CRM booking retry


# ============================================================
# 12. GET APPOINTMENTS & CANCEL ENDPOINT INTERFACE TESTS
# ============================================================

def test_12_crm_get_appointments_and_cancel_interface(test_client, monkeypatch):
    captured_get = []
    captured_post = []

    def mock_get(url, params=None, headers=None, timeout=15):
        captured_get.append({"url": url, "params": params, "headers": headers})
        class MockR:
            status_code = 200
            text = '[{"id": "APT-101", "customerName": "John"}]'
            def json(self): return [{"id": "APT-101", "customerName": "John"}]
        return MockR()

    def mock_post(url, headers=None, json=None, timeout=20):
        captured_post.append({"url": url, "headers": headers, "json": json})
        class MockR:
            status_code = 200
            text = '{"status": "cancelled"}'
            def json(self): return {"status": "cancelled"}
        return MockR()

    monkeypatch.setattr(app.requests, "get", mock_get)
    monkeypatch.setattr(app.requests, "post", mock_post)

    # Test GET appointments via proxy
    r_apts = test_client.get("/api/appointments?client_id=glaze-dental")
    assert r_apts.status_code == 200
    assert len(r_apts.json()) == 1
    assert captured_get[0]["headers"]["X-Tenant-Key"] == "6b4b6128-5b5f-4d2f-b5de-91511ab9b120"

    # Test Cancel interface method
    cancel_res = app.cancel_appointment("glaze-dental", {"appointmentId": "APT-101"})
    assert cancel_res["status"] == "success"
    assert captured_post[0]["headers"]["X-Tenant-Key"] == "6b4b6128-5b5f-4d2f-b5de-91511ab9b120"
    assert "/appointments/cancel" in captured_post[0]["url"]


# ============================================================
# 13. TENANT ISOLATION & DYNAMIC TENANT CONFIGURATION
# ============================================================

def test_13_tenant_isolation_multi_client(monkeypatch):
    app.save_client_crm_config("client-a", "Client A", "https://crm-a.example", "tenant-key-A", phone_number_id="phone_A")
    app.save_client_crm_config("client-b", "Client B", "https://crm-b.example", "tenant-key-B", phone_number_id="phone_B")

    conn = app.get_db()
    conn.execute(
        "UPDATE client_config SET whatsapp_token_env = ?, crm_tenant_key_env = ? WHERE client_id = ?",
        ("CLIENT_A_WHATSAPP_TOKEN", "CLIENT_A_CRM_KEY", "client-a")
    )
    conn.execute(
        "UPDATE client_config SET whatsapp_token_env = ?, crm_tenant_key_env = ? WHERE client_id = ?",
        ("CLIENT_B_WHATSAPP_TOKEN", "CLIENT_B_CRM_KEY", "client-b")
    )
    conn.commit()    conn.close()

    monkeypatch.setenv("CLIENT_A_WHATSAPP_TOKEN", "token-A")
    monkeypatch.setenv("CLIENT_B_WHATSAPP_TOKEN", "token-B")
    monkeypatch.setenv("CLIENT_A_CRM_KEY", "crm-key-A")
    monkeypatch.setenv("CLIENT_B_CRM_KEY", "crm-key-B")

    captured_requests = []

    def mock_get(url, params=None, headers=None, timeout=15):
        captured_requests.append({"url": url, "headers": headers})
        class MockR:
            status_code = 200
            text = '{"availableSlots": ["10:00 AM"]}'
            def json(self): return {"availableSlots": ["10:00 AM"]}
        return MockR()

    monkeypatch.setattr(app.requests, "get", mock_get)

    app.CRMClient.get_available_slots("client-a", "2026-09-21")
    app.CRMClient.get_available_slots("client-b", "2026-09-21")

    assert "https://crm-a.example" in captured_requests[0]["url"]
    assert captured_requests[0]["headers"]["X-Tenant-Key"] == "crm-key-A"
    assert "https://crm-b.example" in captured_requests[1]["url"]
    assert captured_requests[1]["headers"]["X-Tenant-Key"] == "crm-key-B"

    calls = []
    def capture_post(url, headers=None, json=None, timeout=15):
        calls.append({"url": url, "headers": headers})
        return type("R", (), {"status_code": 200, "text": "{}", "raise_for_status": lambda self: None})()
    monkeypatch.setattr(app.requests, "post", capture_post)

    assert app.MetaCloudProvider._send_payload({"to": "111"}, client_id="client-a") is True
    assert app.MetaCloudProvider._send_payload({"to": "222"}, client_id="client-b") is True
    assert "phone_A" in calls[0]["url"]
    assert calls[0]["headers"]["Authorization"] == "Bearer token-A"
    assert "phone_B" in calls[1]["url"]
    assert calls[1]["headers"]["Authorization"] == "Bearer token-B"

    # The same customer phone can have independent conversation state per tenant.
    app.update_conversation("+919000000000", client_id="client-a", state="BOOKING_NAME")
    app.update_conversation("+919000000000", client_id="client-b", state="BOOKING_DATE")
    assert app.get_conversation("+919000000000", "client-a")["state"] == "BOOKING_NAME"
    assert app.get_conversation("+919000000000", "client-b")["state"] == "BOOKING_DATE"


def test_14_webhook_rejects_unknown_tenant(test_client):
    payload = {
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"value": {
            "metadata": {"phone_number_id": "unknown-phone-id"},
            "messages": [{
                "id": "wamid.UNKNOWN1",
                "from": TEST_PHONE,
                "type": "text",
                "text": {"body": "hi"}
            }]
        }}]}]
    }
    r = test_client.post("/webhook", json=payload)
    assert r.status_code == 200
    assert r.json()["processed"] == 0
    assert len(sent_messages) == 0


def test_15_webhook_signature_verification(test_client, monkeypatch):
    import hashlib
    import hmac

    monkeypatch.setattr(app, "META_APP_SECRET", "test-app-secret")
    raw = b'{"object":"whatsapp_business_account","entry":[]}'
    signature = "sha256=" + hmac.new(b"test-app-secret", raw, hashlib.sha256).hexdigest()

    ok = test_client.post(
        "/webhook",
        content=raw,
        headers={"content-type": "application/json", "x-hub-signature-256": signature}
    )
    assert ok.status_code == 200

    bad = test_client.post(
        "/webhook",
        content=raw,
        headers={"content-type": "application/json", "x-hub-signature-256": "sha256=bad"}
    )
    assert bad.status_code == 401


# ============================================================
# 14. EMERGENCY INTERCEPTOR PRIORITY
# ============================================================

def test_14_emergency_interceptor_priority():
    for emergency_kw in ["accident", "severe pain", "extreme pain", "allergy", "urgent"]:
        app.reset_conversation(TEST_PHONE)
        sent_messages.clear()
        app.handle_user_message(TEST_PHONE, "text", f"Emergency! I have {emergency_kw}")
        assert len(sent_messages) == 1
        assert "9822977740" in sent_messages[0]["text"]["body"]
        assert app.get_conversation(TEST_PHONE)["state"] == "IDLE"

    # Emergency during active booking state
    app.reset_conversation(TEST_PHONE)
    app.handle_user_message(TEST_PHONE, "interactive", "📅 Book appointment", action_id="book_appointment")
    app.handle_user_message(TEST_PHONE, "interactive", "Appointment", action_id="booking_service_0")
    assert app.get_conversation(TEST_PHONE)["state"] == "BOOKING_NAME"

    sent_messages.clear()
    app.handle_user_message(TEST_PHONE, "text", "Emergency! I had an accident")
    assert len(sent_messages) == 1
    assert "9822977740" in sent_messages[0]["text"]["body"]
    assert app.get_conversation(TEST_PHONE)["state"] == "IDLE"


# ============================================================
# 15. PRICING SAFETY (NO INVENTED PRICES)
# ============================================================

def test_15_pricing_safety():
    for q in ["How much is RCT?", "What is the cost?", "How much does treatment cost?"]:
        app.reset_conversation(TEST_PHONE)
        sent_messages.clear()
        app.handle_user_message(TEST_PHONE, "text", q)
        assert len(sent_messages) >= 1
        assert "Pricing details are not listed" in sent_messages[0]["text"]["body"]
        assert "9822977740" in sent_messages[0]["text"]["body"]


# ============================================================
# 16. DATE & TIME VALIDATION (PAST DATES & SUNDAYS)
# ============================================================

def test_16_date_time_validation():
    app.reset_conversation(TEST_PHONE)
    app.handle_user_message(TEST_PHONE, "interactive", "📅 Book appointment", action_id="book_appointment")
    app.handle_user_message(TEST_PHONE, "interactive", "Appointment", action_id="booking_service_0")
    app.handle_user_message(TEST_PHONE, "text", "Rahul Sharma")
    app.handle_user_message(TEST_PHONE, "text", "24")
    app.handle_user_message(TEST_PHONE, "interactive", "New patient", action_id="patient_new")

    # Past date
    sent_messages.clear()
    app.handle_user_message(TEST_PHONE, "text", "2019-05-10")
    assert len(sent_messages) == 2
    assert "past dates" in sent_messages[0]["text"]["body"].lower()

    # Sunday
    sent_messages.clear()
    sunday_dt = datetime.date.today() + datetime.timedelta(days=(6 - datetime.date.today().weekday()) % 7 or 7)
    app.handle_user_message(TEST_PHONE, "text", sunday_dt.isoformat())
    assert len(sent_messages) == 2
    assert "closed on sundays" in sent_messages[0]["text"]["body"].lower()


# ============================================================
# 17. CUSTOMER-FACING BRANDING & SERVICE AUDIT
# ============================================================

def test_17_customer_facing_branding_and_services():
    services = app.get_configured_services()
    assert len(services) == 2

    app.reset_conversation(TEST_PHONE)
    sent_messages.clear()
    app.handle_user_message(TEST_PHONE, "text", "hi")

    text_content = sent_messages[0]["interactive"]["body"]["text"]
    assert "Glaze Dental Clinic" in text_content
    assert "Nextlite" not in text_content
    assert "nextlite1" not in text_content
    assert "SmileCare" not in text_content
    assert "Meta" not in text_content


# ============================================================
# 19. APPOINTMENT REMINDERS & SCHEDULER
# ============================================================

def test_19_appointment_reminders():
    conn = app.get_db()
    conn.execute("DELETE FROM reminders")
    conn.commit()
    conn.close()

    today_str = datetime.date.today().isoformat()
    scheduled = app.schedule_appointment_reminder(
        appointment_id=888,
        client_id="glaze-dental",
        phone=TEST_PHONE,
        appointment_date=today_str,
        appointment_time="12:00 PM"
    )
    assert scheduled is True

    # Uniqueness constraint: duplicate call ignores insert
    scheduled_dup = app.schedule_appointment_reminder(
        appointment_id=888,
        client_id="glaze-dental",
        phone=TEST_PHONE,
        appointment_date=today_str,
        appointment_time="12:00 PM"
    )
    assert scheduled_dup is True

    # Force reminder to past to test due worker
    conn = app.get_db()
    past_iso = (datetime.datetime.now(app.TIMEZONE_KOLKATA) - datetime.timedelta(minutes=5)).isoformat()
    conn.execute("UPDATE reminders SET scheduled_at = ? WHERE appointment_id = 888", (past_iso,))
    conn.commit()
    conn.close()

    sent_messages.clear()
    sent_count = app.process_due_reminders()
    assert sent_count == 1
    assert len(sent_messages) >= 1


# ============================================================
# 20. SECURITY & CREDENTIAL EXPOSURE
# ============================================================

def test_20_security_credentials_and_logging(test_client):
    r_cfg = test_client.get("/api/client-config?client_id=glaze-dental")
    assert r_cfg.status_code == 200
    data = r_cfg.json()
    assert data["crm_tenant_configured"] is True
    assert "crm_tenant_id" not in data
    assert "GLAZE_CRM_TENANT_KEY" not in json.dumps(data)
    assert "GLAZE_WHATSAPP_TOKEN" not in json.dumps(data)


# ============================================================
# 19. SERVICES MENU -> BOOKING DATE (NO SILENT STOP)
# ============================================================
def test_19_services_selection_continues_to_booking_date(monkeypatch):
    app.reset_conversation(TEST_PHONE)
    sent_messages.clear()

    app.handle_user_message(TEST_PHONE, "interactive", "🦷 Our services", action_id="services")
    assert len(sent_messages) == 1

    sent_messages.clear()
    app.handle_user_message(TEST_PHONE, "interactive", "Appointment", action_id="service_0")

    conv = app.get_conversation(TEST_PHONE)
    assert conv["state"] == "BOOKING_DATE"
    assert conv["service"] == "Appointment"
    assert len(sent_messages) == 1
    assert "When would you like your appointment?" in sent_messages[0]["interactive"]["body"]["text"]

# ============================================================
# 20. SERVICE-FIRST FLOW COLLECTS DATE, TIME, NAME, PATIENT TYPE
# ============================================================
def test_20_service_first_flow_collects_remaining_booking_fields(monkeypatch):
    app.reset_conversation(TEST_PHONE)
    sent_messages.clear()

    monkeypatch.setattr(app, "get_available_slots", lambda c, d: ["10:00 AM"])
    monkeypatch.setattr(app.CRMClient, "get_available_slots", lambda c, d: ["10:00 AM"])

    app.handle_user_message(TEST_PHONE, "interactive", "🦷 Our services", action_id="services")
    app.handle_user_message(TEST_PHONE, "interactive", "Appointment", action_id="service_0")

    today = datetime.date.today()
    if today.weekday() == 6:
        today = today + datetime.timedelta(days=1)

    app.handle_user_message(TEST_PHONE, "interactive", "Today", action_id=f"date_{today.isoformat()}")
    conv = app.get_conversation(TEST_PHONE)
    assert conv["state"] == "BOOKING_TIME"

    app.handle_user_message(TEST_PHONE, "interactive", "10:00 AM", action_id="time_10:00 AM")
    assert app.get_conversation(TEST_PHONE)["state"] == "BOOKING_NAME"

    app.handle_user_message(TEST_PHONE, "text", "Test Patient")
    assert app.get_conversation(TEST_PHONE)["state"] == "BOOKING_AGE"

    app.handle_user_message(TEST_PHONE, "text", "29")
    assert app.get_conversation(TEST_PHONE)["state"] == "BOOKING_PATIENT_TYPE"

    app.handle_user_message(TEST_PHONE, "interactive", "New patient", action_id="patient_new")
    assert app.get_conversation(TEST_PHONE)["state"] == "BOOKING_CONFIRMATION"

    summary = sent_messages[-1]["interactive"]["body"]["text"]
    assert "Appointment" in summary
    assert "Test Patient" in summary
    assert "10:00 AM" in summary


# ============================================================
# 21. UNKNOWN INPUT DOES NOT TRIGGER WELCOME MENU
# ============================================================
def test_21_unknown_input_no_unsolicited_welcome(monkeypatch):
    app.reset_conversation(TEST_PHONE)
    sent_messages.clear()

    app.handle_user_message(TEST_PHONE, "text", "some unsupported random input")

    assert sent_messages == []

# ============================================================
# 22. CANCEL DOES NOT SEND A WELCOME MENU
# ============================================================
def test_22_cancel_has_single_outbound_no_welcome_loop():
    app.reset_conversation(TEST_PHONE)
    app.update_conversation(
        TEST_PHONE,
        state="BOOKING_CONFIRMATION",
        service="Appointment",
        patient_name="Test Patient",
        patient_type="New",
        appointment_date="2026-09-30",
        appointment_time="06:00 PM",
    )

    sent_messages.clear()
    app.handle_user_message(
        TEST_PHONE,
        "interactive",
        "Cancel",
        action_id="cancel_booking",
    )

    assert app.get_conversation(TEST_PHONE)["state"] == "IDLE"
    assert len(sent_messages) == 1
    body = sent_messages[0]["text"]["body"]
    assert "cancelled" in body.lower()
    assert "Welcome to Glaze" not in body

# ============================================================
# 23. BOOKING COLLECTS AGE AND PASSES META SENDER PHONE TO CRM
# ============================================================
def test_23_booking_collects_age_and_sender_phone(monkeypatch):
    app.reset_conversation(TEST_PHONE)
    sent_messages.clear()

    captured = {}

    monkeypatch.setattr(app, "get_available_slots", lambda c, d: ["10:00 AM"])
    monkeypatch.setattr(app.CRMClient, "get_available_slots", lambda c, d: ["10:00 AM"])

    def mock_book(**kw):
        captured.update(kw)
        return {"status": "success", "data": {"appointmentId": "APT-AGE-1"}}

    monkeypatch.setattr(app, "book_appointment", mock_book)
    monkeypatch.setattr(app.CRMClient, "book_appointment", mock_book)

    app.handle_user_message(TEST_PHONE, "interactive", "📅 Book appointment", action_id="book_appointment")
    app.handle_user_message(TEST_PHONE, "interactive", "Appointment", action_id="booking_service_0")
    assert app.get_conversation(TEST_PHONE)["state"] == "BOOKING_NAME"

    app.handle_user_message(TEST_PHONE, "text", "Rahul Sharma")
    assert app.get_conversation(TEST_PHONE)["state"] == "BOOKING_AGE"

    app.handle_user_message(TEST_PHONE, "text", "24")
    assert app.get_conversation(TEST_PHONE)["state"] == "BOOKING_PATIENT_TYPE"

    app.handle_user_message(TEST_PHONE, "interactive", "New patient", action_id="patient_new")

    # Service-first flow now needs date/time after patient details.
    today = datetime.date.today()
    if today.weekday() == 6:
        today = today + datetime.timedelta(days=1)
    app.handle_user_message(TEST_PHONE, "interactive", "Today", action_id=f"date_{today.isoformat()}")
    app.handle_user_message(TEST_PHONE, "interactive", "10:00 AM", action_id="time_10:00 AM")
    assert app.get_conversation(TEST_PHONE)["state"] == "BOOKING_CONFIRMATION"

    app.handle_user_message(TEST_PHONE, "interactive", "Confirm", action_id="confirm_booking")

    assert captured["customerName"] == "Rahul Sharma"
    assert captured["customerPhone"] == TEST_PHONE
    assert captured["age"] == "24"


# ============================================================
# 24. INVALID AGE IS REJECTED
# ============================================================
def test_24_invalid_age_rejected():
    app.reset_conversation(TEST_PHONE)
    sent_messages.clear()

    app.handle_user_message(TEST_PHONE, "interactive", "📅 Book appointment", action_id="book_appointment")
    app.handle_user_message(TEST_PHONE, "interactive", "Appointment", action_id="booking_service_0")
    app.handle_user_message(TEST_PHONE, "text", "Rahul Sharma")

    sent_messages.clear()
    app.handle_user_message(TEST_PHONE, "text", "abc")
    assert app.get_conversation(TEST_PHONE)["state"] == "BOOKING_AGE"
    assert "age" in sent_messages[0]["text"]["body"].lower()

# ============================================================
# 25. REAL BOOKING-DATE FLOW RETURNS LIVE SLOT OPTIONS
# ============================================================
def test_25_booking_date_flow_returns_live_slot_list(monkeypatch):
    app.reset_conversation(TEST_PHONE, client_id="glaze-dental")
    sent_messages.clear()

    monkeypatch.setattr(
        app,
        "get_available_slots",
        lambda client_id, date_str: ["10:00 AM", "11:00 AM", "06:00 PM"],
    )

    # Follow the same path as production WhatsApp: book -> service -> date.
    app.handle_user_message(
        TEST_PHONE,
        "interactive",
        "📅 Book appointment",
        action_id="book_appointment",
        client_id="glaze-dental",
    )
    app.handle_user_message(
        TEST_PHONE,
        "interactive",
        "Appointment",
        action_id="booking_service_0",
        client_id="glaze-dental",
    )

    future = datetime.date.today() + datetime.timedelta(days=1)
    if future.weekday() == 6:
        future += datetime.timedelta(days=1)

    sent_messages.clear()
    app.handle_user_message(
        TEST_PHONE,
        "interactive",
        "Tomorrow",
        action_id=f"date_{future.isoformat()}",
        client_id="glaze-dental",
    )

    conv = app.get_conversation(TEST_PHONE, client_id="glaze-dental")
    assert conv["state"] == "BOOKING_TIME"
    assert conv["appointment_date"] == future.isoformat()
    assert len(sent_messages) == 1

    payload = sent_messages[0]
    assert payload["type"] == "interactive"
    assert payload["interactive"]["type"] == "list"
    rows = payload["interactive"]["action"]["sections"][0]["rows"]
    assert [row["title"] for row in rows] == ["10:00 AM", "11:00 AM", "06:00 PM"]
    assert rows[0]["id"] == "time_10:00 AM"


def test_26_booking_date_flow_falls_back_to_text_if_list_send_fails(monkeypatch):
    app.reset_conversation(TEST_PHONE, client_id="glaze-dental")
    sent_messages.clear()

    monkeypatch.setattr(app, "get_available_slots", lambda client_id, date_str: ["10:00 AM", "11:00 AM"])

    def fail_list(*args, **kwargs):
        return False

    monkeypatch.setattr(app, "send_list_message", fail_list)

    app.handle_user_message(
        TEST_PHONE,
        "interactive",
        "📅 Book appointment",
        action_id="book_appointment",
        client_id="glaze-dental",
    )
    app.handle_user_message(
        TEST_PHONE,
        "interactive",
        "Appointment",
        action_id="booking_service_0",
        client_id="glaze-dental",
    )

    future = datetime.date.today() + datetime.timedelta(days=1)
    if future.weekday() == 6:
        future += datetime.timedelta(days=1)

    sent_messages.clear()
    app.handle_user_message(
        TEST_PHONE,
        "interactive",
        "Tomorrow",
        action_id=f"date_{future.isoformat()}",
        client_id="glaze-dental",
    )

    assert len(sent_messages) == 1
    assert sent_messages[0]["type"] == "text"
    assert "10:00 AM" in sent_messages[0]["text"]["body"]
    assert "11:00 AM" in sent_messages[0]["text"]["body"]
