# Re-export everything from app.main so `import app` works identically to the old root app.py.
# We explicitly import private names (underscore-prefixed) since `import *` skips them.
from app.main import *  # noqa: F401, F403
from app.main import (  # noqa: F401
    app,
    _send_payload,
    DB_PATH,
    TIMEZONE_KOLKATA,
    MetaCloudProvider,
    CRMClient,
    init_db,
    get_db,
    get_conversation,
    reset_conversation,
    handle_user_message,
    get_available_slots,
    book_appointment,
    cancel_appointment,
    get_configured_services,
    save_client_crm_config,
    schedule_appointment_reminder,
    process_due_reminders,
    requests,
)
