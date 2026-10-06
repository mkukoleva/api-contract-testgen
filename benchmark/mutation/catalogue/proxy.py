"""Read-only Catalogue response interceptor; never forwards arbitrary destinations."""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
from urllib.request import build_opener, ProxyHandler, HTTPRedirectHandler
from urllib.error import HTTPError
from prototype.evaluate.mutation import ResponseMutationPlan, MutationType
from prototype.evaluate.mutation import prepare_response_mutation


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


def match_operation(contract, path):
    # Static /catalogue/size must win over /catalogue/{id}.
    for template in sorted(contract['paths'], key=lambda p: ('{' in p, p)):
        pattern = '/'.join('[^/]+' if part.startswith('{') and part.endswith('}') else re.escape(part)
                           for part in template.split('/'))
        if re.fullmatch(pattern, path): return template
    return None


def serve(config_path='/config/config.json'):
    config = json.loads(Path(config_path).read_text())
    contract = config['contract']
    plan = None
    if config['plan']:
        data = dict(config['plan'])
        data['operator'] = MutationType(data['operator'])
        data['value_path'] = tuple(data['value_path'])
        plan = ResponseMutationPlan(**data)
    consumed = False
    client = build_opener(ProxyHandler({}), NoRedirect())
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            nonlocal consumed
            path = urlsplit(self.path).path
            operation = match_operation(contract, path)
            if not operation:
                self.send_response(404)
                self.end_headers()
                return
            event = {'nodeid': self.headers.get('X-Mutation-Node', ''),
                     'phase': self.headers.get('X-Mutation-Phase', ''),
                     'path': path, 'operation': operation, 'selected': False, 'delivered': False}
            try:
                try:
                    response = client.open('http://catalogue:8080' + self.path, timeout=10)
                except HTTPError as exc:
                    response = exc
                with response:
                    status, raw = response.status, response.read(2 * 1024 * 1024 + 1)
                    media = response.headers.get('Content-Type', '')
                if len(raw) > 2 * 1024 * 1024: raise ValueError('Response exceeds 2 MiB')
                body = json.loads(raw)
                event.update(original_status=status, original_body=body)
                if plan and not consumed and event['phase'] == 'call' and operation == plan.api_path:
                    preparation = prepare_response_mutation(contract, plan, response_status=status,
                                                            body=body, media_type=media)
                    event['preparation'] = preparation.to_dict()
                    if preparation.status == 'ready':
                        consumed = True
                        event['selected'] = True
                        status, body = preparation.mutated_status, preparation.body
                        raw = json.dumps(body, allow_nan=False).encode()
                event.update(status=status, body=body)
                self.send_response(status)
                self.send_header('Content-Type', media)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                self.wfile.flush()
                event['delivered'] = True
            except Exception as exc:
                event['error'] = f'{type(exc).__name__}: {exc}'
                try:
                    self.send_error(502)
                except OSError: pass
            finally:
                with Path('/evidence/http.jsonl').open('a') as stream:
                    stream.write(json.dumps(event) + '\n')
                    stream.flush()
    HTTPServer(('0.0.0.0', 8080), Handler).serve_forever()


if __name__ == '__main__': serve()
