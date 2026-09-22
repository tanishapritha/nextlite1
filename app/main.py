import os
import json
import logging
import sqlite3
import datetime
import asyncio
import hashlib
import hmac
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests

from fastapi import FastAPI, Request, Query
from fastapi.responses import PlainTextResponse, JSONResponse, HTMLResponse


# ============================================================
# APP INITIALIZATION
# ============================================================

app = FastAPI(title="Glaze Dental Clinic WhatsApp AI")


# ============================================================
# CONFIGURATION
# ============================================================

VERIFY_TOKEN = os.getenv("META_VERIFY_TOKEN", "nextlite-demo")
META_APP_SECRET = os.getenv("META_APP_SECRET", "")
GRAPH_API_VERSION = os.getenv("META_GRAPH_API_VERSION", "v26.0")
# Client A (Glaze) credentials are kept by us for now.
# Only the environment-variable name is stored in the tenant configuration.
GLAZE_WHATSAPP_TOKEN = os.getenv("GLAZE_WHATSAPP_TOKEN", "")
GLAZE_CRM_TENANT_KEY = os.getenv("GLAZE_CRM_TENANT_KEY", "")

DB_PATH = "nextlite.db"
KNOWLEDGE_PATH = "nextlite.json"
TIMEZONE_KOLKATA = ZoneInfo("Asia/Kolkata")


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger("nextlite")


# ============================================================
# KNOWLEDGE BASE & SERVICE CONFIGURATION
# ============================================================

DEFAULT_KNOWLEDGE = {
    "client_id": "glaze-dental",
    "client_name": "Glaze Dental Clinic",
    "crm_base_url": "https://vanifyai.online",
    "phone_number_id": "1208541249018781",
    "assistant_name": "Glaze Dental Clinic Assistant",
    "doctor": {
        "name": "Dr SHADAB MULLA",
        "qualification": "B.D.S.",
        "designation": "DENTAL SURGEON",
        "experience": "20 years"
    },
    "languages": [
        "English",
        "Hindi",
        "Marathi",
        "Hinglish"
    ],
    "location": {
        "address": "SHOP NO 1 MONIKA 16 PIMPRI COLONY",
        "maps": "https://maps.app.goo.gl/H6872MYf2gdthTZ7A?g_st=ac"
    },
    "contact": {
        "phone": "9822977740",
        "whatsapp": "9822977740"
    },
    # EXACTLY TWO SERVICES CONFIGURED IN ONE PLACE
    "services": [
        {
            "name": "Appointment",
            "description": "Book a dental appointment with the clinic."
        },
        {
            "name": "Treatment",
            "description": "Ask about or book dental treatment."
        }
    ],
    "clinic": {
        "speciality": "Painless treatment",
        "appointment_duration": "30 minutes"
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
        "morning": "10 AM-1 PM",
        "evening": "6 PM-9 PM"
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


def get_configured_services() -> List[Dict[str, str]]:
    """
    Returns the exact configured services (EXACTLY TWO SERVICES).
    """
    raw_services = KNOWLEDGE.get("services", [])
    result = []
    for s in raw_services[:2]:
        if isinstance(s, str):
            result.append({"name": s, "description": s})
        elif isinstance(s, dict):
            result.append({"name": s.get("name", "Service"), "description": s.get("description", "")})
    return result


# ============================================================
# DATABASE & PERSISTENCE
# ============================================================

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    cursor = conn.cursor()

    # 1. Client configuration table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS client_config (
            client_id TEXT PRIMARY KEY,
            client_name TEXT NOT NULL,
            phone_number_id TEXT,
            crm_base_url TEXT NOT NULL,
            crm_tenant_id TEXT NOT NULL DEFAULT '',
            whatsapp_token_env TEXT NOT NULL DEFAULT '',
            crm_tenant_key_env TEXT NOT NULL DEFAULT '',
            reminder_template_name TEXT NOT NULL DEFAULT 'glaze_appointment_1h_reminder',
            updated_at TEXT NOT NULL
        )
    """)

    _existing_cfg_cols = {row[1] for row in cursor.execute("PRAGMA table_info(client_config)")}
    if "phone_number_id" not in _existing_cfg_cols:
        cursor.execute("ALTER TABLE client_config ADD COLUMN phone_number_id TEXT")
    if "whatsapp_token_env" not in _existing_cfg_cols:
        cursor.execute("ALTER TABLE client_config ADD COLUMN whatsapp_token_env TEXT NOT NULL DEFAULT ''")
    if "crm_tenant_key_env" not in _existing_cfg_cols:
        cursor.execute("ALTER TABLE client_config ADD COLUMN crm_tenant_key_env TEXT NOT NULL DEFAULT ''")
    if "reminder_template_name" not in _existing_cfg_cols:
        cursor.execute("ALTER TABLE client_config ADD COLUMN reminder_template_name TEXT NOT NULL DEFAULT 'glaze_appointment_1h_reminder'")

    # 2. Chats table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT NOT NULL,
            client_id TEXT NOT NULL DEFAULT 'glaze-dental',
            direction TEXT NOT NULL,
            message TEXT,
            message_type TEXT,
            created_at TEXT NOT NULL
        )
    """)

    _existing_chats_cols = {row[1] for row in cursor.execute("PRAGMA table_info(chats)")}
    for _col, _def in [
        ("client_id", "TEXT NOT NULL DEFAULT 'glaze-dental'"),
        ("direction", "TEXT NOT NULL DEFAULT 'inbound'"),
        ("message_type", "TEXT"),
    ]:
        if _col not in _existing_chats_cols:
            cursor.execute(f"ALTER TABLE chats ADD COLUMN {_col} {_def}")

    # 3. Actions table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT NOT NULL,
            client_id TEXT NOT NULL DEFAULT 'glaze-dental',
            action TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    """)

    # 4. Conversations table
    # A conversation is unique per (client_id, phone), not globally per phone.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            phone TEXT NOT NULL,
            client_id TEXT NOT NULL DEFAULT 'glaze-dental',
            state TEXT NOT NULL DEFAULT 'IDLE',
            service TEXT,
            patient_name TEXT,
            patient_age TEXT,
            patient_type TEXT,
            appointment_date TEXT,
            appointment_time TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (client_id, phone)
        )
    """)

    _existing_conv_cols = {row[1] for row in cursor.execute("PRAGMA table_info(conversations)")}
    if "client_id" not in _existing_conv_cols:
        cursor.execute("ALTER TABLE conversations ADD COLUMN client_id TEXT NOT NULL DEFAULT 'glaze-dental'")
    for _col, _def in [
        ("state", "TEXT NOT NULL DEFAULT 'IDLE'"),
        ("service", "TEXT"),
        ("patient_name", "TEXT"),
        ("patient_age", "TEXT"),
        ("patient_type", "TEXT"),
        ("appointment_date", "TEXT"),
        ("appointment_time", "TEXT"),
    ]:        if _col not in _existing_conv_cols:
            cursor.execute(f"ALTER TABLE conversations ADD COLUMN {_col} {_def}")

    # Existing deployments used phone as the sole primary key. Rebuild that
    # table once so the primary key becomes (client_id, phone).
    _conv_pk_cols = [row for row in cursor.execute("PRAGMA table_info(conversations)") if row[5] > 0]
    if len(_conv_pk_cols) == 1 and _conv_pk_cols[0][1] == "phone":
        cursor.execute("""
            CREATE TABLE conversations_v2 (
                phone TEXT NOT NULL,
                client_id TEXT NOT NULL DEFAULT 'glaze-dental',
                state TEXT NOT NULL DEFAULT 'IDLE',
                service TEXT,
                patient_name TEXT,
                patient_age TEXT,
                patient_type TEXT,
                appointment_date TEXT,
                appointment_time TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (client_id, phone)
            )
        """)
        cursor.execute("""
            INSERT OR IGNORE INTO conversations_v2
            (phone, client_id, state, service, patient_name, patient_age, patient_type, appointment_date, appointment_time, updated_at)
            SELECT phone, client_id, state, service, patient_name, patient_age, patient_type, appointment_date, appointment_time, updated_at
            FROM conversations
        """)
        cursor.execute("DROP TABLE conversations")
        cursor.execute("ALTER TABLE conversations_v2 RENAME TO conversations")

    # 5. Local Appointments table
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

    _existing_appt_cols = {row[1] for row in cursor.execute("PRAGMA table_info(appointments)")}
    for _col, _def in [
        ("client_id", "TEXT NOT NULL DEFAULT 'glaze-dental'"),
        ("crm_appointment_id", "TEXT"),
        ("patient_name", "TEXT"),
        ("patient_type", "TEXT"),
    ]:
        if _col not in _existing_appt_cols:
            cursor.execute(f"ALTER TABLE appointments ADD COLUMN {_col} {_def}")

    # 6. Idempotency table: processed_messages
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS processed_messages (
            msg_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL
        )
    """)

    # 7. Reminders table
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            appointment_id INTEGER NOT NULL,
            client_id TEXT NOT NULL,
            phone TEXT NOT NULL,
            reminder_type TEXT NOT NULL DEFAULT '1h_before',
            scheduled_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            sent_at TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(appointment_id, reminder_type)
        )
    """)

    # Seed default Glaze Dental client config
    seed_client_id = KNOWLEDGE.get("client_id", "glaze-dental")
    seed_client_name = KNOWLEDGE.get("client_name", "Glaze Dental Clinic")
    seed_crm_base_url = KNOWLEDGE.get("crm_base_url", "https://vanifyai.online").rstrip("/")
    seed_phone_number_id = KNOWLEDGE.get("phone_number_id", "").strip()
    seed_whatsapp_token_env = "GLAZE_WHATSAPP_TOKEN"
    seed_crm_tenant_key_env = "GLAZE_CRM_TENANT_KEY"
    seed_template_name = os.getenv("META_REMINDER_TEMPLATE", "glaze_appointment_1h_reminder")

    # Secret values are NOT persisted in SQLite. The DB stores only the
    # environment-variable names used to retrieve them at runtime.
    cursor.execute("""
        INSERT INTO client_config
        (client_id, client_name, phone_number_id, crm_base_url, crm_tenant_id,
         whatsapp_token_env, crm_tenant_key_env, reminder_template_name, updated_at)
        VALUES (?, ?, ?, ?, '', ?, ?, ?, ?)
        ON CONFLICT(client_id) DO UPDATE SET
            crm_base_url = excluded.crm_base_url,
            client_name = excluded.client_name,
            phone_number_id = excluded.phone_number_id,
            crm_tenant_id = '',
            whatsapp_token_env = excluded.whatsapp_token_env,
            crm_tenant_key_env = excluded.crm_tenant_key_env,
            reminder_template_name = excluded.reminder_template_name,
            updated_at = excluded.updated_at
    """, (
        seed_client_id, seed_client_name, seed_phone_number_id,
        seed_crm_base_url, seed_whatsapp_token_env, seed_crm_tenant_key_env,
        seed_template_name, datetime.datetime.now(datetime.timezone.utc).isoformat()
    ))
    logger.info("Upserted CRM configuration for client: %s | base_url: %s", seed_client_id, seed_crm_base_url)

    conn.commit()
    conn.close()
    logger.info("SQLite initialized successfully")


init_db()


@app.on_event("startup")
def startup():
    init_db()


# ============================================================
# CLIENT CONFIG RESOLUTION
# ============================================================

def get_client_crm_config(client_id: str) -> Optional[Dict[str, Any]]:
    conn = get_db()
    row = conn.execute(
        """SELECT client_id, client_name, phone_number_id, crm_base_url,
                  crm_tenant_id, whatsapp_token_env, crm_tenant_key_env,
                  reminder_template_name, updated_at
           FROM client_config WHERE client_id = ?""",
        (client_id,)
    ).fetchone()
    conn.close()

    if row:
        config = dict(row)
        env_name = config.get("crm_tenant_key_env") or ""
        if env_name:
            config["crm_tenant_id"] = os.getenv(env_name, "")
        return config

    if client_id == KNOWLEDGE.get("client_id", "glaze-dental"):
        return {
            "client_id": client_id,
            "client_name": KNOWLEDGE.get("client_name", "Glaze Dental Clinic"),
            "phone_number_id": KNOWLEDGE.get("phone_number_id", "").strip(),
            "crm_base_url": KNOWLEDGE.get("crm_base_url", "").rstrip("/"),
            "crm_tenant_id": GLAZE_CRM_TENANT_KEY,
            "whatsapp_token_env": "GLAZE_WHATSAPP_TOKEN",
            "crm_tenant_key_env": "GLAZE_CRM_TENANT_KEY",
            "reminder_template_name": os.getenv("META_REMINDER_TEMPLATE", "glaze_appointment_1h_reminder"),
            "updated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()
        }

    return None


def resolve_client_by_phone_number_id(receiving_phone_number_id: Optional[str]) -> Optional[str]:
    """
    Identifies tenant client_id strictly from Meta's phone_number_id.
    Unknown/missing phone numbers are rejected instead of falling back to a tenant.
    """
    if not receiving_phone_number_id:
        return None

    conn = get_db()
    row = conn.execute(
        "SELECT client_id FROM client_config WHERE phone_number_id = ?",
        (receiving_phone_number_id,)
    ).fetchone()
    conn.close()

    return row["client_id"] if row and row["client_id"] else None


def get_client_whatsapp_config(client_id: str) -> Optional[Dict[str, Any]]:
    conn = get_db()
    row = conn.execute(
        """SELECT client_id, phone_number_id, whatsapp_token_env,
                  reminder_template_name
           FROM client_config WHERE client_id = ?""",
        (client_id,)
    ).fetchone()
    conn.close()

    if not row:
        return None

    config = dict(row)
    env_name = config.get("whatsapp_token_env") or ""
    token = os.getenv(env_name, "") if env_name else ""
    if client_id == "glaze-dental" and not token:
        token = GLAZE_WHATSAPP_TOKEN
    config["whatsapp_token"] = token
    return config


def save_client_crm_config(client_id: str, client_name: str, crm_base_url: str, crm_tenant_id: str = "", phone_number_id: Optional[str] = None) -> bool:
    parsed = urlparse(crm_base_url.strip())
    if not (parsed.scheme in ["http", "https"] and parsed.netloc):
        raise ValueError("crm_base_url must be a valid HTTP or HTTPS URL.")

    crm_tenant_id = crm_tenant_id.strip()
    clean_base_url = crm_base_url.strip().rstrip("/")
    phone_id = phone_number_id.strip() if phone_number_id else ""

    conn = get_db()
    conn.execute("""
        INSERT INTO client_config
        (client_id, client_name, phone_number_id, crm_base_url, crm_tenant_id, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(client_id) DO UPDATE SET
            client_name = excluded.client_name,
            phone_number_id = CASE
                WHEN excluded.phone_number_id <> '' THEN excluded.phone_number_id
                ELSE client_config.phone_number_id
            END,
            crm_base_url = excluded.crm_base_url,
            crm_tenant_id = CASE
                WHEN excluded.crm_tenant_id <> '' THEN excluded.crm_tenant_id
                ELSE client_config.crm_tenant_id
            END,
            updated_at = excluded.updated_at
    """, (client_id.strip(), client_name.strip(), phone_id, clean_base_url, crm_tenant_id, datetime.datetime.now(datetime.timezone.utc).isoformat()))
    conn.commit()
    conn.close()
    return True


# ============================================================
# IDEMPOTENCY HELPERS
# ============================================================

def is_message_processed(msg_id: str) -> bool:
    if not msg_id:
        return False
    conn = get_db()
    row = conn.execute("SELECT msg_id FROM processed_messages WHERE msg_id = ?", (msg_id,)).fetchone()
    conn.close()
    return row is not None

def mark_message_processed(msg_id: str):
    if not msg_id:
        return
    conn = get_db()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO processed_messages (msg_id, created_at) VALUES (?, ?)",
            (msg_id, datetime.datetime.now(datetime.timezone.utc).isoformat())
        )
        conn.commit()
    except Exception as exc:
        logger.error("Failed to mark message processed | %s", exc)
    finally:
        conn.close()


# ============================================================
# CRM API CLIENT
# ============================================================

class CRMClient:
    @staticmethod
    def headers(crm_config: Dict[str, Any], is_post: bool = False) -> Dict[str, str]:
        hdrs = {
            "X-Tenant-Key": crm_config["crm_tenant_id"],
            "ngrok-skip-browser-warning": "true"
        }
        if is_post:
            hdrs["Content-Type"] = "application/json"
        return hdrs

    @classmethod
    def get_available_slots(cls, client_id: str, date_str: str) -> Optional[List[str]]:
        crm_config = get_client_crm_config(client_id)
        if not crm_config:
            logger.error("CRM SLOTS ERROR | No CRM config found for client=%s", client_id)
            return None

        base_url = crm_config["crm_base_url"].rstrip("/")
        url = f"{base_url}/api/v1/integrations/whatsapp/slots"
        headers = cls.headers(crm_config, is_post=False)

        try:
            logger.info("CRM SLOTS REQUEST | client=%s | date=%s", client_id, date_str)
            response = requests.get(url, params={"date": date_str}, headers=headers, timeout=15, verify=True)
            logger.info("CRM SLOTS | client=%s | date=%s | status=%d", client_id, date_str, response.status_code)

            if response.status_code != 200:
                logger.error("CRM SLOTS ERROR | status=%d | body=%s", response.status_code, response.text[:200])
                return None

            data = response.json()
            return data.get("availableSlots", [])
        except requests.exceptions.SSLError as exc:
            logger.error("CRM SLOTS TLS ERROR | url=%s | detail=%s", url, repr(exc))
            return None
        except requests.RequestException as exc:
            logger.error("CRM SLOTS REQUEST ERROR | url=%s | detail=%s", url, repr(exc))
            return None
        except Exception as exc:
            logger.error("CRM SLOTS ERROR | %s", repr(exc))
            return None

    @classmethod
    def book_appointment(
        cls,
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
        headers = cls.headers(crm_config, is_post=True)

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
                return {"status": "success", "data": response.json() if response.text else {}}

            if response.status_code == 409:
                logger.warning("CRM BOOKING CONFLICT | client=%s | date=%s | time=%s", client_id, booking_date, booking_time)
                return {"status": "conflict", "data": response.json() if response.text else {}}

            logger.error("CRM BOOKING ERROR | status=%d | body=%s", response.status_code, response.text[:200])
            return {"status": "error", "data": response.json() if response.text else {}}
        except requests.RequestException as exc:
            logger.error("CRM BOOKING REQUEST ERROR | %s", repr(exc))
            return {"status": "error", "data": {}}
        except Exception as exc:
            logger.error("CRM BOOKING ERROR | %s", repr(exc))
            return {"status": "error", "data": {}}

    @classmethod
    def cancel_appointment(cls, client_id: str, appointment_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Cancel endpoint interface.
        Note: Cancel endpoint exists on CRM, but exact request payload structure requires confirmation from CRM developer.
        """
        crm_config = get_client_crm_config(client_id)
        if not crm_config:
            logger.error("CRM CANCEL ERROR | No CRM config found for client=%s", client_id)
            return {"status": "error", "message": "No CRM config found", "status_code": 404}

        base_url = crm_config["crm_base_url"].rstrip("/")
        url = f"{base_url}/api/v1/integrations/whatsapp/appointments/cancel"
        headers = cls.headers(crm_config, is_post=True)

        try:
            logger.info("CRM CANCEL REQUEST | client=%s | data=%s", client_id, appointment_data)
            response = requests.post(url, headers=headers, json=appointment_data, timeout=20)
            logger.info("CRM CANCEL RESPONSE | client=%s | status=%d", client_id, response.status_code)

            if response.status_code in [200, 204]:
                return {"status": "success", "data": response.json() if response.text else {}, "status_code": response.status_code}
            return {"status": "error", "data": response.json() if response.text else {}, "status_code": response.status_code}
        except requests.RequestException as exc:
            logger.error("CRM CANCEL REQUEST ERROR | %s", repr(exc))
            return {"status": "error", "message": str(exc), "status_code": 500}
        except Exception as exc:
            logger.error("CRM CANCEL ERROR | %s", repr(exc))
            return {"status": "error", "message": str(exc), "status_code": 500}

    @classmethod
    def get_appointments(cls, client_id: str, filters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """
        Retrieves appointment records from CRM for the dashboard proxy.
        """
        crm_config = get_client_crm_config(client_id)
        if not crm_config:
            logger.error("CRM GET APPOINTMENTS ERROR | No CRM config found for client=%s", client_id)
            return {"status": "error", "message": "No CRM config found", "status_code": 404}

        base_url = crm_config["crm_base_url"].rstrip("/")
        url = f"{base_url}/api/v1/integrations/whatsapp/appointments"
        headers = cls.headers(crm_config, is_post=False)

        try:
            logger.info("CRM GET APPOINTMENTS REQUEST | client=%s", client_id)
            response = requests.get(url, params=filters or {}, headers=headers, timeout=15)
            logger.info("CRM GET APPOINTMENTS | client=%s | status=%d", client_id, response.status_code)

            if response.status_code == 200:
                return {"status": "success", "data": response.json() if response.text else [], "status_code": 200}
            return {"status": "error", "message": f"CRM returned status {response.status_code}", "status_code": response.status_code}
        except requests.RequestException as exc:
            logger.error("CRM GET APPOINTMENTS REQUEST ERROR | %s", repr(exc))
            return {"status": "error", "message": str(exc), "status_code": 500}
        except Exception as exc:
            logger.error("CRM GET APPOINTMENTS ERROR | %s", repr(exc))
            return {"status": "error", "message": str(exc), "status_code": 500}


def get_available_slots(client_id: str, date_str: str) -> Optional[List[str]]:
    return CRMClient.get_available_slots(client_id, date_str)


def book_appointment(
    client_id: str,
    customer_name: str,
    customer_phone: str,
    booking_date: str,
    booking_time: str,
    age: str = "",
    place: str = ""
) -> Dict[str, Any]:
    return CRMClient.book_appointment(
        client_id=client_id,
        customer_name=customer_name,
        customer_phone=customer_phone,
        booking_date=booking_date,
        booking_time=booking_time,
        age=age,
        place=place
    )


def cancel_appointment(client_id: str, appointment_data: Dict[str, Any]) -> Dict[str, Any]:
    return CRMClient.cancel_appointment(client_id, appointment_data)


def get_appointments(client_id: str, filters: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return CRMClient.get_appointments(client_id, filters)




# ============================================================
# WHATSAPP PROVIDER LAYER
# ============================================================

class MetaCloudProvider:
    @staticmethod
    def _send_payload(payload: Dict[str, Any], client_id: Optional[str] = None) -> bool:
        if not client_id:
            client_id = "glaze-dental"

        whatsapp_config = get_client_whatsapp_config(client_id)
        if not whatsapp_config:
            logger.error("WHATSAPP CONFIG MISSING | client=%s", client_id)
            return False

        token = whatsapp_config.get("whatsapp_token", "")
        phone_number_id = whatsapp_config.get("phone_number_id", "")
        if not token or not phone_number_id:
            logger.error("WHATSAPP CREDENTIALS MISSING | client=%s", client_id)
            return False

        url = f"https://graph.facebook.com/{GRAPH_API_VERSION}/{phone_number_id}/messages"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        try:
            logger.info("WHATSAPP OUTBOUND | client=%s | TO=%s", client_id, payload.get("to"))
            response = requests.post(url, headers=headers, json=payload, timeout=15)
            logger.info("WHATSAPP RESPONSE | STATUS=%s | BODY=%s", response.status_code, response.text[:500])
            response.raise_for_status()
            return True
        except requests.exceptions.RequestException as exc:
            logger.error("WHATSAPP SEND ERROR | %s: %s", type(exc).__name__, exc)
            return False

    @classmethod    def send_text(cls, phone: str, text: str, client_id: Optional[str] = None) -> bool:
        payload = {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "text",
            "text": {"preview_url": False, "body": text}
        }
        success = _send_payload(payload, client_id=client_id)
        if success:
            log_chat(phone, "outgoing", text, "text", client_id=client_id)
        return success

    @classmethod
    def send_button(cls, phone: str, body: str, buttons: List[Dict[str, str]], client_id: Optional[str] = None) -> bool:
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
        success = _send_payload(payload, client_id=client_id)
        if success:
            log_chat(phone, "outgoing", body, "interactive_button", client_id=client_id)
        return success

    @classmethod
    def send_list(cls, phone: str, body: str, button_text: str, rows: List[Dict[str, str]], client_id: Optional[str] = None) -> bool:
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
        success = _send_payload(payload, client_id=client_id)
        if success:
            log_chat(phone, "outgoing", body, "interactive_list", client_id=client_id)
        return success

    @classmethod
    def send_template(cls, phone: str, template_name: str, language_code: str = "en", components: List[Any] = None, client_id: Optional[str] = None) -> bool:
        payload = {
            "messaging_product": "whatsapp",
            "to": phone,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language_code},
                "components": components or []
            }
        }
        success = _send_payload(payload, client_id=client_id)
        if success:
            log_chat(phone, "outgoing", f"[Template: {template_name}]", "template", client_id=client_id)
        return success


def _send_payload(payload: Dict[str, Any], client_id: Optional[str] = None) -> bool:
    return MetaCloudProvider._send_payload(payload, client_id=client_id)


def _client_id_for_phone(phone: str) -> str:
    conversation = get_conversation(phone)
    return conversation.get("client_id", "glaze-dental")


def send_text_message(phone: str, text: str, client_id: Optional[str] = None) -> bool:
    client_id = client_id or _client_id_for_phone(phone)
    return MetaCloudProvider.send_text(phone, text, client_id=client_id)


def send_button_message(phone: str, body: str, buttons: List[Dict[str, str]], client_id: Optional[str] = None) -> bool:
    client_id = client_id or _client_id_for_phone(phone)
    return MetaCloudProvider.send_button(phone, body, buttons, client_id=client_id)


def send_list_message(phone: str, body: str, button_text: str, rows: List[Dict[str, str]], client_id: Optional[str] = None) -> bool:
    client_id = client_id or _client_id_for_phone(phone)
    return MetaCloudProvider.send_list(phone, body, button_text, rows, client_id=client_id)


def send_template_message(phone: str, template_name: str, language_code: str = "en", components: List[Any] = None, client_id: Optional[str] = None) -> bool:
    client_id = client_id or _client_id_for_phone(phone)
    return MetaCloudProvider.send_template(phone, template_name, language_code, components, client_id=client_id)


# ============================================================
# CONVERSATION STATE & CHAT LOGS
# ============================================================

def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def clinic_name() -> str:
    return KNOWLEDGE.get("client_name", "Glaze Dental Clinic")


def log_chat(phone: str, direction: str, message: str, message_type: str = "text", client_id: Optional[str] = None):
    try:
        client_id = client_id or _client_id_for_phone(phone)
        conn = get_db()
        conn.execute(
            "INSERT INTO chats (phone, client_id, direction, message, message_type, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (phone, client_id, direction, message, message_type, now_iso())
        )
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.error("Chat logging failed | %s: %s", type(exc).__name__, exc)


def log_action(phone: str, action: str, client_id: Optional[str] = None):
    try:
        client_id = client_id or _client_id_for_phone(phone)
        conn = get_db()
        conn.execute("INSERT INTO actions (phone, client_id, action, created_at) VALUES (?, ?, ?, ?)", (phone, client_id, action, now_iso()))
        conn.commit()
        conn.close()
    except Exception as exc:
        logger.error("Action logging failed | %s: %s", type(exc).__name__, exc)


def get_conversation(phone: str, client_id: Optional[str] = None) -> Dict[str, Any]:
    conn = get_db()
    if client_id:
        row = conn.execute(
            "SELECT * FROM conversations WHERE client_id = ? AND phone = ?",
            (client_id, phone)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM conversations WHERE phone = ? ORDER BY updated_at DESC LIMIT 1",
            (phone,)
        ).fetchone()
    conn.close()

    if not row:
        return {
            "phone": phone,
            "client_id": client_id or "glaze-dental",
            "state": "IDLE",
            "service": None,
            "patient_name": None,
            "patient_age": None,
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
    patient_age: Optional[str] = None,
    patient_type: Optional[str] = None,
    appointment_date: Optional[str] = None,
    appointment_time: Optional[str] = None
):
    current = get_conversation(phone, client_id=client_id)
    client_id = client_id if client_id is not None else current["client_id"]
    state = state if state is not None else current["state"]
    service = service if service is not None else current["service"]
    patient_name = patient_name if patient_name is not None else current["patient_name"]
    patient_age = patient_age if patient_age is not None else current.get("patient_age")
    patient_type = patient_type if patient_type is not None else current["patient_type"]
    appointment_date = appointment_date if appointment_date is not None else current["appointment_date"]
    appointment_time = appointment_time if appointment_time is not None else current["appointment_time"]

    conn = get_db()
    conn.execute("""
        INSERT INTO conversations
        (phone, client_id, state, service, patient_name, patient_age, patient_type, appointment_date, appointment_time, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(client_id, phone) DO UPDATE SET
            client_id = excluded.client_id,
            state = excluded.state,
            service = excluded.service,
            patient_name = excluded.patient_name,
            patient_age = excluded.patient_age,
            patient_type = excluded.patient_type,
            appointment_date = excluded.appointment_date,
            appointment_time = excluded.appointment_time,
            updated_at = excluded.updated_at
    """, (phone, client_id, state, service, patient_name, patient_age, patient_type, appointment_date, appointment_time, now_iso()))
    conn.commit()
    conn.close()


def reset_conversation(phone: str, client_id: Optional[str] = None):
    update_conversation(
        phone=phone,
        client_id=client_id,
        state="IDLE",
        service=None,
        patient_name=None,
        patient_age=None,
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
) -> int:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO appointments
        (phone, client_id, crm_appointment_id, service, patient_name, patient_type, appointment_date, appointment_time, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'confirmed', ?)    """, (phone, client_id, crm_appointment_id, service, patient_name, patient_type, appointment_date, appointment_time, now_iso()))
    appt_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return appt_id


# ============================================================
# APPOINTMENT REMINDER WORKER (1-HOUR BEFORE REMINDER)
# ============================================================

def parse_appointment_scheduled_dt(date_str: str, time_str: str) -> Optional[datetime.datetime]:
    """
    Parses date (YYYY-MM-DD) and time (e.g. "12:00 PM" or "06:00 PM") in Asia/Kolkata timezone.
    """
    try:
        clean_time = time_str.strip().upper()
        dt_str = f"{date_str} {clean_time}"
        parsed = datetime.datetime.strptime(dt_str, "%Y-%m-%d %I:%M %p")
        return parsed.replace(tzinfo=TIMEZONE_KOLKATA)
    except ValueError:
        try:
            parsed = datetime.datetime.strptime(f"{date_str} {time_str.strip()}", "%Y-%m-%d %H:%M")
            return parsed.replace(tzinfo=TIMEZONE_KOLKATA)
        except Exception:
            return None


def schedule_appointment_reminder(
    appointment_id: int,
    client_id: str,
    phone: str,
    appointment_date: str,
    appointment_time: str
) -> bool:
    appt_dt = parse_appointment_scheduled_dt(appointment_date, appointment_time)
    if not appt_dt:
        logger.error("Cannot parse appointment datetime for reminder | date=%s time=%s", appointment_date, appointment_time)
        return False

    # 1-hour before appointment reminder
    reminder_dt = appt_dt - datetime.timedelta(hours=1)
    reminder_iso = reminder_dt.isoformat()

    conn = get_db()
    try:
        conn.execute("""
            INSERT OR IGNORE INTO reminders
            (appointment_id, client_id, phone, reminder_type, scheduled_at, status, attempts, created_at)
            VALUES (?, ?, ?, '1h_before', ?, 'pending', 0, ?)
        """, (appointment_id, client_id, phone, reminder_iso, now_iso()))
        conn.commit()
        logger.info("Scheduled 1h appointment reminder | appt_id=%d scheduled_at=%s", appointment_id, reminder_iso)
        return True
    except Exception as exc:
        logger.error("Failed to schedule reminder | %s", exc)
        return False
    finally:
        conn.close()


def process_due_reminders() -> int:
    """
    Background worker process checking due pending reminders.
    Sends Meta WhatsApp template (or fallback message).
    Prevents duplicate sends and retries transient failures.
    """
    now_dt = datetime.datetime.now(TIMEZONE_KOLKATA)
    now_iso_str = now_dt.isoformat()

    conn = get_db()
    rows = conn.execute("""
        SELECT id, appointment_id, client_id, phone, reminder_type, scheduled_at, attempts
        FROM reminders
        WHERE status = 'pending' AND scheduled_at <= ? AND attempts < 3
    """, (now_iso_str,)).fetchall()
    conn.close()

    sent_count = 0
    for r in rows:
        reminder_id = r["id"]
        phone = r["phone"]
        client_id = r["client_id"]
        attempts = r["attempts"] + 1
        scheduled_at_str = r["scheduled_at"]

        # Atomic claim: update status to 'sending'
        conn_claim = get_db()
        cursor = conn_claim.cursor()
        cursor.execute("UPDATE reminders SET status = 'sending', attempts = ? WHERE id = ? AND status = 'pending'", (attempts, reminder_id))
        claimed = cursor.rowcount > 0
        conn_claim.commit()
        conn_claim.close()

        if not claimed:
            continue

        # Check for stale reminders (> 2 hours past scheduled time)
        try:
            sched_dt = datetime.datetime.fromisoformat(scheduled_at_str)
            if (now_dt - sched_dt).total_seconds() > 7200:
                logger.warning("Skipping stale reminder id=%d scheduled_at=%s", reminder_id, scheduled_at_str)
                conn_stale = get_db()
                conn_stale.execute("UPDATE reminders SET status = 'stale' WHERE id = ?", (reminder_id,))
                conn_stale.commit()
                conn_stale.close()
                continue
        except Exception:
            pass

        # Send reminder via WhatsApp template (or fallback text)
        tenant_config = get_client_crm_config(client_id) or {}
        tenant_name = tenant_config.get("client_name") or clinic_name()
        template_name = tenant_config.get("reminder_template_name") or os.getenv("META_REMINDER_TEMPLATE", "glaze_appointment_1h_reminder")
        msg_text = (
            f"⏰ Reminder: You have an upcoming appointment at {tenant_name} in 1 hour. "
            "Please reach the clinic on time."
        )

        template_sent = send_template_message(
            phone=phone,
            template_name=template_name,
            language_code="en",
            components=[
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": clinic_name()}]
                }
            ],
            client_id=client_id
        )

        # Fallback to direct text message if template failed or in test environment
        success = template_sent or send_text_message(phone, msg_text, client_id=client_id)

        conn_fin = get_db()
        if success:
            conn_fin.execute("UPDATE reminders SET status = 'sent', sent_at = ? WHERE id = ?", (now_iso(), reminder_id))
            sent_count += 1
            logger.info("SUCCESSFULLY SENT REMINDER | id=%d | phone=%s", reminder_id, phone)
        else:
            new_status = 'failed' if attempts >= 3 else 'pending'
            conn_fin.execute("UPDATE reminders SET status = ? WHERE id = ?", (new_status, reminder_id))
            logger.warning("FAILED TO SEND REMINDER | id=%d | attempts=%d | status=%s", reminder_id, attempts, new_status)
        conn_fin.commit()
        conn_fin.close()

    return sent_count


# ============================================================
# UI & MESSAGES
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
    services = get_configured_services()
    rows = []
    for i, s in enumerate(services):
        rows.append({"id": f"service_{i}", "title": s["name"][:24], "description": s["description"][:72]})

    return send_list_message(phone, f"Here are the services available at {clinic_name()}:", "View services", rows)


def send_clinic_info(phone: str) -> bool:
    doctor = KNOWLEDGE.get("doctor", {})
    location = KNOWLEDGE.get("location", {})
    contact = KNOWLEDGE.get("contact", {})
    hours = KNOWLEDGE.get("hours", {})

    text = (
        f"📍 *{clinic_name()}*\n\n"
        f"👨‍⚕️ {doctor.get('name', 'Dr SHADAB MULLA')}\n"
        f"{doctor.get('qualification', 'B.D.S.')} · {doctor.get('designation', 'DENTAL SURGEON')}\n"
        f"{doctor.get('experience', '20 years')}\n\n"
        f"📍 {location.get('address', 'SHOP NO 1 MONIKA 16 PIMPRI COLONY')}\n"
        f"🗺️ {location.get('maps', '')}\n\n"
        f"📞 Phone: {contact.get('phone', '9822977740')}\n\n"
        f"🕒 Monday–Saturday\n"
        f"• {hours.get('morning', '10 AM-1 PM')}\n"
        f"• {hours.get('evening', '6 PM-9 PM')}"
    )
    return send_text_message(phone, text)


def send_booking_services(phone: str) -> bool:
    services = get_configured_services()
    rows = []
    for i, s in enumerate(services):
        rows.append({"id": f"booking_service_{i}", "title": s["name"][:24], "description": s["description"][:72]})

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
    body = "When would you like your appointment? (Working days: Monday to Saturday)"
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
            return parsed.isoformat()
        except ValueError:
            continue

    return None


def is_emergency(text: str) -> bool:
    text_lower = text.lower()
    emergency_keywords = ["accident", "extreme pain", "severe pain", "allergy", "emergency", "urgent", "bleeding badly"]
    return any(w in text_lower for w in emergency_keywords)


def is_pricing_question(text: str) -> bool:    text_lower = text.lower()
    pricing_keywords = ["price", "cost", "fee", "fees", "charge", "charges", "how much", "rate", "rates", "pricing"]
    return any(w in text_lower for w in pricing_keywords)


# ============================================================
# DETERMINISTIC BOOKING STATE MACHINE
# ============================================================

def handle_user_message(phone: str, msg_type: str, text: str, action_id: Optional[str] = None, client_id: Optional[str] = None):
    conversation = get_conversation(phone, client_id=client_id)
    client_id = client_id or conversation.get("client_id", "glaze-dental")
    current_state = conversation.get("state", "IDLE")
    normalized = text.strip().lower()

    logger.info(
        "INCOMING MESSAGE | USER=%s | CLIENT=%s | TYPE=%s | STATE=%s | ACTION=%s | TEXT=%s",
        phone, client_id, msg_type, current_state, action_id, text
    )

    log_chat(phone, "incoming", text, msg_type, client_id=client_id)
    if action_id:
        log_action(phone, action_id, client_id=client_id)

    # --------------------------------------------------------
    # 1. EMERGENCY CHECK (Highest Priority)
    # --------------------------------------------------------
    if is_emergency(text):
        reset_conversation(phone, client_id=client_id)
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
        reset_conversation(phone, client_id=client_id)

        if normalized == "cancel" or action_id in {"cancel_booking", "btn_cancel"}:
            send_text_message(phone, "Your booking process has been cancelled. Let us know whenever you'd like to book or ask a question.")
            return

        if normalized in {"restart", "reset", "menu", "main menu"}:
            return main_menu(phone)

        return

    # --------------------------------------------------------
    # 3. GREETINGS
    # --------------------------------------------------------
    greetings = {"hi", "hii", "hiii", "hello", "hey", "hey there", "good morning", "good afternoon", "good evening"}
    if normalized in greetings and current_state == "IDLE":
        reset_conversation(phone, client_id=client_id)
        return main_menu(phone)

    # --------------------------------------------------------
    # 4. PRICING INQUIRIES (SAFETY CHECK)
    # --------------------------------------------------------
    if is_pricing_question(text) and current_state == "IDLE":
        price_msg = "Pricing details are not listed. Please contact Glaze Dental Clinic at 9822977740 for treatment charges."
        send_text_message(phone, price_msg)
        return

    # --------------------------------------------------------
    # 5. IDLE STATE
    # --------------------------------------------------------
    if current_state == "IDLE":
        if action_id in {"book_appointment", "btn_book"} or "book" in normalized or "appointment" in normalized:
            update_conversation(phone, state="BOOKING_SERVICE")
            return send_booking_services(phone)

        if action_id in {"services", "btn_services"}:
            return send_services(phone)

        # Selecting a service from the informational services menu continues into booking.
        if action_id and action_id.startswith("service_"):
            try:
                idx = int(action_id.replace("service_", ""))
                services = get_configured_services()
                if 0 <= idx < len(services):
                    service = services[idx]["name"]
                    update_conversation(phone, state="BOOKING_DATE", service=service)
                    return send_date_options(phone)
            except (ValueError, IndexError):
                pass
            send_text_message(phone, "Please choose one of the available services.")
            return send_services(phone)

        if action_id in {"clinic_info", "btn_info"}:
            return send_clinic_info(phone)

        # Deterministic free-text fallback. The bot does not use an LLM in the booking path.
        send_text_message(
            phone,
            "I can help with appointment bookings, services, and clinic information. Please choose an option from the main menu."
        )
        return

    # --------------------------------------------------------
    # 6. BOOKING_SERVICE
    # --------------------------------------------------------
    if current_state == "BOOKING_SERVICE":
        service = None
        services = get_configured_services()

        if action_id and action_id.startswith("booking_service_"):
            try:
                idx = int(action_id.replace("booking_service_", ""))
                if 0 <= idx < len(services):
                    service = services[idx]["name"]
            except (ValueError, IndexError):
                service = None

        if not service:
            for s in services:
                name = s["name"]
                if name.lower() in normalized or normalized in name.lower():
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

        update_conversation(phone, state="BOOKING_AGE", patient_name=patient_name)
        send_text_message(phone, "Thanks! Please provide the patient's *age*:")
        return

    # --------------------------------------------------------
    # 8. BOOKING_AGE
    # --------------------------------------------------------
    if current_state == "BOOKING_AGE":
        age_text = text.strip()
        if not age_text.isdigit():
            send_text_message(phone, "Please enter the patient's age as a number (for example: 24):")
            return

        age_value = int(age_text)
        if age_value < 1 or age_value > 120:
            send_text_message(phone, "Please enter a valid age between 1 and 120:")
            return

        update_conversation(phone, state="BOOKING_PATIENT_TYPE", patient_age=str(age_value))
        return send_patient_type_options(phone)

    # --------------------------------------------------------
    # 9. BOOKING_PATIENT_TYPE
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

        latest = get_conversation(phone, client_id=client_id)
        if latest.get("appointment_date") and latest.get("appointment_time"):
            update_conversation(phone, state="BOOKING_CONFIRMATION", patient_type=patient_type)
            latest = get_conversation(phone, client_id=client_id)
            summary = (
                "Please confirm your appointment:\n\n"
                f"👤 Name: {latest.get('patient_name', 'Patient')}\n"
                f"🎂 Age: {latest.get('patient_age', 'Not provided')}\n"
                f"📋 Patient: {patient_type} patient\n"
                f"🦷 Service: {latest.get('service', 'Dental Consultation')}\n"
                f"📅 Date: {latest.get('appointment_date')}\n"
                f"⏰ Time: {latest.get('appointment_time')}\n\n"
                "Would you like to confirm?"
            )
            return send_button_message(phone, summary, [
                {"id": "confirm_booking", "title": "Confirm"},
                {"id": "cancel_booking", "title": "Cancel"}
            ])
        update_conversation(phone, state="BOOKING_DATE", patient_type=patient_type)
        return send_date_options(phone)

    # --------------------------------------------------------
    # 10. BOOKING_DATE
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

        # Date Validation: Past Date & Sunday
        try:
            parsed_dt = datetime.datetime.strptime(appointment_date, "%Y-%m-%d").date()
            today_dt = datetime.date.today()

            if parsed_dt < today_dt:
                send_text_message(phone, "Appointments cannot be booked for past dates. Please choose an upcoming date (Monday to Saturday):")
                return send_date_options(phone)

            if parsed_dt.weekday() == 6:  # Sunday
                send_text_message(phone, "Glaze Dental Clinic is closed on Sundays. Please choose a date from Monday to Saturday:")
                return send_date_options(phone)
        except ValueError:
            send_text_message(phone, "Invalid date format. Please use YYYY-MM-DD:")
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
    # 11. BOOKING_TIME
    # --------------------------------------------------------
    if current_state == "BOOKING_TIME":
        appointment_date = conversation.get("appointment_date")

        selected_time = None
        if action_id and action_id.startswith("time_"):
            selected_time = action_id[len("time_"):]
        else:
            available_slots = get_available_slots(client_id, appointment_date) if appointment_date else None
            if available_slots is None:
                send_text_message(
                    phone,                    "Sorry, I'm unable to check live appointment availability right now. "
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

        # Service-first flows reach the time step before patient details.
        latest = get_conversation(phone, client_id=client_id)
        if not latest.get("patient_name"):
            update_conversation(phone, state="BOOKING_NAME", appointment_time=selected_time)
            return send_text_message(phone, "What is the patient's *full name*?")
        if not latest.get("patient_age"):
            update_conversation(phone, state="BOOKING_AGE", appointment_time=selected_time)
            return send_text_message(phone, "Thanks! Please provide the patient's *age*:")
        if not latest.get("patient_type"):
            update_conversation(phone, state="BOOKING_PATIENT_TYPE", appointment_time=selected_time)
            return send_patient_type_options(phone)

        service_val = latest.get("service", "Dental Consultation")
        name_val = latest.get("patient_name", "Patient")
        ptype_val = latest.get("patient_type", "New")

        summary = (
            "Please confirm your appointment:\n\n"
            f"👤 Name: {name_val}\n"
            f"🎂 Age: {latest.get('patient_age', 'Not provided')}\n"
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
    # 12. BOOKING_CONFIRMATION
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
                booking_time=c_time,
                age=conversation.get("patient_age", "")
            )

            status = res.get("status")
            data = res.get("data", {})

            if status == "success":
                # 201 Created
                crm_apt_id = data.get("appointmentId") or data.get("id") or data.get("appointment", {}).get("id")
                local_apt_id = save_appointment_record(
                    phone=phone,
                    client_id=client_id,
                    service=c_service,
                    patient_name=c_name,
                    patient_type=c_ptype,
                    appointment_date=c_date,
                    appointment_time=c_time,
                    crm_appointment_id=crm_apt_id
                )

                # Schedule 1-hour appointment reminder
                schedule_appointment_reminder(
                    appointment_id=local_apt_id,
                    client_id=client_id,
                    phone=phone,
                    appointment_date=c_date,
                    appointment_time=c_time
                )

                # Reset state & Send confirmation
                reset_conversation(phone, client_id=client_id)

                confirmation_msg = (
                    f"✅ Your appointment has been confirmed at {clinic_name()}.\n\n"
                    f"Name: {c_name}\n"
                    f"Date: {c_date}\n"
                    f"Time: {c_time}\n"
                    f"Service: {c_service}\n\n"
                    "Thank you!"
                )
                send_text_message(phone, confirmation_msg)
                # CRITICAL: STOP HERE. DO NOT SEND WELCOME MENU AGAIN.
                return

            elif status == "conflict":
                # 409 Conflict: Slot booked by someone else
                send_text_message(
                    phone,
                    "Sorry, that time slot is already booked by someone else. Please choose another available time."
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
                reset_conversation(phone)
                send_text_message(
                    phone,
                    "I couldn't confirm the appointment right now. "
                    "Please try again shortly or call Glaze Dental Clinic at 9822977740."
                )
                return

        elif action_id in {"cancel_booking", "btn_cancel"} or normalized in {"cancel", "no"}:
            reset_conversation(phone, client_id=client_id)
            send_text_message(phone, "Your appointment booking has been cancelled.")
            return

        else:
            send_text_message(phone, "Please choose Confirm or Cancel:")
            buttons = [
                {"id": "confirm_booking", "title": "Confirm"},
                {"id": "cancel_booking", "title": "Cancel"}
            ]
            return send_button_message(phone, "Would you like to confirm your appointment?", buttons)

    # --------------------------------------------------------
    # 13. FALLBACK
    # --------------------------------------------------------
    logger.info("UNHANDLED MESSAGE | client=%s | state=%s | action=%s | text=%s", client_id, current_state, action_id, text)
    return


# ============================================================
# META WEBHOOK ENDPOINTS & IDEMPOTENCY
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

    logger.warning("WEBHOOK VERIFICATION FAILED | token=%s", hub_verify_token)
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

    # Meta signs the exact raw POST body with the app secret.
    # Keep this disabled only for local tests/dev when META_APP_SECRET is unset.
    if META_APP_SECRET:
        received_signature = request.headers.get("x-hub-signature-256", "")
        expected_signature = "sha256=" + hmac.new(
            META_APP_SECRET.encode("utf-8"),
            raw_body,
            hashlib.sha256
        ).hexdigest()
        if not received_signature or not hmac.compare_digest(received_signature, expected_signature):
            logger.warning("WEBHOOK SIGNATURE VERIFICATION FAILED")
            return JSONResponse({"status": "invalid_signature"}, status_code=401)

    try:
        body = json.loads(raw_body.decode("utf-8"))
    except Exception as exc:
        logger.error("WEBHOOK JSON PARSE ERROR | %s", exc)
        return JSONResponse({"status": "invalid_json", "reason": "json_decode_error"}, status_code=200)

    if body.get("object") != "whatsapp_business_account":
        return JSONResponse({"status": "ignored", "reason": "invalid_object"})

    processed = 0
    ignored_duplicates = 0
    entries = body.get("entry", [])

    for entry in entries:
        for change in entry.get("changes", []):
            value = change.get("value", {})
            messages = value.get("messages", [])

            # Extract receiving phone_number_id from Meta metadata
            meta_info = value.get("metadata", {})
            receiving_phone_number_id = meta_info.get("phone_number_id")

            # Resolve tenant client_id strictly from Meta connection (NOT from patient payload)
            resolved_client_id = resolve_client_by_phone_number_id(receiving_phone_number_id)
            if not resolved_client_id:
                logger.warning("WEBHOOK TENANT NOT FOUND | phone_number_id=%s", receiving_phone_number_id)
                continue

            for message in messages:
                msg_id = message.get("id")
                sender = message.get("from")
                msg_type = message.get("type", "unknown")

                if not sender:
                    continue

                # IDEMPOTENCY DEDUPLICATION CHECK
                if msg_id and is_message_processed(msg_id):
                    logger.info("DUPLICATE WEBHOOK IGNORED | msg_id=%s | sender=%s", msg_id, sender)
                    ignored_duplicates += 1
                    continue

                if msg_id:
                    mark_message_processed(msg_id)

                # Ensure conversation client_id is aligned to resolved tenant
                update_conversation(sender, client_id=resolved_client_id)

                text_content = ""
                action_id = None

                if msg_type == "text":
                    text_content = message.get("text", {}).get("body", "").strip()
                elif msg_type == "interactive":
                    interactive = message.get("interactive", {})
                    itype = interactive.get("type")
                    if itype == "button_reply":                        btn = interactive.get("button_reply", {})
                        action_id = btn.get("id")
                        text_content = btn.get("title", "").strip()
                    elif itype == "list_reply":
                        litem = interactive.get("list_reply", {})
                        action_id = litem.get("id")
                        text_content = litem.get("title", "").strip()
                else:
                    # Gracefully handle unsupported message types (e.g. image, audio, sticker)
                    logger.info("UNSUPPORTED MESSAGE TYPE | sender=%s | type=%s", sender, msg_type)
                    send_text_message(sender, "Thank you for reaching out! I can assist with text messages for booking appointments, services, and clinic details.", client_id=resolved_client_id)
                    processed += 1
                    continue

                if not text_content and not action_id:
                    continue

                try:
                    handle_user_message(
                        phone=sender,
                        msg_type=msg_type,
                        text=text_content,
                        action_id=action_id,
                        client_id=resolved_client_id
                    )
                    processed += 1
                except Exception as exc:
                    logger.error("MESSAGE HANDLER ERROR | USER=%s | %s: %s", sender, type(exc).__name__, exc, exc_info=True)
                    try:
                        send_text_message(sender, "Sorry, something went wrong. Please try again or call 9822977740.", client_id=resolved_client_id)
                    except Exception:
                        pass

    return JSONResponse({"status": "ok", "processed": processed, "ignored_duplicates": ignored_duplicates}, status_code=200)


# ============================================================
# HEALTH, DASHBOARD & MANAGEMENT ENDPOINTS
# ============================================================

@app.get("/")
async def root():
    return {
        "status": "ok",
        "service": "Glaze Dental Clinic WhatsApp AI",
        "webhook": "/webhook",
        "graph_api_version": GRAPH_API_VERSION
    }


@app.get("/health")
async def health_check():
    conn = get_db()
    pending_reminders = conn.execute("SELECT COUNT(*) FROM reminders WHERE status = 'pending'").fetchone()[0]
    processed_count = conn.execute("SELECT COUNT(*) FROM processed_messages").fetchone()[0]
    conn.close()

    return {
        "status": "ok",
        "database": "connected",
        "service": "Glaze Dental Clinic WhatsApp AI",
        "pending_reminders": pending_reminders,
        "processed_messages_count": processed_count
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
            <input type="text" id="crmBaseUrl" value="{config['crm_base_url']}" placeholder="https://vanifyai.online" required>
        </div>
        <div class="form-group">
            <label>WhatsApp Phone Number ID</label>
            <input type="text" id="phoneNumberId" value="{config.get('phone_number_id', '')}" readonly style="background: #f1f5f9;">
        </div>
        <div class="form-group">
            <label>CRM Tenant Key</label>
            <input type="text" value="{('Configured server-side' if config.get('crm_tenant_id') else 'Not configured')}" readonly style="background: #f1f5f9;">
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
                phone_number_id: document.getElementById('phoneNumberId').value
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
        "phone_number_id": config.get("phone_number_id"),
        "crm_base_url": config["crm_base_url"],
        "crm_tenant_configured": bool(config.get("crm_tenant_id")),
        "whatsapp_configured": bool((get_client_whatsapp_config(client_id) or {}).get("whatsapp_token"))
    }


@app.post("/api/client-config")
async def save_client_config_api(request: Request):
    try:
        body = await request.json()
        client_id = str(body.get("client_id", "")).strip()
        client_name = str(body.get("client_name", "")).strip()
        crm_base_url = str(body.get("crm_base_url", "")).strip()
        phone_number_id = str(body.get("phone_number_id", "")).strip()

        if not client_id or not client_name or not crm_base_url:
            return JSONResponse(
                {"status": "error", "message": "client_id, client_name and crm_base_url are required"},
                status_code=400,
            )

        save_client_crm_config(
            client_id=client_id,
            client_name=client_name,
            crm_base_url=crm_base_url,
            phone_number_id=phone_number_id,
        )
        return {"status": "success", "message": "Client configuration saved"}
    except ValueError as exc:
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=400)
    except Exception as exc:
        logger.error("CLIENT CONFIG SAVE ERROR | %s: %s", type(exc).__name__, exc)
        return JSONResponse({"status": "error", "message": "Unable to save client configuration"}, status_code=500)


@app.get("/api/test-crm-connection")
async def test_crm_connection_endpoint(
    client_id: str = "glaze-dental",
    test_date: Optional[str] = None,
):
    date_str = test_date or datetime.date.today().isoformat()
    slots = get_available_slots(client_id, date_str)

    if slots is not None:
        return {
            "status": "success",
            "client_id": client_id,
            "date": date_str,
            "available_slots": slots,
        }

    return JSONResponse(
        {            "status": "error",
            "client_id": client_id,
            "date": date_str,
            "message": "CRM slots endpoint could not be verified",
        },
        status_code=502,
    )