'use strict';
const BASE = 'https://www.dmit.io/cart.php';
const PRODUCTS = {253: 'LAX.AS3.Pro.TINY', 254: 'LAX.AS3.Pro.Pocket'};
const VERIFICATION_COOKIES = ['cf_clearance', '__cf_bm', '_cfuvid'];
const ALARM = 'dmit-official-five-minutes';
const norm = value => String(value).replace(/\s+/g, ' ').trim().toLowerCase();
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
let running = false;

function configurationFieldsAllowed(fields, pid) {
  if (!Object.hasOwn(PRODUCTS, pid) || !fields || typeof fields !== 'object') return false;
  const allowed = new Set(['configure', 'i', 'billingcycle', 'pid', 'token', 'hostname', 'rootpw', 'ns1prefix', 'ns2prefix', 'a', 'ajax']);
  return fields.a === 'confproduct' && fields.ajax === '1' && fields.configure === 'true' &&
    fields.i === '0' && fields.billingcycle === 'monthly' && (!fields.pid || fields.pid === String(pid)) &&
    Object.entries(fields).every(([k,v]) => (allowed.has(k) || /^(configoption|customfield)\[\d+\]$/.test(k)) && typeof v === 'string');
}

class Native {
  constructor() {
    this.next = 1; this.pending = new Map();
    this.port = chrome.runtime.connectNative('com.zoimii.dmit_monitor');
    this.port.onMessage.addListener(message => {
      const pending = this.pending.get(message.id);
      if (!pending) return;
      this.pending.delete(message.id); clearTimeout(pending.timer);
      if (message.error) pending.reject(new Error(message.error)); else pending.resolve(message.result);
    });
    this.port.onDisconnect.addListener(() => {
      // Consume Chrome's error without copying its text into logs or storage.
      void chrome.runtime.lastError;
      for (const p of this.pending.values()) {clearTimeout(p.timer); p.reject(new Error('native_bridge_disconnected'));}
      this.pending.clear();
    });
  }
  call(op, args = {}) {
    const id = this.next++;
    return new Promise((resolve,reject) => {
      const timer = setTimeout(() => {this.pending.delete(id); reject(new Error('native_bridge_timeout'));}, 300000);
      this.pending.set(id, {resolve, reject, timer});
      try {this.port.postMessage({id, op, ...args});}
      catch {clearTimeout(timer); this.pending.delete(id); reject(new Error('native_bridge_unavailable'));}
    });
  }
  close() {this.port.disconnect();}
}

async function isolatedWindow() {
  if (!await chrome.extension.isAllowedIncognitoAccess()) throw new Error('allow_incognito_required');
  const saved = await chrome.storage.session.get('monitorWindow');
  const all = await chrome.windows.getAll({populate:true});
  const privateWindows = all.filter(w => w.incognito);
  let own = saved.monitorWindow && privateWindows.find(w => w.id === saved.monitorWindow.windowId);
  if (privateWindows.some(w => !own || w.id !== own.id)) throw new Error('other_incognito_window_open_pause');
  if (own) {
    if (own.tabs.length !== 1 || own.tabs[0].id !== saved.monitorWindow.tabId) throw new Error('monitor_window_modified_pause');
    const u = own.tabs[0].url;
    if (u && u !== 'about:blank' && !/^https:\/\/(www\.)?dmit\.io\/cart\.php(?:[?#]|$)/.test(u)) throw new Error('monitor_tab_navigation_pause');
  } else {
    own = await chrome.windows.create({incognito:true, focused:false, url:'about:blank', type:'normal'});
    await chrome.windows.update(own.id, {state:'minimized'});
    await chrome.storage.session.set({monitorWindow:{windowId:own.id, tabId:own.tabs[0].id}});
  }
  const tabId = own.tabs[0].id;
  const stores = await chrome.cookies.getAllCookieStores();
  const store = stores.find(s => s.tabIds.includes(tabId));
  if (!store) throw new Error('isolated_cookie_store_missing');
  // Enumerate no ordinary browsing cookies. Only these three named verification cookies are read.
  for (const name of VERIFICATION_COOKIES) {
    const cookie = await chrome.cookies.get({url:BASE, name});
    if (!cookie || !['dmit.io','www.dmit.io'].includes(cookie.domain.replace(/^\./,''))) continue;
    const details = {url:BASE, name:cookie.name, value:cookie.value, storeId:store.id,
      path:cookie.path, secure:cookie.secure, httpOnly:cookie.httpOnly, sameSite:cookie.sameSite};
    if (!cookie.hostOnly) details.domain = cookie.domain;
    if (cookie.expirationDate) details.expirationDate = cookie.expirationDate;
    await chrome.cookies.set(details);
  }
  return {tabId, windowId:own.id, storeId:store.id};
}

async function assertOwned(own) {
  const saved = (await chrome.storage.session.get('monitorWindow')).monitorWindow;
  const all = await chrome.windows.getAll({populate:true});
  const window = all.find(w => w.id === own.windowId);
  if (!saved || saved.tabId !== own.tabId || !window?.incognito || window.tabs.length !== 1 ||
      window.tabs[0].id !== own.tabId || all.some(w => w.incognito && w.id !== own.windowId))
    throw new Error('monitor_isolation_changed_pause');
}

async function execute(own, func, args = []) {
  await assertOwned(own);
  const values = await chrome.scripting.executeScript({target:{tabId:own.tabId}, func, args});
  if (values.length !== 1 || values[0].result === undefined) throw new Error('browser_script_result_missing');
  return values[0].result;
}

async function visit(own, url) {
  await assertOwned(own);
  await chrome.tabs.update(own.tabId, {url});
  for (let attempt = 0; attempt < 60; attempt++) {
    await delay(500);
    const tab = await chrome.tabs.get(own.tabId);
    if (tab.status !== 'complete') continue;
    const result = await execute(own, () => {
      if (location.protocol !== 'https:' || !['www.dmit.io','dmit.io'].includes(location.hostname) || location.pathname !== '/cart.php')
        return {error:'browser_left_official_cart'};
      const html = document.documentElement.outerHTML;
      if (html.length > 2000000) return {error:'browser_page_too_large'};
      const status = performance.getEntriesByType('navigation')[0]?.responseStatus;
      return {url:location.href, html, status,
        headings:Array.from(document.querySelectorAll('h1,h2')).filter(e => e.getClientRects().length).map(e => e.textContent.trim()),
        cards:Array.from(document.querySelectorAll('.cart-products-box[pid]')).map(e => ({pid:e.getAttribute('pid'), name:e.querySelector('.cart-products-title')?.textContent.trim()}))};
    });
    if (result.error) throw new Error(result.error);
    if (result.status !== 200) throw new Error(`official_browser_http_${Number(result.status)||0}`);
    if (/cf-chl-|challenge-platform|verify you are human|just a moment|sorry, you have been blocked|g-recaptcha|hcaptcha/i.test(result.html))
      throw new Error('official_browser_challenge_or_blocked');
    return result;
  }
  throw new Error('official_browser_navigation_timeout');
}

async function check(own, bridge, pid) {
  const checked_at = Math.floor(Date.now()/1000);
  try {
    // Only the dedicated private store is reset, never the ordinary profile's shopping cart/login.
    await assertOwned(own);
    for (const cookie of await chrome.cookies.getAll({domain:'dmit.io', storeId:own.storeId})) {
      if (/^(WHMCS|PHPSESSID)/i.test(cookie.name)) await chrome.cookies.remove({
        url:`https://${cookie.domain.replace(/^\./,'')}${cookie.path}`, name:cookie.name, storeId:own.storeId});
    }
    const catalogue = await visit(own, BASE);
    const cards = catalogue.cards.filter(c => c.pid === String(pid));
    if (cards.length !== 1 || norm(cards[0].name) !== norm(PRODUCTS[pid])) throw new Error('browser_catalog_product_mismatch');
    const configuration = await visit(own, `${BASE}?a=add&pid=${pid}`);
    if (configuration.headings.some(t => ['out of stock','sold out','缺货','售罄'].includes(norm(t))))
      return {status:'sold_out', checked_at, evidence:{catalogue, configuration}};
    const prepared = await bridge.call('prepare_config', {pid, page:configuration});
    if (!configurationFieldsAllowed(prepared.fields, pid)) throw new Error('configuration_post_not_allowlisted');
    const accepted = await execute(own, async fields => {
      if (location.origin !== 'https://www.dmit.io' || location.pathname !== '/cart.php') return false;
      // The sole POST implemented by this extension is anonymous product configuration.
      const response = await fetch('/cart.php', {method:'POST', credentials:'same-origin', redirect:'error',
        headers:{'Content-Type':'application/x-www-form-urlencoded'}, body:new URLSearchParams(fields).toString()});
      return response.status === 200 && (await response.text()).trim() === '';
    }, [prepared.fields]);
    if (!accepted) throw new Error('configuration_not_explicitly_accepted');
    const cart = await visit(own, `${BASE}?a=view`);
    await bridge.call('validate_cart', {pid, page:cart, price:prepared.price});
    const checkout = await visit(own, `${BASE}?a=checkout`);
    // Never click a checkout control or submit any checkout/account/payment request.
    return {status:'available', checked_at, price:prepared.price, evidence:{catalogue,configuration,cart,checkout,accepted:true}};
  } catch (error) {
    return {status:'unknown', checked_at, reason:/^[a-z0-9_]{1,100}$/.test(error.message) ? error.message : 'browser_check_failed'};
  }
}

async function cycle(force = false) {
  if (running) return {running:true};
  running = true;
  let bridge;
  await chrome.storage.local.set({running:true});
  try {
    bridge = new Native();
    await bridge.call('ping');
    const plan = await bridge.call('plan', {force});
    const results = {};
    let isolationError = null;
    if (plan.due.length) {
      try {
        const own = await isolatedWindow();
        for (const pid of plan.due) results[String(pid)] = await check(own, bridge, pid);
      } catch (error) {
        isolationError = /^[a-z0-9_]{1,100}$/.test(error.message) ? error.message : 'browser_isolation_unavailable';
        for (const pid of plan.due) results[String(pid)] = {status:'unknown',checked_at:Math.floor(Date.now()/1000),reason:isolationError};
      }
    }
    const status = await bridge.call('apply', {results});
    await chrome.storage.local.set({status, error:isolationError, lastCycle:Date.now()});
    await chrome.action.setBadgeText({text:status.result_code === 0 ? 'OK' : '?'});
    await chrome.action.setBadgeBackgroundColor({color:status.result_code === 0 ? '#167c45' : '#ad5700'});
    return status;
  } catch (error) {
    const reason = /^[a-z0-9_]{1,100}$/.test(error.message) ? error.message : 'browser_cycle_failed';
    await chrome.storage.local.set({error:reason, lastAttempt:Date.now()});
    await chrome.action.setBadgeText({text:'!'});
    await chrome.action.setBadgeBackgroundColor({color:'#b32424'});
    return {error:reason};
  } finally {
    if (bridge) bridge.close();
    running = false;
    await chrome.storage.local.set({running:false});
  }
}

async function ensureAlarm() {
  const {enabled} = await chrome.storage.local.get('enabled');
  if (enabled && !await chrome.alarms.get(ALARM)) await chrome.alarms.create(ALARM, {delayInMinutes:5,periodInMinutes:5});
}

if (typeof chrome !== 'undefined') {
  chrome.runtime.onInstalled.addListener(() => {ensureAlarm();});
  chrome.runtime.onStartup.addListener(async () => {await chrome.storage.session.remove('monitorWindow'); await ensureAlarm();});
  chrome.alarms.onAlarm.addListener(async alarm => {
    if (alarm.name === ALARM && (await chrome.storage.local.get('enabled')).enabled) await cycle();
  });
  chrome.runtime.onMessage.addListener((message, sender, reply) => {
    if (sender.id !== chrome.runtime.id || sender.tab) return;
    if (message.op === 'start') {
      (async () => {
        await chrome.storage.local.set({enabled:true}); await ensureAlarm(); reply(await cycle(true));
      })(); return true;
    }
    if (message.op === 'check') {cycle().then(reply); return true;}
    if (message.op === 'pause') {
      (async () => {await chrome.storage.local.set({enabled:false}); await chrome.alarms.clear(ALARM); reply({enabled:false});})(); return true;
    }
  });
  ensureAlarm();
}
if (typeof module !== 'undefined') module.exports = {configurationFieldsAllowed, isolatedWindow};
