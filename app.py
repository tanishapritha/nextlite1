import os
import json
import logging
import sqlite3
import datetime
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse

import requests
import google.generativeai as genai

from fastapi import FastAPI, Request, Query
from fastapi.responses import PlainTextResponse, JSONResponse, HTMLResponse


# ============================================================
# APP
# ============================================================

app = FastAPI(title="Glaze Dental Clinic WhatsApp AI")


# ============================================================
# CONFIG
# ============================================================

VERIFY_TOKEN = os.getenv("META_VERIFY_TOKEN", "nextlite-demo")
WHATSAPP_TOKEN = os.getenv("META_WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("META_PHONE_NUMBER_ID", "1208541249018781")
GRAPH_API_VERSION = os.getenv("META_GRAPH_API_VERSION", "v26.0")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

DB_PATH = "nextlite.db"
KNOWLEDGE_PATH = "nextlite.json"


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)

logger = logging.getLogger("nextlite")


# ============================================================
# GEMINI
# ============================================================

gemini_model = None

if GEMINI_API_KEY:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        gemini_model = genai.GenerativeModel("gemini-2.5-flash-lite")
        logger.info("Gemini initialized successfully")
    except Exception as exc:
        logger.error("Gemini initialization failed | %s: %s", type(exc).__name__, exc)


# ============================================================
# KNOWLEDGE BASE
# ============================================================

DEFAULT_KNOWLEDGE = {
    "client_id": "glaze-dental",
    "client_name": "Glaze Dental Clinic",
    "crm_base_url": "https://dandelion-gigantic-challenge.ngrok-free.dev",
    "crm_tenant_id": "6b4b6128-5b5f-4d2f-b5de-91511ab9b120",
    "assistant_name": "Glaze Dental Clinic Assistant",
    "doctor": {
        "name": "Dr. Shadab Mulla",
        "qualification": "B.D.S.",
        "designation": "Dental Surgeon",
        "experience": "20 years of experience"
    },
    "languages": [
        "English",
        "Hindi",
        "Marathi",
        "Hinglish"
    ],
    "location": {
        "address": "Shop No 1, Monika 16, Pimpri Colony",
        "maps": "https://maps.app.goo.gl/H6872MYf2gdthTZ7A?g_st=ac"
    },
    "contact": {
        "phone": "9822977740",
        "whatsapp": "9822977740"
    },
    "services": [
        {
            "name": "RCT",
            "description": "Root canal treatment."
        },
        {
            "name": "Cosmetic dentistry",
            "description": "Cosmetic dental treatments."
        },
        {
            "name": "Painless extractions",
            "description": "Specialised in painless treatment."
        }
    ],
    "clinic": {
        "speciality": "Specialised in painless treatment",
        "appointment_duration": "30 minutes",
        "minimum_duration": "30 minutes"
    },
    "hours": {
        "days": [
            "Monday",
            "Tuesday",
            "Wednesday",
            "Thursday",
            "Friday",
            "Saturday"
        ],
        "morning": "10:00 AM - 1:00 PM",
        "evening": "6:00 PM - 9:00 PM"
    },
    "emergency": {
        "enabled": True,
        "phone": "9822977740",
        "instruction": "🚨 This may require urgent attention. Please call Glaze Dental Clinic at 9822977740 immediately for emergency assistance."
    }
}


def load_knowledge() -> Dict[str, Any]:
    if not os.path.exists(KNOWLEDGE_PATH):
        logger.warning("nextlite.json not found | using default knowledge")
        return DEFAULT_KNOWLEDGE

    try:
        with open(KNOWLEDGE_PATH, "r", encoding="utf-8") as file:
            data = json.load(file)
        logger.info("Knowledge base loaded from nextlite.json")
        return data
    except Exception as exc:
        logger.error("Knowledge loading failed | %s: %s", type(exc).__name__, exc)
        return DEFAULT_KNOWLEDGE


KNOWLEDGE = load_knowledge()


# ============================================================
# DATABASE & CLIENT CONFIGURATION
# ============================================================

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS client_config (
            client_id TEXT PRIMARY KEY,
            client_name TEXT NOT NULL,
            crm_base_url TEXT NOT NULL,
            crm_tenant_id TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT NOT NULL,
            direction TEXT NOT NULL,
            message TEXT,
            message_type TEXT,
            created_at TEXT NOT NULL
        )
    """)

    # Migrate chats table
    _existing_chats_cols = {
        row[1] for row in cursor.execute("PRAGMA table_info(chats)")
    }
    for _col, _def in [
        ("direction", "TEXT NOT NULL DEFAULT 'inbound'"),
        ("message_type", "TEXT"),
    ]:
        if _col not in _existing_chats_cols:
            cursor.execute(f"ALTER TABLE chats ADD COLUMN {_col} {_def}")
            logger.info("Migrated chats table: added column %s", _col)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT NOT NULL,
            action TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            phone TEXT PRIMARY KEY,
            client_id TEXT NOT NULL DEFAULT 'glaze-dental',
            state TEXT NOT NULL DEFAULT 'IDLE',
            service TEXT,
            patient_name TEXT,
            patient_type TEXT,
            appointment_date TEXT,
            appointment_time TEXT,
            updated_at TEXT NOT NULL
        )
    """)

    # Migrate existing conversations table — add columns that may be missing
    _existing_conv_cols = {
        row[1] for row in cursor.execute("PRAGMA table_info(conversations)")
    }
    for _col, _def in [
        ("client_id", "TEXT NOT NULL DEFAULT 'glaze-dental'"),
        ("state", "TEXT NOT NULL DEFAULT 'IDLE'"),
        ("service", "TEXT"),
        ("patient_name", "TEXT"),
        ("patient_type", "TEXT"),
        ("appointment_date", "TEXT"),
        ("appointment_time", "TEXT"),
    ]:
        if _col not in _existing_conv_cols:
            cursor.execute(f"ALTER TABLE conversations ADD COLUMN {_col} {_def}")
            logger.info("Migrated conversations table: added column %s", _col)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS appointments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT NOT NULL,
            client_id TEXT NOT NULL DEFAULT 'glaze-dental',
            crm_appointment_id TEXT,
            service TEXT NOT NULL,
            patient_name TEXT,
            patient_type TEXT,
            appointment_date TEXT NOT NULL,
            appointment_time TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'confirmed',
            created_at TEXT NOT NULL
        )
    """)

    # Migrate existing appointments table — add columns that may be missing
    _existing_appt_cols = {
        row[1] for row in cursor.execute("PRAGMA table_info(appointments)")
    }
    for _col, _def in [
        ("client_id", "TEXT NOT NULL DEFAULT 'glaze-dental'"),
        ("crm_appointment_id", "TEXT"),
        ("patient_name", "TEXT"),
        ("patient_type", "TEXT"),
    ]:
        if _col not in _existing_appt_cols:
            cursor.execute(f"ALTER TABLE appointments ADD COLUMN {_col} {_def}")
            logger.info("Migrated appointments table: added column %s", _col)

    # Seed initial Glaze Dental client configuration if not already set
    seed_client_id = KNOWLEDGE.get("client_id", "glaze-dental")
    seed_client_name = KNOWLEDGE.get("client_name", "Glaze Dental Clinic")
    seed_crm_base_url = KNOWLEDGE.get("crm_base_url", "https://dandelion-gigantic-challenge.ngrok-free.dev").rstrip("/")
    seed_crm_tenant_id = KNOWLEDGE.get("crm_tenant_id", "6b4b6128-5b5f-4d2f-b5de-91511ab9b120").strip()

    cursor.execute("SELECT client_id FROM client_config WHERE client_id = ?", (seed_client_id,))
    if not cursor.fetchone():
        cursor.execute("""
            INSERT INTO client_config (client_id, client_name, crm_base_url, crm_tenant_id, updated_at)
            VALUES (?, ?, ?, ?, ?)
        """, (seed_client_id, seed_client_name, seed_crm_base_url, seed_crm_tenant_id, datetime.datetime.utcnow().isoformat()))
        logger.info("Seeded initial CRM configuration for client: %s", seed_client_id)

    conn.commit()
    conn.close()
    logger.info("SQLite initialized successfully")


init_db()


@app.on_event("startup")
def startup():
    init_db()


# ============================================================
# CLIENT CRM CONFIG HELPERS
# ============================================================

def get_client_crm_config(client_id: str) -> Optional[Dict[str, Any]]:
    conn = get_db()
    row = conn.execute(
        "SELECT client_id, client_name, crm_base_url, crm_tenant_id, updated_at FROM client_config WHERE client_id = ?",
        (client_id,)
    ).fetchone()
    conn.close()

    if row:
        return dict(row)

    # Fallback to seeded Glaze knowledge if matching
    if client_id == KNOWLEDGE.get("client_id", "glaze-dental"):
        return {
            "client_id": client_id,
            "client_name": KNOWLEDGE.get("client_name", "Glaze Dental Clinic"),
            "crm_base_url": KNOWLEDGE.get("crm_base_url", "").rstrip("/"),
            "crm_tenant_id": KNOWLEDGE.get("crm_tenant_id", "").strip(),
            "updated_at": datetime.datetime.utcnow().isoformat()
        }

    return None


def save_client_crm_config(client_id: str, client_name: str, crm_base_url: str, crm_tenant_id: str) -> bool:
    parsed = urlparse(crm_base_url.strip())
    if not (parsed.scheme in ["http", "https"] and parsed.netloc):
        raise ValueError("crm_base_url must be a valid HTTP or HTTPS URL.")

    crm_tenant_id = crm_tenant_id.strip()
    if not crm_tenant_id:
        raise ValueError("crm_tenant_id cannot be empty.")

    clean_base_url = crm_base_url.strip().rstrip("/")

    conn = get_db()
    conn.execute("""
        INSERT INTO client_config (client_id, client_name, crm_base_url, crm_tenant_id, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(client_id) DO UPDATE SET
            client_name = excluded.client_name,
            crm_base_url = excluded.crm_base_url,
            crm_tenant_id = excluded.crm_tenant_id,
            updated_at = excluded.updated_at
    """, (client_id.strip(), client_name.strip(), clean_base_url, crm_tenant_id, datetime.datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()
    return True


def identify_client_for_sender(phone: str) -> str:
    """
    Resolves client_id for incoming WhatsApp sender.
    Defaults to 'glaze-dental' for current practice.
    """
    conn = get_db()
    row = conn.execute("SELECT client_id FROM conversations WHERE phone = ?", (phone,)).fetchone()
    conn.close()
    if row and row["client_id"]:
        return row["client_id"]
    return "glaze-dental"


# ============================================================
# CRM API INTEGRATION
# ============================================================

def crm_headers(crm_config: Dict[str, Any], is_post: bool = False) -> Dict[str, str]:
    headers = {
        "X-Tenant-Key": crm_config["crm_tenant_id"],
        "ngrok-skip-browser-warning": "true"
    }
    if is_post:
        headers["Content-Type"] = "application/json"
    return headers


def get_available_slots(client_id: str, date_str: str) -> Optional[List[str]]:
    crm_config = get_client_crm_config(client_id)
    if not crm_config:
        logger.error("CRM SLOTS ERROR | No CRM config found for client=%s", client_id)
        return None

    base_url = crm_config["crm_base_url"].rstrip("/")
    url = f"{base_url}/api/v1/integrations/whatsapp/slots"
    headers = crm_headers(crm_config, is_post=False)

    try:
        logger.info("CRM SLOTS REQUEST | client=%s | date=%s", client_id, date_str)
        response = requests.get(url, params={"date": date_str}, headers=headers, timeout=15)
        logger.info("CRM SLOTS | client=%s | date=%s | status=%d", client_id, date_str, response.status_code)

        if response.status_code != 200:
            logger.error("CRM SLOTS ERROR | status=%d | body=%s", response.status_code, response.text[:200])
            return None

        data = response.json()
        return data.get("availableSlots", [])
    except requests.RequestException as exc:
        logger.error("CRM SLOTS REQUEST ERROR | %s", repr(exc))
        return None
    except Exception as exc:
        logger.error("CRM SLOTS ERROR | %s", repr(exc))
        return None


def book_appointment(
    client_id: str,
    customer_name: str,
    customer_phone: str,
    booking_date: str,
    booking_time: str,
    age: str = "",
    place: str = ""
) -> Dict[str, Any]:
    crm_config = get_client_crm_config(client_id)
    if not crm_config:
        logger.error("CRM BOOKING ERROR | No CRM config found for client=%s", client_id)
        return {"status": "error", "data": {}}

    base_url = crm_config["crm_base_url"].rstrip("/")
    url = f"{base_url}/api/v1/integrations/whatsapp/appointments/book"
    headers = crm_headers(crm_config, is_post=True)

    payload = {
        "customerName": customer_name,
        "customerPhone": customer_phone,
        "bookingDate": booking_date,
        "bookingTime": booking_time,
        "title": "WhatsApp Consultation",
        "age": age,
        "place": place
    }

    try:
        logger.info("CRM BOOKING REQUEST | client=%s | date=%s | time=%s", client_id, booking_date, booking_time)
        response = requests.post(url, headers=headers, json=payload, timeout=20)
        logger.info(
            "CRM BOOKING | client=%s | date=%s | time=%s | status=%d",
            client_id, booking_date, booking_time, response.status_code
        )

        if response.status_code == 201:
            logger.info("CRM BOOKING SUCCESS | client=%s | date=%s | time=%s", client_id, booking_date, booking_time)
            return {"status": "success", "data": response.json()}

        if response.status_code == 409:
            logger.warning("CRM BOOKING CONFLICT | client=%s | date=%s | time=%s", client_id, booking_date, booking_time)
            return {"status": "conflict", "data": response.json()}

        logger.error("CRM BOOKING ERROR | status=%d | body=%s", response.status_code, response.text[:200])
        return {"status": "error", "data": response.json() if response.text else {}}
    except requests.RequestException as exc:
        logger.error("CRM BOOKING REQUEST ERROR | %s", repr(exc))
        return {"status": "error", "data": {}}
    except Exception as exc:
        logger.error("CRM BOOKING ERROR | %s", repr(exc))
        return {"status": "error", "data": {}}


# ============================================================
# HELPERS & PERSISTENCE
# ============================================================

def now_iso() -> str:
    return datetime.datetime.utcnow().isoformat()


def clinic_name() -> str:
    return KNOWLEDGE.get("client_name", "Glaze Dental Clinic")


def log_chat(phone: str, direction: str, message: str, message_type: str = "text"):
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO chats (phone, direction, message, message_type, created_at) VALUES (?, ?, ?, ?, ?)",
            (phone, direction, message, message_type, now_iso())
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.error("Chat logging failed | %s: %s", type(exc).__name__, exc)


def log_action(phone: str, action: str):
    try:
        conn = get_db()
        conn.execute("INSERT INTO actions (phone, action, created_at) VALUES (?, ?, ?)", (phone, action, now_iso()))
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.error("Action logging failed | %s: %s", type(exc).__name__, exc)


def get_conversation(phone: str) -> Dict[str, Any]:
    conn = get_db()
    row = conn.execute("SELECT * FROM conversations WHERE phone = ?", (phone,)).fetchone()
    conn.close()

    if not row:
        return {
            "phone": phone,
            "client_id": "glaze-dental",
            "state": "IDLE",
            "service": None,
            "patient_name": None,
            "patient_type": None,
            "appointment_date": None,
            "appointment_time": None
        }
    return dict(row)


def update_conversation(
    phone: str,
    client_id: Optional[str] = None,
    state: Optional[str] = None,
    service: Optional[str] = None,
    patient_name: Optional[str] = None,
    patient_type: Optional[str] = None,
    appointment_date: Optional[str] = None,
    appointment_time: Optional[str] = None
):
    current = get_conversation(phone)
    client_id = client_id if client_id is not None else current["client_id"]
    state = state if state is not None else current["state"]
    service = service if service is not None else current["service"]
    patient_name = patient_name if patient_name is not None else current["patient_name"]
    patient_type = patient_type if patient_type is not None else current["patient_type"]
    appointment_date = appointment_date if appointment_date is not None else current["appointment_date"]
    appointment_time = appointment_time if appointment_time is not None else current["appointment_time"]

    conn = get_db()
    conn.execute("""
        INSERT INTO conversations
        (phone, client_id, state, service, patient_name, patient_type, appointment_date, appointment_time, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(phone) DO UPDATE SET
            client_id = excluded.client_id,
            state = excluded.state,
            service = excluded.service,
            patient_name = excluded.patient_name,
            patient_type = excluded.patient_type,
            appointment_date = excluded.appointment_date,
            appointment_time = excluded.appointment_time,
            updated_at = excluded.updated_at
    """, (phone, client_id, state, service, patient_name, patient_type, appointment_date, appointment_time, now_iso()))
    conn.commit()
    conn.close()


def reset_conversation(phone: str):
    update_conversation(
        phone=phone,
        state="IDLE",
        service=None,
        patient_name=None,
        patient_type=None,
        appointment_date=None,
        appointment_time=None
    )


def save_appointment_record(
    phone: str,
    client_id: str,
    service: str,
    patient_name: str,
    patient_type: str,
    appointment_date: str,
    appointment_time: str,
    crm_appointment_id: Optional[str] = None
):
    conn = get_db()
    conn.execute("""
        INSERT INTO appointments
        (phone, client_id, crm_appointment_id, service, patient_name, patient_type, appointment_date, appointment_time, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', ?)
    """, (phone, client_id, crm_appointment_id, service, patient_name, patient_type, appointment_date, appointment_time, now_iso()))
    conn.commit()
    conn.close()


# ============================================================
# META WHATSAPP CLOUD API
# ============================================================

def _send_payload(payload: Dict[str, Any]) -> bool:
    if not WHATSAPP_TOKEN:
        logger.error("META_WHATSAPP_TOKEN is missing")
        return False

    url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }

    try:
        logger.info("WHATSAPP OUTBOUND | URL=%s | TO=%s", url, payload.get("to"))
        response = requests.post(url, headers=headers, json=payload, timeout=15)
        logger.info("WHATSAPP RESPONSE | STATUS=%s | BODY=%s", response.status_code, response.text[:500])
        response.raise_for_status()
        return True
    except requests.exceptions.RequestException as exc:
        logger.error("WHATSAPP SEND ERROR | %s: %s", type(exc).__name__, exc)
        return False


def send_text_message(phone: str, text: str) -> bool:
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {"preview_url": False, "body": text}
    }
    success = _send_payload(payload)
    if success:
        log_chat(phone, "outgoing", text, "text")
    return success


def send_button_message(phone: str, body: str, buttons: List[Dict[str, str]]) -> bool:
    buttons = buttons[:3]
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body},
            "action": {
                "buttons": [
                    {
                        "type": "reply",
                        "reply": {"id": b["id"][:256], "title": b["title"][:20]}
                    }
                    for b in buttons
                ]
            }
        }
    }
    success = _send_payload(payload)
    if success:
        log_chat(phone, "outgoing", body, "interactive_button")
    return success


def send_list_message(phone: str, body: str, button_text: str, rows: List[Dict[str, str]]) -> bool:
    rows = rows[:10]
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body},
            "action": {
                "button": button_text[:20],
                "sections": [
                    {
                        "title": "Options",
                        "rows": [
                            {
                                "id": r["id"][:200],
                                "title": r["title"][:24],
                                "description": r.get("description", "")[:72]
                            }
                            for r in rows
                        ]
                    }
                ]
            }
        }
    }
    success = _send_payload(payload)
    if success:
        log_chat(phone, "outgoing", body, "interactive_list")
    return success


# ============================================================
# UI MENUS
# ============================================================

def main_menu(phone: str) -> bool:
    body = f"Hi! 👋 Welcome to {clinic_name()}.\n\nHow can we help you today?"
    buttons = [
        {"id": "book_appointment", "title": "📅 Book appointment"},
        {"id": "services", "title": "🦷 Our services"},
        {"id": "clinic_info", "title": "📍 Clinic info"}
    ]
    return send_button_message(phone, body, buttons)


def send_services(phone: str) -> bool:
    services = KNOWLEDGE.get("services", [])
    rows = []
    for i, s in enumerate(services[:10]):
        name = s if isinstance(s, str) else s.get("name", f"Service {i+1}")
        desc = "" if isinstance(s, str) else s.get("description", "")
        rows.append({"id": f"service_{i}", "title": name[:24], "description": desc[:72]})

    return send_list_message(phone, f"Here are the services available at {clinic_name()}:", "View services", rows)


def send_clinic_info(phone: str) -> bool:
    doctor = KNOWLEDGE.get("doctor", {})
    location = KNOWLEDGE.get("location", {})
    contact = KNOWLEDGE.get("contact", {})
    hours = KNOWLEDGE.get("hours", {})

    text = (
        f"📍 *{clinic_name()}*\n\n"
        f"👨‍⚕️ {doctor.get('name', 'Dr. Shadab Mulla')}\n"
        f"{doctor.get('qualification', 'B.D.S.')} · {doctor.get('designation', 'Dental Surgeon')}\n"
        f"{doctor.get('experience', '20 years of experience')}\n\n"
        f"📍 {location.get('address', 'Shop No 1, Monika 16, Pimpri Colony')}\n"
        f"🗺️ {location.get('maps', '')}\n\n"
        f"📞 Phone: {contact.get('phone', '9822977740')}\n\n"
        f"🕒 Monday–Saturday\n"
        f"• {hours.get('morning', '10:00 AM - 1:00 PM')}\n"
        f"• {hours.get('evening', '6:00 PM - 9:00 PM')}"
    )
    return send_text_message(phone, text)


def send_booking_services(phone: str) -> bool:
    services = KNOWLEDGE.get("services", [])
    rows = []
    for i, s in enumerate(services[:10]):
        name = s if isinstance(s, str) else s.get("name", f"Service {i+1}")
        desc = "" if isinstance(s, str) else s.get("description", "")
        rows.append({"id": f"booking_service_{i}", "title": name[:24], "description": desc[:72]})

    return send_list_message(phone, "What treatment would you like to book?", "Choose service", rows)


def send_patient_type_options(phone: str) -> bool:
    buttons = [
        {"id": "patient_new", "title": "New patient"},
        {"id": "patient_existing", "title": "Existing patient"}
    ]
    return send_button_message(phone, "Are you a new or existing patient?", buttons)


def send_date_options(phone: str) -> bool:
    today = datetime.date.today()
    tomorrow = today + datetime.timedelta(days=1)
    body = "When would you like your appointment?"
    buttons = [
        {"id": f"date_{today.isoformat()}", "title": "Today"},
        {"id": f"date_{tomorrow.isoformat()}", "title": "Tomorrow"},
        {"id": "choose_date", "title": "Choose another date"}
    ]
    return send_button_message(phone, body, buttons)


def parse_date_input(text: str) -> Optional[str]:
    text = text.strip().lower()
    today = datetime.date.today()

    if text in {"today", "tod", "aaj"}:
        return today.isoformat()
    if text in {"tomorrow", "tmrw", "tmr", "kal"}:
        return (today + datetime.timedelta(days=1)).isoformat()

    formats = ["%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d/%m/%y", "%d-%m-%y"]
    for fmt in formats:
        try:
            parsed = datetime.datetime.strptime(text, fmt).date()
            if parsed < today:
                return None
            return parsed.isoformat()
        except ValueError:
            continue

    return None


def is_emergency(text: str) -> bool:
    text_lower = text.lower()
    emergency_keywords = ["accident", "extreme pain", "severe pain", "allergy", "emergency", "urgent", "bleeding badly"]
    return any(w in text_lower for w in emergency_keywords)


def is_pricing_question(text: str) -> bool:
    text_lower = text.lower()
    pricing_keywords = ["price", "cost", "fee", "fees", "charge", "charges", "how much", "rate", "rates", "pricing"]
    return any(w in text_lower for w in pricing_keywords)


# ============================================================
# GEMINI FREE TEXT FALLBACK
# ============================================================

def ask_gemini(question: str) -> Optional[str]:
    if not gemini_model:
        return None

    knowledge_text = json.dumps(KNOWLEDGE, ensure_ascii=False, indent=2)

    prompt = f"""
You are the customer support assistant for {clinic_name()}.

Answer the customer's question using ONLY the verified information in the knowledge base below.

CRITICAL RULES:
- Do not invent prices, consultation fees, discounts, or treatment costs.
- Do not invent doctors, branches, insurance, or medical guarantees.
- If asked about prices or fees, clearly state that pricing details are not listed and recommend calling {clinic_name()} at 9822977740.
- If the answer is not available in the knowledge base, politely explain that you do not have that information and suggest contacting the clinic.
- Keep the answer short (1-3 sentences), reassuring, and natural for WhatsApp.

Knowledge base:
{knowledge_text}

Customer question:
{question}
"""
    try:
        response = gemini_model.generate_content(
            prompt,
            generation_config={"temperature": 0.2, "max_output_tokens": 200}
        )
        answer = getattr(response, "text", None)
        if answer:
            return answer.strip()
    except Exception as exc:
        logger.error("GEMINI ERROR | %s: %s", type(exc).__name__, exc)

    return None


# ============================================================
# MESSAGE HANDLER & DETERMINISTIC STATE MACHINE
# ============================================================

def handle_user_message(phone: str, msg_type: str, text: str, action_id: Optional[str] = None):
    conversation = get_conversation(phone)
    current_state = conversation.get("state", "IDLE")
    client_id = conversation.get("client_id", "glaze-dental")
    normalized = text.strip().lower()

    logger.info(
        "INCOMING MESSAGE | USER=%s | CLIENT=%s | TYPE=%s | STATE=%s | ACTION=%s | TEXT=%s",
        phone, client_id, msg_type, current_state, action_id, text
    )

    log_chat(phone, "incoming", text, msg_type)
    if action_id:
        log_action(phone, action_id)

    # --------------------------------------------------------
    # 1. EMERGENCY CHECK (Priority: Before normal booking routing)
    # --------------------------------------------------------
    if is_emergency(text):
        emergency_msg = (
            "🚨 This may require urgent attention. Please call Glaze Dental Clinic at 9822977740 "
            "immediately for emergency assistance."
        )
        send_text_message(phone, emergency_msg)
        return

    # --------------------------------------------------------
    # 2. GLOBAL RESET / CANCEL
    # --------------------------------------------------------
    if normalized in {"cancel", "restart", "reset", "menu", "main menu"} or action_id in {"cancel_booking", "btn_cancel"}:
        reset_conversation(phone)
        if normalized in {"cancel", "restart", "reset"} or action_id in {"cancel_booking", "btn_cancel"}:
            send_text_message(phone, "Your booking process has been cancelled. Let us know whenever you'd like to book or ask a question.")
        return main_menu(phone)

    # --------------------------------------------------------
    # 3. GREETINGS
    # --------------------------------------------------------
    greetings = {"hi", "hii", "hiii", "hello", "hey", "hey there", "good morning", "good afternoon", "good evening"}
    if normalized in greetings and current_state == "IDLE":
        reset_conversation(phone)
        return main_menu(phone)

    # --------------------------------------------------------
    # 4. PRICING INQUIRIES
    # --------------------------------------------------------
    if is_pricing_question(text) and current_state == "IDLE":
        price_msg = "Pricing details are not listed. Please contact Glaze Dental Clinic at 9822977740 for treatment charges."
        send_text_message(phone, price_msg)
        return main_menu(phone)

    # --------------------------------------------------------
    # 5. IDLE STATE
    # --------------------------------------------------------
    if current_state == "IDLE":
        if action_id in {"book_appointment", "btn_book"}:
            update_conversation(phone, state="BOOKING_SERVICE")
            return send_booking_services(phone)

        if action_id in {"services", "btn_services"}:
            return send_services(phone)

        if action_id in {"clinic_info", "btn_info"}:
            return send_clinic_info(phone)

        # Free-text Q&A
        answer = ask_gemini(text)
        if answer:
            send_text_message(phone, answer)
        else:
            send_text_message(phone, "I can help with appointment bookings, services, and clinic information.")
        return main_menu(phone)

    # --------------------------------------------------------
    # 6. BOOKING_SERVICE
    # --------------------------------------------------------
    if current_state == "BOOKING_SERVICE":
        service = None
        services = KNOWLEDGE.get("services", [])

        if action_id and action_id.startswith("booking_service_"):
            try:
                idx = int(action_id.replace("booking_service_", ""))
                if 0 <= idx < len(services):
                    item = services[idx]
                    service = item if isinstance(item, str) else item.get("name")
            except (ValueError, IndexError):
                service = None

        if not service:
            for item in services:
                name = item if isinstance(item, str) else item.get("name", "")
                if name.lower() == normalized or normalized in name.lower():
                    service = name
                    break

        if not service:
            send_text_message(phone, "Please choose one of the available dental services below:")
            return send_booking_services(phone)

        update_conversation(phone, state="BOOKING_NAME", service=service)
        send_text_message(phone, f"You selected: *{service}*.\n\nPlease provide the patient's *full name*:")
        return

    # --------------------------------------------------------
    # 7. BOOKING_NAME
    # --------------------------------------------------------
    if current_state == "BOOKING_NAME":
        patient_name = text.strip()
        if len(patient_name) < 2 or patient_name.isdigit():
            send_text_message(phone, "Please enter a valid patient name (e.g. Rahul Sharma):")
            return

        update_conversation(phone, state="BOOKING_PATIENT_TYPE", patient_name=patient_name)
        return send_patient_type_options(phone)

    # --------------------------------------------------------
    # 8. BOOKING_PATIENT_TYPE
    # --------------------------------------------------------
    if current_state == "BOOKING_PATIENT_TYPE":
        patient_type = None
        if action_id == "patient_new" or "new" in normalized:
            patient_type = "New"
        elif action_id == "patient_existing" or "existing" in normalized:
            patient_type = "Existing"

        if not patient_type:
            send_text_message(phone, "Please indicate whether you are a new or existing patient:")
            return send_patient_type_options(phone)

        update_conversation(phone, state="BOOKING_DATE", patient_type=patient_type)
        return send_date_options(phone)

    # --------------------------------------------------------
    # 9. BOOKING_DATE
    # --------------------------------------------------------
    if current_state == "BOOKING_DATE":
        appointment_date = None

        if action_id and action_id.startswith("date_"):
            appointment_date = action_id.replace("date_", "")
        elif action_id == "choose_date":
            send_text_message(phone, "Please send your preferred date in YYYY-MM-DD format (for example: 2026-09-20):")
            return
        else:
            appointment_date = parse_date_input(text)

        if not appointment_date:
            send_text_message(phone, "I couldn't recognize that date. Please use YYYY-MM-DD (e.g., 2026-09-20) or choose Today/Tomorrow:")
            return send_date_options(phone)

        # Query CRM for live available slots
        available_slots = get_available_slots(client_id, appointment_date)

        if available_slots is None:
            # CRM API error
            send_text_message(
                phone,
                "Sorry, I'm unable to check live appointment availability right now. "
                "Please try again shortly or call Glaze Dental Clinic at 9822977740."
            )
            return

        if not available_slots:
            # Successfully checked CRM, but zero slots available
            send_text_message(
                phone,
                f"There are no available slots on {appointment_date}. Please select another date:"
            )
            return send_date_options(phone)

        update_conversation(phone, state="BOOKING_TIME", appointment_date=appointment_date)
        rows = [{"id": f"time_{slot}", "title": slot[:24], "description": "Available"} for slot in available_slots[:10]]
        return send_list_message(phone, f"Available slots for *{appointment_date}*:", "Choose time", rows)

    # --------------------------------------------------------
    # 10. BOOKING_TIME
    # --------------------------------------------------------
    if current_state == "BOOKING_TIME":
        appointment_date = conversation.get("appointment_date")

        selected_time = None
        if action_id and action_id.startswith("time_"):
            # Trust the action_id directly — it was built from our own CRM slot list
            selected_time = action_id[len("time_"):]
        else:
            # Text-based fallback: re-query CRM to validate the typed time
            available_slots = get_available_slots(client_id, appointment_date) if appointment_date else None
            if available_slots is None:
                send_text_message(
                    phone,
                    "Sorry, I'm unable to check live appointment availability right now. "
                    "Please try again shortly or call Glaze Dental Clinic at 9822977740."
                )
                return
            for s in available_slots:
                if normalized == s.lower():
                    selected_time = s
                    break

            if not selected_time:
                send_text_message(phone, "Please select one of the available slots listed below:")
                rows = [{"id": f"time_{slot}", "title": slot[:24], "description": "Available"} for slot in available_slots[:10]]
                return send_list_message(phone, f"Available slots for *{appointment_date}*:", "Choose time", rows)

        update_conversation(phone, state="BOOKING_CONFIRMATION", appointment_time=selected_time)

        service_val = conversation.get("service", "Dental consultation")
        name_val = conversation.get("patient_name", "Patient")
        ptype_val = conversation.get("patient_type", "New")

        summary = (
            "Please confirm your appointment:\n\n"
            f"👤 Name: {name_val}\n"
            f"📋 Patient: {ptype_val} patient\n"
            f"🦷 Service: {service_val}\n"
            f"📅 Date: {appointment_date}\n"
            f"⏰ Time: {selected_time}\n\n"
            "Would you like to confirm?"
        )
        buttons = [
            {"id": "confirm_booking", "title": "Confirm"},
            {"id": "cancel_booking", "title": "Cancel"}
        ]
        return send_button_message(phone, summary, buttons)

    # --------------------------------------------------------
    # 11. BOOKING_CONFIRMATION
    # --------------------------------------------------------
    if current_state == "BOOKING_CONFIRMATION":
        if action_id == "confirm_booking" or normalized in {"confirm", "yes", "ok"}:
            c_name = conversation.get("patient_name", "")
            c_service = conversation.get("service", "")
            c_ptype = conversation.get("patient_type", "")
            c_date = conversation.get("appointment_date", "")
            c_time = conversation.get("appointment_time", "")

            # Call CRM booking endpoint
            res = book_appointment(
                client_id=client_id,
                customer_name=c_name,
                customer_phone=phone,
                booking_date=c_date,
                booking_time=c_time
            )

            status = res.get("status")
            data = res.get("data", {})

            if status == "success":
                # 201 Created
                crm_apt_id = data.get("appointmentId") or data.get("id") or data.get("appointment", {}).get("id")
                save_appointment_record(
                    phone=phone,
                    client_id=client_id,
                    service=c_service,
                    patient_name=c_name,
                    patient_type=c_ptype,
                    appointment_date=c_date,
                    appointment_time=c_time,
                    crm_appointment_id=crm_apt_id
                )
                reset_conversation(phone)

                confirmation_msg = (
                    f"✅ Your appointment has been confirmed at {clinic_name()}.\n\n"
                    f"Date: {c_date}\n"
                    f"Time: {c_time}\n"
                    f"Service: {c_service}\n\n"
                    "Thank you!"
                )
                send_text_message(phone, confirmation_msg)
                return main_menu(phone)

            elif status == "conflict":
                # 409 Conflict: Slot was booked by someone else
                send_text_message(
                    phone,
                    "Sorry, that time slot is already booked. Please choose another available time."
                )
                fresh_slots = get_available_slots(client_id, c_date)
                if fresh_slots:
                    update_conversation(phone, state="BOOKING_TIME")
                    rows = [{"id": f"time_{slot}", "title": slot[:24], "description": "Available"} for slot in fresh_slots[:10]]
                    return send_list_message(phone, f"Fresh available slots for *{c_date}*:", "Choose time", rows)
                else:
                    update_conversation(phone, state="BOOKING_DATE")
                    send_text_message(phone, f"No more slots remain on {c_date}. Please choose another date:")
                    return send_date_options(phone)

            else:
                # Other CRM failure
                send_text_message(
                    phone,
                    "I couldn't confirm the appointment right now. "
                    "Please try again shortly or call Glaze Dental Clinic at 9822977740."
                )
                return main_menu(phone)

        elif action_id in {"cancel_booking", "btn_cancel"} or normalized in {"cancel", "no"}:
            reset_conversation(phone)
            send_text_message(phone, "Your appointment booking has been cancelled.")
            return main_menu(phone)

        else:
            send_text_message(phone, "Please choose Confirm or Cancel:")
            buttons = [
                {"id": "confirm_booking", "title": "Confirm"},
                {"id": "cancel_booking", "title": "Cancel"}
            ]
            return send_button_message(phone, "Would you like to confirm your appointment?", buttons)

    # --------------------------------------------------------
    # 12. FALLBACK
    # --------------------------------------------------------
    reset_conversation(phone)
    send_text_message(phone, f"Welcome to {clinic_name()}. Send 'hi' to see the main menu.")
    return main_menu(phone)


# ============================================================
# META WEBHOOK VERIFICATION & RECEIVER
# ============================================================

@app.get("/webhook")
async def verify_webhook(
    hub_mode: Optional[str] = Query(default=None, alias="hub.mode"),
    hub_verify_token: Optional[str] = Query(default=None, alias="hub.verify_token"),
    hub_challenge: Optional[str] = Query(default=None, alias="hub.challenge")
):
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        logger.info("WEBHOOK VERIFICATION SUCCESS")
        return PlainTextResponse(hub_challenge or "")

    logger.warning("WEBHOOK VERIFICATION FAILED")
    return PlainTextResponse("Forbidden", status_code=403)


@app.post("/webhook")
async def whatsapp_webhook(request: Request):
    try:
        raw_body = await request.body()
    except Exception as exc:
        logger.error("WEBHOOK BODY READ ERROR | %s: %s", type(exc).__name__, exc)
        return JSONResponse({"status": "body_read_error"}, status_code=200)

    if not raw_body:
        return JSONResponse({"status": "invalid_json", "reason": "empty_body"}, status_code=200)

    try:
        body = json.loads(raw_body.decode("utf-8"))
    except Exception as exc:
        logger.error("WEBHOOK JSON PARSE ERROR | %s", exc)
        return JSONResponse({"status": "invalid_json", "reason": "json_decode_error"}, status_code=200)

    if body.get("object") != "whatsapp_business_account":
        return JSONResponse({"status": "ignored", "reason": "invalid_object"})

    processed = 0
    entries = body.get("entry", [])

    for entry in entries:
        for change in entry.get("changes", []):
            value = change.get("value", {})
            messages = value.get("messages", [])

            for message in messages:
                sender = message.get("from")
                msg_type = message.get("type", "unknown")
                if not sender:
                    continue

                text_content = ""
                action_id = None

                if msg_type == "text":
                    text_content = message.get("text", {}).get("body", "").strip()
                elif msg_type == "interactive":
                    interactive = message.get("interactive", {})
                    itype = interactive.get("type")
                    if itype == "button_reply":
                        btn = interactive.get("button_reply", {})
                        action_id = btn.get("id")
                        text_content = btn.get("title", "").strip()
                    elif itype == "list_reply":
                        litem = interactive.get("list_reply", {})
                        action_id = litem.get("id")
                        text_content = litem.get("title", "").strip()

                if not text_content and not action_id:
                    continue

                try:
                    handle_user_message(
                        phone=sender,
                        msg_type=msg_type,
                        text=text_content,
                        action_id=action_id
                    )
                    processed += 1
                except Exception as exc:
                    logger.error("MESSAGE HANDLER ERROR | USER=%s | %s: %s", sender, type(exc).__name__, exc, exc_info=True)
                    try:
                        send_text_message(sender, "Sorry, something went wrong. Please try again.")
                    except Exception:
                        pass

    return JSONResponse({"status": "ok", "processed": processed}, status_code=200)


# ============================================================
# HEALTH & DASHBOARD / PORTAL ENDPOINTS
# ============================================================

@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "Glaze Dental Clinic WhatsApp AI",
        "webhook": "/webhook",
        "graph_api_version": GRAPH_API_VERSION
    }


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_portal(client_id: str = "glaze-dental"):
    config = get_client_crm_config(client_id) or {
        "client_id": client_id,
        "client_name": "Glaze Dental Clinic",
        "crm_base_url": "",
        "crm_tenant_id": ""
    }

    html = f"""<!DOCTYPE html>
<html>
<head>
    <title>WhatsApp CRM Configuration</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #f4f6f8; margin: 0; padding: 30px; }}
        .card {{ max-width: 600px; margin: 0 auto; background: white; padding: 25px; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
        h2 {{ margin-top: 0; color: #1e293b; }}
        .form-group {{ margin-bottom: 16px; }}
        label {{ display: block; font-weight: 600; margin-bottom: 6px; color: #334155; }}
        input[type="text"] {{ width: 100%; box-sizing: border-box; padding: 10px; border: 1px solid #cbd5e1; border-radius: 6px; font-size: 14px; }}
        .btn {{ background: #2563eb; color: white; border: none; padding: 10px 18px; border-radius: 6px; cursor: pointer; font-size: 14px; font-weight: 600; }}
        .btn:hover {{ background: #1d4ed8; }}
        .btn-test {{ background: #059669; margin-left: 8px; }}
        .btn-test:hover {{ background: #047857; }}
        #status {{ margin-top: 15px; font-weight: 600; }}
        .success {{ color: #059669; }}
        .error {{ color: #dc2626; }}
    </style>
</head>
<body>
<div class="card">
    <h2>CRM Integration & Client Settings</h2>
    <form id="crmForm">
        <div class="form-group">
            <label>Client ID</label>
            <input type="text" id="clientId" value="{config['client_id']}" readonly style="background: #f1f5f9;">
        </div>
        <div class="form-group">
            <label>Client Name</label>
            <input type="text" id="clientName" value="{config['client_name']}" required>
        </div>
        <div class="form-group">
            <label>CRM API Base URL</label>
            <input type="text" id="crmBaseUrl" value="{config['crm_base_url']}" placeholder="https://dandelion-gigantic-challenge.ngrok-free.dev" required>
        </div>
        <div class="form-group">
            <label>Tenant ID (X-Tenant-Key)</label>
            <input type="text" id="crmTenantId" value="{config['crm_tenant_id']}" placeholder="6b4b6128-5b5f-4d2f-b5de-91511ab9b120" required>
        </div>
        <div>
            <button type="submit" class="btn">Save CRM Configuration</button>
            <button type="button" class="btn btn-test" onclick="testConnection()">Test CRM Connection</button>
        </div>
    </form>
    <div id="status"></div>
</div>

<script>
document.getElementById('crmForm').onsubmit = async (e) => {{
    e.preventDefault();
    const statusDiv = document.getElementById('status');
    statusDiv.textContent = 'Saving...';
    statusDiv.className = '';
    
    try {{
        const res = await fetch('/api/client-config', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify({{
                client_id: document.getElementById('clientId').value,
                client_name: document.getElementById('clientName').value,
                crm_base_url: document.getElementById('crmBaseUrl').value,
                crm_tenant_id: document.getElementById('crmTenantId').value
            }})
        }});
        const data = await res.json();
        if (res.ok) {{
            statusDiv.textContent = 'Configuration saved successfully!';
            statusDiv.className = 'success';
        }} else {{
            statusDiv.textContent = 'Error: ' + (data.detail || data.error || 'Save failed');
            statusDiv.className = 'error';
        }}
    }} catch (err) {{
        statusDiv.textContent = 'Error saving configuration.';
        statusDiv.className = 'error';
    }}
}};

async function testConnection() {{
    const statusDiv = document.getElementById('status');
    statusDiv.textContent = 'Testing connection...';
    statusDiv.className = '';
    
    try {{
        const clientId = document.getElementById('clientId').value;
        const res = await fetch(`/api/test-crm-connection?client_id=${{encodeURIComponent(clientId)}}`);
        const data = await res.json();
        if (data.status === 'success') {{
            statusDiv.textContent = 'Connection successful! (CRM slots verified)';
            statusDiv.className = 'success';
        }} else {{
            statusDiv.textContent = 'Connection failed: ' + (data.message || 'Unable to reach CRM');
            statusDiv.className = 'error';
        }}
    }} catch (err) {{
        statusDiv.textContent = 'Connection failed';
        statusDiv.className = 'error';
    }}
}}
</script>
</body>
</html>
"""
    return HTMLResponse(content=html)


@app.get("/api/client-config")
async def get_client_config_api(client_id: str = "glaze-dental"):
    config = get_client_crm_config(client_id)
    if not config:
        return JSONResponse({"status": "error", "message": "Client not found"}, status_code=404)
    return {
        "client_id": config["client_id"],
        "client_name": config["client_name"],
        "crm_base_url": config["crm_base_url"],
        "crm_tenant_id": config["crm_tenant_id"]
    }


@app.post("/api/client-config")
async def save_client_config_api(request: Request):
    try:
        body = await request.json()
        client_id = body.get("client_id", "glaze-dental")
        client_name = body.get("client_name", "Glaze Dental Clinic")
        crm_base_url = body.get("crm_base_url", "")
        crm_tenant_id = body.get("crm_tenant_id", "")

        save_client_crm_config(
            client_id=client_id,
            client_name=client_name,
            crm_base_url=crm_base_url,
            crm_tenant_id=crm_tenant_id
        )
        return {"status": "success", "message": "CRM configuration saved successfully"}
    except ValueError as val_err:
        return JSONResponse({"status": "error", "message": str(val_err)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=500)


@app.get("/api/test-crm-connection")
async def test_crm_connection_endpoint(client_id: str = "glaze-dental", test_date: str = "2026-09-16"):
    slots = get_available_slots(client_id, test_date)
    if slots is not None:
        return {
            "status": "success",
            "message": "Connection successful",
            "date_tested": test_date,
            "available_slots_count": len(slots)
        }
    else:
        return JSONResponse(
            {
                "status": "error",
                "message": "Connection failed. Check CRM API URL and Tenant ID."
            },
            status_code=502
        )