# Glaze Dental Clinic - WhatsApp CRM Bot Backend

A FastAPI backend for the **Glaze Dental Clinic** WhatsApp bot using Meta WhatsApp Cloud API and integrated directly with the CRM appointment management system.

---

## Quick Setup & Local Development

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Create Environment Configuration (`.env`)
Copy `.env.example` to `.env` and fill in your Meta WhatsApp credentials:
```bash
cp .env.example .env
```

Contents of `.env`:
```env
META_VERIFY_TOKEN=nextlite-demo
GLAZE_WHATSAPP_TOKEN=your_glaze_meta_access_token
META_VERIFY_TOKEN=your_webhook_verify_token
META_APP_SECRET=your_meta_app_secret
META_GRAPH_API_VERSION=v26.0
GLAZE_CRM_TENANT_KEY=your_glaze_crm_tenant_key
META_REMINDER_TEMPLATE=glaze_appointment_1h_reminder
```

### 3. Run Locally & Import Check
Verify local application import:
```bash
python -c "import app; print('App initialized successfully!')"
```

### 4. Run Automated Test Suite
```bash
pytest -q
```

### 5. Start FastAPI Server
```bash
uvicorn app:app --host 0.0.0.0 --port 8000 --reload
```

---

## Deploying & Webhook Setup

### 6. Test Webhook Locally
You can test webhook verification:
```bash
curl "http://localhost:8000/webhook?hub.mode=subscribe&hub.verify_token=nextlite-demo&hub.challenge=test_123"
```

### 7. Deploy to Render
Render Start Command:
```bash
uvicorn app:app --host 0.0.0.0 --port $PORT
```

### 8. Configure Meta Webhook
In the Meta App Dashboard:
1. Set Callback URL: `https://<your-render-app>.onrender.com/webhook`
2. Set Verify Token: `nextlite-demo` (or matching `META_VERIFY_TOKEN`)
3. Subscribe to `messages` webhook events.

### 9. Configure CRM Integration
Open the admin dashboard at:
`https://<your-render-app>.onrender.com/dashboard`
Client credentials are kept server-side; do not put them in `nextlite.json`.
- CRM Base URL: `https://nextlite-voice-prod.indiasouthcentral.cloudapp.azure.com`
- Tenant ID: `6b4b6128-5b5f-4d2f-b5de-91511ab9b120`

### 10. Configure Meta Reminder Template
Configure the Meta WhatsApp template named `glaze_appointment_1h_reminder` in Meta Business Manager for 1-hour appointment notifications.
