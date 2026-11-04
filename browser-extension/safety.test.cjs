const test=require('node:test');
const assert=require('node:assert/strict');
const {configurationFieldsAllowed,isolatedWindow}=require('./background.js');
const good={a:'confproduct',ajax:'1',configure:'true',i:'0',billingcycle:'monthly',pid:'253'};
test('configuration is the only permitted POST payload',()=>{
  assert.equal(configurationFieldsAllowed(good,253),true);
  for(const extra of [{a:'checkout'},{accepttos:'1'},{paymentmethod:'paypal'},{pid:'254'},{billingcycle:'annually'},{i:'1'},{'customfield[x]':'x'}])
    assert.equal(configurationFieldsAllowed({...good,...extra},253),false);
  assert.equal(configurationFieldsAllowed({...good,pid:'255'},255),false);
});
test('verification transfer reads only named cookies and writes only the owned private store',async()=>{
  const reads=[], writes=[];
  global.chrome={extension:{isAllowedIncognitoAccess:async()=>true},
    storage:{session:{get:async()=>({monitorWindow:{windowId:9,tabId:10}})}},
    windows:{getAll:async()=>[{id:9,incognito:true,tabs:[{id:10,url:'about:blank'}]}]},
    cookies:{getAllCookieStores:async()=>[{id:'normal',tabIds:[3]},{id:'private',tabIds:[10]}],
      get:async request=>{reads.push(request);return {name:request.name,value:'local-only',domain:'.dmit.io',path:'/',secure:true,httpOnly:true,sameSite:'lax'};},
      set:async request=>{writes.push(request);}}};
  try {
    const own=await isolatedWindow();assert.equal(own.storeId,'private');
    assert.deepEqual(reads.map(r=>r.name),['cf_clearance','__cf_bm','_cfuvid']);
    assert.ok(writes.every(r=>r.storeId==='private'));
    chrome.windows.getAll=async()=>[{id:11,incognito:true,tabs:[{id:12}]}];
    await assert.rejects(isolatedWindow(),/other_incognito_window_open_pause/);
    assert.equal(reads.length,3);
  } finally {delete global.chrome;}
});
