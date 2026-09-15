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


# ============================================================
# APP
# ============================================================

app = FastAPI(title="SmileCare Dental WhatsApp AI")


# ============================================================
# CONFIG
# ============================================================

VERIFY_TOKEN = os.getenv("META_VERIFY_TOKEN", "nextlite-demo")
WHATSAPP_TOKEN = os.getenv("META_WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.getenv(
    "META_PHONE_NUMBER_ID",
    "1208541249018781"
)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GRAPH_API_VERSION = os.getenv(
    "META_GRAPH_API_VERSION",
    "v26.0"
)

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

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

gemini_model = None

if GEMINI_API_KEY:
    try:
        gemini_model = genai.GenerativeModel(
            "gemini-2.5-flash-lite"
        )
        logger.info("Gemini initialized successfully")
    except Exception as exc:
        logger.error(
            "Gemini initialization failed | %s: %s",
            type(exc).__name__,
            exc
        )


# ============================================================
# DATABASE
# ============================================================

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()

    cursor = conn.cursor()

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
            state TEXT NOT NULL DEFAULT 'IDLE',
            service TEXT,
            appointment_date TEXT,
            appointment_time TEXT,
            updated_at TEXT NOT NULL
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS appointments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phone TEXT NOT NULL,
            service TEXT NOT NULL,
            appointment_date TEXT NOT NULL,
            appointment_time TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'confirmed',
            created_at TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()

    logger.info("Database initialized")


@app.on_event("startup")
def startup():
    init_db()


# ============================================================
# KNOWLEDGE BASE
# ============================================================

DEFAULT_KNOWLEDGE = {
    "clinic_name": "SmileCare Dental Clinic",
    "description": "A dental clinic providing general and cosmetic dental care.",
    "services": [
        {
            "name": "Dental check-up",
            "description": "Routine dental examination."
        },
        {
            "name": "Teeth cleaning",
            "description": "Professional dental cleaning."
        },
        {
            "name": "Teeth whitening",
            "description": "Professional teeth whitening treatment."
        },
        {
            "name": "Root canal",
            "description": "Root canal treatment."
        },
        {
            "name": "Dental filling",
            "description": "Treatment for cavities and damaged teeth."
        }
    ],
    "clinic_info": {
        "address": "Please contact the clinic for the latest address.",
        "phone": "Please contact the clinic for the clinic phone number.",
        "hours": "Please contact the clinic for current opening hours."
    }
}


def load_knowledge() -> Dict[str, Any]:
    if not os.path.exists(KNOWLEDGE_PATH):
        logger.warning(
            "Knowledge file not found | using default knowledge"
        )
        return DEFAULT_KNOWLEDGE

    try:
        with open(
            KNOWLEDGE_PATH,
            "r",
            encoding="utf-8"
        ) as file:
            data = json.load(file)

        logger.info("Knowledge base loaded")
        return data

    except Exception as exc:
        logger.error(
            "Knowledge loading failed | %s: %s",
            type(exc).__name__,
            exc
        )
        return DEFAULT_KNOWLEDGE


KNOWLEDGE = load_knowledge()


# ============================================================
# DATABASE HELPERS
# ============================================================

def now_iso():
    return datetime.datetime.utcnow().isoformat()


def log_chat(
    phone: str,
    direction: str,
    message: str,
    message_type: str = "text"
):
    try:
        conn = get_db()

        conn.execute(
            """
            INSERT INTO chats
            (phone, direction, message, message_type, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                phone,
                direction,
                message,
                message_type,
                now_iso()
            )
        )

        conn.commit()
        conn.close()

    except Exception as exc:
        logger.error(
            "Chat logging failed | %s: %s",
            type(exc).__name__,
            exc
        )


def log_action(phone: str, action: str):
    try:
        conn = get_db()

        conn.execute(
            """
            INSERT INTO actions
            (phone, action, created_at)
            VALUES (?, ?, ?)
            """,
            (
                phone,
                action,
                now_iso()
            )
        )

        conn.commit()
        conn.close()

    except Exception as exc:
        logger.error(
            "Action logging failed | %s: %s",
            type(exc).__name__,
            exc
        )


def get_conversation(phone: str) -> Dict[str, Any]:
    conn = get_db()

    row = conn.execute(
        """
        SELECT *
        FROM conversations
        WHERE phone = ?
        """,
        (phone,)
    ).fetchone()

    conn.close()

    if not row:
        return {
            "phone": phone,
            "state": "IDLE",
            "service": None,
            "appointment_date": None,
            "appointment_time": None
        }

    return dict(row)


def update_conversation(
    phone: str,
    state: Optional[str] = None,
    service: Optional[str] = None,
    appointment_date: Optional[str] = None,
    appointment_time: Optional[str] = None
):
    current = get_conversation(phone)

    state = state if state is not None else current["state"]
    service = service if service is not None else current["service"]
    appointment_date = (
        appointment_date
        if appointment_date is not None
        else current["appointment_date"]
    )
    appointment_time = (
        appointment_time
        if appointment_time is not None
        else current["appointment_time"]
    )

    conn = get_db()

    conn.execute(
        """
        INSERT INTO conversations
        (phone, state, service, appointment_date,
         appointment_time, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(phone)
        DO UPDATE SET
            state = excluded.state,
            service = excluded.service,
            appointment_date = excluded.appointment_date,
            appointment_time = excluded.appointment_time,
            updated_at = excluded.updated_at
        """,
        (
            phone,
            state,
            service,
            appointment_date,
            appointment_time,
            now_iso()
        )
    )

    conn.commit()
    conn.close()


def reset_conversation(phone: str):
    update_conversation(
        phone=phone,
        state="IDLE",
        service=None,
        appointment_date=None,
        appointment_time=None
    )


def save_appointment(
    phone: str,
    service: str,
    appointment_date: str,
    appointment_time: str
):
    conn = get_db()

    conn.execute(
        """
        INSERT INTO appointments
        (phone, service, appointment_date,
         appointment_time, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            phone,
            service,
            appointment_date,
            appointment_time,
            "confirmed",
            now_iso()
        )
    )

    conn.commit()
    conn.close()


# ============================================================
# WHATSAPP API
# ============================================================

def _send_payload(payload: Dict[str, Any]) -> bool:

    if not WHATSAPP_TOKEN:
        logger.error(
            "WHATSAPP_TOKEN is missing"
        )
        return False

    url = (
        f"https://graph.facebook.com/"
        f"{GRAPH_API_VERSION}/"
        f"{PHONE_NUMBER_ID}/messages"
    )

    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }

    try:

        logger.info(
            "WHATSAPP OUTBOUND | URL=%s | PAYLOAD=%s",
            url,
            json.dumps(payload, ensure_ascii=False)
        )

        response = requests.post(
            url,
            headers=headers,
            json=payload,
            timeout=15
        )

        logger.info(
            "WHATSAPP RESPONSE | STATUS=%s | BODY=%s",
            response.status_code,
            response.text[:2000]
        )

        response.raise_for_status()

        return True

    except requests.exceptions.RequestException as exc:

        logger.error(
            "WHATSAPP SEND ERROR | %s: %s",
            type(exc).__name__,
            exc
        )

        return False


def send_text_message(
    phone: str,
    text: str
):

    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {
            "preview_url": False,
            "body": text
        }
    }

    success = _send_payload(payload)

    if success:
        log_chat(
            phone,
            "outgoing",
            text,
            "text"
        )

    return success


def send_button_message(
    phone: str,
    body: str,
    buttons: List[Dict[str, str]]
):

    buttons = buttons[:3]

    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {
                "text": body
            },
            "action": {
                "buttons": [
                    {
                        "type": "reply",
                        "reply": {
                            "id": button["id"],
                            "title": button["title"][:20]
                        }
                    }
                    for button in buttons
                ]
            }
        }
    }

    success = _send_payload(payload)

    if success:
        log_chat(
            phone,
            "outgoing",
            body,
            "interactive_button"
        )

    return success


def send_list_message(
    phone: str,
    body: str,
    button_text: str,
    rows: List[Dict[str, str]]
):

    rows = rows[:10]

    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {
                "text": body
            },
            "action": {
                "button": button_text[:20],
                "sections": [
                    {
                        "title": "Options",
                        "rows": [
                            {
                                "id": row["id"],
                                "title": row["title"][:24],
                                "description": row.get(
                                    "description",
                                    ""
                                )[:72]
                            }
                            for row in rows
                        ]
                    }
                ]
            }
        }
    }

    success = _send_payload(payload)

    if success:
        log_chat(
            phone,
            "outgoing",
            body,
            "interactive_list"
        )

    return success


# ============================================================
# UI
# ============================================================

def clinic_name():
    return KNOWLEDGE.get(
        "clinic_name",
        "SmileCare Dental Clinic"
    )


def main_menu(phone: str):

    body = (
        f"Hi! 👋 Welcome to {clinic_name()}.\n\n"
        "How can we help you today?"
    )

    return send_button_message(
        phone,
        body,
        [
            {
                "id": "book_appointment",
                "title": "📅 Book appointment"
            },
            {
                "id": "services",
                "title": "🦷 Our services"
            },
            {
                "id": "clinic_info",
                "title": "📍 Clinic info"
            }
        ]
    )


def send_services(phone: str):

    services = KNOWLEDGE.get(
        "services",
        []
    )

    if not services:
        return send_text_message(
            phone,
            "Please contact the clinic for information about our services."
        )

    rows = []

    for index, service in enumerate(services[:10]):

        if isinstance(service, str):
            name = service
            description = ""
        else:
            name = service.get(
                "name",
                f"Service {index + 1}"
            )
            description = service.get(
                "description",
                ""
            )

        rows.append(
            {
                "id": f"service_{index}",
                "title": name,
                "description": description
            }
        )

    return send_list_message(
        phone,
        "Here are our available services:",
        "View services",
        rows
    )


def send_clinic_info(phone: str):

    info = KNOWLEDGE.get(
        "clinic_info",
        {}
    )

    text = (
        f"📍 {clinic_name()}\n\n"
        f"Address: {info.get('address', 'Not available')}\n"
        f"Phone: {info.get('phone', 'Not available')}\n"
        f"Hours: {info.get('hours', 'Not available')}"
    )

    return send_text_message(
        phone,
        text
    )


def send_date_options(phone: str):

    today = datetime.date.today()
    tomorrow = today + datetime.timedelta(days=1)

    body = "When would you like your appointment?"

    return send_button_message(
        phone,
        body,
        [
            {
                "id": f"date_{today.isoformat()}",
                "title": "Today"
            },
            {
                "id": f"date_{tomorrow.isoformat()}",
                "title": "Tomorrow"
            },
            {
                "id": "choose_date",
                "title": "Choose another date"
            }
        ]
    )


def send_time_options(phone: str):

    times = [
        "10:00 AM",
        "11:00 AM",
        "12:00 PM",
        "02:00 PM",
        "03:00 PM",
        "04:00 PM",
        "05:00 PM"
    ]

    rows = [
        {
            "id": f"time_{time}",
            "title": time
        }
        for time in times
    ]

    return send_list_message(
        phone,
        "Choose an available time:",
        "View times",
        rows
    )


# ============================================================
# DATE PARSING
# ============================================================

def parse_date_input(text: str) -> Optional[str]:

    text = text.strip().lower()

    today = datetime.date.today()
    tomorrow = today + datetime.timedelta(days=1)

    if text in {
        "today",
        "tod",
        "aaj"
    }:
        return today.isoformat()

    if text in {
        "tomorrow",
        "tmrw",
        "tmr",
        "kal"
    }:
        return tomorrow.isoformat()

    formats = [
        "%Y-%m-%d",
        "%d-%m-%Y",
        "%d/%m/%Y",
        "%d/%m/%y",
        "%d-%m-%y"
    ]

    for fmt in formats:
        try:
            parsed = datetime.datetime.strptime(
                text,
                fmt
            ).date()

            if parsed < today:
                return None

            return parsed.isoformat()

        except ValueError:
            continue

    return None


# ============================================================
# GEMINI
# ============================================================

def ask_gemini(
    question: str
) -> Optional[str]:

    if not gemini_model:
        return None

    knowledge_text = json.dumps(
        KNOWLEDGE,
        ensure_ascii=False,
        indent=2
    )

    prompt = f"""
You are the customer support assistant for {clinic_name()}.

Answer the customer's question using ONLY the verified
information in the knowledge base below.

Do not invent:
- prices
- doctors
- timings
- addresses
- phone numbers
- medical claims
- availability
- policies

If the answer is not available in the knowledge base,
say that you do not have that information and suggest
contacting the clinic.

Keep the answer short and natural for WhatsApp.

Knowledge base:
{knowledge_text}

Customer question:
{question}
"""

    try:

        response = gemini_model.generate_content(
            prompt,
            generation_config={
                "temperature": 0.2,
                "max_output_tokens": 200
            }
        )

        answer = getattr(
            response,
            "text",
            None
        )

        if answer:
            return answer.strip()

    except Exception as exc:

        logger.error(
            "GEMINI ERROR | %s: %s",
            type(exc).__name__,
            exc
        )

    return None


# ============================================================
# MESSAGE HANDLER
# ============================================================

def handle_user_message(
    phone: str,
    msg_type: str,
    text: str,
    action_id: Optional[str] = None
):

    conversation = get_conversation(phone)

    current_state = conversation["state"]

    selected_option = action_id or text

    logger.info(
        "INCOMING MESSAGE | USER=%s | TYPE=%s | "
        "STATE=%s | ACTION=%s | TEXT=%s",
        phone,
        msg_type,
        current_state,
        action_id,
        text
    )

    log_chat(
        phone,
        "incoming",
        text,
        msg_type
    )

    if action_id:
        log_action(
            phone,
            action_id
        )

    normalized = text.strip().lower()

    # --------------------------------------------------------
    # GLOBAL RESET
    # --------------------------------------------------------

    if normalized in {
        "cancel",
        "restart",
        "reset",
        "menu",
        "main menu"
    } or action_id == "cancel_booking":

        reset_conversation(phone)

        if normalized == "cancel" or action_id == "cancel_booking":
            send_text_message(
                phone,
                "No problem. Your booking has been cancelled."
            )

        return main_menu(phone)

    # --------------------------------------------------------
    # GREETINGS
    # --------------------------------------------------------

    greetings = {
        "hi",
        "hii",
        "hiii",
        "hello",
        "hey",
        "hey there",
        "good morning",
        "good afternoon",
        "good evening"
    }

    if normalized in greetings and current_state == "IDLE":
        return main_menu(phone)

    # --------------------------------------------------------
    # MAIN MENU
    # --------------------------------------------------------

    if current_state == "IDLE":

        if action_id == "book_appointment":
            update_conversation(
                phone,
                state="BOOKING_SERVICE"
            )

            services = KNOWLEDGE.get(
                "services",
                []
            )

            rows = []

            for index, service in enumerate(services[:10]):

                if isinstance(service, str):
                    name = service
                    description = ""
                else:
                    name = service.get(
                        "name",
                        f"Service {index + 1}"
                    )
                    description = service.get(
                        "description",
                        ""
                    )

                rows.append(
                    {
                        "id": f"booking_service_{index}",
                        "title": name,
                        "description": description
                    }
                )

            if not rows:
                return send_text_message(
                    phone,
                    "Please contact the clinic to book an appointment."
                )

            return send_list_message(
                phone,
                "What service would you like to book?",
                "Choose service",
                rows
            )

        if action_id == "services":
            return send_services(phone)

        if action_id == "clinic_info":
            return send_clinic_info(phone)

        # Free-text question
        answer = ask_gemini(text)

        if answer:
            send_text_message(
                phone,
                answer
            )
        else:
            send_text_message(
                phone,
                "I can help with appointments, services, "
                "and clinic information."
            )

        return main_menu(phone)

    # --------------------------------------------------------
    # BOOKING SERVICE
    # --------------------------------------------------------

    if current_state == "BOOKING_SERVICE":

        service = None

        if action_id and action_id.startswith(
            "booking_service_"
        ):
            try:
                index = int(
                    action_id.replace(
                        "booking_service_",
                        ""
                    )
                )

                services = KNOWLEDGE.get(
                    "services",
                    []
                )

                if 0 <= index < len(services):

                    selected = services[index]

                    if isinstance(selected, str):
                        service = selected
                    else:
                        service = selected.get(
                            "name"
                        )

            except (ValueError, IndexError):
                service = None

        if not service:
            services = KNOWLEDGE.get(
                "services",
                []
            )

            for item in services:

                name = (
                    item
                    if isinstance(item, str)
                    else item.get("name", "")
                )

                if name.lower() == normalized:
                    service = name
                    break

        if not service:

            send_text_message(
                phone,
                "Please choose one of the available services."
            )

            return send_services(phone)

        update_conversation(
            phone,
            state="BOOKING_DATE",
            service=service
        )

        send_text_message(
            phone,
            f"Great. You selected: {service}"
        )

        return send_date_options(phone)

    # --------------------------------------------------------
    # BOOKING DATE
    # --------------------------------------------------------

    if current_state == "BOOKING_DATE":

        appointment_date = None

        if action_id and action_id.startswith("date_"):
            appointment_date = action_id.replace(
                "date_",
                ""
            )

        elif action_id == "choose_date":

            send_text_message(
                phone,
                "Please send the date in this format:\n"
                "YYYY-MM-DD\n\n"
                "Example: 2026-09-20"
            )

            return None

        else:
            appointment_date = parse_date_input(
                text
            )

        if not appointment_date:

            send_text_message(
                phone,
                "I couldn't understand that date.\n\n"
                "Please use YYYY-MM-DD, for example "
                "2026-09-20."
            )

            return None

        update_conversation(
            phone,
            state="BOOKING_TIME",
            appointment_date=appointment_date
        )

        return send_time_options(phone)

    # --------------------------------------------------------
    # BOOKING TIME
    # --------------------------------------------------------

    if current_state == "BOOKING_TIME":

        appointment_time = None

        if action_id and action_id.startswith(
            "time_"
        ):
            appointment_time = action_id.replace(
                "time_",
                ""
            )

        else:

            available_times = [
                "10:00 AM",
                "11:00 AM",
                "12:00 PM",
                "02:00 PM",
                "03:00 PM",
                "04:00 PM",
                "05:00 PM"
            ]

            for available_time in available_times:

                if normalized == available_time.lower():
                    appointment_time = available_time
                    break

        if not appointment_time:

            send_text_message(
                phone,
                "Please choose one of the available appointment times."
            )

            return send_time_options(phone)

        update_conversation(
            phone,
            state="BOOKING_CONFIRMATION",
            appointment_time=appointment_time
        )

        conversation = get_conversation(phone)

        date_value = conversation[
            "appointment_date"
        ]

        service_value = conversation[
            "service"
        ]

        body = (
            "Please confirm your appointment:\n\n"
            f"🦷 Service: {service_value}\n"
            f"📅 Date: {date_value}\n"
            f"⏰ Time: {appointment_time}"
        )

        return send_button_message(
            phone,
            body,
            [
                {
                    "id": "confirm_booking",
                    "title": "Confirm"
                },
                {
                    "id": "change_booking",
                    "title": "Change"
                },
                {
                    "id": "cancel_booking",
                    "title": "Cancel"
                }
            ]
        )

    # --------------------------------------------------------
    # BOOKING CONFIRMATION
    # --------------------------------------------------------

    if current_state == "BOOKING_CONFIRMATION":

        if action_id == "confirm_booking":

            conversation = get_conversation(phone)

            service = conversation["service"]
            appointment_date = conversation[
                "appointment_date"
            ]
            appointment_time = conversation[
                "appointment_time"
            ]

            save_appointment(
                phone=phone,
                service=service,
                appointment_date=appointment_date,
                appointment_time=appointment_time
            )

            logger.info(
                "APPOINTMENT CONFIRMED | USER=%s | "
                "SERVICE=%s | DATE=%s | TIME=%s",
                phone,
                service,
                appointment_date,
                appointment_time
            )

            reset_conversation(phone)

            send_text_message(
                phone,
                "✅ Your appointment has been confirmed!\n\n"
                f"🦷 {service}\n"
                f"📅 {appointment_date}\n"
                f"⏰ {appointment_time}\n\n"
                "Thank you!"
            )

            return main_menu(phone)

        if action_id == "change_booking":

            update_conversation(
                phone,
                state="BOOKING_SERVICE",
                service=None,
                appointment_date=None,
                appointment_time=None
            )

            return send_text_message(
                phone,
                "Sure. Let's start again."
            ) and send_services(phone)

        if action_id == "cancel_booking":

            reset_conversation(phone)

            send_text_message(
                phone,
                "Your appointment booking has been cancelled."
            )

            return main_menu(phone)

        send_text_message(
            phone,
            "Please choose Confirm, Change, or Cancel."
        )

        return None

    # --------------------------------------------------------
    # FALLBACK
    # --------------------------------------------------------

    reset_conversation(phone)

    answer = ask_gemini(text)

    if answer:
        send_text_message(
            phone,
            answer
        )
    else:
        send_text_message(
            phone,
            "I'm sorry, I couldn't process that request."
        )

    return main_menu(phone)


# ============================================================
# META WEBHOOK VERIFICATION
# ============================================================

@app.get("/webhook")
async def verify_webhook(
    hub_mode: Optional[str] = Query(
        default=None,
        alias="hub.mode"
    ),
    hub_verify_token: Optional[str] = Query(
        default=None,
        alias="hub.verify_token"
    ),
    hub_challenge: Optional[str] = Query(
        default=None,
        alias="hub.challenge"
    )
):

    logger.info(
        "WEBHOOK VERIFICATION | MODE=%s | TOKEN_MATCH=%s",
        hub_mode,
        hub_verify_token == VERIFY_TOKEN
    )

    if (
        hub_mode == "subscribe"
        and hub_verify_token == VERIFY_TOKEN
    ):

        logger.info(
            "WEBHOOK VERIFICATION SUCCESS"
        )

        return PlainTextResponse(
            hub_challenge or ""
        )

    logger.warning(
        "WEBHOOK VERIFICATION FAILED"
    )

    return PlainTextResponse(
        "Forbidden",
        status_code=403
    )


# ============================================================
# META WEBHOOK RECEIVER
# ============================================================

@app.post("/webhook")
async def whatsapp_webhook(
    request: Request
):

    logger.info(
        "=================================================="
    )

    logger.info(
        "WEBHOOK RECEIVED"
    )

    logger.info(
        "METHOD=%s | PATH=%s | CONTENT_TYPE=%s",
        request.method,
        request.url.path,
        request.headers.get("content-type")
    )

    # --------------------------------------------------------
    # READ RAW BODY FIRST
    # --------------------------------------------------------

    try:

        raw_body = await request.body()

        logger.info(
            "WEBHOOK BODY | BYTES=%d",
            len(raw_body)
        )

    except Exception as exc:

        logger.error(
            "WEBHOOK BODY READ ERROR | %s: %s",
            type(exc).__name__,
            exc,
            exc_info=True
        )

        return JSONResponse(
            {
                "status": "body_read_error"
            },
            status_code=200
        )

    # --------------------------------------------------------
    # EMPTY BODY
    # --------------------------------------------------------

    if not raw_body:

        logger.error(
            "WEBHOOK EMPTY BODY"
        )

        return JSONResponse(
            {
                "status": "invalid_json",
                "reason": "empty_body"
            },
            status_code=200
        )

    # --------------------------------------------------------
    # PARSE JSON
    # --------------------------------------------------------

    try:

        body_text = raw_body.decode(
            "utf-8"
        )

        logger.info(
            "WEBHOOK RAW BODY | %s",
            body_text[:3000]
        )

        body = json.loads(
            body_text
        )

    except UnicodeDecodeError as exc:

        logger.error(
            "WEBHOOK UTF8 ERROR | %s",
            exc,
            exc_info=True
        )

        return JSONResponse(
            {
                "status": "invalid_json",
                "reason": "invalid_utf8"
            },
            status_code=200
        )

    except json.JSONDecodeError as exc:

        logger.error(
            "WEBHOOK JSON PARSE ERROR | "
            "LINE=%s COLUMN=%s MSG=%s",
            exc.lineno,
            exc.colno,
            exc.msg,
            exc_info=True
        )

        logger.error(
            "WEBHOOK INVALID BODY | %s",
            raw_body[:3000]
        )

        return JSONResponse(
            {
                "status": "invalid_json",
                "reason": "json_decode_error"
            },
            status_code=200
        )

    # --------------------------------------------------------
    # VALID JSON
    # --------------------------------------------------------

    logger.info(
        "WEBHOOK JSON PARSED | TYPE=%s",
        body.get("object")
    )

    if body.get("object") != "whatsapp_business_account":

        logger.warning(
            "WEBHOOK IGNORED | OBJECT=%s",
            body.get("object")
        )

        return JSONResponse(
            {
                "status": "ignored",
                "reason": "invalid_object"
            }
        )

    processed = 0

    # --------------------------------------------------------
    # ENTRIES
    # --------------------------------------------------------

    entries = body.get(
        "entry",
        []
    )

    logger.info(
        "WEBHOOK ENTRIES | COUNT=%d",
        len(entries)
    )

    for entry_index, entry in enumerate(entries):

        logger.info(
            "PROCESSING ENTRY | INDEX=%d | ID=%s",
            entry_index,
            entry.get("id")
        )

        changes = entry.get(
            "changes",
            []
        )

        logger.info(
            "ENTRY CHANGES | COUNT=%d",
            len(changes)
        )

        # ----------------------------------------------------
        # CHANGES
        # ----------------------------------------------------

        for change_index, change in enumerate(changes):

            value = change.get(
                "value",
                {}
            )

            logger.info(
                "PROCESSING CHANGE | INDEX=%d | FIELD=%s",
                change_index,
                change.get("field")
            )

            metadata = value.get(
                "metadata",
                {}
            )

            logger.info(
                "WEBHOOK METADATA | PHONE_NUMBER_ID=%s",
                metadata.get("phone_number_id")
            )

            # ------------------------------------------------
            # STATUS EVENTS
            # ------------------------------------------------

            statuses = value.get(
                "statuses",
                []
            )

            if statuses:

                logger.info(
                    "WHATSAPP STATUSES | COUNT=%d",
                    len(statuses)
                )

                for status in statuses:

                    logger.info(
                        "MESSAGE STATUS | ID=%s | STATUS=%s | "
                        "RECIPIENT=%s | ERRORS=%s",
                        status.get("id"),
                        status.get("status"),
                        status.get("recipient_id"),
                        status.get("errors")
                    )

            # ------------------------------------------------
            # MESSAGES
            # ------------------------------------------------

            messages = value.get(
                "messages",
                []
            )

            logger.info(
                "WEBHOOK MESSAGES | COUNT=%d",
                len(messages)
            )

            for message_index, message in enumerate(
                messages
            ):

                logger.info(
                    "PROCESSING MESSAGE | INDEX=%d | ID=%s",
                    message_index,
                    message.get("id")
                )

                sender = message.get(
                    "from"
                )

                msg_type = message.get(
                    "type",
                    "unknown"
                )

                if not sender:

                    logger.warning(
                        "MESSAGE IGNORED | NO SENDER"
                    )

                    continue

                text_content = ""
                action_id = None

                # --------------------------------------------
                # TEXT
                # --------------------------------------------

                if msg_type == "text":

                    text_content = (
                        message
                        .get("text", {})
                        .get("body", "")
                        .strip()
                    )

                # --------------------------------------------
                # INTERACTIVE
                # --------------------------------------------

                elif msg_type == "interactive":

                    interactive = message.get(
                        "interactive",
                        {}
                    )

                    interactive_type = interactive.get(
                        "type"
                    )

                    logger.info(
                        "INTERACTIVE MESSAGE | TYPE=%s",
                        interactive_type
                    )

                    if interactive_type == "button_reply":

                        button_reply = interactive.get(
                            "button_reply",
                            {}
                        )

                        action_id = button_reply.get(
                            "id"
                        )

                        text_content = button_reply.get(
                            "title",
                            ""
                        ).strip()

                    elif interactive_type == "list_reply":

                        list_reply = interactive.get(
                            "list_reply",
                            {}
                        )

                        action_id = list_reply.get(
                            "id"
                        )

                        text_content = list_reply.get(
                            "title",
                            ""
                        ).strip()

                # --------------------------------------------
                # OTHER MESSAGE TYPES
                # --------------------------------------------

                else:

                    logger.info(
                        "UNSUPPORTED MESSAGE TYPE | %s",
                        msg_type
                    )

                logger.info(
                    "MESSAGE EXTRACTED | USER=%s | "
                    "TYPE=%s | ACTION=%s | TEXT=%s",
                    sender,
                    msg_type,
                    action_id,
                    text_content
                )

                if not text_content and not action_id:

                    logger.warning(
                        "MESSAGE IGNORED | NO TEXT/ACTION"
                    )

                    continue

                # --------------------------------------------
                # HANDLE MESSAGE
                # --------------------------------------------

                try:

                    handle_user_message(
                        phone=sender,
                        msg_type=msg_type,
                        text=text_content,
                        action_id=action_id
                    )

                    processed += 1

                    logger.info(
                        "MESSAGE PROCESSED SUCCESSFULLY | USER=%s",
                        sender
                    )

                except Exception as exc:

                    logger.error(
                        "MESSAGE HANDLER ERROR | USER=%s | "
                        "%s: %s",
                        sender,
                        type(exc).__name__,
                        exc,
                        exc_info=True
                    )

                    # Do not return 500 to Meta.
                    # Meta can retry failed webhooks.
                    try:
                        send_text_message(
                            sender,
                            "Sorry, something went wrong. "
                            "Please try again."
                        )
                    except Exception as send_exc:
                        logger.error(
                            "ERROR MESSAGE SEND FAILED | %s: %s",
                            type(send_exc).__name__,
                            send_exc,
                            exc_info=True
                        )

    logger.info(
        "WEBHOOK COMPLETE | PROCESSED=%d",
        processed
    )

    logger.info(
        "=================================================="
    )

    return JSONResponse(
        {
            "status": "ok",
            "processed": processed
        },
        status_code=200
    )


# ============================================================
# HEALTH CHECK
# ============================================================

@app.get("/")
async def root():

    return {
        "status": "ok",
        "service": "Nextlite WhatsApp AI",
        "webhook": "/webhook",
        "graph_api_version": GRAPH_API_VERSION
    }


# ============================================================
# LOCAL TEST ENDPOINT
# ============================================================

@app.post("/test-message")
async def test_message(
    request: Request
):

    try:

        body = await request.json()

    except Exception:

        return JSONResponse(
            {
                "status": "invalid_json"
            },
            status_code=400
        )

    phone = body.get(
        "phone"
    )

    text = body.get(
        "text",
        ""
    )

    if not phone or not text:

        return JSONResponse(
            {
                "status": "error",
                "message": "phone and text are required"
            },
            status_code=400
        )

    try:

        handle_user_message(
            phone=phone,
            msg_type="text",
            text=text,
            action_id=None
        )

        return {
            "status": "ok"
        }

    except Exception as exc:

        logger.error(
            "TEST MESSAGE ERROR | %s: %s",
            type(exc).__name__,
            exc,
            exc_info=True
        )

        return JSONResponse(
            {
                "status": "error",
                "error": str(exc)
            },
            status_code=500
        )