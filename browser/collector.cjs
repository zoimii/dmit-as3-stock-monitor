'use strict';
// Standalone application collector, with its own Chrome profile. No Codex/UI tools required.
const { chromium } = require('playwright');
const crypto = require('node:crypto');
const readline = require('node:readline');
const fs = require('node:fs');
const path = require('node:path');
const net = require('node:net');
const {spawn} = require('node:child_process');
const PRODUCTS = {253: 'LAX.AS3.Pro.TINY', 254: 'LAX.AS3.Pro.Pocket'};
const BASE = 'https://www.dmit.io/cart.php';
const norm = v => String(v).replace(/\s+/g, ' ').trim().toLowerCase();
class CheckError extends Error {}

function allowRequest(url, method, data = '', navigation = false) {
  let u; try { u = new URL(url); } catch { return false; }
  const official = u.protocol === 'https:' && ['www.dmit.io', 'dmit.io'].includes(u.hostname);
  if (navigation && !official) return false;
  if (official && ['checkout','accepttos','paymentmethod'].some(k=>u.searchParams.has(k))) return false;
  if (navigation && official && u.pathname==='/cart.php') {
    if (u.searchParams.has('pid') && !Object.hasOwn(PRODUCTS,u.searchParams.get('pid'))) return false;
    if (u.searchParams.has('a') && !['add','confproduct','view','checkout','confdomains'].includes(u.searchParams.get('a'))) return false;
  }
  // Let the official site's ordinary automatic browser checks complete; never solve a CAPTCHA.
  if (official && method==='POST' && u.pathname.startsWith('/cdn-cgi/challenge-platform/')) return true;
  if (!official || !['POST', 'PUT', 'DELETE', 'PATCH'].includes(method)) return true;
  if (method !== 'POST' || u.pathname !== '/cart.php') return false;
  const q = new URLSearchParams(data);
  const action = q.get('a') || u.searchParams.get('a');
  return ['confproduct', 'cyclechange'].includes(action) &&
    !q.has('checkout') && !q.has('accepttos') && !q.has('paymentmethod') &&
    (!q.has('pid') || Object.hasOwn(PRODUCTS, q.get('pid'))) &&
    (!q.has('i') || q.get('i') === '0');
}

function price(text) {
  if (/\b(?:CAD|AUD|HKD|SGD|CNY|EUR)\b/i.test(text)) throw new CheckError('unsupported_currency');
  const m = text.match(/(?:US\$|\$)\s*(\d+\.\d{2})/);
  if (!m || Number(m[1]) <= 0) throw new CheckError('missing_positive_usd_price');
  return m[1];
}

class Collector {
  constructor(profile, headless = false) { this.profile = profile; this.headless = headless; }
  async start() {
    fs.mkdirSync(this.profile, { recursive: true, mode: 0o700 });
    // Start an ordinary installed Chrome with an isolated monitor profile.
    // Its debugging endpoint is loopback-only and contains no personal browser data.
    const port=await new Promise((resolve,reject)=>{
      const s=net.createServer();s.once('error',reject);s.listen(0,'127.0.0.1',()=>{
        const p=s.address().port;s.close(()=>resolve(p));
      });
    });
    const executable=process.env.DMIT_BROWSER_EXECUTABLE || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
    const args=[`--user-data-dir=${this.profile}`,`--remote-debugging-port=${port}`,
      '--remote-debugging-address=127.0.0.1','--no-first-run','--no-default-browser-check','--start-minimized',BASE];
    if(this.headless) args.push('--headless=new');
    this.chrome=spawn(executable,args,{stdio:'ignore'});
    this.chrome.on('error',()=>{});
    let connected=false;
    for(let i=0;i<40;i++) {
      if(this.chrome.exitCode!==null) break;
      try {
        this.browser=await chromium.connectOverCDP(`http://127.0.0.1:${port}`,{timeout:1000});connected=true;break;
      } catch {await new Promise(r=>setTimeout(r,250));}
    }
    if(!connected) {if(this.chrome) this.chrome.kill('SIGTERM');throw new CheckError('local_browser_connection_failed');}
    this.context=this.browser.contexts()[0];
    this.guard = async route => {
      const r = route.request();
      const topNavigation=r.isNavigationRequest() && r.frame().parentFrame()===null;
      if (!allowRequest(r.url(), r.method(), r.postData() || '', topNavigation)) return route.abort();
      return route.continue();
    };
    this.page = this.context.pages()[0] || await this.context.newPage();
    this.page.setDefaultTimeout(20000);
    this.page.on('response', r=>{
      if(r.request().isNavigationRequest() && r.frame()===this.page.mainFrame()) this.lastStatus=r.status();
    });
    for (const p of this.context.pages()) if (p !== this.page) await p.close();
  }
  async close() {
    if(this.context) await this.context.close().catch(()=>{});
    if(this.browser) await this.browser.close().catch(()=>{});
    if(this.chrome && this.chrome.exitCode===null) this.chrome.kill('SIGTERM');
    this.context=null;this.browser=null;this.chrome=null;
  }
  async html() {
    const text = await this.page.content();
    if (text.length > 2000000) throw new CheckError('response_too_large');
    this.hash.update(text);
    return text;
  }
  async anonymous() {
    for (const a of await this.page.locator('a[href*="logout"]').all())
      if (await a.isVisible()) throw new CheckError('unexpected_authenticated_session');
  }
  async visit(url, catalog = false) {
    this.lastStatus=null;
    const response = await this.page.goto(url, {waitUntil: 'domcontentloaded', timeout: 45000});
    if (catalog) await this.page.locator('.cart-products-box[pid]').first().waitFor({timeout: 30000});
    else await this.page.waitForFunction(()=>
      document.querySelector('#frmConfigureProduct,.view-cart-items,#frmCheckout') ||
      Array.from(document.querySelectorAll('h1,h2')).some(e=>/^(out of stock|sold out)$/i.test(e.textContent.trim())),
      null,{timeout:30000});
    const u = new URL(this.page.url());
    if (!['www.dmit.io','dmit.io'].includes(u.hostname) || u.pathname !== '/cart.php')
      throw new CheckError('browser_left_official_cart');
    await this.anonymous();
    const title = await this.page.title();
    if (/just a moment|attention required|verify.*human/i.test(title)) throw new CheckError('official_browser_challenge');
    if (this.lastStatus && this.lastStatus >= 400) throw new CheckError(`official_browser_http_${this.lastStatus}`);
    return this.html();
  }
  async check(pid) {
    if (!Object.hasOwn(PRODUCTS,pid)) throw new CheckError('product_not_allowlisted');
    this.hash = crypto.createHash('sha256');
    try {
      if (!this.context || !this.page || this.page.isClosed()) { await this.close(); await this.start(); }
      // Ordinary read-only navigation runs without network interception.
      // Arm the order-submission guard only before any configuration can be submitted.
      await this.context.unrouteAll({behavior:'wait'});
      await this.visit(BASE, true);
      const card = this.page.locator(`.cart-products-box[pid="${pid}"]`);
      if (await card.count() !== 1 || norm(await card.locator('.cart-products-title').innerText()) !== norm(PRODUCTS[pid]))
        throw new CheckError('catalog_product_identity_mismatch');
      // Discard only the monitor's anonymous WHMCS cart session, retaining ordinary browser site cookies.
      await this.context.clearCookies({name: /^(WHMCS|PHPSESSID)/i});
      const configHTML = await this.visit(`${BASE}?a=add&pid=${pid}`);
      const headings = await this.page.locator('h1,h2').allInnerTexts();
      if (headings.some(t => ['out of stock','sold out','缺货','售罄'].includes(norm(t))))
        return {status: 'sold_out', reason: 'official_browser_purchase_page_sold_out', digest: this.hash.digest('hex')};
      const form = this.page.locator('#frmConfigureProduct');
      if (await form.count() !== 1) throw new CheckError('browser_configuration_form_missing');
      const titles = await form.locator('.product-title,.cart-products-title').allInnerTexts();
      if (titles.length !== 1 || norm(titles[0]) !== norm(PRODUCTS[pid])) throw new CheckError('wrong_configuration_product');
      const options = await form.locator('[name="billingcycle"] option').evaluateAll(es => es.map(e => ({value:e.value,text:e.textContent,disabled:e.disabled})));
      const monthly = options.filter(e => e.value === 'monthly' && !e.disabled);
      if (monthly.length !== 1) throw new CheckError('monthly_option_missing');
      const monthlyPrice = price(monthly[0].text);
      await this.context.route('**/*',this.guard);
      await form.locator('[name="billingcycle"]').selectOption('monthly');
      const values = {hostname:'inventory-probe.invalid',rootpw:crypto.randomBytes(24).toString('base64url')+'aA9!',ns1prefix:'ns1',ns2prefix:'ns2'};
      for (const [name,value] of Object.entries(values)) {
        const input=form.locator(`[name="${name}"]`);
        if (await input.count() === 1 && await input.isVisible()) await input.fill(value);
      }
      const fields = await form.locator('input[required],textarea[required],select[required]').evaluateAll(es => es.map(e=>({name:e.name,value:e.value,disabled:e.disabled})));
      if (fields.some(e=>!e.disabled && !e.value)) throw new CheckError('unhandled_required_configuration_field');
      const button=form.locator('#btnCompleteProductConfig');
      if (!await button.isVisible() || !await button.isEnabled()) throw new CheckError('configuration_continue_disabled');
      const acceptedPromise=this.page.waitForResponse(r=> {
        const q=new URLSearchParams(r.request().postData() || '');
        return r.request().method()==='POST' && new URL(r.url()).pathname==='/cart.php' &&
          q.get('a')==='confproduct' && q.get('configure')==='true' && !q.has('calctotal');
      },{timeout:30000});
      await button.click();
      const accepted=await acceptedPromise;
      if (accepted.status()!==200 || (await accepted.text()).trim()) throw new CheckError('configuration_not_explicitly_accepted');
      await this.page.waitForURL(u=>u.searchParams.get('a')!=='confproduct', {timeout:5000}).catch(()=>{});
      const cartHTML=await this.visit(`${BASE}?a=view`);
      const cartTitles=this.page.locator('.view-cart-items .item-title');
      if (await cartTitles.count()!==1) throw new CheckError('cart_must_have_one_product');
      const title=await cartTitles.evaluate(e=>{const c=e.cloneNode(true);c.querySelectorAll('a,button').forEach(n=>n.remove());return c.textContent;});
      if (norm(title)!==norm(PRODUCTS[pid])) throw new CheckError('wrong_cart_product');
      const checkoutLink=this.page.locator('a#checkout');
      if (!await checkoutLink.isVisible() || !await checkoutLink.isEnabled()) throw new CheckError('checkout_link_disabled');
      const checkoutHTML=await this.visit(`${BASE}?a=checkout`);
      const checkout=this.page.locator('form#frmCheckout');
      const complete=checkout.locator('#btnCompleteOrder');
      if (await checkout.count()!==1 || !await complete.isVisible() || !await complete.isEnabled()) throw new CheckError('checkout_not_ready');
      // Never click Complete Order. All checkout POST requests are blocked at the routing layer.
      return {status:'available',reason:'official_browser_configuration_cart_checkout_verified',price:monthlyPrice,
        digest:this.hash.digest('hex'),evidence:{configuration:configHTML,cart:cartHTML,checkout:checkoutHTML,accepted:true}};
    } catch (error) {
      let reason = error instanceof CheckError ? error.message : 'browser_navigation_or_template_error';
      if (!(error instanceof CheckError) && !this.context)
        reason = /operation not permitted|bootstrap_check_in|EPERM|sandbox/i.test(String(error.message))
          ? 'local_browser_start_permission_denied' : 'local_browser_launch_failed';
      if (!(error instanceof CheckError) && this.context) {
        const title=await this.page.title().catch(()=> '');
        if(/just a moment|attention required|verify.*human/i.test(title)) reason='official_browser_challenge_needs_user';
        else if(this.lastStatus && this.lastStatus>=400) reason=`official_browser_http_${this.lastStatus}`;
        await this.page.screenshot({path:path.join(this.profile,'last-page.png'),fullPage:false}).catch(()=>{});
      }
      return {status:'unknown',reason,
        digest:this.hash.digest('hex')};
    }
  }
}

async function main() {
  const profile=process.env.DMIT_BROWSER_PROFILE;
  if (!profile || !path.isAbsolute(profile)) throw new CheckError('absolute_profile_path_required');
  const collector=new Collector(profile, process.env.DMIT_BROWSER_HEADLESS==='true');
  const lines=readline.createInterface({input:process.stdin,crlfDelay:Infinity});
  for await (const line of lines) {
    let message; try {message=JSON.parse(line);} catch {continue;}
    if (message.op==='shutdown') break;
    if (message.op!=='check' || !Object.hasOwn(PRODUCTS,message.pid)) {
      process.stdout.write(JSON.stringify({status:'unknown',reason:'invalid_collector_command'})+'\n');continue;
    }
    const result=await collector.check(message.pid);
    process.stdout.write(JSON.stringify(result)+'\n');
  }
  await collector.close();
}
module.exports={allowRequest,price,Collector};
if(require.main===module) main().catch(()=>{process.stderr.write('browser_collector_failed\n');process.exitCode=1;});
