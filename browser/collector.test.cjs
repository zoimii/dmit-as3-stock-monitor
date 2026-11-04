'use strict';
const test=require('node:test');
const assert=require('node:assert/strict');
const {allowRequest,price}=require('./collector.cjs');
test('checkout and account mutations are blocked independently of page content',()=>{
  for(const [url,method,data] of [
    ['https://www.dmit.io/cart.php?a=checkout','POST','checkout=true'],
    ['https://www.dmit.io/cart.php','POST','a=confproduct&checkout=true'],
    ['https://www.dmit.io/cart.php?a=checkout&checkout=true','GET',''],
    ['https://www.dmit.io/clientarea.php','POST','username=x'],
    ['https://www.dmit.io/cart.php','POST','a=confproduct&pid=999&i=0'],
    ['https://www.dmit.io/cart.php','POST','a=confproduct&i=1'],
  ]) assert.equal(allowRequest(url,method,data,true),false);
});
test('only target products are allowed as purchase-page navigations',()=>{
  for(const pid of [253,254]) assert.equal(allowRequest(`https://www.dmit.io/cart.php?a=add&pid=${pid}`,'GET','',true),true);
  assert.equal(allowRequest('https://www.dmit.io/cart.php?a=add&pid=999','GET','',true),false);
  assert.equal(allowRequest('https://third-party.example/cart.php?a=add&pid=253','GET','',true),false);
  assert.equal(allowRequest('https://www.dmit.io/cart.php?a=checkout','GET','',true),true);
  assert.equal(allowRequest('https://www.dmit.io/cart.php','POST','a=confproduct&configure=true&i=0',true),true);
});
test('only a positive dollar price is accepted',()=>{
  assert.equal(price('$ 10.90 USD Monthly'),'10.90');
  for(const value of ['$0.00 USD','$10.90 CAD','Free','10.90']) assert.throws(()=>price(value));
});
