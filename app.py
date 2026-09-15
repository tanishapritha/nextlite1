import os
import json
import logging
import sqlite3
import datetime
from typing import Optional, Dict, Any, List
import requests
import google.generativeai as genai
from fastapi import FastAPI, Request, Query
from fastapi.responses import PlainTextResponse, JSONResponse

# ---------------------------------------------------------
# Logging Configuration
# ---------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("dental_whatsapp")

app = FastAPI(title="SmileCare Dental WhatsApp AI")

# ---------------------------------------------------------
# Configuration
# ---------------------------------------------------------
VERIFY_TOKEN = os.getenv("META_VERIFY_TOKEN", "nextlite-demo")
WHATSAPP_TOKEN = os.getenv("META_WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("META_PHONE_NUMBER_ID", "1208541249018781")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GRAPH_API_VERSION = os.getenv("META_GRAPH_API_VERSION", "v26.0")

DB_FILE = os.path.join(os.path.dirname(__file__), "nextlite.db")
KNOWLEDGE_FILE = os.path.join(os.path.dirname(__file__), "nextlite.json")

# ---------------------------------------------------------
# Database Setup
# ---------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        # Preserve existing Streamlit dashboard tables
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chats(
                id INTEGER PRIMARY KEY,
                phone TEXT,
                role TEXT,
                message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS actions(
                id INTEGER PRIMARY KEY,
                phone TEXT,
                action TEXT,
                details TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # State machine conversation tracking
        conn.execute("""
            CREATE TABLE IF NOT EXISTS conversations(
                phone TEXT PRIMARY KEY,
                state TEXT DEFAULT 'IDLE',
                service TEXT,
                date TEXT,
                time TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # Booked appointments table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS appointments(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT,
                patient_name TEXT,
                service TEXT,
                appointment_date TEXT,
                appointment_time TEXT,
                status TEXT DEFAULT 'REQUESTED',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.commit()

init_db()

# State persistence helpers
def get_user_session(phone: str) -> Dict[str, Any]:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM conversations WHERE phone = ?", (phone,)).fetchone()
        if row:
            return dict(row)
        conn.execute("INSERT OR IGNORE INTO conversations (phone, state) VALUES (?, 'IDLE')", (phone,))
        conn.commit()
        return {"phone": phone, "state": "IDLE", "service": None, "date": None, "time": None}

def update_user_session(phone: str, state: str, service: Optional[str] = None, date: Optional[str] = None, time: Optional[str] = None):
    with get_db() as conn:
        conn.execute("""
            INSERT INTO conversations (phone, state, service, date, time, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(phone) DO UPDATE SET
                state = excluded.state,
                service = COALESCE(excluded.service, conversations.service),
                date = COALESCE(excluded.date, conversations.date),
                time = COALESCE(excluded.time, conversations.time),
                updated_at = CURRENT_TIMESTAMP
        """, (phone, state, service, date, time))
        conn.commit()

def reset_user_session(phone: str):
    with get_db() as conn:
        conn.execute("""
            UPDATE conversations
            SET state = 'IDLE', service = NULL, date = NULL, time = NULL, updated_at = CURRENT_TIMESTAMP
            WHERE phone = ?
        """, (phone,))
        conn.commit()

def save_appointment(phone: str, service: str, date: str, time: str, patient_name: Optional[str] = None) -> int:
    with get_db() as conn:
        cursor = conn.execute("""
            INSERT INTO appointments (phone, patient_name, service, appointment_date, appointment_time, status)
            VALUES (?, ?, ?, ?, ?, 'REQUESTED')
        """, (phone, patient_name, service, date, time))
        # Also record in actions table for Streamlit dashboard compatibility
        conn.execute("""
            INSERT INTO actions (phone, action, details)
            VALUES (?, 'appointment_booked', ?)
        """, (phone, json.dumps({"service": service, "date": date, "time": time, "status": "REQUESTED"})))
        conn.commit()
        return cursor.lastrowid

def log_chat_history(phone: str, role: str, message: str):
    try:
        with get_db() as conn:
            conn.execute("INSERT INTO chats (phone, role, message) VALUES (?, ?, ?)", (phone, role, message))
            conn.commit()
    except Exception as e:
        logger.error(f"Error logging chat to db: {e}")

# ---------------------------------------------------------
# Knowledge base
# ---------------------------------------------------------
def load_knowledge():
    try:
        with open(KNOWLEDGE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"Could not load knowledge file: {e}")
        return {
            "company": {
                "name": "SmileCare Dental Clinic (Demo)",
                "description": "A modern dental clinic providing quality dental care.",
                "location": "Nagpur, Maharashtra, India"
            },
            "services": [
                "Dental consultation",
                "Teeth cleaning",
                "Root canal consultation",
                "Cavity fillings"
            ],
            "business_hours": {
                "monday_to_friday": "9:00 AM to 7:00 PM",
                "saturday": "9:00 AM to 3:00 PM",
                "sunday": "Closed"
            },
            "appointment": {
                "demo_slots": ["10:00 AM", "11:30 AM", "2:00 PM", "4:30 PM"]
            }
        }

KNOWLEDGE = load_knowledge()

# ---------------------------------------------------------
# Meta WhatsApp Cloud API Client
# ---------------------------------------------------------
def _send_payload(payload: dict) -> dict:
    if not WHATSAPP_TOKEN:
        logger.warning("META_WHATSAPP_TOKEN is not configured; skipping actual HTTP call.")
        return {"messaging_product": "whatsapp", "status": "simulated"}

    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        response.raise_for_status()
        res_json = response.json()
        logger.info(f"WHATSAPP SEND SUCCESS | TO: {payload.get('to')} | MSG_ID: {res_json.get('messages', [{}])[0].get('id')}")
        return res_json
    except Exception as exc:
        logger.error(f"WHATSAPP SEND ERROR | TO: {payload.get('to')} | ERROR: {type(exc).__name__}: {exc}")
        if hasattr(exc, 'response') and exc.response is not None:
            logger.error(f"WHATSAPP RESPONSE DETAILS: {exc.response.text}")
        raise exc

def send_text_message(to: str, message: str) -> dict:
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": message},
    }
    return _send_payload(payload)

def send_button_message(to: str, body: str, buttons: List[Dict[str, str]], header: Optional[str] = None, footer: Optional[str] = None) -> dict:
    """
    Sends interactive button reply message (up to 3 buttons max per WhatsApp specs).
    buttons: [{'id': 'btn_id', 'title': 'Button Title'}]
    """
    interactive = {
        "type": "button",
        "body": {"text": body},
        "action": {
            "buttons": [
                {
                    "type": "reply",
                    "reply": {
                        "id": btn["id"][:256],
                        "title": btn["title"][:20]  # WhatsApp 20-char button title limit
                    }
                }
                for btn in buttons[:3]
            ]
        }
    }
    if header:
        interactive["header"] = {"type": "text", "text": header}
    if footer:
        interactive["footer"] = {"text": footer}

    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": interactive
    }
    return _send_payload(payload)

def send_list_message(to: str, body: str, button_text: str, sections: List[Dict[str, Any]], title: Optional[str] = None, footer: Optional[str] = None) -> dict:
    """
    Sends interactive list message (for >3 options, e.g. services, times).
    sections: [{'title': 'Section Title', 'rows': [{'id': 'row_id', 'title': 'Row Title', 'description': ''}]}]
    """
    interactive = {
        "type": "list",
        "body": {"text": body},
        "action": {
            "button": button_text[:20],
            "sections": sections
        }
    }
    if title:
        interactive["header"] = {"type": "text", "text": title}
    if footer:
        interactive["footer"] = {"text": footer}

    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "interactive",
        "interactive": interactive
    }
    return _send_payload(payload)

# Backward-compatibility alias
def send_whatsapp(to: str, message: str) -> dict:
    return send_text_message(to, message)

# ---------------------------------------------------------
# Intent / Retrieval / Dental AI Layer
# ---------------------------------------------------------
def normalize(text: str) -> str:
    return " ".join(text.lower().strip().split())

def is_greeting(text: str) -> bool:
    q = normalize(text)
    greetings = {
        "hi", "hii", "hiii", "hello", "hey", "heyy", "heyyy",
        "yo", "hola", "start", "menu", "main menu",
        "hey there", "hello there", "good morning", "good afternoon", "good evening"
    }
    return q in greetings

def is_thanks(text: str) -> bool:
    q = normalize(text)
    if any(q == t for t in ["thanks", "thank you", "thankyou", "thx", "thanks!", "thank you!", "great thanks"]):
        return True
    if any(t in q for t in ["thank you", "thanks", "thx", "thankyou"]):
        return True
    return False

def retrieve_facts(question: str) -> List[Dict[str, Any]]:
    q = normalize(question)
    facts = []

    aliases = {
        "company": ["clinic", "dentist", "smilecare", "who are you", "about you", "doctor"],
        "services": ["service", "services", "offer", "cleaning", "root canal", "whitening", "cavity", "consultation", "fillings", "treatment", "treat"],
        "hours": ["hour", "hours", "open", "opening", "close", "closing", "timing", "timings", "available", "sunday", "saturday"],
        "location": ["location", "located", "address", "where are you", "direction", "reach clinic"],
        "emergency": ["emergency", "urgent", "severe pain", "bleeding", "trauma", "night"],
        "refund": ["cancel", "reschedule", "refund", "policy", "late"],
        "contact": ["contact", "phone", "call", "email", "helpline", "speak to doctor", "reception"]
    }

    matched = set()
    for category, keywords in aliases.items():
        for keyword in keywords:
            if keyword in q:
                matched.add(category)
                break

    if "company" in matched:
        facts.append({"company": KNOWLEDGE.get("company", {})})
    if "services" in matched:
        facts.append({"services": KNOWLEDGE.get("services", [])})
    if "hours" in matched:
        facts.append({"business_hours": KNOWLEDGE.get("business_hours", {})})
    if "location" in matched:
        facts.append({"location": KNOWLEDGE.get("company", {}).get("location", "Nagpur, Maharashtra, India")})
        facts.append({"contact_address": KNOWLEDGE.get("contact", {}).get("address", "")})
    if "emergency" in matched:
        facts.append({"emergency_info": KNOWLEDGE.get("clinic_info", {}).get("emergency", "")})
    if "refund" in matched:
        facts.append({"policy": KNOWLEDGE.get("refund_policy", "")})
    if "contact" in matched:
        facts.append({"contact": KNOWLEDGE.get("contact", {})})

    # Always provide basic company info for context
    if not facts:
        facts.append({"company": KNOWLEDGE.get("company", {})})
        facts.append({"services": KNOWLEDGE.get("services", [])})
        facts.append({"business_hours": KNOWLEDGE.get("business_hours", {})})

    return facts

def ask_gemini(question: str, facts: List[Dict[str, Any]]) -> Optional[str]:
    if not GEMINI_API_KEY:
        return None

    try:
        genai.configure(api_key=GEMINI_API_KEY)
        model = genai.GenerativeModel("gemini-2.5-flash-lite")

        clinic_name = KNOWLEDGE.get("company", {}).get("name", "SmileCare Dental Clinic")

        prompt = f"""
You are the friendly WhatsApp dental assistant for {clinic_name}.

A patient is chatting with the clinic. Be polite, reassuring, concise, and helpful.

SOURCE OF TRUTH (Verified Dental Clinic Information):
{json.dumps(facts, indent=2, ensure_ascii=False)}

CRITICAL RULES:
1. Answer ONLY using the verified clinic information provided above.
2. DO NOT provide medical or dental diagnoses, prescribe medications, or guess treatment outcomes.
3. If the patient asks about tooth pain, cavities, or dental symptoms, politely recommend booking an appointment for a clinical examination by the dentist.
4. If the exact answer is not in the verified information, do not invent facts (e.g., specific unlisted prices, insurance coverage, staff names). Instead, politely mention that our clinic reception team can assist them directly.
5. Keep the response short (1 to 3 sentences, WhatsApp-friendly).
6. Never mention "context", "prompts", "Gemini", "databases", or "AI system".

Patient question:
{question}

Helpful WhatsApp reply:
"""
        response = model.generate_content(
            prompt,
            generation_config={
                "temperature": 0.2,
                "max_output_tokens": 200,
            }
        )
        answer = (response.text or "").strip()
        if answer:
            return answer
    except Exception as exc:
        logger.warning(f"Gemini API call failed: {exc}")

    return None

def build_fallback(question: str, facts: List[Dict[str, Any]]) -> str:
    clinic_name = KNOWLEDGE.get("company", {}).get("name", "SmileCare Dental Clinic")
    q = normalize(question)

    if any(w in q for w in ["hour", "timing", "open", "close"]):
        hours = KNOWLEDGE.get("business_hours", {})
        return (
            f"🕒 *{clinic_name} Hours:*\n"
            f"• Mon - Fri: {hours.get('monday_to_friday', '9:00 AM - 7:00 PM')}\n"
            f"• Saturday: {hours.get('saturday', '9:00 AM - 3:00 PM')}\n"
            f"• Sunday: {hours.get('sunday', 'Closed')}\n\n"
            "Would you like to book an appointment? Reply 'book' or 'hi' for the menu."
        )

    if any(w in q for w in ["service", "treat", "cleaning", "root canal", "cavity"]):
        services = KNOWLEDGE.get("services", [])
        services_text = "\n".join(f"• {s}" for s in services)
        return (
            f"🦷 *Our Services at {clinic_name}:*\n{services_text}\n\n"
            "To schedule a visit, reply 'book' or send 'hi' for the main menu."
        )

    if any(w in q for w in ["location", "address", "where"]):
        contact = KNOWLEDGE.get("contact", {})
        addr = contact.get("address", KNOWLEDGE.get("company", {}).get("location", "Nagpur, India"))
        return f"📍 *Clinic Location:*\n{addr}\n\nPhone: {contact.get('phone', 'N/A')}"

    if any(w in q for w in ["pain", "hurt", "ache", "emergency"]):
        return (
            "We're sorry you're feeling discomfort! 🦷 For tooth pain or dental issues, we recommend an in-person dental consultation so our dentist can examine you.\n\n"
            "Reply 'book' to schedule a consultation, or 'hi' for options."
        )

    contact = KNOWLEDGE.get("contact", {})
    return (
        f"Thank you for contacting {clinic_name}. 😊\n\n"
        f"Our team is happy to help you with consultations, dental services, or timings. "
        f"For specific inquiries, you can also reach us at {contact.get('phone', '+91 98765 43210')}.\n\n"
        "Send 'hi' anytime to view the main menu or book an appointment."
    )

# ---------------------------------------------------------
# Booking & Flow State Machine
# ---------------------------------------------------------
def get_service_options() -> List[str]:
    configured = KNOWLEDGE.get("services", [])
    standard = ["Dental consultation", "Teeth cleaning", "Root canal consultation", "Other"]
    # Ensure options are clean and unique
    combined = []
    for s in standard:
        if s not in combined:
            combined.append(s)
    return combined

def get_demo_slots() -> List[str]:
    return KNOWLEDGE.get("appointment", {}).get("demo_slots", ["10:00 AM", "11:30 AM", "2:00 PM", "4:30 PM"])

def parse_date_input(text: str) -> Optional[str]:
    raw = normalize(text)
    today = datetime.date.today()

    if raw in ["today", "btn_date_today"]:
        return today.strftime("%d %B")
    elif raw in ["tomorrow", "btn_date_tomorrow"]:
        tomorrow = today + datetime.timedelta(days=1)
        return tomorrow.strftime("%d %B")

    # Try standard formats
    for fmt in ["%d %B", "%d %b", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%B %d", "%b %d"]:
        try:
            dt = datetime.datetime.strptime(text.strip(), fmt)
            if "%Y" not in fmt:
                dt = dt.replace(year=today.year)
            return dt.strftime("%d %B")
        except ValueError:
            pass

    # If it has digits and letters (e.g. "16 September" or "Sept 16")
    if len(text.strip()) >= 3 and any(char.isdigit() for char in text):
        return text.strip().title()

    return None

def handle_user_message(phone: str, msg_type: str, text: str, action_id: Optional[str] = None):
    """
    Main deterministic conversation router and state machine.
    """
    session = get_user_session(phone)
    current_state = session.get("state", "IDLE")
    text_norm = normalize(text)
    selected_option = action_id or text.strip()

    logger.info(f"INCOMING MESSAGE | USER: {phone} | MESSAGE TYPE: {msg_type} | CURRENT STATE: {current_state} | SELECTED OPTION: {selected_option}")

    log_chat_history(phone, "user", text or action_id or "")

    # 1. Global Reset / Main Menu Commands
    if action_id == "btn_menu" or is_greeting(text) or text_norm in ["menu", "main menu"]:
        reset_user_session(phone)
        clinic_name = KNOWLEDGE.get("company", {}).get("name", "SmileCare Dental Clinic")
        welcome_text = f"Hi! 👋 Welcome to {clinic_name}.\n\nHow can we help you today?"
        buttons = [
            {"id": "btn_book", "title": "📅 Book appointment"},
            {"id": "btn_services", "title": "🦷 Our services"},
            {"id": "btn_info", "title": "📍 Clinic info"}
        ]
        send_button_message(phone, welcome_text, buttons)
        logger.info(f"GENERATED REPLY | USER: {phone} | ACTION: main_menu")
        return

    # 2. Global Cancellation
    if action_id == "btn_cancel" or text_norm in ["cancel", "stop", "abort"]:
        reset_user_session(phone)
        reply = "Your booking process has been cancelled. 😊 Let us know whenever you'd like to book or ask a question. Send 'hi' to start over."
        send_text_message(phone, reply)
        logger.info(f"GENERATED REPLY | USER: {phone} | ACTION: booking_cancelled")
        return

    # 3. Global Start Over
    if action_id == "btn_start_over" or text_norm in ["start over", "restart"]:
        reset_user_session(phone)
        # Re-trigger booking flow
        action_id = "btn_book"
        current_state = "IDLE"

    # 4. Service Information Quick Buttons
    if action_id == "btn_services" or (current_state == "IDLE" and text_norm in ["our services", "services"]):
        services = KNOWLEDGE.get("services", [])
        services_text = "\n".join(f"• {s}" for s in services)
        clinic_name = KNOWLEDGE.get("company", {}).get("name", "SmileCare Dental Clinic")
        body = f"🦷 *Our Services at {clinic_name}:*\n\n{services_text}\n\nWould you like to book an appointment for any of these?"
        buttons = [
            {"id": "btn_book", "title": "📅 Book appointment"},
            {"id": "btn_info", "title": "📍 Clinic info"}
        ]
        send_button_message(phone, body, buttons)
        logger.info(f"GENERATED REPLY | USER: {phone} | ACTION: show_services")
        return

    # 5. Clinic Info Quick Buttons
    if action_id == "btn_info" or (current_state == "IDLE" and text_norm in ["clinic info", "info"]):
        clinic_name = KNOWLEDGE.get("company", {}).get("name", "SmileCare Dental Clinic")
        hours = KNOWLEDGE.get("business_hours", {})
        contact = KNOWLEDGE.get("contact", {})
        body = (
            f"📍 *{clinic_name} Info*\n\n"
            f"🏢 Address: {contact.get('address', 'Civil Lines, Nagpur')}\n"
            f"📞 Phone: {contact.get('phone', '+91 98765 43210')}\n\n"
            f"🕒 *Business Hours:*\n"
            f"• Mon - Fri: {hours.get('monday_to_friday', '9:00 AM - 7:00 PM')}\n"
            f"• Saturday: {hours.get('saturday', '9:00 AM - 3:00 PM')}\n"
            f"• Sunday: {hours.get('sunday', 'Closed')}"
        )
        buttons = [
            {"id": "btn_book", "title": "📅 Book appointment"},
            {"id": "btn_menu", "title": "🏠 Main menu"}
        ]
        send_button_message(phone, body, buttons)
        logger.info(f"GENERATED REPLY | USER: {phone} | ACTION: show_info")
        return

    # 6. Thanks Intent
    if current_state == "IDLE" and is_thanks(text):
        reply = "You're very welcome! 😊 If you have any other questions or need to book an appointment, just send 'hi'."
        send_text_message(phone, reply)
        logger.info(f"GENERATED REPLY | USER: {phone} | ACTION: thanks_reply")
        return

    # -----------------------------------------------------
    # BOOKING FLOW STATE MACHINE
    # -----------------------------------------------------

    # Trigger booking
    if action_id == "btn_book" or (current_state == "IDLE" and text_norm in ["book appointment", "book", "appointment", "schedule"]):
        update_user_session(phone, "BOOKING_SERVICE")
        services = get_service_options()

        # Build interactive list for services
        rows = [{"id": f"srv_{i}", "title": s[:20], "description": f"Book {s}"} for i, s in enumerate(services)]
        sections = [{"title": "Select a Service", "rows": rows}]

        body = "Sure! What would you like to book?"
        send_list_message(phone, body=body, button_text="Select service", sections=sections)
        logger.info(f"GENERATED REPLY | USER: {phone} | STATE -> BOOKING_SERVICE")
        return

    # State: BOOKING_SERVICE
    if current_state == "BOOKING_SERVICE":
        services = get_service_options()
        selected_service = None

        if action_id and action_id.startswith("srv_"):
            try:
                idx = int(action_id.split("_")[1])
                selected_service = services[idx]
            except (IndexError, ValueError):
                pass

        if not selected_service:
            # Check if user typed service name
            for s in services:
                if s.lower() in text_norm or text_norm in s.lower():
                    selected_service = s
                    break

        if selected_service:
            update_user_session(phone, state="BOOKING_DATE", service=selected_service)
            prompt_date_selection(phone, selected_service)
            return
        else:
            # Polite recovery
            rows = [{"id": f"srv_{i}", "title": s[:20], "description": f"Book {s}"} for i, s in enumerate(services)]
            sections = [{"title": "Select a Service", "rows": rows}]
            send_list_message(phone, body="Please select one of the available dental services below:", button_text="Choose service", sections=sections)
            return

    # State: BOOKING_DATE
    if current_state == "BOOKING_DATE":
        if action_id == "btn_date_custom" or text_norm in ["choose another date", "other date", "custom"]:
            send_text_message(phone, "Please type your preferred date (for example: *16 September* or *Tomorrow*):")
            return

        parsed_date = parse_date_input(action_id or text)
        if parsed_date:
            update_user_session(phone, state="BOOKING_TIME", date=parsed_date)
            prompt_time_selection(phone, parsed_date)
            return
        else:
            prompt_date_selection(phone, session.get("service") or "your appointment", retry=True)
            return

    # State: BOOKING_TIME
    if current_state == "BOOKING_TIME":
        slots = get_demo_slots()
        selected_time = None

        if action_id and action_id.startswith("time_"):
            try:
                idx = int(action_id.split("_")[1])
                selected_time = slots[idx]
            except (IndexError, ValueError):
                pass

        if not selected_time:
            # Check if typed time matches any slot
            for slot in slots:
                if normalize(slot) in text_norm or text_norm in normalize(slot):
                    selected_time = slot
                    break

        if selected_time:
            update_user_session(phone, state="BOOKING_CONFIRMATION", time=selected_time)
            service = session.get("service", "Dental consultation")
            date = session.get("date", "Upcoming")
            confirm_body = (
                "Please confirm your appointment:\n\n"
                f"Service: {service}\n"
                f"Date: {date}\n"
                f"Time: {selected_time}"
            )
            buttons = [
                {"id": "btn_confirm", "title": "✅ Confirm"},
                {"id": "btn_change", "title": "↩️ Change"},
                {"id": "btn_cancel", "title": "❌ Cancel"}
            ]
            send_button_message(phone, confirm_body, buttons)
            logger.info(f"GENERATED REPLY | USER: {phone} | STATE -> BOOKING_CONFIRMATION")
            return
        else:
            prompt_time_selection(phone, session.get("date", "selected date"), retry=True)
            return

    # State: BOOKING_CONFIRMATION
    if current_state == "BOOKING_CONFIRMATION":
        if action_id == "btn_confirm" or text_norm in ["confirm", "yes", "ok", "proceed"]:
            service = session.get("service", "Dental consultation")
            date = session.get("date", "Upcoming")
            time_slot = session.get("time", "10:00 AM")

            # Save in SQLite
            appointment_id = save_appointment(phone, service, date, time_slot)
            reset_user_session(phone)

            success_msg = (
                "You're all set! 🎉\n\n"
                "Your appointment request is received for:\n"
                f"{service}\n"
                f"{date}\n"
                f"{time_slot}\n\n"
                "We'll see you then. If you need to make changes or have questions, just send 'hi'."
            )
            send_text_message(phone, success_msg)
            logger.info(f"GENERATED REPLY | USER: {phone} | APPOINTMENT SAVED: ID={appointment_id}")
            return

        elif action_id == "btn_change" or text_norm in ["change", "edit", "modify"]:
            buttons = [
                {"id": "btn_change_service", "title": "Change Service"},
                {"id": "btn_change_date", "title": "Change Date"},
                {"id": "btn_change_time", "title": "Change Time"}
            ]
            send_button_message(phone, "What would you like to change?", buttons)
            return

        elif action_id == "btn_change_service":
            update_user_session(phone, "BOOKING_SERVICE")
            services = get_service_options()
            rows = [{"id": f"srv_{i}", "title": s[:20], "description": f"Book {s}"} for i, s in enumerate(services)]
            send_list_message(phone, body="Select new service:", button_text="Select service", sections=[{"title": "Services", "rows": rows}])
            return

        elif action_id == "btn_change_date":
            update_user_session(phone, "BOOKING_DATE")
            prompt_date_selection(phone, session.get("service", "consultation"))
            return

        elif action_id == "btn_change_time":
            update_user_session(phone, "BOOKING_TIME")
            prompt_time_selection(phone, session.get("date", "selected date"))
            return
        else:
            confirm_body = (
                "Please confirm your appointment:\n\n"
                f"Service: {session.get('service')}\n"
                f"Date: {session.get('date')}\n"
                f"Time: {session.get('time')}"
            )
            buttons = [
                {"id": "btn_confirm", "title": "✅ Confirm"},
                {"id": "btn_change", "title": "↩️ Change"},
                {"id": "btn_cancel", "title": "❌ Cancel"}
            ]
            send_button_message(phone, confirm_body, buttons)
            return

    # -----------------------------------------------------
    # 7. NATURAL LANGUAGE & DENTAL QUESTIONS (IDLE STATE)
    # -----------------------------------------------------
    facts = retrieve_facts(text)
    ai_reply = ask_gemini(text, facts)
    if not ai_reply:
        ai_reply = build_fallback(text, facts)

    send_text_message(phone, ai_reply)
    logger.info(f"GENERATED REPLY | USER: {phone} | ACTION: natural_language_reply")

def prompt_date_selection(phone: str, service_name: str, retry: bool = False):
    today = datetime.date.today()
    tomorrow = today + datetime.timedelta(days=1)

    today_label = f"Today ({today.strftime('%d %b')})"
    tomorrow_label = f"Tomorrow ({tomorrow.strftime('%d %b')})"

    prefix = "I couldn't recognize that date format. " if retry else ""
    body = f"{prefix}When would you like to schedule your {service_name}?"

    buttons = [
        {"id": "btn_date_today", "title": "Today"},
        {"id": "btn_date_tomorrow", "title": "Tomorrow"},
        {"id": "btn_date_custom", "title": "Choose another date"}
    ]
    send_button_message(phone, body, buttons)
    logger.info(f"GENERATED REPLY | USER: {phone} | STATE -> BOOKING_DATE")

def prompt_time_selection(phone: str, date_str: str, retry: bool = False):
    slots = get_demo_slots()
    prefix = "Please select one of our available slots. " if retry else ""
    body = f"{prefix}Select an available time slot for *{date_str}*:"

    rows = [{"id": f"time_{i}", "title": slot[:20], "description": "Demo availability slot"} for i, slot in enumerate(slots)]
    sections = [{"title": "Available Times", "rows": rows}]

    send_list_message(phone, body=body, button_text="Select time", sections=sections)
    logger.info(f"GENERATED REPLY | USER: {phone} | STATE -> BOOKING_TIME")

# ---------------------------------------------------------
# Meta Webhook Verification
# ---------------------------------------------------------
@app.get("/webhook")
async def verify_webhook(
    hub_mode: str = Query("", alias="hub.mode"),
    hub_verify_token: str = Query("", alias="hub.verify_token"),
    hub_challenge: str = Query("", alias="hub.challenge"),
):
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        logger.info("Webhook verified successfully with Meta challenge.")
        return PlainTextResponse(hub_challenge)

    logger.warning("Webhook verification failed: token mismatch.")
    return PlainTextResponse("Verification failed", status_code=403)

# ---------------------------------------------------------
# WhatsApp Webhook Receiver
# ---------------------------------------------------------
@app.post("/webhook")
async def whatsapp_webhook(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"status": "invalid_json"}, status_code=200)

    # Return 200 for unrelated Meta webhook events
    if body.get("object") != "whatsapp_business_account":
        return JSONResponse({"status": "ignored"})

    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for message in value.get("messages", []):
                sender = message.get("from")
                if not sender:
                    continue

                msg_type = message.get("type", "unknown")
                text_content = ""
                action_id = None

                # 1. Plain Text Message
                if msg_type == "text":
                    text_content = message.get("text", {}).get("body", "").strip()

                # 2. Interactive Button Reply
                elif msg_type == "interactive":
                    interactive = message.get("interactive", {})
                    interactive_type = interactive.get("type")

                    if interactive_type == "button_reply":
                        btn = interactive.get("button_reply", {})
                        action_id = btn.get("id")
                        text_content = btn.get("title", "")
                    elif interactive_type == "list_reply":
                        item = interactive.get("list_reply", {})
                        action_id = item.get("id")
                        text_content = item.get("title", "")

                # Ignore unsupported types gracefully without crashing
                if not text_content and not action_id:
                    continue

                try:
                    handle_user_message(
                        phone=sender,
                        msg_type=msg_type,
                        text=text_content,
                        action_id=action_id
                    )
                except Exception as exc:
                    logger.error(f"Error handling message from {sender}: {type(exc).__name__}: {exc}", exc_info=True)

    return JSONResponse({"status": "ok"})

# ---------------------------------------------------------
# Health check
# ---------------------------------------------------------
@app.get("/")
async def root():
    return {
        "status": "online",
        "service": "SmileCare Dental Clinic WhatsApp AI",
        "version": "2.0",
        "webhook": "/webhook",
        "meta_graph_api_version": GRAPH_API_VERSION
    }
