import os
import json
import sqlite3
import datetime
from fastapi.testclient import TestClient

# Set dummy env vars for test suite
os.environ["META_VERIFY_TOKEN"] = "nextlite-demo"
os.environ["META_GRAPH_API_VERSION"] = "v26.0"

import app

client = TestClient(app.app)

# Capture outbound payloads for assertion
sent_messages = []

def mock_send_payload(payload):
    sent_messages.append(payload)
    return True

app._send_payload = mock_send_payload

TEST_PHONE = "+919999988888"


def run_tests():
    global sent_messages
    passed = 0
    total = 0

    def assert_test(name, condition):
        nonlocal passed, total
        total += 1
        if condition:
            print(f"PASS | {name}")
            passed += 1
        else:
            print(f"FAIL | {name}")

    print("\n--- Running Glaze Dental Clinic CRM WhatsApp Tests ---")

    # 1. Health Check
    r_root = client.get("/")
    assert_test(
        "1. GET / Health check",
        r_root.status_code == 200 and r_root.json().get("service") == "Glaze Dental Clinic WhatsApp AI"
    )

    # 2. Webhook verification
    r_ver = client.get("/webhook?hub.mode=subscribe&hub.verify_token=nextlite-demo&hub.challenge=test_challenge")
    assert_test(
        "2. GET /webhook verification",
        r_ver.status_code == 200 and r_ver.text == "test_challenge"
    )

    # 3. Client CRM Config resolution & Dashboard API
    crm_cfg = app.get_client_crm_config("glaze-dental")
    assert_test(
        "3. Client CRM config resolution",
        crm_cfg is not None and
        crm_cfg["crm_tenant_id"] == "6b4b6128-5b5f-4d2f-b5de-91511ab9b120" and
        crm_cfg["crm_base_url"] == "https://dandelion-gigantic-challenge.ngrok-free.dev"
    )

    # 4. Test CRM slots live check
    slots = app.get_available_slots("glaze-dental", "2026-09-16")
    assert_test(
        "4. Live CRM GET /slots integration",
        slots is not None and isinstance(slots, list) and len(slots) > 0
    )

    # 5. Reset & Greeting ("hi")
    app.reset_conversation(TEST_PHONE)
    sent_messages = []
    app.handle_user_message(TEST_PHONE, "text", "hi")
    assert_test(
        "5. 'hi' -> Glaze Dental Clinic main menu buttons",
        len(sent_messages) == 1 and
        sent_messages[0]["type"] == "interactive" and
        "Glaze Dental Clinic" in sent_messages[0]["interactive"]["body"]["text"] and
        "SmileCare" not in sent_messages[0]["interactive"]["body"]["text"] and
        "Nextlite" not in sent_messages[0]["interactive"]["body"]["text"]
    )

    # 6. Emergency routing (Priority)
    sent_messages = []
    app.handle_user_message(TEST_PHONE, "text", "I have severe pain after an accident")
    assert_test(
        "6. Emergency keyword priority routing",
        len(sent_messages) == 1 and
        "9822977740" in sent_messages[0]["text"]["body"] and
        "urgent" in sent_messages[0]["text"]["body"].lower() and
        app.get_conversation(TEST_PHONE)["state"] == "IDLE"
    )

    # 7. Price inquiry safety
    sent_messages = []
    app.handle_user_message(TEST_PHONE, "text", "How much does RCT cost?")
    assert_test(
        "7. Pricing safety (no invented prices)",
        len(sent_messages) >= 1 and
        "Pricing details are not listed" in sent_messages[0]["text"]["body"] and
        "9822977740" in sent_messages[0]["text"]["body"]
    )

    # 8. Booking Flow: Step 1 -> Service Selection
    sent_messages = []
    app.handle_user_message(TEST_PHONE, "interactive", "📅 Book appointment", action_id="book_appointment")
    conv = app.get_conversation(TEST_PHONE)
    assert_test(
        "8. Book appointment -> BOOKING_SERVICE",
        conv["state"] == "BOOKING_SERVICE" and
        len(sent_messages) == 1 and
        sent_messages[0]["interactive"]["type"] == "list"
    )

    # 9. Booking Flow: Step 2 -> Name input
    sent_messages = []
    app.handle_user_message(TEST_PHONE, "interactive", "RCT", action_id="booking_service_0")
    conv = app.get_conversation(TEST_PHONE)
    assert_test(
        "9. Service selected -> BOOKING_NAME",
        conv["state"] == "BOOKING_NAME" and
        conv["service"] == "RCT" and
        len(sent_messages) == 1 and
        "full name" in sent_messages[0]["text"]["body"]
    )

    # 10. Booking Flow: Step 3 -> Patient Type
    sent_messages = []
    app.handle_user_message(TEST_PHONE, "text", "Rahul Sharma")
    conv = app.get_conversation(TEST_PHONE)
    assert_test(
        "10. Name entered -> BOOKING_PATIENT_TYPE",
        conv["state"] == "BOOKING_PATIENT_TYPE" and
        conv["patient_name"] == "Rahul Sharma" and
        len(sent_messages) == 1 and
        sent_messages[0]["interactive"]["type"] == "button"
    )

    # 11. Booking Flow: Step 4 -> Date Selection
    sent_messages = []
    app.handle_user_message(TEST_PHONE, "interactive", "New patient", action_id="patient_new")
    conv = app.get_conversation(TEST_PHONE)
    assert_test(
        "11. Patient type selected -> BOOKING_DATE",
        conv["state"] == "BOOKING_DATE" and
        conv["patient_type"] == "New" and
        len(sent_messages) == 1 and
        sent_messages[0]["interactive"]["type"] == "button"
    )

    # 12. Booking Flow: Step 5 -> CRM Live Slots fetched -> Time Selection
    sent_messages = []
    app.handle_user_message(TEST_PHONE, "text", "2026-09-16")
    conv = app.get_conversation(TEST_PHONE)
    assert_test(
        "12. Date entered -> CRM slots fetched -> BOOKING_TIME",
        conv["state"] == "BOOKING_TIME" and
        conv["appointment_date"] == "2026-09-16" and
        len(sent_messages) == 1 and
        sent_messages[0]["interactive"]["type"] == "list" and
        len(sent_messages[0]["interactive"]["action"]["sections"][0]["rows"]) > 0
    )

    # 13. Booking Flow: Step 6 -> Confirmation Prompt
    sent_messages = []
    app.handle_user_message(TEST_PHONE, "interactive", "12:00 PM", action_id="time_12:00 PM")
    conv = app.get_conversation(TEST_PHONE)
    assert_test(
        "13. Time selected -> BOOKING_CONFIRMATION summary",
        conv["state"] == "BOOKING_CONFIRMATION" and
        conv["appointment_time"] == "12:00 PM" and
        len(sent_messages) == 1 and
        "Rahul Sharma" in sent_messages[0]["interactive"]["body"]["text"] and
        "RCT" in sent_messages[0]["interactive"]["body"]["text"]
    )

    # 14. CRM Conflict (409) Handling on confirmation
    # Re-run booking flow up to BOOKING_CONFIRMATION, then mock book_appointment to return 409
    app.reset_conversation(TEST_PHONE)
    app.handle_user_message(TEST_PHONE, "interactive", "📅 Book appointment", action_id="book_appointment")
    app.handle_user_message(TEST_PHONE, "interactive", "RCT", action_id="booking_service_0")
    app.handle_user_message(TEST_PHONE, "text", "Rahul Sharma")
    app.handle_user_message(TEST_PHONE, "interactive", "New patient", action_id="patient_new")
    app.handle_user_message(TEST_PHONE, "text", "2026-09-16")
    app.handle_user_message(TEST_PHONE, "interactive", "12:00 PM", action_id="time_12:00 PM")
    _real_book = app.book_appointment
    try:
        app.book_appointment = lambda **kw: {"status": "conflict", "data": {}}
        sent_messages = []
        app.handle_user_message(TEST_PHONE, "interactive", "Confirm", action_id="confirm_booking")
    finally:
        app.book_appointment = _real_book
    assert_test(
        "14. CRM 409 conflict detected -> No confirmation, refresh slots",
        len(sent_messages) >= 1 and
        "already booked" in sent_messages[0]["text"]["body"].lower() and
        app.get_conversation(TEST_PHONE)["state"] in ["BOOKING_TIME", "BOOKING_DATE"]
    )

    # 15. Dashboard UI & Test CRM Connection Endpoint
    r_dash = client.get("/dashboard?client_id=glaze-dental")
    r_test_crm = client.get("/api/test-crm-connection?client_id=glaze-dental&test_date=2026-09-16")
    assert_test(
        "15. Dashboard UI and GET /api/test-crm-connection",
        r_dash.status_code == 200 and
        r_test_crm.status_code == 200 and
        r_test_crm.json().get("status") == "success"
    )

    print(f"\n--- Total: {passed}/{total} tests passed ---")
    if passed != total:
        raise SystemExit(1)

if __name__ == "__main__":
    run_tests()
