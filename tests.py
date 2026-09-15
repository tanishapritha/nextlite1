import os
import json
import sqlite3
import datetime
from pathlib import Path
import app

# Set dummy environment for isolated testing
os.environ["META_VERIFY_TOKEN"] = "nextlite-demo"
os.environ["META_GRAPH_API_VERSION"] = "v26.0"

# Mock WhatsApp API sender so we can inspect outbound messages during testing
sent_messages = []

def mock_send_payload(payload):
    sent_messages.append(payload)
    return {"messaging_product": "whatsapp", "messages": [{"id": "wamid.mock_123"}]}

app._send_payload = mock_send_payload

TEST_PHONE = "919999988888"

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

    print("\n--- Running Dental WhatsApp Assistant Tests ---")

    # Clean up test user state before starting
    app.reset_user_session(TEST_PHONE)

    # 1. "hi" -> main menu interactive buttons
    sent_messages = []
    app.handle_user_message(TEST_PHONE, "text", "hi")
    assert_test(
        "1. 'hi' -> main menu buttons",
        len(sent_messages) == 1 and
        sent_messages[0]["type"] == "interactive" and
        sent_messages[0]["interactive"]["type"] == "button" and
        any(b["reply"]["id"] == "btn_book" for b in sent_messages[0]["interactive"]["action"]["buttons"])
    )

    # 2. "thanks" -> friendly response
    sent_messages = []
    app.handle_user_message(TEST_PHONE, "text", "thank you so much")
    assert_test(
        "2. 'thanks' -> friendly response",
        len(sent_messages) == 1 and
        sent_messages[0]["type"] == "text" and
        "welcome" in sent_messages[0]["text"]["body"].lower()
    )

    # 3. Book appointment -> service selection state
    sent_messages = []
    app.handle_user_message(TEST_PHONE, "interactive", "📅 Book appointment", action_id="btn_book")
    session = app.get_user_session(TEST_PHONE)
    assert_test(
        "3. Book appointment -> service state",
        session["state"] == "BOOKING_SERVICE" and
        len(sent_messages) == 1 and
        sent_messages[0]["type"] == "interactive" and
        sent_messages[0]["interactive"]["type"] == "list"
    )

    # 4. Service selection -> date selection state
    sent_messages = []
    app.handle_user_message(TEST_PHONE, "interactive", "Dental consultation", action_id="srv_0")
    session = app.get_user_session(TEST_PHONE)
    assert_test(
        "4. Service selection -> date state",
        session["state"] == "BOOKING_DATE" and
        session["service"] == "Dental consultation" and
        len(sent_messages) == 1 and
        sent_messages[0]["interactive"]["type"] == "button" and
        any(b["reply"]["id"] == "btn_date_tomorrow" for b in sent_messages[0]["interactive"]["action"]["buttons"])
    )

    # 5. Date selection -> time selection state
    sent_messages = []
    app.handle_user_message(TEST_PHONE, "interactive", "Tomorrow", action_id="btn_date_tomorrow")
    session = app.get_user_session(TEST_PHONE)
    assert_test(
        "5. Date selection -> time state",
        session["state"] == "BOOKING_TIME" and
        session["date"] is not None and
        len(sent_messages) == 1 and
        sent_messages[0]["interactive"]["type"] == "list" and
        any(row["id"] == "time_0" for row in sent_messages[0]["interactive"]["action"]["sections"][0]["rows"])
    )

    # 6. Time selection -> confirmation state
    sent_messages = []
    app.handle_user_message(TEST_PHONE, "interactive", "11:30 AM", action_id="time_1")
    session = app.get_user_session(TEST_PHONE)
    assert_test(
        "6. Time selection -> confirmation state",
        session["state"] == "BOOKING_CONFIRMATION" and
        session["time"] == "11:30 AM" and
        len(sent_messages) == 1 and
        sent_messages[0]["interactive"]["type"] == "button" and
        any(b["reply"]["id"] == "btn_confirm" for b in sent_messages[0]["interactive"]["action"]["buttons"])
    )

    # 7. Confirm -> appointment saved in SQLite
    sent_messages = []
    app.handle_user_message(TEST_PHONE, "interactive", "✅ Confirm", action_id="btn_confirm")
    session = app.get_user_session(TEST_PHONE)
    
    with app.get_db() as conn:
        saved_apt = conn.execute("SELECT * FROM appointments WHERE phone = ? ORDER BY id DESC LIMIT 1", (TEST_PHONE,)).fetchone()

    assert_test(
        "7. Confirm -> appointment saved in DB",
        session["state"] == "IDLE" and
        saved_apt is not None and
        saved_apt["service"] == "Dental consultation" and
        saved_apt["appointment_time"] == "11:30 AM" and
        len(sent_messages) == 1 and
        "all set" in sent_messages[0]["text"]["body"].lower()
    )

    # 8. Cancel -> state reset
    # Start booking again
    app.handle_user_message(TEST_PHONE, "interactive", "📅 Book appointment", action_id="btn_book")
    assert app.get_user_session(TEST_PHONE)["state"] == "BOOKING_SERVICE"
    sent_messages = []
    app.handle_user_message(TEST_PHONE, "text", "cancel")
    session = app.get_user_session(TEST_PHONE)
    assert_test(
        "8. Cancel -> state reset to IDLE",
        session["state"] == "IDLE" and
        len(sent_messages) == 1 and
        "cancelled" in sent_messages[0]["text"]["body"].lower()
    )

    # 9. Unknown dental question -> Gemini path or grounded knowledge retrieval
    facts = app.retrieve_facts("Do you treat cavities and severe tooth pain?")
    assert_test(
        "9. Fact retrieval for dental question",
        len(facts) > 0 and any("services" in f or "emergency_info" in f for f in facts)
    )

    # 10. Fallback when Gemini is unavailable
    sent_messages = []
    app.handle_user_message(TEST_PHONE, "text", "What are your business hours?")
    assert_test(
        "10. Safe fallback without Gemini",
        len(sent_messages) == 1 and
        sent_messages[0]["type"] == "text" and
        "hours" in sent_messages[0]["text"]["body"].lower()
    )

    # 11. Graph API Version check
    assert_test(
        "11. Graph API version is v26.0",
        app.GRAPH_API_VERSION == "v26.0"
    )

    print(f"\nResult: {passed}/{total} tests passed")
    if passed != total:
        raise SystemExit(1)

if __name__ == "__main__":
    run_tests()
