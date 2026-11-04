'use strict';
const names = {'253':'TINY','254':'Pocket'};
const errors = {
  allow_incognito_required:'请在扩展详情中打开“在无痕模式下启用”。',
  native_bridge_disconnected:'本机验证桥未连接，请检查安装情况和 GitHub 登录。',
  native_bridge_unavailable:'本机验证桥不可用。',
  other_incognito_window_open_pause:'有其他无痕窗口，采集已暂停；关闭后会按计划重试。',
  monitor_window_modified_pause:'专用监控窗口已被修改，请关闭该窗口后重试。',
  monitor_isolation_changed_pause:'无痕窗口隔离条件改变，本轮未继续采集。',
  github_cli_request_failed:'GitHub 未连接，请检查网络或 GitHub CLI 登录。'
};
function time(value){return value ? new Date(value*1000).toLocaleString('zh-CN') : '尚未检查';}
async function render(){
  const value = await chrome.storage.local.get(['enabled','running','error','status']);
  const lines = [value.enabled ? '监控已启用' : '监控未启用', value.running ? '正在核验官网…' : ''];
  if(value.error) lines.push(errors[value.error] || `本轮未完成：${value.error}`);
  for(const [pid,p] of Object.entries(value.status?.products || {})) {
    const state = {sold_out:'官网明确缺货',available:'官网可下单已验证',unknown:'官网无法验证',uninitialized:'尚未检查'}[p.status] || p.status;
    lines.push(`${names[pid]}：${state}\n${time(p.checked_at)}\n下次：${time(p.next_check)}`);
  }
  document.querySelector('#status').textContent=lines.filter(Boolean).join('\n');
  document.querySelector('#start').disabled=!!value.running || !!value.enabled;
  document.querySelector('#check').disabled=!!value.running || !value.enabled;
}
for(const op of ['start','check','pause']) document.getElementById(op).addEventListener('click',async()=>{
  chrome.runtime.sendMessage({op}).catch(()=>{}); await new Promise(r=>setTimeout(r,100)); render();
});
chrome.storage.onChanged.addListener(render); render();
