#!/usr/bin/env python3
"""Prepare a reviewable local extension; register its native host only with --register."""
import argparse
import json
import os
from pathlib import Path
import shlex
import shutil

ROOT = Path(__file__).resolve().parents[1]
HOST = 'com.zoimii.dmit_monitor'
DEFAULT_RUNTIME = Path.home() / 'Library/Application Support/DMITStockMonitor'
DEFAULT_HOST = Path.home() / 'Library/Application Support/Google/Chrome/NativeMessagingHosts' / f'{HOST}.json'


def prepare(runtime, gh):
    os.umask(0o077)
    runtime = runtime.expanduser().resolve()
    app = runtime / 'app'
    for folder in (runtime, app, runtime/'logs'):
        folder.mkdir(parents=True, exist_ok=True)
    os.chmod(runtime, 0o700)
    shutil.copytree(ROOT/'dmit_monitor', app/'dmit_monitor', dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    extension = runtime/'chrome-extension'
    shutil.copytree(ROOT/'browser-extension', extension, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('*.test.cjs'))
    extension_id = (extension/'extension-id.txt').read_text().strip()
    config = {'repository':'zoimii/dmit-as3-stock-monitor', 'interval':300, 'gh':gh,
              'status_file':str(runtime/'status.json'), 'log_file':str(runtime/'logs/monitor.jsonl'),
              'lock_file':str(runtime/'native.lock')}
    config_path = runtime/'config.json'
    config_path.write_text(json.dumps(config, indent=2)+'\n')
    wrapper = runtime/'native-host'
    wrapper.write_text('#!/bin/sh\nset -eu\nexport PYTHONDONTWRITEBYTECODE=1\n'
                       'export PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin\n'
                       f'cd {shlex.quote(str(app))}\n'
                       f'exec /usr/bin/python3 -u -m dmit_monitor.browser_native --config {shlex.quote(str(config_path))} "$@"\n')
    wrapper.chmod(0o700)
    document = {'name':HOST, 'description':'DMIT AS3 official inventory validation and GitHub Telegram relay',
                'path':str(wrapper), 'type':'stdio', 'allowed_origins':[f'chrome-extension://{extension_id}/']}
    preview = runtime/'native-host.preview.json'
    preview.write_text(json.dumps(document, indent=2)+'\n')
    return {'runtime':str(runtime), 'extension':str(extension), 'extension_id':extension_id,
            'native_manifest_preview':str(preview), 'config':str(config_path)}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--runtime', type=Path, default=DEFAULT_RUNTIME)
    parser.add_argument('--host-manifest', type=Path, default=DEFAULT_HOST)
    parser.add_argument('--register', action='store_true')
    args = parser.parse_args()
    gh = shutil.which('gh')
    if not gh:
        raise SystemExit('GitHub CLI required; Telegram credentials remain in GitHub Secrets')
    result = prepare(args.runtime, gh)
    if args.register:
        host = args.host_manifest.expanduser().resolve()
        host.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(result['native_manifest_preview'], host)
        host.chmod(0o600)
        result['registered_native_host'] = str(host)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
