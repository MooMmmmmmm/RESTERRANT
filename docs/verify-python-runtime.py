"""Run the real Python entrypoint with test-only MCP responses; no credentials or network MCP."""
import sys,threading,runpy,json,urllib.request,urllib.error,platform
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import app
fixtures=runpy.run_path(str(ROOT/'tests/test_app.py'))
case=fixtures['CoreTests']('test_budget_and_lock_preserved');case.setUp()
real_server=app.ThreadingHTTPServer
server=real_server(('127.0.0.1',0),app.Handler)
port=server.server_port
startup=threading.Event();failures=[]
def provide_server(address,handler):
 assert address==('127.0.0.1',port)
 startup.set();return server
def run():
 try:
  assert app.main()==0
 except BaseException as exc:failures.append(type(exc).__name__)
def get(path):
 with urllib.request.urlopen(f'http://127.0.0.1:{port}'+path,timeout=5) as response:return response.status,response.read()
def post(path,body,origin=None):
 headers={'Content-Type':'application/json'}
 if origin:headers['Origin']=origin
 req=urllib.request.Request(f'http://127.0.0.1:{port}'+path,data=json.dumps(body).encode(),headers=headers)
 with urllib.request.urlopen(req,timeout=5) as response:return json.load(response)
try:
 with patch.object(app.getpass,'getpass',return_value='0'),patch.object(app,'MCP',return_value=app.CLIENT),patch.object(app,'ensure_assets'),patch.object(app,'ThreadingHTTPServer',side_effect=provide_server),patch.object(sys,'argv',['app.py','--port',str(port)]):
  worker=threading.Thread(target=run,daemon=True);worker.start();assert startup.wait(5),'entrypoint failed to start'
  for path in ['/','/live-demo.js','/live-demo.css']:
   status,body=get(path);assert status==200 and body
  catalog=json.loads(get('/api/catalog')[1]);assert len(catalog['foods'])==5
  result=post('/api/plan',{'budget':20,'mandatory':['1'],'added':['2'],'previous':[]});assert result['valid']
  quote=result['options']['meal'];assert {'1','2'}<=set(quote['ids']) and quote['price']<=2000
  confirmation=post('/api/confirm',{'budget':20,'ids':quote['ids']});assert confirmation['valid'];assert confirmation['quote']['ids']==quote['ids']
  assert not any(x in json.dumps(confirmation) for x in ['Authorization','Bearer ','couponCode','couponId'])
  for path in ['/.env','/.codex/config.toml','/app.py','/../README.md']:
   try:get(path);raise AssertionError('private path allowed')
   except urllib.error.HTTPError as e:assert e.code==404
  try:post('/api/plan',{'budget':20},'https://example.com');raise AssertionError('foreign origin allowed')
  except urllib.error.HTTPError as e:assert e.code==403
  server.shutdown();worker.join(5);assert not worker.is_alive() and not failures
  evidence={'passed':True,'python':platform.python_version(),'real_entrypoint':True,'http_static':3,'plan_preserves_mandatory_and_locks':True,'confirm_same_cart':True,'private_paths_blocked':4,'foreign_origin_blocked':True,'test_mcp_only':True,'real_mcp_called':False,'credentials_read':False}
  (ROOT/'docs/verify-python-runtime-results.json').write_text(json.dumps(evidence,indent=2),encoding='utf-8')
  print(json.dumps(evidence))
finally:
 server.server_close();case.tearDown()
