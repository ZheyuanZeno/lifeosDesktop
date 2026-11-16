#!/usr/bin/env python3
"""Compact, append-only LifeOS client. No model API calls; no direct DB writes."""
import argparse
import base64
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request


class Client:
    def __init__(self, config):
        config = Path(config).resolve()
        options = json.loads(config.read_text(encoding='utf-8'))
        self.app = (config.parent / options['app_dir']).resolve()
        self.data = (config.parent / options['data_dir']).resolve()
        self.port = int(options.get('port', 8765))
        if not 1 <= self.port <= 65535:
            raise ValueError('端口无效')
        self.url = f'http://127.0.0.1:{self.port}'
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, route, payload=None, token=None):
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['X-LifeOS-Token'] = token
        raw = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
        req = urllib.request.Request(self.url + route, data=raw, headers=headers)
        try:
            with self.http.open(req, timeout=15) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise ValueError(f'接口错误 {exc.code}: {exc.read().decode("utf-8", errors="replace")}') from exc

    def state(self, start=True):
        try:
            state = self.request('/api/state')
        except urllib.error.URLError as exc:
            # Only connection refusal permits startup; permissions/errors are not bypassed.
            if not start or not isinstance(exc.reason, ConnectionRefusedError):
                raise
            if not (self.app / 'app.py').is_file() or not (self.data / 'lifeos.sqlite').is_file():
                raise ValueError('找不到已配置的 LifeOS 应用或数据库；请修正 config.json，不会创建空库')
            log = self.data / 'skill-service.log'
            with log.open('ab') as out:
                args = [sys.executable, str(self.app / 'app.py'), '--port', str(self.port),
                        '--data-dir', str(self.data), '--no-browser']
                flags = {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS} if os.name == 'nt' else {'start_new_session': True}
                subprocess.Popen(args, cwd=self.app, stdin=subprocess.DEVNULL, stdout=out, stderr=out, **flags)
            for _ in range(40):
                time.sleep(.15)
                try:
                    return self.state(start=False)
                except urllib.error.URLError as retry:
                    if not isinstance(retry.reason, ConnectionRefusedError):
                        raise
            raise ValueError(f'服务未能启动，请查看 {log}')
        if Path(state.get('data_path', '')).resolve() != self.data:
            raise ValueError('端口对应的数据库与 config.json 不符，已停止操作')
        return state

    def context(self, date=None):
        state = self.state()
        result = {'today': dt.date.today().isoformat(), 'local_time': dt.datetime.now().astimezone().isoformat(),
                  'modules': [{k: m[k] for k in ('id', 'name', 'kind', 'view', 'fields')} for m in state['modules'] if not m['archived']]}
        if date:
            dt.date.fromisoformat(date)
            active = {m['id'] for m in result['modules']}
            result['records'] = [{k: r[k] for k in ('id', 'module_id', 'type', 'date', 'title', 'body', 'status', 'amount', 'currency', 'properties', 'attachments')}
                                 for r in state['records'] if r['date'] == date and not r['deleted'] and r['module_id'] in active]
        return result

    def inbox(self):
        state = self.state()
        pending = []
        for item in state.get('inbox', []):
            if item.get('processed_at'):
                continue
            row = json.loads(json.dumps(item))
            for attachment in row.get('attachments', []):
                attachment['path'] = str((self.data / 'attachments' / attachment['file']).resolve())
            pending.append(row)
        return {
            'today': dt.date.today().isoformat(),
            'local_time': dt.datetime.now().astimezone().isoformat(),
            'revision': state['updated_at'],
            'modules': [{k: m[k] for k in ('id', 'name', 'kind', 'view', 'fields')} for m in state['modules'] if not m['archived']],
            'items': pending,
        }

    def complete(self, ids):
        if not isinstance(ids, list) or not ids or any(not isinstance(value, str) for value in ids):
            raise ValueError('需要至少一个原始收件箱编号')
        state = self.state()
        result = self.request('/api/inbox/complete', {'ids': ids, 'expected_revision': state['updated_at']}, state['token'])
        pending = {item['id'] for item in self.state(start=False).get('inbox', []) if not item.get('processed_at')}
        if any(value in pending for value in ids):
            raise ValueError('原始收件箱清理后校验失败')
        return {**result, 'verified': True}

    def append(self, batch):
        batch = json.loads(json.dumps(batch))
        if not isinstance(batch, dict) or set(batch) - {'modules', 'records'}:
            raise ValueError('批量文件只能包含 modules 和 records')
        modules, rows = batch.setdefault('modules', []), batch.setdefault('records', [])
        if not isinstance(modules, list) or not isinstance(rows, list) or not (modules or rows):
            raise ValueError('需要非空的模块或记录数组')
        state = self.state()
        existing_modules = {m['id']: m for m in state['modules']}
        current = {r['id']: r for r in state['records']}
        available = dict(existing_modules)
        for module in modules:
            if not isinstance(module, dict) or not module.get('id') or module.get('archived'):
                raise ValueError('新模块需要明确 id，且不能放入回收站')
            old = existing_modules.get(module['id'])
            if old and (old['archived'] or any(old.get(k) != v for k, v in module.items())):
                raise ValueError('模块编号已存在且定义不同或已删除，请重新读取 context')
            available[module['id']] = module
        for row in rows:
            if not isinstance(row, dict) or row.get('deleted'):
                raise ValueError('仅支持新增记录')
            if not row.get('id'):
                raw = json.dumps(row, ensure_ascii=False, sort_keys=True).encode()
                row['id'] = 'lifeos-' + hashlib.sha256(raw).hexdigest()[:32]
            module = available.get(row.get('module_id'))
            if not module or module.get('archived'):
                raise ValueError('记录所属模块不存在或已删除，请重新读取 context')
            old = current.get(row['id'])
            if old and (old['deleted'] or any(old.get(k) != v for k, v in row.items())):
                raise ValueError('记录编号冲突或记录已删除，已停止；不会覆盖或自动恢复')
        for row in rows:
            source = row.get('source_id')
            if source and source not in current and source not in {r['id'] for r in rows}:
                raise ValueError('source_id 指向不存在的记录')
        result = self.request('/api/import', {'text': json.dumps(batch, ensure_ascii=False),
                              'expected_revision': state['updated_at'], 'overwrite': False}, state['token'])
        saved = self.state(start=False)
        actual = {r['id']: r for r in saved['records']}
        actual_modules = {m['id']: m for m in saved['modules']}
        if any(r['id'] not in actual or actual[r['id']]['deleted'] or
               any(actual[r['id']].get(k) != v for k, v in r.items()) for r in rows):
            raise ValueError('写入后记录校验不一致，请检查数据库，勿重复生成新编号')
        if any(m['id'] not in actual_modules or actual_modules[m['id']]['archived'] for m in modules):
            raise ValueError('写入后模块校验不一致')
        return {**result, 'verified': True, 'record_ids': [r['id'] for r in rows]}

    def upload(self, file):
        file = Path(file)
        if file.stat().st_size > 10 * 1024 * 1024:
            raise ValueError('单个附件最多 10MB')
        state = self.state()
        return self.request('/api/attachment', {'name': file.name, 'data': base64.b64encode(file.read_bytes()).decode()}, state['token'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=str(Path(__file__).resolve().parents[1] / 'config.json'))
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('context').add_argument('--date')
    sub.add_parser('inbox')
    sub.add_parser('append').add_argument('--file', required=True)
    sub.add_parser('upload').add_argument('--file', required=True)
    complete = sub.add_parser('complete')
    complete.add_argument('--ids', nargs='+', required=True)
    args = parser.parse_args()
    try:
        client = Client(args.config)
        if args.command == 'context':
            result = client.context(args.date)
        elif args.command == 'inbox':
            result = client.inbox()
        elif args.command == 'append':
            result = client.append(json.loads(Path(args.file).read_text(encoding='utf-8')))
        elif args.command == 'upload':
            result = client.upload(args.file)
        else:
            result = client.complete(args.ids)
        print(json.dumps(result, ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
