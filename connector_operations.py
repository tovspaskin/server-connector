"""MCP operations on the SAME SftpSession used by the desktop application."""
from __future__ import annotations

import hashlib
import os
import shlex
import stat
import threading
import time
import uuid
from dataclasses import asdict

MAX_FILE = 1024 * 1024


def absolute(path: str) -> str:
    if not isinstance(path, str) or not path.startswith('/') or '\x00' in path:
        raise ValueError('Use an absolute remote POSIX path')
    return path


def read_regular(sftp, path):
    info = sftp.lstat(path)
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE:
        raise ValueError('Expected a regular file up to 1 MiB; symlinks are not followed')
    with sftp.open(path, 'rb') as f:
        data = f.read(MAX_FILE + 1)
    if len(data) > MAX_FILE:
        raise ValueError('File exceeds 1 MiB')
    return data, info


def sha(data):
    return hashlib.sha256(data).hexdigest()


def bounded_command(session, command, cwd, timeout=30, max_output_bytes=24000):
    if not isinstance(command, str) or not command or '\x00' in command or len(command.encode()) > 65536:
        raise ValueError('Command must contain 1..65536 bytes without NUL')
    if not 1 <= timeout <= 300 or not 1024 <= max_output_bytes <= 131072:
        raise ValueError('Timeout must be 1..300 seconds, output limit 1024..131072 bytes')
    full = 'cd -- ' + shlex.quote(absolute(cwd)) + ' && ' + command
    with session._lock:
        session._s()
        client = session._transport
        transport = client.get_transport()
    channel = transport.open_session(timeout=min(timeout, 20))
    key = None
    try:
        with session._lock:
            if session._transport is not client or not session.connected:
                raise RuntimeError('Connection changed before command dispatch')
            key = session._register_channel('mcp', channel)
        channel.settimeout(timeout)
        channel.exec_command(full)
        channel.shutdown_write()
        out, err = bytearray(), bytearray()
        totals = [0, 0]
        deadline = time.monotonic() + timeout
        timed_out = False
        while True:
            for i, (ready, recv, buf) in enumerate(((channel.recv_ready, channel.recv, out),
                                                   (channel.recv_stderr_ready, channel.recv_stderr, err))):
                # One chunk per iteration keeps stderr and timeout responsive under continuous output.
                if ready():
                    chunk = recv(32768)
                    totals[i] += len(chunk)
                    buf.extend(chunk[:max(0, max_output_bytes - len(buf))])
            if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                code = channel.recv_exit_status()
                break
            if time.monotonic() >= deadline:
                timed_out, code = True, None
                break
            if channel.closed and not channel.exit_status_ready():
                raise ConnectionError('Channel closed; command completion is unknown')
            time.sleep(0.01)
        return {'ok': code == 0 and not timed_out, 'exit_code': code, 'timed_out': timed_out,
                'stdout': out.decode('utf-8', 'replace'), 'stderr': err.decode('utf-8', 'replace'),
                'stdout_truncated': totals[0] > max_output_bytes,
                'stderr_truncated': totals[1] > max_output_bytes,
                'note': 'SSH channel closed on timeout. Remote completion is unknown; inspect before retrying.' if timed_out else None}
    finally:
        if key is not None:
            session._unregister_channel(key, channel)
        channel.close()


def operate(session, operation, args, cwd):
    if operation == 'execute':
        return bounded_command(session, args['command'], args.get('cwd') or cwd,
                               args.get('timeout_seconds', 30), args.get('max_output_bytes', 24000))
    path = absolute(args.get('path') or cwd)
    if operation == 'list_directory':
        return {'ok': True, 'path': path, 'entries': [asdict(e) for e in session.listdir(path)]}
    if operation == 'upload_file':
        local = os.path.abspath(args['local_path'])
        if not os.path.isfile(local):
            raise ValueError('local_path must be an existing local file')
        # Large binary transfers use the application's existing resumable transfer implementation.
        if session.exists(path) and not args.get('overwrite', False):
            raise FileExistsError('Destination exists; set overwrite only when authorized')
        transfer(session, lambda cancel: session.upload_file(local, path, operation_id='mcp', cancel=cancel))
        return {'ok': True, 'path': path, 'local_path': local}
    if operation == 'download_file':
        local = os.path.abspath(args['local_path'])
        if os.path.exists(local) and not args.get('overwrite', False):
            raise FileExistsError('Local destination exists; set overwrite only when authorized')
        transfer(session, lambda cancel: session.download_file(path, local, operation_id='mcp', cancel=cancel))
        return {'ok': True, 'path': path, 'local_path': local}
    with session._transfer_channel('mcp') as sftp:
        if operation == 'read_file':
            data, info = read_regular(sftp, path)
            return {'ok': True, 'path': path, 'content': data.decode('utf-8'),
                    'sha256': sha(data), 'mode': format(stat.S_IMODE(info.st_mode), '04o')}
        if operation != 'write_file':
            raise ValueError('Unknown operation')
        data = args['content'].encode('utf-8')
        if len(data) > MAX_FILE:
            raise ValueError('Content exceeds 1 MiB')
        try:
            old, info = read_regular(sftp, path)
        except FileNotFoundError:
            old, info = None, None
        actual = 'missing' if old is None else sha(old)
        if args['expected_sha256'] != actual:
            return {'ok': False, 'conflict': True, 'actual_sha256': actual}
        temp = path + '.mcp-' + uuid.uuid4().hex + '.tmp'
        backup = None
        try:
            with sftp.open(temp, 'wx') as f:
                sftp.chmod(temp, 0o600)
                f.write(data)
            sftp.chmod(temp, stat.S_IMODE(info.st_mode) if info else 0o600)
            if info:
                temp_info = sftp.stat(temp)
                if (temp_info.st_uid, temp_info.st_gid) != (info.st_uid, info.st_gid):
                    sftp.chown(temp, info.st_uid, info.st_gid)
                current, _ = read_regular(sftp, path)
                if sha(current) != actual:
                    return {'ok': False, 'conflict': True}
                backup = path + '.mcp-backup-' + uuid.uuid4().hex
                with sftp.open(backup, 'wx') as f:
                    sftp.chmod(backup, 0o600)
                    f.write(old)
                sftp.chmod(backup, stat.S_IMODE(info.st_mode))
                # Never remove the original as an unsafe fallback if the extension is unsupported.
                sftp.posix_rename(temp, path)
            else:
                sftp.rename(temp, path)  # Standard SFTP rename refuses an existing destination.
            return {'ok': True, 'path': path, 'sha256': sha(data), 'backup_path': backup}
        finally:
            try:
                sftp.remove(temp)
            except FileNotFoundError:
                pass


def transfer(session, fn):
    cancelled = threading.Event()
    def expire():
        cancelled.set()
        session.interrupt_transfers('mcp')
    timer = threading.Timer(300, expire)
    timer.daemon = True
    timer.start()
    try:
        fn(cancelled.is_set)
    finally:
        timer.cancel()
