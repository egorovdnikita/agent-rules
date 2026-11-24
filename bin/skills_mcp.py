#!/usr/bin/env python3
"""Общий источник навыков через MCP: stdio или защищённый локальный HTTP."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
LOCK = threading.Lock()


def skill_path(name):
    if not re.fullmatch(r'[a-z0-9-]{1,64}', name):
        raise ValueError('Недопустимое имя навыка')
    path = ROOT / 'skills' / name / 'SKILL.md'
    if not path.is_file() or not path.resolve().is_relative_to((ROOT / 'skills').resolve()):
        raise ValueError('Навык не найден')
    return path


def snapshot(name):
    data = skill_path(name).read_bytes()
    return {'name': name, 'revision': hashlib.sha256(data).hexdigest(), 'content': data.decode()}


def call(name, args):
    if name == 'list_skills':
        return [{'name': p.parent.name, 'revision': hashlib.sha256(p.read_bytes()).hexdigest()}
                for p in sorted((ROOT / 'skills').glob('*/SKILL.md'))]
    if name == 'read_skill':
        return snapshot(args['name'])
    if name != 'update_skill':
        raise ValueError('Неизвестный инструмент')
    with LOCK:
        current = snapshot(args['name'])
        if not secrets.compare_digest(current['revision'], args['expected_revision']):
            raise ValueError('Конфликт версии: прочитайте актуальный навык и повторите правку')
        content = args['content']
        if len(content.encode()) > 200000 or not content.startswith('---\n'):
            raise ValueError('Некорректный SKILL.md')
        header = content.split('\n---\n', 1)
        if len(header) != 2 or '<!-- PLATFORM_ADAPTER -->' not in content:
            raise ValueError('Нет YAML или маркера адаптера')
        match = re.search(r'^name:\s*["\']?([a-z0-9-]+)["\']?\s*$', header[0], re.M)
        if not match or match[1] != args['name'] or not re.search(r'^description:\s*\S', header[0], re.M):
            raise ValueError('Имя или описание не соответствует навыку')
        path = skill_path(args['name'])
        old = path.read_bytes()
        temp = path.with_suffix('.pending')
        temp.write_text(content)
        temp.replace(path)
        try:
            subprocess.run([sys.executable, str(ROOT / 'bin/skills.py'), 'build'], check=True, capture_output=True)
        except Exception:
            path.write_bytes(old)
            subprocess.run([sys.executable, str(ROOT / 'bin/skills.py'), 'build'], capture_output=True)
            raise
        return {'name': args['name'], 'revision': snapshot(args['name'])['revision'],
                'status': 'saved', 'cloud_native_installations_updated': False}


TOOLS = [
    {'name': 'list_skills', 'description': 'Список общих навыков и актуальных версий.',
     'inputSchema': {'type': 'object', 'properties': {}, 'additionalProperties': False}},
    {'name': 'read_skill', 'description': 'Прочитать актуальные инструкции из общего источника перед применением или правкой.',
     'inputSchema': {'type': 'object', 'properties': {'name': {'type': 'string'}}, 'required': ['name'], 'additionalProperties': False}},
    {'name': 'update_skill', 'description': 'Сохранить явно запрошенную пользователем правку общего навыка. Требуется revision из read_skill; конфликт не перезаписывается. Нативные облачные копии не обновляются.',
     'inputSchema': {'type': 'object', 'properties': {k: {'type': 'string'} for k in ('name', 'expected_revision', 'content')}, 'required': ['name', 'expected_revision', 'content'], 'additionalProperties': False}}
]


def dispatch(req):
    if 'id' not in req:
        return None
    method = req.get('method')
    response = {'jsonrpc': '2.0', 'id': req['id']}
    if method == 'initialize':
        response['result'] = {'protocolVersion': req.get('params', {}).get('protocolVersion', '2025-03-26'),
                              'capabilities': {'tools': {}}, 'serverInfo': {'name': 'shared-skills', 'version': '1.0.0'}}
    elif method == 'ping':
        response['result'] = {}
    elif method == 'tools/list':
        response['result'] = {'tools': TOOLS}
    elif method == 'tools/call':
        try:
            params = req['params']
            result = call(params['name'], params.get('arguments', {}))
            response['result'] = {'content': [{'type': 'text', 'text': json.dumps(result, ensure_ascii=False)}]}
        except Exception as exc:
            response['result'] = {'isError': True, 'content': [{'type': 'text', 'text': str(exc)}]}
    else:
        response['error'] = {'code': -32601, 'message': 'Method not found'}
    return response


def serve_http(port):
    token = os.environ.get('SHARED_SKILLS_TOKEN', '')
    if len(token) < 32:
        raise SystemExit('SHARED_SKILLS_TOKEN должен содержать не менее 32 символов; храните его вне репозитория')

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            if self.path != '/mcp':
                self.send_error(404); return
            if not secrets.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token):
                self.send_error(401); return
            # localhost transport needs an authenticated HTTPS reverse proxy for cloud use.
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 300000:
                    self.send_error(413); return
                result = dispatch(json.loads(self.rfile.read(length)))
                body = json.dumps(result, ensure_ascii=False).encode() if result is not None else b''
                self.send_response(200 if result is not None else 202)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers(); self.wfile.write(body)
            except (ValueError, TypeError, KeyError):
                self.send_error(400)
        def do_GET(self):
            self.send_error(405)
        def log_message(self, *args):
            pass
    ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--http', type=int, metavar='PORT')
    args = parser.parse_args()
    if args.http:
        serve_http(args.http)
    else:
        for line in sys.stdin:
            try:
                result = dispatch(json.loads(line))
                if result is not None:
                    print(json.dumps(result, ensure_ascii=False), flush=True)
            except (ValueError, TypeError, KeyError):
                print(json.dumps({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Parse error'}}), flush=True)
