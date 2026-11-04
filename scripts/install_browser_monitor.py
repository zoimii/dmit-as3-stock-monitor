#!/usr/bin/env python3
"""Install the reviewed worker as a per-user macOS LaunchAgent. No sudo or Telegram tokens."""
import argparse
import json
import os
import plistlib
import shutil
import subprocess
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
LABEL='com.zoimii.dmit-browser-monitor'
DEFAULT_RUNTIME=Path.home()/'Library/Application Support/DMITStockMonitor'
DEFAULT_PLIST=Path.home()/'Library/LaunchAgents'/f'{LABEL}.plist'


def prepare(runtime,plist_path,node,gh):
    runtime=runtime.expanduser().resolve()
    plist_path=plist_path.expanduser().resolve()
    os.umask(0o077)
    for folder in [runtime,runtime/'app',runtime/'logs',runtime/'browser-profile',plist_path.parent]:
        folder.mkdir(parents=True,exist_ok=True)
    os.chmod(runtime,0o700)
    app=runtime/'app'
    shutil.copytree(ROOT/'dmit_monitor',app/'dmit_monitor',dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    shutil.copytree(ROOT/'browser',app/'browser',dirs_exist_ok=True)
    config={'repository':'zoimii/dmit-as3-stock-monitor','interval':300,'node':node,'gh':gh,
            'profile':str(runtime/'browser-profile'),'status_file':str(runtime/'status.json'),
            'log_file':str(runtime/'logs/monitor.jsonl'),
            'lock_file':str(runtime/'service.lock'),'headless':False}
    config_path=runtime/'config.json';config_path.write_text(json.dumps(config,indent=2)+'\n')
    document={'Label':LABEL,'ProgramArguments':['/usr/bin/python3','-u','-m','dmit_monitor.browser_service','--config',str(config_path)],
              'WorkingDirectory':str(app),'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':60,
              'LimitLoadToSessionType':'Aqua','ProcessType':'Background','Umask':63,
              'ExitTimeOut':30,
              'EnvironmentVariables':{'PYTHONDONTWRITEBYTECODE':'1'},
              'StandardOutPath':str(runtime/'logs/service.log'),
              'StandardErrorPath':str(runtime/'logs/launchd-error.log')}
    plist_path.write_bytes(plistlib.dumps(document))
    os.chmod(plist_path,0o600)
    return config_path


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--prepare-only',action='store_true')
    parser.add_argument('--runtime',type=Path,default=DEFAULT_RUNTIME)
    parser.add_argument('--plist',type=Path,default=DEFAULT_PLIST)
    args=parser.parse_args()
    node=shutil.which('node');gh=shutil.which('gh')
    if not node or not gh:raise SystemExit('Node.js and GitHub CLI are required')
    if not (ROOT/'browser/node_modules/playwright').is_dir():raise SystemExit('Install locked browser dependencies first')
    runtime=args.runtime.expanduser().resolve()
    target=args.plist.expanduser().resolve()
    preview=runtime/'launchagent.preview.plist'
    config=prepare(runtime,preview,node,gh)
    print(json.dumps({'config':str(config),'launchagent_preview':str(preview),'intended_launchagent':str(target),'label':LABEL}))
    if args.prepare_only:return
    # Do not place a RunAtLoad plist in LaunchAgents until real official reads pass.
    app=runtime/'app'
    base=['/usr/bin/python3','-m','dmit_monitor.browser_service','--config',str(config)]
    subprocess.run([*base,'--initialize'],cwd=str(app),check=True)
    verified=subprocess.run([*base,'--once','--force'],cwd=str(app))
    if verified.returncode:
        raise SystemExit('Official browser verification failed; no autostart service was installed')
    current=json.loads((runtime/'status.json').read_text())
    if current.get('result_code')!=0 or any(p.get('status') not in {'sold_out','available'} for p in current.get('products',{}).values()):
        raise SystemExit('Browser preflight did not verify inventory; service remains uninstalled')
    target.parent.mkdir(parents=True,exist_ok=True)
    shutil.copy2(preview,target)
    os.chmod(target,0o600)
    domain=f'gui/{os.getuid()}'
    existing=subprocess.run(['launchctl','print',f'{domain}/{LABEL}'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    if existing.returncode==0:
        subprocess.run(['launchctl','bootout',f'{domain}/{LABEL}'],check=True)
    subprocess.run(['launchctl','bootstrap',domain,str(target)],check=True)
    print('Browser monitor LaunchAgent installed and started')


if __name__=='__main__':main()
