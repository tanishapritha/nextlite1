# Nextlite AI demo

Run `run.bat` on Windows. Add your Gemini key to `.streamlit/secrets.toml`.

Or:
```bash
pip install -r requirements.txt
streamlit run app.py
```

The app includes grounded Gemini answers, a no-context hallucination gate, SQLite chat/action logging, dashboard, company JSON, and simple tests.

## Important WhatsApp note

Streamlit is the dashboard/demo UI. **Do not use Streamlit Community Cloud as the Meta WhatsApp webhook server.** Meta needs a public HTTPS webhook endpoint that reliably accepts webhook POST requests. For real WhatsApp integration, use a small FastAPI service separately; the Streamlit app can remain the dashboard.
