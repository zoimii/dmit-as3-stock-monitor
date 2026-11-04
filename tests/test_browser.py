import copy
import unittest
import io
from contextlib import redirect_stdout
from dmit_monitor.browser_service import validate_browser_result, BrowserStore
from dmit_monitor.browser_relay import guard_event, deliver
from dmit_monitor.http import SafeError
from dmit_monitor.monitor import observe
from dmit_monitor.official import Observation, PRODUCTS
from dmit_monitor.storage import fresh_state
from tests.test_monitor import config, cart, CHECKOUT, MemoryStore, FakeTelegram

P=PRODUCTS[0]
DIGEST='a'*64

def verified_result():
    return {'status':'available','reason':'official_browser_configuration_cart_checkout_verified',
            'price':'10.90','digest':DIGEST,
            'evidence':{'accepted':True,'configuration':config(),'cart':cart(),'checkout':CHECKOUT}}


def pending_state():
    state=fresh_state()
    state.update(executor='macos_persistent_chrome',source='official_browser')
    observe(state,P,Observation('available','official_browser_configuration_cart_checkout_verified','10.90',DIGEST),1000)
    state['outbox']['stock:253:1']['status']='sending'
    return state


class BrowserTests(unittest.TestCase):
    def setUp(self):
        self.output=redirect_stdout(io.StringIO());self.output.__enter__()

    def tearDown(self):
        self.output.__exit__(None,None,None)

    def test_complete_evidence_is_required_for_both_product_titles(self):
        result=verified_result()
        self.assertEqual(validate_browser_result(result,P).status,'available')
        result['evidence']['configuration']=config().replace('product-title','cart-products-title')
        self.assertEqual(validate_browser_result(result,P).status,'available')
        for edit in (lambda r:r['evidence'].update(accepted=False),
                     lambda r:r['evidence'].update(cart=cart(PRODUCTS[1])),
                     lambda r:r['evidence'].update(checkout=CHECKOUT.replace('id="btnCompleteOrder"','id="btnCompleteOrder" disabled')),
                     lambda r:r.update(price='1.00'),
                     lambda r:r.update(reason='third_party_stock')):
            value=verified_result();edit(value)
            with self.assertRaises(SafeError):validate_browser_result(value,P)

    def test_wrong_evidence_type_cannot_claim_sold_out(self):
        with self.assertRaises(SafeError):
            validate_browser_result({'status':'sold_out','reason':'official_http_403','digest':DIGEST},P)

    def test_relay_validates_source_freshness_and_confirmation(self):
        self.assertIsNotNone(guard_event(pending_state(),'stock:253:1',1100))
        for edit in (lambda s:s.update(source='third_party'),
                     lambda s:s['products']['253'].update(status='unknown'),
                     lambda s:s['products']['253'].update(reason='official_http_403'),
                     lambda s:s['products']['253'].update(checked_at=0),
                     lambda s:s['outbox']['stock:253:1'].update(text='Unverified stock alert')):
            state=pending_state();edit(state)
            with self.assertRaises(SafeError):guard_event(state,'stock:253:1',1100)

    def test_relay_claims_before_send_and_duplicate_run_is_quiet(self):
        store=MemoryStore(pending_state());notifier=FakeTelegram()
        self.assertEqual(deliver(store,notifier,'stock:253:1',1100),0)
        self.assertEqual(store.saves[0]['outbox']['stock:253:1']['status'],'uncertain')
        self.assertEqual(deliver(store,notifier,'stock:253:1',1100),0)
        self.assertEqual(len(notifier.calls),1)

    def test_relay_state_failure_prevents_notification(self):
        store=MemoryStore(pending_state());store.fail_at=1
        notifier=FakeTelegram()
        with self.assertRaises(SafeError):deliver(store,notifier,'stock:253:1',1100)
        self.assertEqual(notifier.calls,[])

    def test_browser_state_is_isolated_from_cloud_scraper(self):
        self.assertEqual(BrowserStore.BRANCH,'browser-monitor-state')


if __name__=='__main__':unittest.main()
