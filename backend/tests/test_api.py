import os
import pytest
from fastapi.testclient import TestClient
from backend.app import app, driver

@pytest.fixture(scope='module')
def client():
    with TestClient(app) as c: yield c

def test_seed_and_user_flow(client):
    if os.getenv('TRACELINK_TEST_RESET') != 'yes':
        pytest.skip('Set TRACELINK_TEST_RESET=yes to reset the local Neo4j database for integration testing')
    with driver.session() as session:
        session.run('MATCH (n) DETACH DELETE n').consume()
    seeded=client.post('/api/seed')
    assert seeded.status_code==200
    assert seeded.json()['accounts']==80
    assert client.get('/api/dashboard').json()['transactions']>=120
    overview=client.get('/api/database/overview')
    assert overview.status_code==200
    assert {item['label'] for item in overview.json()['nodes']} >= {'Account','Transaction'}
    assert {item['type'] for item in overview.json()['relationships']} >= {'SENT','RECEIVED'}
    assert len(overview.json()['constraints'])==4
    scan=client.post('/api/alerts/scan')
    assert scan.status_code==200, scan.text
    assert scan.json()['by_rule']['cycles']>=1
    assert scan.json()['by_rule']['many_to_one']>=1
    assert scan.json()['by_rule']['rapid']>=1
    graph=client.get('/api/graph?account_id=AC-0004').json()
    assert graph['nodes'] and graph['edges']
    assert client.get('/api/accounts/AC-0004').status_code==200
    case=client.post('/api/cases',json={'title':'Review transfer cycle','account_id':'AC-0004','priority':'High'}).json()
    cid=case['id']
    assert client.post(f'/api/cases/{cid}/notes',json={'text':'Investigate circular transfer.'}).status_code==200
    assert client.patch(f'/api/cases/{cid}',json={'status':'Investigating'}).status_code==200
    detail=client.get(f'/api/cases/{cid}').json()
    assert detail['status']=='Investigating' and len(detail['notes'])==1
    newtx=client.post('/api/transactions',json={'sender':'AC-0004','receiver':'AC-0007','amount':500}).json()
    assert any(t['id']==newtx['id'] for t in client.get('/api/transactions').json())

    csv_data = "Timestamp,From Bank,Account,To Bank,Account.1,Amount Paid,Payment Currency,Payment Format,Is Laundering\n2022/09/01 00:00,1,A100,2,B200,1500,US Dollar,Wire,1\n"
    imported=client.post('/api/import/ibm', files={'file':('sample.csv',csv_data,'text/csv')})
    assert imported.status_code==200, imported.text
    assert imported.json()['transactions']==1
    assert client.get('/api/accounts/1:A100').status_code==200
