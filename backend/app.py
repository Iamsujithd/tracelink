import csv
import io
import os
import random
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import uuid4
from fastapi import FastAPI, HTTPException, UploadFile, File, Query, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from neo4j import GraphDatabase

URI = os.getenv('NEO4J_URI', 'bolt://localhost:7687')
USER = os.getenv('NEO4J_USER', 'neo4j')
PASSWORD = os.getenv('NEO4J_PASSWORD', 'tracelink123')
app = FastAPI(title='TraceLink API', version='1.0')
app.add_middleware(CORSMiddleware, allow_origins=['http://localhost:5173','http://127.0.0.1:5173'], allow_methods=['*'], allow_headers=['*'])
driver = GraphDatabase.driver(URI, auth=(USER, PASSWORD))

@app.middleware('http')
async def protect_mutations(request: Request, call_next):
    key = os.getenv('TRACELINK_ACCESS_KEY', '')
    if request.url.path.startswith('/api/') and request.method in {'POST', 'PATCH', 'PUT', 'DELETE'}:
        if os.getenv('VERCEL') and not key:
            return JSONResponse({'detail':'Analyst access key is not configured'}, status_code=503)
        if key and request.headers.get('X-TraceLink-Key') != key:
            return JSONResponse({'detail':'Analyst access key required'}, status_code=401)
    return await call_next(request)


def run(query, **params):
    with driver.session() as session:
        return [dict(r) for r in session.run(query, **params)]

def scalar(query, **params):
    rows = run(query, **params)
    return rows[0] if rows else None

class TransactionIn(BaseModel):
    sender: str
    receiver: str
    amount: float = Field(gt=0)
    currency: str = 'USD'
    method: str = 'Wire'

class CaseIn(BaseModel):
    title: str = Field(min_length=3)
    account_id: str
    alert_id: Optional[str] = None
    priority: str = 'Medium'

class CaseUpdate(BaseModel):
    status: str

class NoteIn(BaseModel):
    text: str = Field(min_length=1)

@app.on_event('startup')
def startup():
    driver.verify_connectivity()
    for q in ['CREATE CONSTRAINT account_id IF NOT EXISTS FOR (a:Account) REQUIRE a.id IS UNIQUE', 'CREATE CONSTRAINT transaction_id IF NOT EXISTS FOR (t:Transaction) REQUIRE t.id IS UNIQUE', 'CREATE CONSTRAINT alert_id IF NOT EXISTS FOR (a:Alert) REQUIRE a.id IS UNIQUE', 'CREATE CONSTRAINT case_id IF NOT EXISTS FOR (c:Case) REQUIRE c.id IS UNIQUE']:
        run(q)

@app.on_event('shutdown')
def shutdown(): driver.close()

@app.get('/api/health')
def health():
    try:
        return {'status':'ok','database':'connected','accounts':scalar('MATCH (a:Account) RETURN count(a) AS n')['n']}
    except Exception as e: raise HTTPException(503, str(e))

@app.get('/api/dashboard')
def dashboard():
    a=scalar('MATCH (a:Account) RETURN count(a) AS n')['n']
    t=scalar('MATCH (t:Transaction) RETURN count(t) AS n, coalesce(sum(t.amount),0) AS volume')
    alerts=scalar('MATCH (a:Alert) RETURN count(a) AS n')['n']
    cases=scalar("MATCH (c:Case) WHERE c.status <> 'Closed' RETURN count(c) AS n")['n']
    recent=run('MATCH (t:Transaction) RETURN t.id AS id,t.sender AS sender,t.receiver AS receiver,t.amount AS amount,t.currency AS currency,t.time AS time,t.laundering AS laundering ORDER BY t.time DESC LIMIT 8')
    return {'accounts':a,'transactions':t['n'],'volume':t['volume'],'alerts':alerts,'open_cases':cases,'recent':recent}

@app.get('/api/accounts')
def accounts(q: str = '', limit: int = Query(30, ge=1, le=100)):
    return run('MATCH (a:Account) WHERE toLower(a.id) CONTAINS toLower($q) OR toLower(a.name) CONTAINS toLower($q) OPTIONAL MATCH (a)-[:SENT|RECEIVED]-(t:Transaction) RETURN a.id AS id,a.name AS name,a.bank AS bank,a.risk AS risk,count(DISTINCT t) AS tx_count ORDER BY risk DESC,tx_count DESC LIMIT $limit',q=q,limit=limit)

@app.get('/api/accounts/{account_id}')
def account(account_id: str):
    row=scalar('MATCH (a:Account {id:$id}) OPTIONAL MATCH (a)-[:SENT]->(out:Transaction) WITH a,count(out) AS sent,coalesce(sum(out.amount),0) AS outflow OPTIONAL MATCH (a)-[:RECEIVED]->(inc:Transaction) RETURN a.id AS id,a.name AS name,a.bank AS bank,a.risk AS risk,sent,outflow,count(inc) AS received,coalesce(sum(inc.amount),0) AS inflow',id=account_id)
    if not row: raise HTTPException(404,'Account not found')
    return row

@app.get('/api/graph')
def graph(account_id: str = '', limit: int = Query(70, ge=5, le=150)):
    if account_id:
        rows=run('MATCH (a:Account {id:$id}) MATCH (a)-[:SENT|RECEIVED]-(t:Transaction)-[:SENT|RECEIVED]-(b:Account) WHERE a <> b RETURN t.id AS id,t.sender AS sender,t.receiver AS receiver,t.amount AS amount,t.time AS time,t.laundering AS laundering LIMIT $limit',id=account_id,limit=limit)
    else:
        rows=run('MATCH (t:Transaction) WHERE t.laundering = true RETURN t.id AS id,t.sender AS sender,t.receiver AS receiver,t.amount AS amount,t.time AS time,t.laundering AS laundering ORDER BY t.time DESC LIMIT $limit',limit=limit)
        if not rows: rows=run('MATCH (t:Transaction) RETURN t.id AS id,t.sender AS sender,t.receiver AS receiver,t.amount AS amount,t.time AS time,t.laundering AS laundering LIMIT $limit',limit=limit)
    ids=list({x for r in rows for x in (r['sender'],r['receiver'])})
    nodes=run('MATCH (a:Account) WHERE a.id IN $ids RETURN a.id AS id,a.name AS label,a.bank AS bank,a.risk AS risk',ids=ids)
    return {'nodes':nodes,'edges':rows}

@app.get('/api/transactions')
def transactions(q: str = '', flagged: bool = False, limit: int = Query(50, ge=1, le=200)):
    return run('MATCH (t:Transaction) WHERE ($q = "" OR t.sender CONTAINS $q OR t.receiver CONTAINS $q OR t.id CONTAINS $q) AND ($flagged = false OR t.laundering = true) RETURN t.id AS id,t.sender AS sender,t.receiver AS receiver,t.amount AS amount,t.currency AS currency,t.method AS method,t.time AS time,t.laundering AS laundering ORDER BY t.time DESC LIMIT $limit',q=q,flagged=flagged,limit=limit)

@app.post('/api/transactions')
def add_transaction(body: TransactionIn):
    if body.sender == body.receiver: raise HTTPException(400,'Sender and receiver must differ')
    if not scalar('MATCH (a:Account {id:$id}) RETURN a.id AS id',id=body.sender) or not scalar('MATCH (a:Account {id:$id}) RETURN a.id AS id',id=body.receiver): raise HTTPException(404,'Account not found')
    tid='TX-'+uuid4().hex[:10].upper(); now=datetime.now(timezone.utc).isoformat()
    run('MATCH (s:Account {id:$sender}),(r:Account {id:$receiver}) CREATE (t:Transaction {id:$id,sender:$sender,receiver:$receiver,amount:$amount,currency:$currency,method:$method,time:$time,laundering:false}) CREATE (s)-[:SENT]->(t)-[:RECEIVED]->(r)',id=tid,time=now,**body.model_dump())
    return {'id':tid}

@app.get('/api/alerts')
def alerts():
    return run('MATCH (a:Alert) RETURN a.id AS id,a.account_id AS account_id,a.type AS type,a.description AS description,a.score AS score,a.time AS time,a.evidence AS evidence ORDER BY score DESC,a.time DESC LIMIT 100')

@app.post('/api/alerts/scan')
def scan():
    run('MATCH (a:Alert) WHERE a.source="rule" DETACH DELETE a')
    # Rule 1: cycle of three distinct accounts. Rule 2: concentrated incoming. Rule 3: rapid onward transfer.
    cycles=run('MATCH (a:Account)-[:SENT]->(t1:Transaction)-[:RECEIVED]->(b:Account)-[:SENT]->(t2:Transaction)-[:RECEIVED]->(c:Account)-[:SENT]->(t3:Transaction)-[:RECEIVED]->(a) WHERE a.id < b.id AND a.id < c.id AND a <> b AND b <> c AND c <> a RETURN a.id AS account_id,[t1.id,t2.id,t3.id] AS evidence LIMIT 30')
    incoming=run('MATCH (t:Transaction)-[:RECEIVED]->(a:Account) WITH a,count(DISTINCT t.sender) AS senders,collect(t.id)[0..8] AS evidence WHERE senders >= 7 RETURN a.id AS account_id,senders,evidence LIMIT 30')
    rapid=run('MATCH (a:Account)-[:RECEIVED]-(i:Transaction),(a)-[:SENT]->(o:Transaction) WHERE i.sender <> o.receiver AND i.time < o.time AND duration.between(datetime(i.time),datetime(o.time)).minutes <= 120 AND o.amount >= i.amount * 0.7 RETURN DISTINCT a.id AS account_id,[i.id,o.id] AS evidence LIMIT 30')
    now=datetime.now(timezone.utc).isoformat(); output=[]
    for typ,items,score in [('Circular transfer',cycles,92),('Many-to-one transfers',incoming,76),('Rapid onward transfer',rapid,83)]:
        for item in items:
            aid=item['account_id']; evidence=item['evidence']; desc=f'{typ} pattern involving account {aid}'
            output.append({'id':'AL-'+uuid4().hex[:8].upper(),'account_id':aid,'type':typ,'description':desc,'score':score,'time':now,'evidence':evidence,'source':'rule'})
    if output:
        run('UNWIND $items AS item CREATE (a:Alert) SET a = item',items=output)
        run('MATCH (a:Alert),(acc:Account) WHERE a.account_id=acc.id AND a.time=$time CREATE (a)-[:FLAGS]->(acc)',time=now)
    return {'created':len(output),'by_rule':{'cycles':len(cycles),'many_to_one':len(incoming),'rapid':len(rapid)}}

@app.get('/api/cases')
def cases():
    return run('MATCH (c:Case) RETURN c.id AS id,c.title AS title,c.account_id AS account_id,c.alert_id AS alert_id,c.priority AS priority,c.status AS status,c.created AS created,c.updated AS updated ORDER BY c.updated DESC')

@app.post('/api/cases')
def create_case(body: CaseIn):
    if not scalar('MATCH (a:Account {id:$id}) RETURN a.id AS id',id=body.account_id): raise HTTPException(404,'Account not found')
    cid='CASE-'+uuid4().hex[:7].upper(); now=datetime.now(timezone.utc).isoformat()
    run('MATCH (a:Account {id:$account_id}) CREATE (c:Case {id:$id,title:$title,account_id:$account_id,alert_id:$alert_id,priority:$priority,status:"Open",created:$now,updated:$now}) CREATE (c)-[:INVESTIGATES]->(a)',id=cid,now=now,**body.model_dump())
    return {'id':cid}

@app.get('/api/cases/{case_id}')
def case_detail(case_id: str):
    row=scalar('MATCH (c:Case {id:$id}) RETURN c.id AS id,c.title AS title,c.account_id AS account_id,c.alert_id AS alert_id,c.priority AS priority,c.status AS status,c.created AS created,c.updated AS updated',id=case_id)
    if not row: raise HTTPException(404,'Case not found')
    row['notes']=run('MATCH (c:Case {id:$id})-[:HAS_NOTE]->(n:Note) RETURN n.id AS id,n.text AS text,n.time AS time ORDER BY n.time DESC',id=case_id)
    return row

@app.patch('/api/cases/{case_id}')
def update_case(case_id: str, body: CaseUpdate):
    if body.status not in ['Open','Investigating','Closed']: raise HTTPException(400,'Invalid status')
    row=scalar('MATCH (c:Case {id:$id}) SET c.status=$status,c.updated=$now RETURN c.id AS id',id=case_id,status=body.status,now=datetime.now(timezone.utc).isoformat())
    if not row: raise HTTPException(404,'Case not found')
    return row

@app.post('/api/cases/{case_id}/notes')
def add_note(case_id: str, body: NoteIn):
    row=scalar('MATCH (c:Case {id:$id}) CREATE (n:Note {id:$nid,text:$text,time:$now}) CREATE (c)-[:HAS_NOTE]->(n) SET c.updated=$now RETURN n.id AS id',id=case_id,nid=uuid4().hex,text=body.text,now=datetime.now(timezone.utc).isoformat())
    if not row: raise HTTPException(404,'Case not found')
    return row

@app.post('/api/seed')
def seed():
    if scalar('MATCH (a:Account) RETURN count(a) AS n')['n'] > 0: raise HTTPException(409,'Database already contains accounts')
    rng=random.Random(42); now=datetime.now(timezone.utc)
    names=['Avery Stone','Maya Chen','Noah Patel','Sofia Rivera','Liam Brooks','Zara Khan','Ethan Park','Isla Morgan','Arjun Shah','Emma Reed']
    accounts=[{'id':f'AC-{i:04d}','name':f'{names[i%len(names)]} {i//len(names)+1}','bank':['Northstar','Meridian','Harbor'][i%3],'risk':rng.randint(5,36)} for i in range(1,81)]
    run('UNWIND $items AS item CREATE (a:Account) SET a=item',items=accounts)
    tx=[]
    def add(s,r,amt,days=0,mins=0,flag=False):
        tx.append({'id':f'TX-{len(tx)+1:05d}','sender':f'AC-{s:04d}','receiver':f'AC-{r:04d}','amount':amt,'currency':'USD','method':'Wire','time':(now-timedelta(days=days,minutes=mins)).isoformat(),'laundering':flag})
    for i in range(110):
        s=rng.randint(1,40); r=rng.randint(41,80)
        if s!=r: add(s,r,round(rng.uniform(120,8600),2),rng.randint(1,45),rng.randint(1,1000))
    add(4,7,12500,0,70,True);add(7,12,12100,0,50,True);add(12,4,11900,0,30,True)
    for i in range(21,28): add(i,30,round(3000+rng.random()*500,2),0,100-i,True)
    add(44,45,8700,0,90,True);add(45,46,8500,0,45,True)
    run('UNWIND $items AS item MATCH (s:Account {id:item.sender}),(r:Account {id:item.receiver}) CREATE (t:Transaction) SET t=item CREATE (s)-[:SENT]->(t)-[:RECEIVED]->(r)',items=tx)
    return {'accounts':len(accounts),'transactions':len(tx)}

@app.post('/api/import/ibm')
async def import_ibm(file: UploadFile = File(...), limit: int = Query(10000, ge=1, le=50000)):
    raw=await file.read()
    if len(raw)>30_000_000: raise HTTPException(413,'Upload a CSV under 30 MB; split larger files first')
    try: rows=list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))))[:limit]
    except Exception: raise HTTPException(400,'Invalid UTF-8 CSV')
    required={'From Bank','Account','To Bank','Account.1','Amount Paid','Timestamp'}
    if not rows or not required.issubset(rows[0]): raise HTTPException(400,'Expected IBM AML transaction CSV headers')
    items=[];acc={}
    for i,r in enumerate(rows):
        try:
            s=f"{r['From Bank']}:{r['Account']}";d=f"{r['To Bank']}:{r['Account.1']}";amount=float(r['Amount Paid'])
            dt=datetime.strptime(r['Timestamp'],'%Y/%m/%d %H:%M').replace(tzinfo=timezone.utc).isoformat()
            if s==d or amount<=0: continue
            acc[s]={'id':s,'name':r['Account'],'bank':r['From Bank'],'risk':10};acc[d]={'id':d,'name':r['Account.1'],'bank':r['To Bank'],'risk':10}
            items.append({'id':f'IBM-{uuid4().hex[:10]}','sender':s,'receiver':d,'amount':amount,'currency':r.get('Payment Currency','USD'),'method':r.get('Payment Format','Transfer'),'time':dt,'laundering':r.get('Is Laundering','0')=='1'})
        except (ValueError,KeyError): continue
    if not items: raise HTTPException(400,'No valid transactions found')
    run('UNWIND $items AS item MERGE (a:Account {id:item.id}) ON CREATE SET a.name=item.name,a.bank=item.bank,a.risk=item.risk',items=list(acc.values()))
    for offset in range(0,len(items),1000): run('UNWIND $items AS item MATCH (s:Account {id:item.sender}),(r:Account {id:item.receiver}) CREATE (t:Transaction) SET t=item CREATE (s)-[:SENT]->(t)-[:RECEIVED]->(r)',items=items[offset:offset+1000])
    return {'accounts':len(acc),'transactions':len(items)}
