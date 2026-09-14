import json, os, re, sqlite3
from pathlib import Path
import streamlit as st
from google import genai
from google.genai import types

BASE=Path(__file__).parent; DB=BASE/"nextlite.db"; DATA=BASE/"nextlite.json"
st.set_page_config(page_title="Nextlite AI",page_icon="💬",layout="wide")

def data():
    return json.loads(DATA.read_text(encoding="utf-8"))

def connect():
    c=sqlite3.connect(DB)
    c.execute("CREATE TABLE IF NOT EXISTS chats(id INTEGER PRIMARY KEY,phone TEXT,role TEXT,message TEXT,created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    c.execute("CREATE TABLE IF NOT EXISTS actions(id INTEGER PRIMARY KEY,phone TEXT,action TEXT,details TEXT,created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
    c.commit(); return c

def retrieve(q):
    words=set(re.findall(r"[a-z0-9]+",q.lower())); hits=[]
    def walk(v,label=""):
        if isinstance(v,dict):
            for k,x in v.items(): walk(x,f"{label} {k}")
        elif isinstance(v,list):
            for x in v: walk(x,label)
        else:
            text=f"{label} {v}"; score=sum(w in text.lower() for w in words)
            if score: hits.append((score,text))
    walk(data()); hits.sort(reverse=True)
    return [x[1] for x in hits[:5]]

def action(q):
    q=q.lower()
    for name,words in [("human_handoff",["human","agent","representative"]),("callback",["callback","call me back"]),("meeting_request",["meeting","demo","appointment"]),("quote_request",["quote","quotation","pricing proposal"])]:
        if any(w in q for w in words): return name

def ask(q,key):
    facts=retrieve(q)
    if not facts: return "I don't have that information in my Nextlite knowledge base.",facts
    client=genai.Client(api_key=key)
    prompt=f"""You are the official Nextlite assistant. Answer ONLY from these company facts. Never invent prices, policies, products, dates, contacts, guarantees, or capabilities. If the facts do not answer the question, say you don't have that information. Be concise and natural.
FACTS:
{chr(10).join("- "+x for x in facts)}
USER: {q}"""
    r=client.models.generate_content(model="gemini-2.5-flash-lite",contents=prompt,config=types.GenerateContentConfig(temperature=0,max_output_tokens=250))
    return r.text.strip(),facts

def log(phone,role,msg):
    c=connect(); c.execute("INSERT INTO chats(phone,role,message) VALUES(?,?,?)",(phone,role,msg)); c.commit(); c.close()

st.title("Nextlite AI assistant")
st.caption("Gemini • grounded company answers • SQLite")
key=st.secrets.get("GEMINI_API_KEY",os.getenv("GEMINI_API_KEY",""))
phone=st.sidebar.text_input("Test customer phone","+91XXXXXXXXXX")
q=st.chat_input("Ask about Nextlite...")
if q:
    log(phone,"user",q); a=action(q)
    if a:
        c=connect(); c.execute("INSERT INTO actions(phone,action,details) VALUES(?,?,?)",(phone,a,q)); c.commit(); c.close()
    with st.chat_message("user"): st.write(q)
    with st.chat_message("assistant"):
        if not key: ans="Add GEMINI_API_KEY to Streamlit secrets."
        else:
            try: ans,facts=ask(q,key)
            except Exception as e: ans=f"Gemini error: {e}"; facts=[]
        st.write(ans)
        if facts:
            with st.expander("Retrieved facts"):
                for f in facts: st.write("• "+f)
    log(phone,"assistant",ans)

st.divider()
t1,t2,t3=st.tabs(["Dashboard","AI tests","Company data"])
with t1:
    c=connect()
    msgs=c.execute("SELECT phone,role,message,created_at FROM chats ORDER BY id DESC LIMIT 30").fetchall()
    acts=c.execute("SELECT phone,action,details,created_at FROM actions ORDER BY id DESC LIMIT 20").fetchall()
    customers=c.execute("SELECT COUNT(DISTINCT phone) FROM chats").fetchone()[0]; total=c.execute("SELECT COUNT(*) FROM chats").fetchone()[0]; c.close()
    a,b,d=st.columns(3); a.metric("Customers",customers); b.metric("Messages",total); d.metric("Actions",len(acts))
    st.subheader("Recent messages")
    for x in msgs: st.write(f"**{x[0]} · {x[1]}** — {x[2]}  \n`{x[3]}`")
    st.subheader("Recent actions")
    for x in acts: st.write(f"**{x[1]}** · {x[0]} — {x[2]}")
with t2:
    cases=[("What are Nextlite's business hours?",True),("Where is Nextlite located?",True),("What services does Nextlite offer?",True),("What is the refund policy?",True),("What is Nextlite's stock price today?",False),("Who is the CEO?",False)]
    passed=sum(bool(retrieve(q))==expected for q,expected in cases)
    for q,expected in cases: st.write(("✅" if bool(retrieve(q))==expected else "❌")+" "+q)
    st.write(f"**{passed}/{len(cases)} tests passed.**")
with t3: st.json(data())
