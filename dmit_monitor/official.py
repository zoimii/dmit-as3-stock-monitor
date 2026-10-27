"""Conservative WHMCS standard-cart adapter. An unfamiliar page is UNKNOWN."""
import hashlib
import re
import secrets
import urllib.parse
from dataclasses import dataclass, field
from html.parser import HTMLParser

from .http import SafeError, Transport


@dataclass(frozen=True)
class Product:
    pid: int
    name: str
    reference_price: str

    @property
    def url(self):
        return f"https://www.dmit.io/cart.php?a=add&pid={self.pid}"


PRODUCTS = (Product(253, "LAX.AS3.Pro.TINY", "10.90"),
            Product(254, "LAX.AS3.Pro.Pocket", "16.90"))
BASE = "https://www.dmit.io/cart.php"


@dataclass
class Node:
    tag: str
    attrs: dict = field(default_factory=dict)
    children: list = field(default_factory=list)
    parent: object = None

    def find(self, predicate):
        found = [self] if predicate(self) else []
        for child in self.children:
            if isinstance(child, Node):
                found.extend(child.find(predicate))
        return found

    def has_class(self, name):
        return name in self.attrs.get("class", "").split()

    def hidden(self):
        return ("hidden" in self.attrs or self.attrs.get("aria-hidden") == "true" or
                bool(set(self.attrs.get("class", "").split()) & {"hidden", "d-none", "w-hidden"}) or
                bool(re.search(r"display\s*:\s*none|visibility\s*:\s*hidden", self.attrs.get("style", ""), re.I)) or
                bool(self.parent and self.parent.hidden()))

    def enabled(self):
        return (not self.hidden() and "disabled" not in self.attrs and
                self.attrs.get("aria-disabled") != "true" and not self.has_class("disabled") and
                not any(p.tag == "fieldset" and "disabled" in p.attrs for p in self.ancestors()))

    def ancestors(self):
        current = self.parent
        while current:
            yield current
            current = current.parent

    def text(self, skip_links=False):
        if self.tag in {"script", "style", "template", "noscript"} or self.hidden():
            return ""
        return " ".join(c if isinstance(c, str) else
                        ("" if skip_links and c.tag in {"a", "button"} else c.text(skip_links))
                        for c in self.children)


class DOM(HTMLParser):
    VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self, body):
        super().__init__(convert_charrefs=True)
        self.root = Node("root")
        self.current = self.root
        self.feed(body)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, dict(attrs), parent=self.current)
        self.current.children.append(node)
        if tag not in self.VOID:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for node in [self.current, *self.current.ancestors()]:
            if node.tag == tag and node.parent:
                self.current = node.parent
                return

    def handle_data(self, data):
        self.current.children.append(data)


def normalized(value):
    return re.sub(r"\s+", " ", value).strip().casefold()


def one(nodes, reason):
    if len(nodes) != 1:
        raise SafeError(reason)
    return nodes[0]


def usd_price(text):
    if re.search(r"\b(?:CAD|AUD|HKD|SGD|CNY|EUR)\b", text, re.I):
        raise SafeError("unsupported_currency")
    match = re.search(r"(?:US\$|\$)\s*(\d+\.\d{2})", text)
    if not match or float(match[1]) <= 0:
        raise SafeError("missing_positive_usd_price")
    return match[1]


def configuration(root, product):
    form = one(root.find(lambda n: n.tag == "form" and n.attrs.get("id") == "frmConfigureProduct" and n.enabled()), "configuration_form_missing")
    title = one(form.find(lambda n: n.has_class("product-title") and not n.hidden()), "product_title_missing")
    if normalized(title.text()) != normalized(product.name):
        raise SafeError("wrong_product_identity")
    if form.attrs.get("action"):
        action = urllib.parse.urlparse(urllib.parse.urljoin(BASE, form.attrs["action"]))
        if (action.scheme != "https" or action.hostname not in {"www.dmit.io", "dmit.io"} or
                action.path != "/cart.php" or
                urllib.parse.parse_qs(action.query).get("a") not in (None, ["confproduct"])):
            raise SafeError("unexpected_configuration_action")
    one(form.find(lambda n: n.attrs.get("id") == "btnCompleteProductConfig" and n.enabled()), "configuration_disabled")
    fields = {}
    for node in form.find(lambda n: n.tag in {"input", "select", "textarea"} and n.attrs.get("name")):
        name = node.attrs["name"]
        kind = node.attrs.get("type", "text").lower()
        if "disabled" in node.attrs or (kind != "hidden" and not node.enabled()):
            continue
        if name.startswith("addons[") or kind in {"submit", "button", "reset"}:
            continue
        if node.tag == "select":
            options = node.find(lambda n: n.tag == "option" and n.enabled())
            if name == "billingcycle":
                option = one([n for n in options if n.attrs.get("value") == "monthly"], "monthly_option_missing")
                price = usd_price(option.text())
            else:
                selected = [n for n in options if "selected" in n.attrs]
                option = one(selected or options[:1], "configuration_option_missing")
            value = option.attrs.get("value", option.text().strip())
        else:
            if kind in {"checkbox", "radio"} and "checked" not in node.attrs:
                continue
            value = node.attrs.get("value", "") if node.tag == "input" else node.text().strip()
            value = {"hostname": "inventory-probe.invalid", "ns1prefix": "ns1", "ns2prefix": "ns2",
                     "rootpw": secrets.token_urlsafe(24) + "aA9!"}.get(name, value)
        if name in fields:
            raise SafeError("duplicate_form_field")
        if not value and ("required" in node.attrs or name.startswith("customfield[")):
            raise SafeError("unhandled_required_field")
        fields[name] = value
    if fields.get("configure") != "true" or fields.get("i") != "0" or fields.get("billingcycle") != "monthly":
        raise SafeError("unexpected_cart_configuration")
    if "pid" in fields and fields["pid"] != str(product.pid):
        raise SafeError("wrong_product_id")
    # Only configuration fields are posted; never trust arbitrary hidden action fields.
    allowed = {"configure", "i", "billingcycle", "pid", "token", "hostname", "rootpw", "ns1prefix", "ns2prefix"}
    if any(k not in allowed and not re.fullmatch(r"(?:configoption|customfield)\[\d+\]", k) for k in fields):
        raise SafeError("unrecognized_form_field")
    fields.update(a="confproduct", ajax="1")
    return fields, price


def cart_evidence(root, product):
    cart = one(root.find(lambda n: n.has_class("view-cart-items") and not n.hidden()), "cart_items_missing")
    title = one(cart.find(lambda n: n.has_class("item-title") and not n.hidden()), "cart_must_have_one_product")
    if normalized(title.text(skip_links=True)) != normalized(product.name):
        raise SafeError("wrong_cart_product")
    row = next((n for n in title.ancestors() if n.has_class("item")), None)
    if row is None:
        raise SafeError("cart_row_missing")
    price = one(row.find(lambda n: n.has_class("item-price") and not n.hidden()), "cart_price_missing")
    cycle = one(price.find(lambda n: n.has_class("cycle")), "cart_cycle_missing")
    if normalized(cycle.text()) != "monthly":
        raise SafeError("cart_not_monthly")
    one(root.find(lambda n: n.tag == "a" and n.enabled() and n.attrs.get("id") == "checkout" and
                  urllib.parse.urlparse(urllib.parse.urljoin(BASE, n.attrs.get("href", ""))).path == "/cart.php" and
                  urllib.parse.parse_qs(urllib.parse.urlparse(n.attrs.get("href", "")).query).get("a") == ["checkout"]), "checkout_link_missing")
    return usd_price(price.text())


@dataclass
class Observation:
    status: str
    reason: str
    price: str = ""
    digest: str = ""
    retry_after: int = 0


class OfficialProbe:
    def __init__(self, transport=None):
        # New cookie jar per product, never an authenticated customer session.
        self.transport = transport or Transport({"www.dmit.io", "dmit.io"})
        self.digest = hashlib.sha256()

    def fetch(self, method, url, data=None):
        response = self.transport.request(method, url, data,
            {"Content-Type": "application/x-www-form-urlencoded"} if data else {})
        self.digest.update(response.body.encode())
        if response.status != 200:
            error = SafeError(f"official_http_{response.status}")
            error.retry_after = response.retry_after()
            raise error
        target = urllib.parse.urlparse(response.url)
        if target.hostname not in {"www.dmit.io", "dmit.io"} or target.scheme != "https" or target.path != "/cart.php":
            raise SafeError("official_cart_redirected")
        query = urllib.parse.parse_qs(target.query)
        if query.get("a") not in (["add"], ["confproduct"], ["view"], ["checkout"], None):
            raise SafeError("unrecognized_cart_route")
        root = DOM(response.body).root
        if any(marker in response.body.casefold() for marker in
               ("cf-chl-", "challenge-platform", "verify you are human", "just a moment", "g-recaptcha", "hcaptcha")):
            raise SafeError("official_challenge_or_captcha")
        if any(n for n in root.find(lambda n: n.tag == "form" and "login" in n.attrs.get("action", "").lower())):
            raise SafeError("official_login_required")
        errors = root.find(lambda n: not n.hidden() and (n.has_class("alert-danger") or n.has_class("alert-warning")))
        if any(re.search(r"out of stock|sold out|currently unavailable|缺货|售罄", normalized(n.text())) for n in errors):
            raise SafeError("official_sold_out")
        if any(n.text().strip() for n in errors if n.has_class("alert-danger")):
            raise SafeError("official_validation_error")
        return response, root

    def check(self, product):
        if product not in PRODUCTS:
            raise SafeError("product_not_allowlisted")
        try:
            response, root = self.fetch("GET", product.url + "&billingcycle=monthly&currency=1&language=english")
            pid = urllib.parse.parse_qs(urllib.parse.urlparse(response.url).query).get("pid")
            if pid is not None and pid != [str(product.pid)]:
                raise SafeError("wrong_redirect_product_id")
            fields, initial_price = configuration(root, product)
            # Standard WHMCS themes sometimes expose CSRF only in same-origin page JS.
            tokens = set(re.findall(r'''\bcsrfToken\s*=\s*["']([A-Za-z0-9_-]{16,200})["']''', response.body))
            if "token" not in fields and len(tokens) == 1:
                fields["token"] = tokens.pop()
            # AJAX acceptance must be an empty 200, as in WHMCS standard_cart.
            accepted, _ = self.fetch("POST", BASE, urllib.parse.urlencode(fields).encode())
            if accepted.body.strip():
                raise SafeError("configuration_not_explicitly_accepted")
            _, cart = self.fetch("GET", BASE + "?a=view")
            price = cart_evidence(cart, product)
            if price != initial_price:
                raise SafeError("configuration_cart_price_mismatch")
            checkout, checkout_root = self.fetch("GET", BASE + "?a=checkout")
            form = one(checkout_root.find(lambda n: n.tag == "form" and n.attrs.get("id") == "frmCheckout" and n.enabled()), "checkout_form_missing")
            action = urllib.parse.urlparse(urllib.parse.urljoin(BASE, form.attrs.get("action", "")))
            if (form.attrs.get("method", "").lower() != "post" or action.scheme != "https" or
                    action.hostname not in {"www.dmit.io", "dmit.io"} or action.path != "/cart.php" or
                    urllib.parse.parse_qs(action.query).get("a") != ["checkout"]):
                raise SafeError("checkout_action_unrecognized")
            one(form.find(lambda n: n.tag == "button" and n.attrs.get("type") == "submit" and
                          n.attrs.get("id") == "btnCompleteOrder" and n.enabled()), "checkout_order_button_disabled")
            return Observation("available", "official_configuration_cart_checkout_verified", price, self.digest.hexdigest())
        except SafeError as exc:
            reason = str(exc)
            return Observation("sold_out" if reason == "official_sold_out" else "unknown",
                               reason, digest=self.digest.hexdigest(), retry_after=getattr(exc, "retry_after", 0))
