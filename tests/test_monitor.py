"""Synthetic fixtures: these are NOT captured evidence of live DMIT stock."""
import base64
import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from urllib.parse import parse_qs

from dmit_monitor.http import Response, SafeError, RestrictedRedirect
from dmit_monitor.official import PRODUCTS, OfficialProbe, Product
from dmit_monitor.monitor import run
from dmit_monitor.storage import FileStore, GitHubStore, fresh_state, validate
from dmit_monitor.telegram import Delivery, Telegram

TINY, POCKET = PRODUCTS
TOKEN = "123456789:" + "synthetic_secret_do_not_log_123456789"


def config(product=TINY):
    return f'''<form id="frmConfigureProduct">
      <input type="hidden" name="configure" value="true">
      <input type="hidden" name="i" value="0">
      <input type="hidden" name="token" value="synthetic-csrf">
      <p class="product-title">{product.name}</p>
      <select name="billingcycle"><option value="monthly" selected>${product.reference_price} USD Monthly</option></select>
      <input name="hostname"><input name="rootpw" type="password">
      <button type="submit" id="btnCompleteProductConfig">Continue</button></form>'''


def cart(product=TINY):
    return f'''<div class="view-cart-items"><div class="item"><div class="row">
      <span class="item-title">{product.name} <a href="cart.php?a=confproduct&i=0">Edit</a></span>
      <div class="item-price"><span>${product.reference_price} USD</span><span class="cycle">Monthly</span></div>
      </div></div></div><a id="checkout" href="cart.php?a=checkout">Checkout</a>'''


CHECKOUT = '''<form method="post" action="cart.php?a=checkout" id="frmCheckout">
 <button type="submit" id="btnCompleteOrder">Complete Order</button></form>'''


def response(body, url="https://www.dmit.io/cart.php", status=200, headers=None):
    return Response(status, url, body, headers or {})


class SequenceTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, data=None, headers=None):
        self.calls.append((method, url, data, headers))
        if not self.responses:
            raise AssertionError("unexpected network call")
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def successful_transport(product=TINY):
    return SequenceTransport([response(config(product), product.url), response(""),
                              response(cart(product), "https://www.dmit.io/cart.php?a=view"),
                              response(CHECKOUT, "https://www.dmit.io/cart.php?a=checkout")])


class OfficialTests(unittest.TestCase):
    def test_both_exact_products_require_full_path_and_never_post_checkout(self):
        for product in PRODUCTS:
            transport = successful_transport(product)
            result = OfficialProbe(transport).check(product)
            self.assertEqual(result.status, "available")
            self.assertEqual(result.price, product.reference_price)
            self.assertEqual(len(result.digest), 64)
            self.assertEqual([c[0] for c in transport.calls], ["GET", "POST", "GET", "GET"])
            posted = parse_qs(transport.calls[1][2].decode())
            self.assertEqual(posted["a"], ["confproduct"])
            self.assertNotIn("checkout", posted)
            self.assertEqual(posted["hostname"], ["inventory-probe.invalid"])

    def test_403_is_unknown_even_if_body_claims_stock(self):
        transport = SequenceTransport([response(config(), status=403)])
        result = OfficialProbe(transport).check(TINY)
        self.assertEqual((result.status, result.reason), ("unknown", "official_http_403"))
        self.assertEqual(len(transport.calls), 1)

    def test_429_honors_retry_after(self):
        result = OfficialProbe(SequenceTransport([response("", status=429, headers={"retry-after": "7200"})])).check(TINY)
        self.assertEqual(result.retry_after, 7200)
        self.assertEqual(result.status, "unknown")

    def test_server_errors_timeout_login_challenge_and_generic_catalog_are_unknown(self):
        cases = [response("", status=500), SafeError("network_or_tls_error"),
                 response('<form action="clientarea.php?login">Login</form>'),
                 response("Just a moment" + config()), response("<a>Add to cart</a>" + TINY.name),
                 response(config(), "https://third-party.example/cart.php")]
        for page in cases:
            with self.subTest(page=type(page).__name__):
                self.assertEqual(OfficialProbe(SequenceTransport([page])).check(TINY).status, "unknown")

    def test_wrong_family_or_pid_never_matches(self):
        wrong = config().replace("LAX.AS3", "LAX.AN5")
        for page in (response(wrong, TINY.url), response(config(), POCKET.url)):
            self.assertEqual(OfficialProbe(SequenceTransport([page])).check(TINY).status, "unknown")
        with self.assertRaises(SafeError):
            OfficialProbe().check(Product(999, TINY.name, "10.90"))

    def test_visible_stock_error_is_negative_hidden_errors_do_not_count(self):
        page = '<div class="alert alert-danger">Out of Stock</div>'
        self.assertEqual(OfficialProbe(SequenceTransport([response(page)])).check(TINY).status, "sold_out")
        transport = successful_transport()
        transport.responses[0].body += '<div class="alert alert-danger d-none">Out of Stock</div>'
        self.assertEqual(OfficialProbe(transport).check(TINY).status, "available")

    def test_disabled_monthly_or_configuration_button_cannot_confirm(self):
        for body in (config().replace('value="monthly"', 'value="monthly" disabled'),
                     config().replace('id="btnCompleteProductConfig"', 'id="btnCompleteProductConfig" disabled'),
                     config().replace('value="monthly"', 'value="annually"'),
                     config().replace('$10.90', '$0.00'),
                     config().replace('USD', 'CAD'),
                     config().replace('</form>', '<input name="checkout" value="true"></form>')):
            self.assertEqual(OfficialProbe(SequenceTransport([response(body)])).check(TINY).status, "unknown")

    def test_configuration_rejection_stops_before_cart(self):
        t = SequenceTransport([response(config()), response("<li>Invalid hostname</li>")])
        self.assertEqual(OfficialProbe(t).check(TINY).status, "unknown")
        self.assertEqual(len(t.calls), 2)

    def test_missing_wrong_or_annual_cart_cannot_confirm(self):
        cases = [cart(POCKET), cart().replace("Monthly", "Annually"), cart().replace("$10.90", "$11.90"),
                 cart().replace('id="checkout"', 'id="checkout" class="disabled"'), "Your cart is empty"]
        for body in cases:
            t = SequenceTransport([response(config()), response(""), response(body)])
            self.assertEqual(OfficialProbe(t).check(TINY).status, "unknown")

    def test_checkout_must_be_accessible_and_enabled(self):
        for page in (response("", status=403), response("login required"),
                     response(CHECKOUT.replace('id="btnCompleteOrder"', 'id="btnCompleteOrder" disabled')),
                     response(CHECKOUT + '<div class="g-recaptcha"></div>')):
            t = successful_transport()
            t.responses[-1] = page
            self.assertEqual(OfficialProbe(t).check(TINY).status, "unknown")


class MemoryStore:
    def __init__(self, state=None):
        self.state = copy.deepcopy(state or fresh_state())
        self.saves = []
        self.fail_at = None

    def load(self):
        return copy.deepcopy(self.state)

    def save(self, state):
        if len(self.saves) + 1 == self.fail_at:
            raise SafeError("simulated_state_failure")
        self.state = copy.deepcopy(state)
        self.saves.append(copy.deepcopy(state))


class FakeTelegram:
    def __init__(self, delivery=None):
        self.calls = []
        self.delivery = delivery or Delivery("sent", 42)

    def send(self, text):
        self.calls.append(text)
        return self.delivery


def probe_factory(status="available", reason="verified", retry_after=0):
    from dmit_monitor.official import Observation
    class Probe:
        def check(self, product):
            return Observation(status, reason, product.reference_price, "digest", retry_after)
    return Probe


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.store, self.telegram = MemoryStore(), FakeTelegram()
        self.output = io.StringIO()
        self.context = redirect_stdout(self.output)
        self.context.__enter__()

    def tearDown(self):
        self.context.__exit__(None, None, None)

    def check(self, now, status="available", **kwargs):
        return run(self.store, self.telegram, probe_factory(status), now=now, **kwargs)

    def test_persisted_dedup_across_fresh_processes(self):
        self.check(1000)
        next_store = MemoryStore(self.store.state)
        run(next_store, self.telegram, probe_factory(), now=1300)
        self.assertEqual(len(self.telegram.calls), 2)
        self.assertEqual(self.store.saves[0]["outbox"]["stock:253:1"]["status"], "pending")
        self.assertEqual(self.store.saves[1]["outbox"]["stock:253:1"]["status"], "sending")

    def test_unknown_does_not_rearm_and_two_confirmed_sold_out_checks_do(self):
        self.check(1000)
        self.check(1300, "unknown")
        self.check(1600)
        self.assertEqual(len(self.telegram.calls), 2)
        self.check(1900, "sold_out")
        self.check(2200)
        self.assertEqual(len(self.telegram.calls), 2)
        self.check(2500, "sold_out")
        self.check(2800, "sold_out")
        self.check(3100)
        self.assertEqual(len(self.telegram.calls), 4)

    def test_403_backoff_and_health_message_is_not_restock(self):
        state, code = run(self.store, self.telegram, probe_factory("unknown", "official_http_403"), now=1000)
        self.assertEqual(code, 2)
        self.assertEqual(state["products"]["253"]["next_check"], 1300)
        self.check(1300, "unknown")
        self.check(1500, "unknown")  # skipped
        self.assertEqual(self.store.state["products"]["253"]["failures"], 2)
        self.check(1900, "unknown")
        self.assertEqual(len(self.telegram.calls), 2)
        self.assertTrue(all("[监控异常，非补货]" in s for s in self.telegram.calls))
        self.check(4000, "unknown")
        self.assertEqual(len(self.telegram.calls), 2)

    def test_server_retry_after_can_exceed_exponential_cap(self):
        state, _ = run(self.store, self.telegram, probe_factory("unknown", retry_after=7200), now=1000)
        self.assertEqual(state["products"]["253"]["next_check"], 8200)

    def test_state_failure_prevents_telegram_send(self):
        self.store.fail_at = 2  # Claim could not be durably committed.
        with self.assertRaises(SafeError):
            self.check(1000)
        self.assertEqual(self.telegram.calls, [])

    def test_success_followed_by_state_failure_is_not_resent(self):
        self.store.fail_at = 3
        with self.assertRaises(SafeError):
            self.check(1000)
        self.store.fail_at = None
        self.check(1300)
        self.assertEqual(len([s for s in self.telegram.calls if TINY.name in s]), 1)
        self.assertEqual(self.store.state["outbox"]["stock:253:1"]["status"], "uncertain")

    def test_ambiguous_telegram_delivery_never_auto_retries(self):
        self.telegram.delivery = Delivery("uncertain")
        self.check(1000)
        self.check(1300)
        self.assertEqual(len(self.telegram.calls), 2)

    def test_explicit_rejection_retries_only_after_fresh_official_confirmation(self):
        self.telegram.delivery = Delivery("pending", retry_after=300)
        self.check(1000)
        self.check(1300, "unknown")
        self.assertEqual(len(self.telegram.calls), 2)
        self.telegram.delivery = Delivery("sent", 9)
        self.check(1600)
        self.assertEqual(len(self.telegram.calls), 4)

    def test_pending_stock_notification_is_cancelled_on_sold_out(self):
        self.telegram.delivery = Delivery("pending")
        self.check(1000)
        self.check(1300, "sold_out")
        self.assertTrue(all(e["status"] == "cancelled" for e in self.store.state["outbox"].values()))


class StorageTests(unittest.TestCase):
    def test_local_initialization_explicit_and_non_destructive(self):
        with tempfile.TemporaryDirectory() as temp:
            store = FileStore(Path(temp) / "state.json")
            with self.assertRaises(SafeError):
                store.load()
            store.initialize()
            self.assertEqual(store.load(), fresh_state())
            with self.assertRaises(SafeError):
                store.initialize()
            store.path.write_text("bad json")
            with self.assertRaises(SafeError):
                store.load()

    def test_corrupt_state_does_not_reset_dedup(self):
        for mutation in (lambda s: s.update(schema=99), lambda s: s["products"].pop("253"),
                         lambda s: s["products"]["253"].pop("armed")):
            state = fresh_state()
            mutation(state)
            with self.assertRaises(SafeError):
                validate(state)

    def test_github_state_revision_is_carried_to_conditional_write(self):
        content = base64.b64encode(json.dumps(fresh_state()).encode()).decode()
        transport = SequenceTransport([response(json.dumps({"sha": "old", "content": content})),
                                       response(json.dumps({"content": {"sha": "new"}}))])
        store = GitHubStore("owner/repo", "not-a-real-secret", "a" * 40, transport)
        state = store.load()
        store.save(state)
        payload = json.loads(transport.calls[1][2])
        self.assertEqual(payload["sha"], "old")
        self.assertEqual(payload["branch"], "monitor-state")
        self.assertEqual(store.sha, "new")

    def test_github_conflict_raises_and_does_not_retry_blindly(self):
        t = SequenceTransport([response("{}", status=409)])
        with self.assertRaises(SafeError):
            GitHubStore("owner/repo", "secret", "a" * 40, t).save(fresh_state())
        self.assertEqual(len(t.calls), 1)


class TelegramTests(unittest.TestCase):
    def test_success_and_rejection_and_ambiguous_network(self):
        cases = [(response('{"ok":true,"result":{"message_id":10}}'), "sent"),
                 (response('{"ok":false,"parameters":{"retry_after":600}}', status=429), "pending"),
                 (response('{"ok":false}', status=403), "pending"),
                 (response('gateway', status=502), "uncertain"),
                 (SafeError("network_or_tls_error"), "uncertain")]
        for result, expected in cases:
            t = SequenceTransport([result])
            delivery = Telegram(TOKEN, "1234", t).send("hello")
            self.assertEqual(delivery.status, expected)
            self.assertEqual(len(t.calls), 1)
            self.assertNotIn(TOKEN, t.calls[0][2].decode())


if __name__ == "__main__":
    unittest.main()
