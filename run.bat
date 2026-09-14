@echo off
if not exist .venv python -m venv .venv
call .venv\Scripts\activate
python -m pip install -r requirements.txt
if not exist .streamlit\secrets.toml copy .streamlit\secrets.toml.example .streamlit\secrets.toml
python tests.py
python -m streamlit run app.py
