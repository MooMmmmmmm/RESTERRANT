import unittest,copy,threading,urllib.request,urllib.error,json
from unittest.mock import patch
import app

class FakeMCP:
    def __init__(self, missing=False): self.missing=missing
    def call(self,name,args):
        items=args['items']
        amount=sum(app.STATE['catalog'][i['productCode']]['price'] for i in items)
        return {'data':{'price':amount,'originalPrice':amount,'discount':0,'productList':[] if self.missing else [{'productCode':i['productCode'],'quantity':i['quantity']} for i in items]},'datetime':'test'}

class CoreTests(unittest.TestCase):
    def setUp(self):
        self.old=copy.deepcopy(app.STATE);self.client=app.CLIENT
        app.STATE.update(catalog={str(i):{'id':str(i),'name':f'food{i}','category':c,'price':p,'original':p,'image':'/assets/test.png','supportModify':False,'contents':[],'types':[c],'foodNames':[f'food{i}'],'item':{'productCode':str(i),'quantity':1}} for i,c,p in [(1,'主食',1000),(2,'配餐',500),(3,'饮品',300),(4,'甜品',200),(5,'配餐',400)]},coupons={},time='test')
        app.CLIENT=FakeMCP()
        self.refresh=patch.object(app,'refresh');self.refresh.start()
    def tearDown(self):app.STATE.clear();app.STATE.update(self.old);app.CLIENT=self.client;self.refresh.stop()
    def test_catalog_does_not_expose_selected_location(self):
        self.assertEqual(app.public_catalog()['store'], {'name':'本机所选门店'})
        self.assertNotIn('store', app.SEED)
    def test_budget_and_lock_preserved(self):
        result=app.plan({'budget':20,'mandatory':['1'],'added':['2'],'previous':[]})
        self.assertTrue(result['valid'])
        for quote in result['options'].values():
            self.assertLessEqual(quote['price'],2000);self.assertTrue({'1','2'}<=set(quote['ids']))
        self.assertGreater(len(result['options']['meal']['ids']),3)
    def test_fixed_over_budget_does_not_delete(self):
        result=app.plan({'budget':10,'mandatory':['1'],'added':['2']});self.assertFalse(result['valid']);self.assertIn('超过预算',result['reason'])
    def test_unavailable_lock_is_rejected(self):self.assertFalse(app.plan({'budget':30,'mandatory':['missing']})['valid'])
    def test_empty_zero_quote_rejected(self):
        app.CLIENT=FakeMCP(missing=True)
        with self.assertRaises(app.MCPError):app.official(['1'])
    def test_invalid_budget(self):
        for value in ['NaN','Infinity',0,201,1.001]:self.assertFalse(app.plan({'budget':value})['valid'])
    def test_coupon_comparison_does_not_overwrite(self):
        app.STATE['coupons']={'1':[{'couponId':'a','couponCode':'a','title':'A'},{'couponId':'b','couponCode':'b','title':'B'}]}
        class DiscountMCP(FakeMCP):
            def call(self,name,args):
                result=super().call(name,args);code=args['items'][0].get('couponCode');result['data']['price']={'a':800,'b':600}.get(code,1000);return result
        app.CLIENT=DiscountMCP();quote=app.official(['1']);self.assertEqual(quote['price'],600);self.assertEqual(quote['couponTitles'],['B'])
    def test_coupon_without_reference_price_gets_real_quote(self):
        seed=copy.deepcopy(app.STATE['catalog']['1']);seed['price']=None;seed['original']=None
        class UnknownPriceMCP:
            def call(self,name,args):
                if name=='query-meals':return {'data':{'meals':{}},'datetime':'test'}
                if name=='query-store-coupons':return {'data':[{'title':'coupon','couponId':'a','couponCode':'a','products':[{'productCode':'1'}]}]}
                return {'data':{'price':200,'originalPrice':200,'discount':0,'productList':[{'productCode':'1','quantity':1}]},'datetime':'test'}
        self.refresh.stop();app.CLIENT=UnknownPriceMCP()
        with patch.object(app,'SEED',{'catalog':[seed]}),patch.object(app,'SOURCES',{'1':'test.png'}):app.refresh()
        self.assertEqual(app.STATE['catalog']['1']['price'],200)
    def test_failed_coupon_does_not_claim_best(self):
        app.STATE['coupons']={'1':[{'couponId':'a','couponCode':'a','title':'A'}]}
        class FailedCouponMCP(FakeMCP):
            def call(self,name,args):
                if args['items'][0].get('couponCode'):raise app.MCPError('temporary failure')
                return super().call(name,args)
        app.CLIENT=FailedCouponMCP()
        with self.assertRaises(app.MCPError):app.official(['1'])
    def test_over_reference_budget_can_pass_after_discount(self):
        class ThresholdMCP(FakeMCP):
            def call(self,name,args):
                result=super().call(name,args)
                if len(args['items'])>=2:result['data']['price']=800
                return result
        app.CLIENT=ThresholdMCP();result=app.plan({'budget':9,'mandatory':['1']});self.assertTrue(result['valid']);self.assertLessEqual(result['options']['meal']['price'],900);self.assertIn('1',result['options']['meal']['ids'])
    def test_malformed_selection(self):
        for value in ['1',[{}],['1']*31]:
            with self.assertRaises(ValueError):app.plan({'budget':20,'mandatory':value})
    def test_api_never_exposes_item_payload(self):
        public=json.dumps(app.public_catalog());self.assertNotIn('modification',public);self.assertNotIn('Authorization',public)
    def test_confirmation_preserves_exact_cart(self):
        result=app.confirm({'budget':20,'ids':['1','2']});self.assertTrue(result['valid']);self.assertEqual(result['quote']['ids'],['1','2']);self.assertEqual(result['quote']['price'],1500)
    def test_confirmation_over_budget_is_rejected(self):self.assertFalse(app.confirm({'budget':10,'ids':['1','2']})['valid'])
    def test_confirmation_unavailable_is_rejected(self):self.assertFalse(app.confirm({'budget':20,'ids':['missing']})['valid'])
    def test_duplicate_confirmation_is_rejected(self):
        with self.assertRaises(ValueError):app.confirm({'budget':20,'ids':['1','1']})

class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.old_port=app.PORT;cls.server=app.ThreadingHTTPServer(('127.0.0.1',0),app.Handler);app.PORT=cls.server.server_port
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start();cls.url=f'http://127.0.0.1:{app.PORT}'
    @classmethod
    def tearDownClass(cls):cls.server.shutdown();cls.server.server_close();app.PORT=cls.old_port
    def test_home_and_static(self):
        with urllib.request.urlopen(self.url) as r:self.assertEqual(r.status,200);self.assertIn('mm-setup',r.read().decode())
    def test_private_files_blocked(self):
        for path in ['/app.py','/data/menu-seed.json','/.env','/../README.md']:
            with self.assertRaises(urllib.error.HTTPError) as e:urllib.request.urlopen(self.url+path)
            self.assertEqual(e.exception.code,404)
    def test_foreign_origin_rejected(self):
        req=urllib.request.Request(self.url+'/api/plan',data=b'{}',headers={'Content-Type':'application/json','Origin':'https://example.com'})
        with self.assertRaises(urllib.error.HTTPError) as e:urllib.request.urlopen(req)
        self.assertEqual(e.exception.code,403)

if __name__=='__main__':unittest.main()
