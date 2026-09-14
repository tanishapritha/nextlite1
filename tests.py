import json,re
from pathlib import Path
D=json.loads((Path(__file__).parent/"nextlite.json").read_text())
def search(q):
    words=set(re.findall(r"[a-z0-9]+",q.lower())); out=[]
    def walk(v):
        if isinstance(v,dict):
            for x in v.values(): walk(x)
        elif isinstance(v,list):
            for x in v: walk(x)
        else:
            s=str(v).lower()
            if sum(w in s for w in words): out.append(s)
    walk(D); return out
tests=[("What are Nextlite's business hours?",1),("Where is Nextlite located?",1),("What services does Nextlite offer?",1),("What is the refund policy?",1),("What is Nextlite's stock price?",0),("Who is the CEO?",0)]
p=0
for q,e in tests:
    ok=bool(search(q))==bool(e); print(("PASS" if ok else "FAIL")+" | "+q); p+=ok
print(f"{p}/{len(tests)} tests passed"); raise SystemExit(p!=len(tests))
