"""Authenticated loopback bridge hosted INSIDE the Server Connector Tk process."""
from __future__ import annotations

import concurrent.futures
import hmac
import json
import os
import queue
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from connector_operations import operate
from sftp_client import SftpSession
from storage import APP_DIR

RUNTIME_FILE = APP_DIR / 'mcp-runtime.json'
PUBLIC_FIELDS = ('id', 'name', 'host', 'port', 'username', 'remote_path')
OPERATIONS = {'state', 'connect', 'disconnect', 'execute', 'list_directory', 'read_file',
              'write_file', 'upload_file', 'download_file'}


class ConnectorBridge:
    def __init__(self, app, runtime_file=RUNTIME_FILE):
        self.app = app
        self.runtime_file = Path(runtime_file)
        self.token = secrets.token_urlsafe(32)
        self.pending = queue.Queue()
        self.lock = threading.Lock()
        self.closed = False
        self.server = None

    def start(self):
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                try:
                    if self.path != '/rpc' or self.headers.get('Origin'):
                        self.send_error(403)
                        return
                    auth = self.headers.get('Authorization', '')
                    if not hmac.compare_digest(auth, 'Bearer ' + bridge.token):
                        self.send_error(401)
                        return
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= 8 * 1024 * 1024:
                        self.send_error(413)
                        return
                    self.connection.settimeout(10)
                    req = json.loads(self.rfile.read(size))
                    result = bridge.dispatch(req['operation'], req.get('arguments', {}))
                except Exception as exc:
                    result = {'ok': False, 'error': str(exc), 'error_type': type(exc).__name__}
                payload = json.dumps(result, ensure_ascii=True).encode()
                try:
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except (OSError, ConnectionError):
                    pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.runtime_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.runtime_file.with_suffix('.tmp-' + str(os.getpid()))
        tmp.write_text(json.dumps({'port': self.server.server_port, 'token': self.token,
                                  'pid': os.getpid(), 'project_path': str(Path(__file__).parent)}), encoding='utf-8')
        os.chmod(tmp, 0o600)
        tmp.replace(self.runtime_file)
        self.poll_id = self.app.after(40, self._poll)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _poll(self):
        if self.closed:
            return
        while True:
            try:
                future, fn = self.pending.get_nowait()
            except queue.Empty:
                break
            if future.set_running_or_notify_cancel():
                try:
                    future.set_result(fn())
                except Exception as exc:
                    future.set_exception(exc)
        self.poll_id = self.app.after(40, self._poll)

    def ui(self, fn):
        if self.closed:
            raise RuntimeError('Server Connector is closing')
        future = concurrent.futures.Future()
        self.pending.put((future, fn))
        try:
            return future.result(timeout=10)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise RuntimeError('Server Connector UI is busy; request was not scheduled')

    def snapshot(self):
        app = self.app
        connected_id = app.connected_server_id if app.session.connected else None
        return {'ok': True, 'application': 'Server Connector', 'pid': os.getpid(),
                'project_path': str(Path(__file__).parent),
                'connected_server_id': connected_id, 'selected_server_id': app.current_id,
                'remote_path': app.remote.path, 'busy': bool(app.busy or app.active_jobs or app.mcp_busy),
                'servers': [{**{k: s.get(k) for k in PUBLIC_FIELDS}, 'connected': s['id'] == connected_id}
                            for s in app.store.servers]}

    def prepare(self, operation, args):
        app = self.app
        if app.busy or app.active_jobs or getattr(app, '_command_running', False):
            raise RuntimeError('The application has an active operation; wait before retrying')
        sid = args.get('server_id')
        profile = app.store.get(sid)
        if profile is None:
            raise ValueError('Unknown server_id. Obtain it from get_state first.')
        if operation != 'connect' and (not app.session.connected or app.connected_server_id != sid):
            raise RuntimeError('This server is not connected in the application. Call connect_server with its id first.')
        app.mcp_busy = True
        app.set_status('MCP: ' + operation + ' · ' + str(profile.get('name')))
        return dict(profile), app.session, app.remote.path

    def dispatch(self, operation, args):
        if operation not in OPERATIONS:
            raise ValueError('Unknown operation')
        if operation == 'state':
            return self.ui(self.snapshot)
        if not self.lock.acquire(blocking=False):
            raise RuntimeError('An MCP operation is already running; do not duplicate it')
        prepared = False
        try:
            profile, session, cwd = self.ui(lambda: self.prepare(operation, args))
            prepared = True
            if operation == 'connect':
                if session.connected and self.ui(lambda: self.app.connected_server_id) == profile['id']:
                    return self.ui(self.snapshot)
                new_session = SftpSession()
                try:
                    home = new_session.connect(**{
                        k: profile.get(k) for k in (
                            'host', 'port', 'username', 'password', 'key_path',
                            'proxy_jump', 'sftp_server_command'
                        )
                    })
                    path = profile.get('remote_path') or home
                    try:
                        entries = new_session.listdir(path)
                    except OSError:
                        path, entries = home, new_session.listdir(home)

                    def adopt():
                        if self.app.session is not session:
                            raise RuntimeError('Connection changed during MCP connect')
                        self.app.session = new_session
                        saved = self.app.store.get(profile['id'])
                        saved['remote_path'] = path
                        self.app.servers_tree.selection_set(profile['id'])
                        self.app._connect_ok(saved, home, entries)
                    self.ui(adopt)
                except Exception:
                    new_session.close()
                    raise
                session.close()
                return self.ui(self.snapshot)
            if operation == 'disconnect':
                self.ui(self.app.disconnect)
                return self.ui(self.snapshot)
            result = operate(session, operation, args, cwd)
            result['server_id'] = profile['id']
            return result
        finally:
            if prepared:
                def finish():
                    self.app.mcp_busy = False
                    self.app.set_status('MCP: операция завершена; результат передан агенту')
                    if operation in {'write_file', 'upload_file'} and self.app.session is session:
                        self.app.remote.refresh()
                try:
                    self.ui(finish)
                except RuntimeError:
                    pass
            self.lock.release()

    def close(self):
        self.closed = True
        if hasattr(self, 'poll_id'):
            self.app.after_cancel(self.poll_id)
        if self.server:
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            self.server.server_close()
        try:
            if json.loads(self.runtime_file.read_text(encoding='utf-8')).get('token') == self.token:
                self.runtime_file.unlink()
        except (OSError, ValueError):
            pass
