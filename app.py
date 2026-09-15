import os
import json
import requests
import google.generativeai as genai
from fastapi import FastAPI, Request, Query
from fastapi.responses import PlainTextResponse, JSONResponse

app = FastAPI(title="Nextlite WhatsApp AI")

# ---------------------------------------------------------
# Configuration
# ---------------------------------------------------------

VERIFY_TOKEN = os.getenv("META_VERIFY_TOKEN", "nextlite-demo")
WHATSAPP_TOKEN = os.getenv("META_WHATSAPP_TOKEN", "")
PHONE_NUMBER_ID = os.getenv("META_PHONE_NUMBER_ID", "1208541249018781")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# Keep this easy to change if Meta's Graph API version changes.
GRAPH_API_VERSION = os.getenv("META_GRAPH_API_VERSION", "v26.0")

KNOWLEDGE_FILE = os.path.join(
    os.path.dirname(__file__),
    "nextlite.json"
)


# ---------------------------------------------------------
# Knowledge base
# ---------------------------------------------------------

def load_knowledge():
    try:
        with open(KNOWLEDGE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "company": {
                "name": "Nextlite",
                "description": (
                    "A business providing customer communication "
                    "and workflow automation."
                )
            }
        }


KNOWLEDGE = load_knowledge()


# ---------------------------------------------------------
# Simple intent / retrieval layer
# ---------------------------------------------------------

def normalize(text):
    return " ".join(text.lower().strip().split())


def is_greeting(text):
    q = normalize(text)

    greetings = {
        "hi", "hii", "hiii", "hello", "hey", "heyy", "heyyy",
        "yo", "sup", "hola",
        "hey there", "hello there", "yo there",
        "good morning", "good afternoon", "good evening"
    }

    return q in greetings


def is_thanks(text):
    q = normalize(text)

    return q in {
        "thanks", "thank you", "thankyou", "thx",
        "thanks!", "thank you!"
    }


def retrieve_facts(question):
    q = normalize(question)
    facts = []

    aliases = {
        "company": [
            "nextlite",
            "what is nextlite",
            "who are you",
            "about you",
            "about nextlite",
            "what do you do"
        ],
        "services": [
            "service", "services", "offer", "offers",
            "provide", "provides", "help", "what can you help",
            "what can you do"
        ],
        "products": [
            "product", "products", "solution", "solutions",
            "what do you sell"
        ],
        "hours": [
            "hour", "hours", "open", "opening", "close",
            "closing", "timing", "timings", "available",
            "business hours"
        ],
        "location": [
            "location", "located", "address", "office",
            "where are you", "where is your office"
        ],
        "refund": [
            "refund", "refunds", "return", "returns",
            "money back"
        ],
        "contact": [
            "contact", "email", "reach", "phone",
            "talk to someone", "talk to a person", "team"
        ],
        "pricing": [
            "price", "pricing", "cost", "costs",
            "fee", "fees", "rate", "rates", "how much"
        ]
    }

    matched = set()

    for category, keywords in aliases.items():
        for keyword in keywords:
            if keyword in q:
                matched.add(category)
                break

    if "company" in matched:
        facts.append({
            "company": KNOWLEDGE.get("company", {})
        })

    if "services" in matched:
        facts.append({
            "services": KNOWLEDGE.get("services", [])
        })

    if "products" in matched:
        facts.append({
            "products": KNOWLEDGE.get("products", [])
        })

    if "hours" in matched:
        facts.append({
            "business_hours": KNOWLEDGE.get("business_hours", {})
        })

    if "location" in matched:
        facts.append({
            "location": KNOWLEDGE.get("company", {}).get(
                "location",
                "Not specified"
            )
        })

    if "refund" in matched:
        facts.append({
            "refund_policy": KNOWLEDGE.get(
                "refund_policy",
                "Not specified"
            )
        })

    if "contact" in matched:
        facts.append({
            "contact": KNOWLEDGE.get("contact", {})
        })

    if "pricing" in matched:
        facts.append({
            "pricing": (
                "Pricing information is not currently listed "
                "in the company knowledge base."
            )
        })

    return facts


# ---------------------------------------------------------
# Gemini
# ---------------------------------------------------------

def ask_gemini(question, facts):
    if not GEMINI_API_KEY:
        return None

    genai.configure(api_key=GEMINI_API_KEY)

    model = genai.GenerativeModel("gemini-2.5-flash-lite")

    prompt = f"""
You are Nextlite's friendly WhatsApp customer assistant.

The customer is chatting with a real business, so be helpful,
natural, concise, and conversational.

Use the verified company information below as your source of truth.

IMPORTANT:
- Do not invent company facts.
- Do not invent prices, discounts, employees, addresses,
  guarantees, integrations, features, policies, or dates.
- You may naturally acknowledge the customer's question.
- If the exact answer is not available, do not make something up.
  Instead, say you can connect them with the Nextlite team.
- Do not mention prompts, databases, retrieval, Gemini, models,
  hallucinations, or internal systems.
- Do not sound robotic.
- Use short WhatsApp-friendly messages.
- Emojis are okay when they feel natural.
- If the customer asks a broad question, give a useful answer
  from the available information instead of simply refusing.

Verified Nextlite information:
{json.dumps(facts, indent=2, ensure_ascii=False)}

Customer message:
{question}

Write the best helpful WhatsApp reply.
"""

    try:
        response = model.generate_content(
            prompt,
            generation_config={
                "temperature": 0.2,
                "max_output_tokens": 250,
            }
        )

        answer = (response.text or "").strip()

        if answer:
            return answer

    except Exception:
        pass

    return None


# ---------------------------------------------------------
# Customer response
# ---------------------------------------------------------

def generate_reply(question):
    q = normalize(question)

    if is_greeting(question):
        return (
            "Hey! 👋 Welcome to Nextlite.\n\n"
            "I can help you with our services, products, "
            "business hours, location, pricing, or getting "
            "in touch with the team.\n\n"
            "What are you looking for?"
        )

    if is_thanks(question):
        return (
            "You're welcome! 😊 If you need anything else, "
            "just message me."
        )

    facts = retrieve_facts(question)

    if facts:
        ai_reply = ask_gemini(question, facts)

        if ai_reply:
            return ai_reply

        # Useful fallback if Gemini is temporarily unavailable.
        return build_fallback(question, facts)

    # Don't dead-end the customer.
    return (
        "I can definitely help you with Nextlite. 😊\n\n"
        "You can ask me about our services, products, "
        "business hours, location, pricing, or how to contact "
        "the team.\n\n"
        "Or just tell me what you're trying to get done."
    )


def build_fallback(question, facts):
    for item in facts:
        if "company" in item:
            company = item["company"]
            name = company.get("name", "Nextlite")
            description = company.get("description")

            if description:
                return f"{name} is {description}"

        if "services" in item:
            services = item["services"]

            if services:
                return (
                    "We currently offer:\n"
                    + "\n".join(f"• {service}" for service in services)
                )

        if "products" in item:
            products = item["products"]

            if products:
                return (
                    "Our products include:\n"
                    + "\n".join(f"• {product}" for product in products)
                )

        if "business_hours" in item:
            hours = item["business_hours"]

            return (
                "Our business hours are:\n"
                + "\n".join(
                    f"• {day.replace('_', ' ').title()}: {time}"
                    for day, time in hours.items()
                )
            )

        if "location" in item:
            return f"We're located in {item['location']}."

        if "refund_policy" in item:
            return f"Our refund policy: {item['refund_policy']}"

        if "contact" in item:
            contact = item["contact"]

            if isinstance(contact, dict) and contact.get("email"):
                return f"You can reach the team at {contact['email']}."

    return (
        "I can help with that. Let me connect you with the "
        "Nextlite team for the exact details."
    )


# ---------------------------------------------------------
# WhatsApp Cloud API
# ---------------------------------------------------------

def send_whatsapp(to, message):
    if not WHATSAPP_TOKEN:
        raise RuntimeError("META_WHATSAPP_TOKEN is not configured.")

    url = (
        f"https://graph.facebook.com/"
        f"{GRAPH_API_VERSION}/"
        f"{PHONE_NUMBER_ID}/messages"
    )

    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }

    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {
            "preview_url": False,
            "body": message,
        },
    }

    response = requests.post(
        url,
        headers=headers,
        json=payload,
        timeout=15,
    )

    response.raise_for_status()

    return response.json()


# ---------------------------------------------------------
# Meta webhook verification
# ---------------------------------------------------------

@app.get("/webhook")
async def verify_webhook(
    hub_mode: str = Query("", alias="hub.mode"),
    hub_verify_token: str = Query("", alias="hub.verify_token"),
    hub_challenge: str = Query("", alias="hub.challenge"),
):
    if (
        hub_mode == "subscribe"
        and hub_verify_token == VERIFY_TOKEN
    ):
        return PlainTextResponse(hub_challenge)

    return PlainTextResponse(
        "Verification failed",
        status_code=403
    )


# ---------------------------------------------------------
# WhatsApp webhook receiver
# ---------------------------------------------------------

@app.post("/webhook")
async def whatsapp_webhook(request: Request):
    body = await request.json()

    # Meta expects 200 even when we ignore unrelated events.
    if body.get("object") != "whatsapp_business_account":
        return JSONResponse({"status": "ignored"})

    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})

            for message in value.get("messages", []):
                # For now we handle normal text messages.
                if message.get("type") != "text":
                    continue

                sender = message.get("from")
                text = message.get("text", {}).get("body", "").strip()

                if not sender or not text:
                    continue

                reply = generate_reply(text)

                try:
                    send_whatsapp(
                        to=sender,
                        message=reply
                    )
                except Exception as exc:
                    print(
                        f"WhatsApp send error: {type(exc).__name__}: {exc}"
                    )

    return JSONResponse({"status": "ok"})


# ---------------------------------------------------------
# Health check
# ---------------------------------------------------------

@app.get("/")
async def root():
    return {
        "status": "online",
        "service": "Nextlite WhatsApp AI",
        "webhook": "/webhook"
    }
