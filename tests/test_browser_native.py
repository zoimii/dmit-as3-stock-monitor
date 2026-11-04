import copy
import io
import json
import struct
import unittest
from dmit_monitor.browser_native import read_message, write_message, validate_collection, prepare_config, snapshot
from dmit_monitor.http import SafeError
from dmit_monitor.official import PRODUCTS
from tests.test_monitor import config, cart, CHECKOUT

P = PRODUCTS[0]


def page(html, query=''):
    return {'url':'https://www.dmit.io/cart.php'+query, 'html':html, 'status':200}


def collection(status='sold_out'):
    evidence = {'catalogue':page('<div class="cart-products-box" pid="253"><div class="cart-products-title">LAX.AS3.Pro.TINY</div></div>'),
                'configuration':page('<h1>Out of Stock</h1>', '?a=add&pid=253')}
    if status == 'available':
        evidence.update(configuration=page(config(), '?a=add&pid=253'), cart=page(cart(), '?a=view'),
                        checkout=page(CHECKOUT, '?a=checkout'), accepted=True)
    return {'status':status, 'checked_at':1000, 'price':'10.90', 'evidence':evidence}


class NativeTests(unittest.TestCase):
    def test_frames_round_trip_and_reject_truncation_oversize(self):
        stream=io.BytesIO();write_message(stream, {'id':1, 'result':{'status':'ready'}});stream.seek(0)
        self.assertEqual(read_message(stream)['id'],1)
        self.assertIsNone(read_message(stream))
        for body in (b'\x01', struct.pack('<I',7_000_001), struct.pack('<I',20)+b'{}'):
            with self.assertRaises(SafeError):read_message(io.BytesIO(body))

    def test_negative_requires_target_url_catalogue_and_actual_heading(self):
        self.assertEqual(validate_collection(collection(),P,1010).status,'sold_out')
        for edit in (lambda v:v['evidence']['configuration'].update(status=403),
                     lambda v:v['evidence']['configuration'].update(url='https://www.dmit.io/cart.php?a=add&pid=254'),
                     lambda v:v['evidence']['configuration'].update(html='<h1>Order Now</h1>'),
                     lambda v:v['evidence']['catalogue'].update(html='<p>LAX.AN5.Pro.TINY</p>'),
                     lambda v:v.update(checked_at=0)):
            value=collection();edit(value)
            with self.assertRaises(SafeError):validate_collection(value,P,1010)

    def test_positive_requires_full_fresh_anonymous_official_evidence(self):
        self.assertEqual(validate_collection(collection('available'),P,1010).status,'available')
        for edit in (lambda v:v['evidence'].update(accepted=False),
                     lambda v:v['evidence']['checkout'].update(status=403),
                     lambda v:v['evidence']['cart'].update(html=cart(PRODUCTS[1])),
                     lambda v:v['evidence']['checkout'].update(html=CHECKOUT+'<a href="logout.php">Logout</a>'),
                     lambda v:v['evidence']['configuration'].update(html='Just a moment '+config())):
            value=collection('available');edit(value)
            with self.assertRaises(SafeError):validate_collection(value,P,1010)

    def test_configuration_fields_cannot_include_checkout_or_other_pid(self):
        self.assertEqual(prepare_config(page(config()),P)['fields']['a'],'confproduct')
        with self.assertRaises(SafeError):prepare_config(page(config().replace('name="configure"','name="checkout"')),P)
        with self.assertRaises(SafeError):snapshot(page(config(),'?a=checkout'),'view')


if __name__=='__main__':unittest.main()
