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

class FullMenuTests(unittest.TestCase):
    setUp=CoreTests.setUp
    tearDown=CoreTests.tearDown
    def test_single_food_filter_keeps_portions_and_excludes_bundles(self):
        for name,category in [('麦辣鸡腿汉堡','主食'),('麦乐鸡6块','小食'),('韩式甜辣酱鸡块8块','小食拼盘 /多人餐'),('新地','甜品')]:
            self.assertTrue(app.is_single_food({'name':name,'category':category}),name)
        for name in ['巨无霸三件套','板烧鸡腿堡套餐','人气经典随心配','全明星双人分享餐','旋旋星愿贴','麦当劳联名徽章']:
            self.assertFalse(app.is_single_food({'name':name}),name)
        self.assertFalse(app.is_single_food({'name':'神秘组合','contents':['汉堡','可乐']}))
    def test_refresh_does_not_restore_bundle_via_coupon_seed(self):
        class MenuMCP:
            def call(self,name,args):
                if name=='query-meals':return {'data':{'meals':{'single':{'name':'麦乐鸡6块','currentPrice':'10'},'bundle':{'name':'汉堡三件套','currentPrice':'20'}},'categories':[]},'datetime':'test'}
                if name=='query-store-coupons':return {'data':[{'products':[{'productCode':'bundle'},{'productCode':'coupon-single'},{'productCode':'coupon-bundle'}]}]}
                raise AssertionError('no price requests needed')
        self.refresh.stop();app.CLIENT=MenuMCP()
        seed=[{'id':code,'name':name,'category':'优惠','price':1000,'contents':[]} for code,name in [('coupon-single','麦香鸡'),('coupon-bundle','麦香鸡套餐')]]
        with patch.object(app,'SEED',{'catalog':seed}),patch.object(app,'SOURCES',{'coupon-single':'test.png'}):app.refresh()
        self.assertEqual(set(app.STATE['catalog']),{'single','coupon-single'})
        self.assertIn('coupon-single',app.STATE['coupons'])
    def test_refresh_keeps_full_menu_without_seed_or_detail_prefetch(self):
        class MenuMCP:
            def __init__(self):self.calls=[]
            def call(self,name,args):
                self.calls.append(name)
                if name=='query-meals':return {'data':{'meals':{str(i):{'name':f'food{i}','currentPrice':'10','originalPrice':'12','image':'https://menu-img.mcd.cn/food.png','canWithOrder':i%2==0} for i in range(250)},'categories':[{'name':'主食','meals':[{'code':str(i)} for i in range(250)]}]},'datetime':'test'}
                if name=='query-store-coupons':return {'data':[]}
                raise AssertionError('details must load only on demand')
        self.refresh.stop();app.CLIENT=MenuMCP()
        with patch.object(app,'SEED',{'catalog':[]}):app.refresh()
        self.assertEqual(len(app.public_catalog()['foods']),250)
        self.assertTrue(all(f['selectable'] for f in app.public_catalog()['foods']))
        self.assertEqual(app.CLIENT.calls,['query-meals','query-store-coupons'])
    def test_new_food_details_preserve_defaults_and_rounds(self):
        app.STATE['catalog']['1'].pop('item');app.DETAILS.clear()
        class DetailMCP(FakeMCP):
            def __init__(self):super().__init__();self.calls=[]
            def call(self,name,args):
                self.calls.append((name,copy.deepcopy(args)))
                if name=='query-meal-detail':return {'data':{'code':'1','supportModify':True,'modification':{'items':[{'values':[{'code':'m1','selectedQuantity':1,'selectedKey':'on'},{'code':'m2','selectedQuantity':0,'unselectedKey':'off'}]}]},'rounds':[{'id':1,'minQuantity':1,'maxQuantity':1,'choices':[{'code':'drink','name':'drink','isDefault':1,'quantity':1}]}]}}
                return super().call(name,args)
        app.CLIENT=DetailMCP();app.official(['1']);app.official(['1'])
        self.assertEqual(sum(n=='query-meal-detail' for n,a in app.CLIENT.calls),1)
        item=next(a['items'][0] for n,a in app.CLIENT.calls if n=='calculate-price')
        self.assertEqual(item['modification']['values'][1]['key'],'off')
        self.assertEqual(item['roundList'][0]['comboItemList'][0]['code'],'drink')
        self.assertNotIn('withOrder',next(a for n,a in app.CLIENT.calls if n=='calculate-price'))
    def test_missing_default_round_rejects_quote(self):
        app.STATE['catalog']['1'].pop('item');app.DETAILS.clear()
        class BrokenDetail:
            def call(self,name,args):
                if name=='query-meal-detail':return {'data':{'code':'1','rounds':[{'id':1,'minQuantity':1,'maxQuantity':1,'choices':[]}]}}
                raise AssertionError('incomplete defaults must not be quoted')
        app.CLIENT=BrokenDetail()
        with self.assertRaises(app.MCPError):app.official(['1'])
    def test_missing_default_modification_quantity_is_rejected(self):
        app.STATE['catalog']['1'].pop('item');app.DETAILS.clear()
        class MissingQuantity:
            def call(self,name,args):
                if name=='query-meal-detail':return {'data':{'code':'1','supportModify':True,'modification':{'items':[{'values':[{'code':'m1','selectedKey':'on'}]}]}}}
                raise AssertionError('missing default must not reach price calculation')
        app.CLIENT=MissingQuantity()
        with self.assertRaises(app.MCPError):app.official(['1'])
    def test_large_menu_search_keeps_pool_tail_and_bounds_quotes(self):
        import time
        base=copy.deepcopy(app.STATE['catalog']['1']);catalog={}
        for i in range(250):
            f=copy.deepcopy(base);f.update(id=str(i),name=f'food{i}',price=500,foodNames=[f'food{i}']);f['item']['productCode']=str(i);catalog[str(i)]=f
        app.STATE['catalog']=catalog
        class CountMCP(FakeMCP):
            def __init__(self):super().__init__();self.calls=0
            def call(self,name,args):self.calls+=1;return super().call(name,args)
        app.CLIENT=CountMCP();start=time.monotonic();result=app.plan({'budget':25,'mandatory':['249'],'added':['248']})
        self.assertTrue(result['valid']);self.assertTrue({'249','248'}<=set(result['options']['meal']['ids']))
        self.assertLessEqual(app.CLIENT.calls,7);self.assertLess(time.monotonic()-start,2)

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
