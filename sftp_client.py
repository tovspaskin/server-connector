"""Потокобезопасная обёртка над Paramiko SFTP."""

from __future__ import annotations

import errno
import hashlib
import os
import socket
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable

ProgressFn = Callable[[int, int], None]
StatusFn = Callable[[str], None]
CHUNK = 256 * 1024
PART_SUFFIX = ".serverconnector.part"
META_SUFFIX = ".meta"
BACKUP_SUFFIX = ".serverconnector.backup"
MAX_REMOTE_TEXT_BYTES = 1024 * 1024
DEFAULT_RECONNECT_ATTEMPTS = 12
DEFAULT_RECONNECT_BASE_DELAY = 1.0
DEFAULT_RECONNECT_MAX_DELAY = 15.0


class TransferCancelled(RuntimeError):
    """Raised when the user cancels a running or recovering transfer."""


class ReconnectUnavailable(RuntimeError):
    """Raised when reconnect cannot run because the session was explicitly closed."""


@dataclass
class FileEntry:
    name: str
    path: str
    is_dir: bool
    size: int
    mtime: float
    mode: int = 0

    @property
    def display_size(self) -> str:
        if self.is_dir:
            return ""
        return human_size(self.size)

    @property
    def display_mtime(self) -> str:
        if not self.mtime:
            return ""
        try:
            return datetime.fromtimestamp(self.mtime).strftime("%Y-%m-%d %H:%M")
        except (OSError, OverflowError, ValueError):
            return ""


@dataclass(frozen=True)
class RemoteTextDocument:
    """A UTF-8 remote file together with the version used for safe saving."""

    text: str
    sha256: str
    mode: int
    size: int

    @property
    def version(self) -> str:
        return self.sha256


@dataclass(frozen=True)
class RemoteTextSaveResult:
    """Outcome of an optimistic remote text save."""

    saved: bool
    sha256: str
    actual_sha256: str
    mode: int | None

    @property
    def conflict(self) -> bool:
        return not self.saved


def human_size(n: float | int | None) -> str:
    if n is None:
        return "?"
    value = float(n)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if abs(value) < 1024:
            return f"{int(value)} {unit}" if unit == "Б" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} ПБ"


def posix_join(base: str, name: str) -> str:
    base = (base or "/").replace("\\", "/")
    name = name.replace("\\", "/").lstrip("/")
    if not base.endswith("/"):
        base += "/"
    if base == "//":
        base = "/"
    path = base + name if name else base.rstrip("/") or "/"
    return path if path.startswith("/") else "/" + path


def posix_parent(path: str) -> str:
    path = (path or "/").replace("\\", "/").rstrip("/")
    if not path or path == "/":
        return "/"
    parent = path.rsplit("/", 1)[0]
    return parent or "/"


class SftpSession:
    def __init__(
        self,
        reconnect_attempts: int = DEFAULT_RECONNECT_ATTEMPTS,
        reconnect_base_delay: float = DEFAULT_RECONNECT_BASE_DELAY,
        reconnect_max_delay: float = DEFAULT_RECONNECT_MAX_DELAY,
        keepalive_interval: int = 15,
        io_timeout: float = 60,
    ) -> None:
        self._lock = threading.RLock()
        self._text_save_lock = threading.Lock()
        self._transport = None
        self._jump_client = None
        self._sftp = None
        self._active_channels: dict[str, set[object]] = {}
        self._pending_clients: dict[object, int] = {}
        self._connect_args: dict[str, object] | None = None
        self._generation = 0
        self.reconnect_attempts = max(0, int(reconnect_attempts))
        self.reconnect_base_delay = max(0.0, float(reconnect_base_delay))
        self.reconnect_max_delay = max(0.0, float(reconnect_max_delay))
        self.keepalive_interval = max(0, int(keepalive_interval))
        self.io_timeout = max(1.0, float(io_timeout))
        self.connected = False
        self.label = ""
        self.home = "/"

    def connect(
        self,
        host: str,
        port: int,
        username: str,
        password: str = "",
        key_path: str = "",
        timeout: float = 20,
        *,
        proxy_jump: str = "",
        sftp_server_command: str = "",
    ) -> str:
        host = (host or "").strip()
        username = (username or "").strip()
        if not host:
            raise RuntimeError("Не указан хост")
        if not username:
            raise RuntimeError("Не указан пользователь")
        config: dict[str, object] = {
            "host": host,
            "port": int(port or 22),
            "username": username,
            "password": password or "",
            "key_path": (key_path or "").strip(),
            "proxy_jump": (proxy_jump or "").strip(),
            "sftp_server_command": (sftp_server_command or "").strip(),
            "timeout": float(timeout),
        }
        with self._lock:
            self._generation += 1
            generation = self._generation
        client, sftp, home, jump_client = self._open_connection(config, generation=generation)
        with self._lock:
            if generation != self._generation:
                self._close_pair(client, sftp, jump_client)
                raise TransferCancelled("Подключение отменено")
            self._close_channels_locked()
            self._set_connection_locked(client, sftp, config, home, jump_client)
            return self.home

    def _open_connection(
        self, config: dict[str, object], generation: int | None = None
    ):
        try:
            import paramiko
        except ImportError as exc:
            raise RuntimeError(
                "Не установлен paramiko. Запустите run.bat или:\npython -m pip install paramiko"
            ) from exc

        client = paramiko.SSHClient()
        jump_client = None
        sftp = None
        try:
            with self._lock:
                if generation is None:
                    generation = self._generation
                self._check_connect_generation(generation)
                self._pending_clients[client] = generation
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            pkey = None
            key_file = str(config.get("key_path") or "")
            password = str(config.get("password") or "")
            if key_file:
                pkey = self._load_key(key_file, password)
            self._check_connect_generation(generation)
            connect_options = dict(
                password=password or None,
                pkey=pkey,
                timeout=float(config["timeout"]),
                auth_timeout=float(config["timeout"]),
                banner_timeout=float(config["timeout"]),
                channel_timeout=self.io_timeout,
                allow_agent=not pkey and not password,
                look_for_keys=not pkey and not password,
            )
            sock = None
            proxy_jump = str(config.get("proxy_jump") or "")
            if proxy_jump:
                jump_host, jump_port, jump_username = self._parse_proxy_jump(
                    proxy_jump, str(config["username"])
                )
                jump_client = paramiko.SSHClient()
                with self._lock:
                    self._check_connect_generation(generation)
                    self._pending_clients[jump_client] = generation
                jump_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                jump_client.connect(
                    hostname=jump_host,
                    port=jump_port,
                    username=jump_username,
                    **connect_options,
                )
                self._check_connect_generation(generation)
                jump_transport = jump_client.get_transport()
                if jump_transport is None or not jump_transport.is_active():
                    raise ConnectionError("Промежуточный SSH-сервер не отвечает")
                if self.keepalive_interval:
                    jump_transport.set_keepalive(self.keepalive_interval)
                sock = jump_transport.open_channel(
                    "direct-tcpip",
                    (str(config["host"]), int(config["port"])),
                    ("127.0.0.1", 0),
                    timeout=float(config["timeout"]),
                )
                if sock is None:
                    raise ConnectionError("Промежуточный сервер не открыл SSH-туннель")
                self._check_connect_generation(generation)
            client.connect(
                hostname=str(config["host"]),
                port=int(config["port"]),
                username=str(config["username"]),
                **connect_options,
                **({"sock": sock} if sock is not None else {}),
            )
            self._check_connect_generation(generation)
            transport = client.get_transport()
            if transport is not None and self.keepalive_interval:
                transport.set_keepalive(self.keepalive_interval)
            negotiation_timeout = min(
                self.io_timeout,
                max(1.0, float(config.get("timeout") or self.io_timeout)),
            )
            sftp = self._open_primary_sftp(
                client,
                negotiation_timeout,
                str(config.get("sftp_server_command") or ""),
            )
            self._check_connect_generation(generation)
            self._configure_sftp_timeout(sftp)
            try:
                home = sftp.normalize(".")
            except Exception:
                home = "/"
            self._check_connect_generation(generation)
            return client, sftp, home, jump_client
        except Exception:
            self._close_pair(client, sftp, jump_client)
            self._check_connect_generation(generation)
            raise
        finally:
            with self._lock:
                self._pending_clients.pop(client, None)
                if jump_client is not None:
                    self._pending_clients.pop(jump_client, None)

    @staticmethod
    def _parse_proxy_jump(value: str, default_username: str) -> tuple[str, int, str]:
        """Accept a jump host as ``host``, ``user@host`` or ``user@host:port``."""
        username, separator, address = value.strip().rpartition("@")
        if not separator:
            username, address = default_username, value.strip()
        host, port = address, 22
        if ":" in address:
            host, separator, raw_port = address.rpartition(":")
            if not separator or not raw_port.isdecimal():
                raise ValueError("Неверный порт промежуточного сервера")
            port = int(raw_port)
        if not host or not username or not 1 <= port <= 65535:
            raise ValueError("Неверный адрес промежуточного сервера")
        return host, port, username

    def _check_connect_generation(self, generation: int | None) -> None:
        with self._lock:
            if generation != self._generation:
                raise TransferCancelled("Подключение отменено")

    def _configure_sftp_timeout(self, sftp) -> None:
        try:
            channel = sftp.get_channel()
            channel.settimeout(self.io_timeout)
        except (AttributeError, OSError):
            pass

    @staticmethod
    def _open_primary_sftp(client, timeout: float, server_command: str = ""):
        """Bound Paramiko's otherwise unbounded SFTP subsystem negotiation."""
        timed_out = threading.Event()
        channel = None

        def expire() -> None:
            timed_out.set()
            try:
                client.close()
            except Exception:
                pass

        timer = threading.Timer(max(1.0, float(timeout)), expire)
        timer.daemon = True
        timer.start()
        try:
            if server_command:
                import paramiko

                transport = client.get_transport()
                if transport is None or not transport.is_active():
                    raise ConnectionError("SSH-соединение потеряно до запуска SFTP")
                channel = transport.open_session(timeout=timeout)
                channel.settimeout(timeout)
                channel.exec_command(server_command)
                sftp = paramiko.SFTPClient(channel)
            else:
                sftp = client.open_sftp()
            if timed_out.is_set():
                try:
                    sftp.close()
                except Exception:
                    pass
                raise TimeoutError("Сервер не ответил при открытии SFTP-канала")
            return sftp
        except Exception as exc:
            if channel is not None:
                try:
                    channel.close()
                except Exception:
                    pass
            if timed_out.is_set() and not isinstance(exc, TimeoutError):
                raise TimeoutError("Сервер не ответил при открытии SFTP-канала") from exc
            raise
        finally:
            timer.cancel()

    @staticmethod
    def _close_pair(client, sftp, jump_client=None) -> None:
        for obj in (sftp, client, jump_client):
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass

    def _set_connection_locked(
        self,
        client,
        sftp,
        config: dict[str, object],
        home: str,
        jump_client=None,
    ) -> None:
        self._transport = client
        self._jump_client = jump_client
        self._sftp = sftp
        self._connect_args = dict(config)
        self.connected = True
        self.label = f"{config['username']}@{config['host']}:{config['port']}"
        self.home = home or "/"

    def reconnect(self) -> str:
        """Reconnect with the last successful in-memory credentials."""
        with self._lock:
            if self._connect_args is None:
                raise ReconnectUnavailable("Нет данных для автоматического переподключения")
            config = dict(self._connect_args)
            self._generation += 1
            generation = self._generation
            self._close_channels_locked()
        client, sftp, home, jump_client = self._open_connection(config, generation=generation)
        with self._lock:
            if generation != self._generation or self._connect_args is None:
                self._close_pair(client, sftp, jump_client)
                raise TransferCancelled("Переподключение отменено")
            self._set_connection_locked(client, sftp, config, home, jump_client)
            return self.home

    def _load_key(self, path: str, password: str):
        import paramiko

        loaders = (
            paramiko.Ed25519Key.from_private_key_file,
            paramiko.RSAKey.from_private_key_file,
            paramiko.ECDSAKey.from_private_key_file,
        )
        last_error: Exception | None = None
        for loader in loaders:
            try:
                return loader(path, password=password or None)
            except paramiko.PasswordRequiredException:
                if not password:
                    raise RuntimeError("Ключ защищён паролем — укажите пароль/фразу")
                last_error = RuntimeError("Неверная фраза к ключу")
            except Exception as exc:
                last_error = exc
        raise RuntimeError(f"Не удалось прочитать ключ: {last_error}")

    def close(self) -> None:
        with self._lock:
            self._generation += 1
            self._connect_args = None
            pending_clients = tuple(self._pending_clients)
            self._pending_clients.clear()
            self._close_channels_locked()
        # A client must be reachable while SSH/SFTP is still opening: Paramiko
        # can wait for subsystem negotiation before an SFTP timeout is set.
        # Close outside the state lock so an interrupted opener can unwind.
        for client in pending_clients:
            self._close_pair(client, None)

    def interrupt_transfers(self, operation_id: str = "") -> None:
        """Close channels for one job without forgetting the SSH session."""
        with self._lock:
            if operation_id:
                active = tuple(
                    self._active_channels.pop(self._channel_key(operation_id), set())
                )
            else:
                active = tuple(
                    channel
                    for channels in self._active_channels.values()
                    for channel in channels
                )
                self._active_channels.clear()
        for channel in active:
            try:
                channel.close()
            except Exception:
                pass

    def _close_channels_locked(self) -> None:
        active_channels = tuple(
            channel
            for channels in self._active_channels.values()
            for channel in channels
        )
        for active in active_channels:
            try:
                active.close()
            except Exception:
                pass
        self._active_channels.clear()
        for obj in (self._sftp, self._transport, self._jump_client):
            if obj is not None:
                try:
                    obj.close()
                except Exception:
                    pass
        self._sftp = None
        self._transport = None
        self._jump_client = None
        self.connected = False
        self.label = ""

    def _s(self):
        if self._sftp is None or not self.connected:
            raise RuntimeError("Нет подключения")
        client = self._transport
        get_transport = getattr(client, "get_transport", None)
        if callable(get_transport):
            transport = get_transport()
            if transport is None or not transport.is_active():
                self.connected = False
                raise ConnectionError("SSH-соединение потеряно")
        return self._sftp

    @staticmethod
    def _channel_key(operation_id: str = "") -> str:
        return operation_id or f"thread:{threading.get_ident()}"

    def _register_channel(self, operation_id: str, channel) -> str:
        key = self._channel_key(operation_id)
        with self._lock:
            self._active_channels.setdefault(key, set()).add(channel)
        return key

    def _unregister_channel(self, key: str, channel) -> None:
        with self._lock:
            channels = self._active_channels.get(key)
            if channels is None:
                return
            channels.discard(channel)
            if not channels:
                self._active_channels.pop(key, None)

    def _open_transfer_sftp(self, operation_id: str = ""):
        with self._lock:
            shared = self._s()
            client = self._transport
            transport = client.get_transport() if client is not None else None
            server_command = str(
                (self._connect_args or {}).get("sftp_server_command") or ""
            )
        open_session = getattr(transport, "open_session", None)
        if not callable(open_session):
            return shared, False, self._channel_key(operation_id)

        channel = open_session(timeout=self.io_timeout)
        if channel is None:
            raise ConnectionError("Сервер не открыл SFTP-канал")
        key = ""
        timer = None
        timed_out = threading.Event()
        try:
            settimeout = getattr(channel, "settimeout", None)
            if callable(settimeout):
                settimeout(self.io_timeout)
            with self._lock:
                if client is not self._transport or not self.connected:
                    raise ConnectionError("SSH-соединение изменилось во время передачи")
                key = self._channel_key(operation_id)
                self._active_channels.setdefault(key, set()).add(channel)

            # Paramiko's subsystem request waits on an unbounded Event.  Keep the
            # raw channel registered so cancellation can close it, and add a
            # watchdog for a server that accepts the channel but never replies.
            def expire() -> None:
                timed_out.set()
                try:
                    channel.close()
                except Exception:
                    pass

            timer = threading.Timer(self.io_timeout, expire)
            timer.daemon = True
            timer.start()
            if server_command:
                channel.exec_command(server_command)
            else:
                channel.invoke_subsystem("sftp")

            import paramiko

            transfer_sftp = paramiko.SFTPClient(channel)
            self._configure_sftp_timeout(transfer_sftp)
            with self._lock:
                channels = self._active_channels.get(key)
                if (
                    channels is None
                    or channel not in channels
                    or client is not self._transport
                    or not self.connected
                ):
                    try:
                        transfer_sftp.close()
                    except Exception:
                        pass
                    raise ConnectionError("SSH-соединение изменилось во время передачи")
                channels.discard(channel)
                channels.add(transfer_sftp)
            return transfer_sftp, True, key
        except Exception as exc:
            if key:
                self._unregister_channel(key, channel)
            try:
                channel.close()
            except Exception:
                pass
            if timed_out.is_set():
                raise TimeoutError("Сервер не ответил при открытии SFTP-канала") from exc
            raise
        finally:
            if timer is not None:
                timer.cancel()

    @contextmanager
    def _transfer_channel(self, operation_id: str = ""):
        sftp, owned, key = self._open_transfer_sftp(operation_id)
        try:
            yield sftp
        finally:
            if owned:
                self._unregister_channel(key, sftp)
                try:
                    sftp.close()
                except Exception:
                    pass

    def listdir(self, path: str) -> list[FileEntry]:
        with self._lock:
            sftp = self._s()
            path = path or "/"
            items: list[FileEntry] = []
            for attr in sftp.listdir_attr(path):
                name = attr.filename
                if name in (".", ".."):
                    continue
                mode = int(getattr(attr, "st_mode", 0) or 0)
                items.append(
                    FileEntry(
                        name=name,
                        path=posix_join(path, name),
                        is_dir=stat.S_ISDIR(mode),
                        size=int(getattr(attr, "st_size", 0) or 0),
                        mtime=float(getattr(attr, "st_mtime", 0) or 0),
                        mode=mode,
                    )
                )
            items.sort(key=lambda e: (not e.is_dir, e.name.lower()))
            return items

    def mkdir(self, path: str) -> None:
        with self._lock:
            self._s().mkdir(path)

    def rename(self, src: str, dst: str) -> None:
        with self._lock:
            self._s().rename(src, dst)

    def remove(self, path: str, is_dir: bool) -> None:
        with self._lock:
            sftp = self._s()
            if is_dir:
                self._rmtree(sftp, path)
            else:
                sftp.remove(path)

    def clear_upload_state(self, remote: str, operation_id: str) -> None:
        """Remove only the resumable sidecars owned by one completed job."""
        if not operation_id:
            raise ValueError("Для очистки состояния загрузки нужен идентификатор операции")
        token = self._operation_token(operation_id)
        partial = f"{remote}{PART_SUFFIX}.{token}"
        with self._lock:
            sftp = self._s()
            for path in (
                partial,
                partial + META_SUFFIX,
                f"{remote}{BACKUP_SUFFIX}.{token}",
            ):
                self._remove_remote_if_exists(sftp, path)

    def _rmtree(self, sftp, path: str) -> None:
        for attr in sftp.listdir_attr(path):
            name = attr.filename
            if name in (".", ".."):
                continue
            child = posix_join(path, name)
            if stat.S_ISDIR(int(getattr(attr, "st_mode", 0) or 0)):
                self._rmtree(sftp, child)
            else:
                sftp.remove(child)
        sftp.rmdir(path)

    def is_dir(self, path: str) -> bool:
        with self._lock:
            mode = int(getattr(self._s().stat(path), "st_mode", 0) or 0)
            return stat.S_ISDIR(mode)

    def write_file(self, path: str, data: bytes = b"") -> None:
        with self._lock:
            sftp = self._s()
            self._ensure_remote_dir(sftp, posix_parent(path))
            with sftp.open(path, "wb") as handle:
                if data:
                    handle.write(data)

    def walk_remote(self, root: str) -> list[tuple[str, str, bool]]:
        """Returns (abs_remote, rel_posix, is_dir) for root and everything under it."""
        root = (root or "/").replace("\\", "/")
        if not self.is_dir(root):
            return [(root, Path(root).name, False)]
        out: list[tuple[str, str, bool]] = []
        stack = [(root, "")]
        while stack:
            current, rel = stack.pop()
            for entry in self.listdir(current):
                child_rel = f"{rel}/{entry.name}" if rel else entry.name
                out.append((entry.path, child_rel, entry.is_dir))
                if entry.is_dir:
                    stack.append((entry.path, child_rel))
        return out

    def download_file(
        self,
        remote: str,
        local: str,
        progress: ProgressFn | None = None,
        cancel: Callable[[], bool] | None = None,
        status: StatusFn | None = None,
        operation_id: str = "",
    ) -> None:
        final_path = Path(local)
        operation_token = self._operation_token(operation_id)
        partial_path = Path(f"{local}{PART_SUFFIX}.{operation_token}")
        metadata_path = Path(str(partial_path) + META_SUFFIX)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        expected_identity: bytes | None = None
        trusted_prefix = 0
        recovery_budget = [self.reconnect_attempts]
        while True:
            self._raise_if_cancelled(cancel)
            try:
                with self._transfer_channel(operation_id) as sftp:
                    self._raise_if_cancelled(cancel)
                    rstat = sftp.stat(remote)
                    size = int(getattr(rstat, "st_size", 0) or 0)
                    remote_identity = self._download_identity(remote, rstat)
                    if expected_identity is None:
                        expected_identity = remote_identity
                    elif remote_identity != expected_identity:
                        raise RuntimeError("Файл на сервере изменился во время скачивания")

                    stored_identity = self._read_local_metadata(metadata_path)
                    has_partial_state = partial_path.exists() or metadata_path.exists()
                    if has_partial_state and stored_identity != remote_identity:
                        raise RuntimeError(
                            "Контрольная точка скачивания принадлежит другому файлу; "
                            "запустите новую операцию"
                        )
                    if not has_partial_state:
                        self._write_local_metadata(metadata_path, remote_identity)
                    start = partial_path.stat().st_size if partial_path.exists() else 0
                    if start > size:
                        partial_path.unlink(missing_ok=True)
                        start = 0
                    if start > trusted_prefix:
                        self._notify(status, "Проверка сохранённой части файла…")
                        if not self._partial_matches_remote(
                            sftp,
                            remote,
                            partial_path,
                            start,
                            cancel=cancel,
                        ):
                            partial_path.unlink(missing_ok=True)
                            start = 0
                            trusted_prefix = 0
                            self._notify(
                                status,
                                "Контрольная точка не совпала с сервером; "
                                "скачивание начато заново…",
                            )
                        else:
                            trusted_prefix = start
                    mode = "ab" if start else "wb"
                    if start:
                        self._notify(
                            status,
                            f"Продолжение скачивания с {start} из {size} байт…",
                        )
                    transferred = start
                    if progress:
                        progress(transferred, size)
                    needs_read = transferred < size or not partial_path.exists()
                    if needs_read:
                        with sftp.open(remote, "rb") as rf, open(partial_path, mode) as lf:
                            if start:
                                rf.seek(start)
                            while transferred < size:
                                self._raise_if_cancelled(cancel)
                                chunk = rf.read(min(CHUNK, size - transferred))
                                if not chunk:
                                    raise EOFError(
                                        "Соединение закрылось до завершения скачивания"
                                    )
                                lf.write(chunk)
                                transferred += len(chunk)
                                trusted_prefix = transferred
                                if progress:
                                    progress(transferred, size)
                    final_stat = sftp.stat(remote)
                    if self._download_identity(remote, final_stat) != expected_identity:
                        raise RuntimeError("Файл на сервере изменился во время скачивания")
                    os.replace(partial_path, final_path)
                    metadata_path.unlink(missing_ok=True)
                    if progress:
                        progress(size, size)
                return
            except TransferCancelled:
                raise
            except Exception as exc:
                if not self._is_connection_error(exc):
                    raise
                self._recover_connection(
                    exc, cancel=cancel, status=status, budget=recovery_budget
                )

    def upload_file(
        self,
        local: str,
        remote: str,
        progress: ProgressFn | None = None,
        cancel: Callable[[], bool] | None = None,
        status: StatusFn | None = None,
        resume_final: bool = False,
        operation_id: str = "",
    ) -> None:
        snapshot = os.stat(local)
        size = int(snapshot.st_size)
        source_mtime_ns = int(snapshot.st_mtime_ns)
        self._notify(status, "Проверка файла перед загрузкой…")
        source_identity = self._upload_identity(local, snapshot, cancel)
        operation_token = self._operation_token(operation_id)
        partial = f"{remote}{PART_SUFFIX}.{operation_token}"
        metadata = partial + META_SUFFIX
        backup = f"{remote}{BACKUP_SUFFIX}.{operation_token}"
        may_have_finalized = False
        recovery_budget = [self.reconnect_attempts]
        while True:
            self._raise_if_cancelled(cancel)
            try:
                with self._transfer_channel(operation_id) as sftp:
                    self._raise_if_cancelled(cancel)
                    self._ensure_remote_dir(sftp, posix_parent(remote))
                    partial_size = self._remote_size(sftp, partial)
                    stored_identity = self._read_remote_metadata(sftp, metadata)
                    can_resume_final = may_have_finalized or (
                        resume_final and stored_identity == source_identity
                    )
                    if partial_size is None and can_resume_final:
                        if self._remote_size(sftp, remote) == size:
                            self._remove_remote_if_exists(sftp, backup)
                            if not resume_final:
                                self._remove_remote_if_exists(sftp, metadata)
                            self._notify(status, "Файл уже полностью загружен…")
                            if progress:
                                progress(size, size)
                            return
                    has_partial_state = partial_size is not None or stored_identity is not None
                    if has_partial_state and stored_identity != source_identity:
                        raise RuntimeError(
                            "Контрольная точка загрузки принадлежит другому файлу; "
                            "запустите новую операцию"
                        )
                    if partial_size is not None and partial_size > size:
                        self._remove_remote_if_exists(sftp, partial)
                        partial_size = None
                    if not has_partial_state:
                        self._write_remote_metadata(sftp, metadata, source_identity)

                    if partial_size is None:
                        transferred = 0
                        remote_mode = "wb"
                    else:
                        transferred = partial_size
                        remote_mode = "r+b"

                    if transferred:
                        self._notify(
                            status,
                            f"Продолжение загрузки с {transferred} из {size} байт…",
                        )
                    if progress:
                        progress(transferred, size)

                    needs_write = (
                        transferred < size
                        or partial_size is None
                        or partial_size > size
                    )
                    if needs_write:
                        with open(local, "rb") as lf, sftp.open(partial, remote_mode) as rf:
                            if transferred:
                                lf.seek(transferred)
                                rf.seek(transferred)
                            while transferred < size:
                                self._raise_if_cancelled(cancel)
                                chunk = lf.read(min(CHUNK, size - transferred))
                                if not chunk:
                                    raise RuntimeError(
                                        "Локальный файл изменился во время загрузки"
                                    )
                                rf.write(chunk)
                                transferred += len(chunk)
                                if progress:
                                    progress(transferred, size)
                            flush = getattr(rf, "flush", None)
                            if callable(flush):
                                flush()

                    actual_size = self._remote_size(sftp, partial)
                    if actual_size != size:
                        raise EOFError(
                            f"Сервер подтвердил {actual_size or 0} из {size} байт"
                        )
                    current = os.stat(local)
                    if (
                        int(current.st_size) != size
                        or int(current.st_mtime_ns) != source_mtime_ns
                    ):
                        raise RuntimeError("Локальный файл изменился во время загрузки")
                    may_have_finalized = True
                    self._replace_remote(sftp, partial, remote, backup)
                    final_size = self._remote_size(sftp, remote)
                    if final_size != size:
                        raise EOFError(
                            "Итоговый файл на сервере имеет размер "
                            f"{final_size or 0} из {size} байт"
                        )
                    self._remove_remote_if_exists(sftp, backup)
                    if not resume_final:
                        self._remove_remote_if_exists(sftp, metadata)
                    if progress:
                        progress(size, size)
                return
            except TransferCancelled:
                raise
            except Exception as exc:
                if not self._is_connection_error(exc):
                    raise
                self._recover_connection(
                    exc, cancel=cancel, status=status, budget=recovery_budget
                )

    @staticmethod
    def _upload_identity(
        local: str,
        snapshot: os.stat_result,
        cancel: Callable[[], bool] | None = None,
    ) -> bytes:
        digest = hashlib.sha256()
        with open(local, "rb") as handle:
            while True:
                SftpSession._raise_if_cancelled(cancel)
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        current = os.stat(local)
        if (
            int(current.st_size) != int(snapshot.st_size)
            or int(current.st_mtime_ns) != int(snapshot.st_mtime_ns)
        ):
            raise RuntimeError("Локальный файл изменился во время проверки")
        return (
            f"serverconnector-upload-v1\n{int(snapshot.st_size)}\n{digest.hexdigest()}\n"
        ).encode("ascii")

    @staticmethod
    def _download_identity(remote: str, remote_stat) -> bytes:
        size = int(getattr(remote_stat, "st_size", 0) or 0)
        mtime = int(getattr(remote_stat, "st_mtime", 0) or 0)
        return f"serverconnector-download-v1\n{remote}\n{size}\n{mtime}\n".encode("utf-8")

    @staticmethod
    def _operation_token(operation_id: str = "") -> str:
        """Return a filesystem-safe per-job namespace; anonymous calls are one-shot."""
        value = (operation_id or uuid.uuid4().hex).encode("utf-8", errors="surrogatepass")
        return hashlib.sha256(value).hexdigest()[:32]

    @staticmethod
    def _partial_matches_remote(
        sftp,
        remote: str,
        local: Path,
        length: int,
        cancel: Callable[[], bool] | None = None,
    ) -> bool:
        """Compare the complete saved prefix before appending to an old download."""
        if length <= 0:
            return True
        with sftp.open(remote, "rb") as remote_handle, open(local, "rb") as local_handle:
            compared = 0
            while compared < length:
                SftpSession._raise_if_cancelled(cancel)
                wanted = min(CHUNK, length - compared)
                remote_data = remote_handle.read(wanted)
                if not remote_data:
                    return False
                local_data = local_handle.read(len(remote_data))
                if remote_data != local_data:
                    return False
                compared += len(remote_data)
        return True

    @staticmethod
    def _read_local_metadata(path: Path) -> bytes | None:
        try:
            return path.read_bytes()
        except FileNotFoundError:
            return None

    @staticmethod
    def _write_local_metadata(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f"{path.name}.tmp.{uuid.uuid4().hex}")
        try:
            temp_path.write_bytes(data)
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)

    @staticmethod
    def _read_remote_metadata(sftp, path: str) -> bytes | None:
        try:
            with sftp.open(path, "rb") as handle:
                return handle.read()
        except OSError as exc:
            if SftpSession._is_missing_error(exc):
                return None
            raise

    @staticmethod
    def _write_remote_metadata(sftp, path: str, data: bytes) -> None:
        temp_path = f"{path}.tmp.{uuid.uuid4().hex}"
        try:
            with sftp.open(temp_path, "wb") as handle:
                handle.write(data)
                flush = getattr(handle, "flush", None)
                if callable(flush):
                    flush()
            sftp.rename(temp_path, path)
        finally:
            try:
                SftpSession._remove_remote_if_exists(sftp, temp_path)
            except Exception:
                # A unique unfinished temp file is safer than masking the real
                # connection error or deleting an unverified path.
                pass

    @staticmethod
    def _remove_remote_if_exists(sftp, path: str) -> None:
        try:
            sftp.remove(path)
        except OSError as exc:
            if not SftpSession._is_missing_error(exc):
                raise

    @staticmethod
    def _remote_size(sftp, path: str) -> int | None:
        try:
            remote_stat = sftp.stat(path)
        except OSError as exc:
            if SftpSession._is_missing_error(exc):
                return None
            raise
        return int(getattr(remote_stat, "st_size", 0) or 0)

    @staticmethod
    def _is_missing_error(exc: OSError) -> bool:
        if getattr(exc, "errno", None) == errno.ENOENT:
            return True
        text = str(exc).lower()
        return "no such file" in text or "not found" in text

    @staticmethod
    def _replace_remote(
        sftp,
        source: str,
        destination: str,
        backup: str,
    ) -> None:
        posix_rename = getattr(sftp, "posix_rename", None)
        if callable(posix_rename):
            try:
                posix_rename(source, destination)
                return
            except Exception as exc:
                if SftpSession._is_connection_error(exc):
                    raise
                err_no = getattr(exc, "errno", None)
                text = str(exc).lower()
                unsupported = err_no in (errno.ENOSYS, errno.EOPNOTSUPP) or any(
                    marker in text
                    for marker in ("unsupported", "not supported", "operation unsupported")
                )
                if not unsupported:
                    raise
        destination_exists = SftpSession._remote_size(sftp, destination) is not None
        backup_exists = SftpSession._remote_size(sftp, backup) is not None
        if destination_exists and backup_exists:
            SftpSession._remove_remote_if_exists(sftp, backup)
            backup_exists = False
        if destination_exists:
            sftp.rename(destination, backup)
            backup_exists = True
        try:
            sftp.rename(source, destination)
        except Exception as exc:
            if SftpSession._is_connection_error(exc):
                raise
            if backup_exists:
                try:
                    if SftpSession._remote_size(sftp, destination) is None:
                        sftp.rename(backup, destination)
                    else:
                        SftpSession._remove_remote_if_exists(sftp, backup)
                except Exception as rollback_exc:
                    raise RuntimeError(
                        f"Не удалось заменить файл; резервная копия сохранена: {backup}"
                    ) from rollback_exc
            raise
        if backup_exists:
            SftpSession._remove_remote_if_exists(sftp, backup)

    @staticmethod
    def _notify(status: StatusFn | None, message: str) -> None:
        if status is None:
            return
        try:
            status(message)
        except Exception:
            pass

    @staticmethod
    def _raise_if_cancelled(cancel: Callable[[], bool] | None) -> None:
        if cancel is not None and cancel():
            raise TransferCancelled("Передача отменена")

    def _wait_for_retry(
        self,
        delay: float,
        cancel: Callable[[], bool] | None,
    ) -> None:
        deadline = time.monotonic() + max(0.0, delay)
        while True:
            self._raise_if_cancelled(cancel)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.1, remaining))

    def _recover_connection(
        self,
        cause: BaseException,
        cancel: Callable[[], bool] | None,
        status: StatusFn | None,
        budget: list[int] | None = None,
    ) -> None:
        self._raise_if_cancelled(cancel)
        last_error: BaseException = cause
        remaining = budget if budget is not None else [self.reconnect_attempts]
        while remaining[0] > 0:
            attempt = self.reconnect_attempts - remaining[0] + 1
            remaining[0] -= 1
            delay = self.reconnect_base_delay * (2 ** (attempt - 1))
            if self.reconnect_max_delay:
                delay = min(delay, self.reconnect_max_delay)
            self._notify(
                status,
                "Соединение потеряно. "
                f"Переподключение {attempt}/{self.reconnect_attempts} через {delay:g} с…",
            )
            self._wait_for_retry(delay, cancel)
            try:
                self.reconnect()
            except TransferCancelled:
                raise
            except ReconnectUnavailable:
                raise RuntimeError(
                    "Передача прервана: автоматическое переподключение недоступно"
                ) from cause
            except Exception as exc:
                last_error = exc
                continue
            self._notify(
                status,
                "Соединение восстановлено. Продолжаю с места обрыва…",
            )
            return
        attempts_used = self.reconnect_attempts - remaining[0]
        raise RuntimeError(
            f"Не удалось восстановить соединение после {attempts_used} попыток: "
            f"{last_error}"
        ) from cause

    @staticmethod
    def _is_connection_error(exc: BaseException) -> bool:
        current: BaseException | None = exc
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            if isinstance(
                current,
                (EOFError, TimeoutError, ConnectionError, socket.timeout, BrokenPipeError),
            ):
                return True
            if isinstance(current, OSError):
                network_errnos = {
                    errno.ECONNABORTED,
                    errno.ECONNREFUSED,
                    errno.ECONNRESET,
                    errno.EHOSTDOWN,
                    errno.EHOSTUNREACH,
                    errno.ENETDOWN,
                    errno.ENETRESET,
                    errno.ENETUNREACH,
                    errno.EPIPE,
                    errno.ETIMEDOUT,
                }
                if getattr(current, "errno", None) in network_errnos:
                    return True
            try:
                import paramiko

                retryable_ssh = (
                    paramiko.ssh_exception.NoValidConnectionsError,
                    paramiko.ssh_exception.ProxyCommandFailure,
                )
                if isinstance(current, retryable_ssh):
                    return True
            except (ImportError, AttributeError):
                pass
            text = str(current).lower()
            if any(
                marker in text
                for marker in (
                    "socket is closed",
                    "server connection dropped",
                    "connection reset",
                    "connection aborted",
                    "connection lost",
                    "connection closed",
                    "broken pipe",
                    "channel closed",
                    "unable to open channel",
                    "timeout opening channel",
                    "transport is not active",
                    "ssh session not active",
                    "eof in transport thread",
                    "error reading ssh protocol banner",
                    "not connected",
                    "timed out",
                    "нет подключения",
                    "соединение потеряно",
                )
            ):
                return True
            current = current.__cause__ or current.__context__
        return False

    def _ensure_remote_dir(self, sftp, path: str) -> None:
        path = (path or "/").replace("\\", "/")
        if path in ("", "/"):
            return
        parts = [p for p in path.split("/") if p]
        cur = ""
        for part in parts:
            cur += "/" + part
            try:
                sftp.stat(cur)
            except OSError:
                sftp.mkdir(cur)

    def ensure_dir(self, path: str) -> None:
        with self._lock:
            self._ensure_remote_dir(self._s(), path)

    def exists(self, path: str) -> bool:
        with self._lock:
            try:
                self._s().stat(path)
                return True
            except OSError:
                return False

    def read_remote_text(
        self,
        path: str,
        *,
        cancel: Callable[[], bool] | None = None,
        status: StatusFn | None = None,
        operation_id: str = "",
    ) -> RemoteTextDocument:
        """Read one regular UTF-8 file and return its content version.

        Connection failures are safe to retry because this operation is read-only.
        The returned SHA-256 must be passed to :meth:`save_remote_text`.
        """
        path = self._validate_remote_text_path(path)
        recovery_budget = [self.reconnect_attempts]
        while True:
            self._raise_if_cancelled(cancel)
            try:
                with self._transfer_channel(operation_id) as sftp:
                    data, info = self._read_remote_text_bytes(
                        sftp, path, cancel=cancel
                    )
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ValueError(
                        "Удалённый файл не является корректным UTF-8 текстом"
                    ) from exc
                return RemoteTextDocument(
                    text=text,
                    sha256=self._remote_text_sha256(data),
                    mode=stat.S_IMODE(int(getattr(info, "st_mode", 0) or 0)),
                    size=len(data),
                )
            except TransferCancelled:
                raise
            except Exception as exc:
                if not self._is_connection_error(exc):
                    raise
                self._recover_connection(
                    exc, cancel=cancel, status=status, budget=recovery_budget
                )

    def save_remote_text(
        self,
        path: str,
        text: str,
        expected_sha256: str,
        *,
        cancel: Callable[[], bool] | None = None,
        status: StatusFn | None = None,
        operation_id: str = "",
    ) -> RemoteTextSaveResult:
        """Atomically save UTF-8 text if the remote SHA-256 still matches.

        Existing destinations require the OpenSSH ``posix-rename`` extension;
        there is deliberately no remove/rename fallback.  A lost rename reply is
        resolved after reconnect by reading the destination before any retry.
        """
        path = self._validate_remote_text_path(path)
        if not isinstance(text, str):
            raise TypeError("Текст для сохранения должен быть строкой")
        try:
            data = text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ValueError("Текст нельзя закодировать как UTF-8") from exc
        if len(data) > MAX_REMOTE_TEXT_BYTES:
            raise ValueError("Текст превышает ограничение 1 МиБ")
        expected_sha256 = self._validate_remote_text_version(expected_sha256)
        new_sha256 = self._remote_text_sha256(data)
        recovery_budget = [self.reconnect_attempts]
        stale_temps: set[str] = set()
        outcome_unknown = False

        # Serialising editor saves closes the same-session race where two workers
        # both validate one version before either one replaces the destination.
        with self._text_save_lock:
            while True:
                # Once rename was dispatched its outcome must be reconciled even
                # when the caller cancels.  Otherwise a completed save could be
                # reported as cancelled, or a retry could overwrite a newer edit.
                if not outcome_unknown:
                    self._raise_if_cancelled(cancel)
                try:
                    with self._transfer_channel(operation_id) as sftp:
                        self._cleanup_remote_text_temps(sftp, stale_temps)

                        if outcome_unknown:
                            state = self._try_read_remote_text_bytes(
                                sftp, path, cancel=None
                            )
                            actual, current_mode = self._remote_text_state_version(state)
                            if actual == new_sha256:
                                return RemoteTextSaveResult(
                                    saved=True,
                                    sha256=new_sha256,
                                    actual_sha256=actual,
                                    mode=current_mode,
                                )
                            if actual != expected_sha256:
                                return RemoteTextSaveResult(
                                    saved=False,
                                    sha256=actual,
                                    actual_sha256=actual,
                                    mode=current_mode,
                                )
                            outcome_unknown = False
                            self._raise_if_cancelled(cancel)

                        state = self._try_read_remote_text_bytes(
                            sftp, path, cancel=cancel
                        )
                        actual, current_mode = self._remote_text_state_version(state)
                        if actual != expected_sha256:
                            return RemoteTextSaveResult(
                                saved=False,
                                sha256=actual,
                                actual_sha256=actual,
                                mode=current_mode,
                            )

                        old_info = state[1] if state is not None else None
                        destination_exists = state is not None
                        temp = f"{path}.serverconnector-edit.{uuid.uuid4().hex}.tmp"
                        stale_temps.add(temp)
                        try:
                            self._write_remote_text_temp(
                                sftp,
                                temp,
                                data,
                                old_info=old_info,
                                cancel=cancel,
                            )

                            # Re-read immediately before the atomic replace.  This
                            # catches edits made while the temporary file was sent.
                            state = self._try_read_remote_text_bytes(
                                sftp, path, cancel=cancel
                            )
                            actual, current_mode = self._remote_text_state_version(state)
                            if actual != expected_sha256:
                                return RemoteTextSaveResult(
                                    saved=False,
                                    sha256=actual,
                                    actual_sha256=actual,
                                    mode=current_mode,
                                )

                            self._raise_if_cancelled(cancel)
                            outcome_unknown = True
                            try:
                                self._commit_remote_text(
                                    sftp,
                                    temp,
                                    path,
                                    destination_exists=destination_exists,
                                )
                            except Exception as exc:
                                if destination_exists or self._is_connection_error(exc):
                                    raise

                                # Standard SFTP rename is non-overwriting.  If a
                                # file appeared after our missing-version check,
                                # report a conflict instead of turning the race
                                # into a generic save error.
                                raced_state = self._try_read_remote_text_bytes(
                                    sftp, path, cancel=cancel
                                )
                                raced_actual, raced_mode = (
                                    self._remote_text_state_version(raced_state)
                                )
                                if raced_actual == new_sha256:
                                    return RemoteTextSaveResult(
                                        saved=True,
                                        sha256=new_sha256,
                                        actual_sha256=raced_actual,
                                        mode=raced_mode,
                                    )
                                if raced_actual != expected_sha256:
                                    return RemoteTextSaveResult(
                                        saved=False,
                                        sha256=raced_actual,
                                        actual_sha256=raced_actual,
                                        mode=raced_mode,
                                    )
                                raise

                            stale_temps.discard(temp)
                            result_mode = (
                                stat.S_IMODE(
                                    int(getattr(old_info, "st_mode", 0) or 0)
                                )
                                if old_info is not None
                                else 0o600
                            )
                            return RemoteTextSaveResult(
                                saved=True,
                                sha256=new_sha256,
                                actual_sha256=new_sha256,
                                mode=result_mode,
                            )
                        finally:
                            try:
                                self._remove_remote_if_exists(sftp, temp)
                                stale_temps.discard(temp)
                            except Exception:
                                # A unique 0600 temporary file is harmless.  If
                                # the channel died, remove it after reconnect.
                                pass
                except TransferCancelled:
                    raise
                except Exception as exc:
                    if not self._is_connection_error(exc):
                        raise
                    self._recover_connection(
                        exc,
                        cancel=None if outcome_unknown else cancel,
                        status=status,
                        budget=recovery_budget,
                    )

    @staticmethod
    def _validate_remote_text_path(path: str) -> str:
        if (
            not isinstance(path, str)
            or not path.startswith("/")
            or "\x00" in path
        ):
            raise ValueError("Нужен абсолютный удалённый POSIX-путь без NUL")
        return path

    @staticmethod
    def _validate_remote_text_version(version: str) -> str:
        if not isinstance(version, str):
            raise TypeError("Версия файла должна быть SHA-256 или 'missing'")
        version = version.lower()
        if version == "missing":
            return version
        if len(version) != 64 or any(
            character not in "0123456789abcdef" for character in version
        ):
            raise ValueError("Версия файла должна быть SHA-256 или 'missing'")
        return version

    @staticmethod
    def _remote_text_sha256(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _remote_text_stat_identity(info) -> tuple[int, int, int, int, int]:
        return (
            int(getattr(info, "st_size", 0) or 0),
            int(getattr(info, "st_mtime", 0) or 0),
            int(getattr(info, "st_mode", 0) or 0),
            int(getattr(info, "st_uid", 0) or 0),
            int(getattr(info, "st_gid", 0) or 0),
        )

    @staticmethod
    def _validate_remote_text_info(info) -> None:
        mode = int(getattr(info, "st_mode", 0) or 0)
        size = int(getattr(info, "st_size", 0) or 0)
        if not stat.S_ISREG(mode):
            raise ValueError(
                "Ожидался обычный файл; каталоги и символические ссылки не открываются"
            )
        if size > MAX_REMOTE_TEXT_BYTES:
            raise ValueError("Удалённый файл превышает ограничение 1 МиБ")

    @classmethod
    def _read_remote_text_bytes(
        cls,
        sftp,
        path: str,
        *,
        cancel: Callable[[], bool] | None = None,
    ):
        lstat = getattr(sftp, "lstat", None)
        if not callable(lstat):
            raise RuntimeError(
                "SFTP-клиент не поддерживает lstat; безопасная проверка ссылок невозможна"
            )
        before = lstat(path)
        cls._validate_remote_text_info(before)
        chunks: list[bytes] = []
        total = 0
        with sftp.open(path, "rb") as handle:
            while total <= MAX_REMOTE_TEXT_BYTES:
                cls._raise_if_cancelled(cancel)
                chunk = handle.read(
                    min(CHUNK, MAX_REMOTE_TEXT_BYTES + 1 - total)
                )
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
        if total > MAX_REMOTE_TEXT_BYTES:
            raise ValueError("Удалённый файл превышает ограничение 1 МиБ")
        try:
            after = lstat(path)
        except OSError as exc:
            if cls._is_missing_error(exc):
                raise RuntimeError(
                    "Удалённый файл изменился во время чтения"
                ) from exc
            raise
        cls._validate_remote_text_info(after)
        if (
            cls._remote_text_stat_identity(before)
            != cls._remote_text_stat_identity(after)
            or total != int(getattr(after, "st_size", 0) or 0)
        ):
            raise RuntimeError("Удалённый файл изменился во время чтения")
        return b"".join(chunks), after

    @classmethod
    def _try_read_remote_text_bytes(
        cls,
        sftp,
        path: str,
        *,
        cancel: Callable[[], bool] | None = None,
    ):
        try:
            return cls._read_remote_text_bytes(sftp, path, cancel=cancel)
        except OSError as exc:
            if cls._is_missing_error(exc):
                return None
            raise

    @staticmethod
    def _remote_text_state_version(state) -> tuple[str, int | None]:
        if state is None:
            return "missing", None
        data, info = state
        return (
            SftpSession._remote_text_sha256(data),
            stat.S_IMODE(int(getattr(info, "st_mode", 0) or 0)),
        )

    @classmethod
    def _write_remote_text_temp(
        cls,
        sftp,
        temp: str,
        data: bytes,
        *,
        old_info,
        cancel: Callable[[], bool] | None = None,
    ) -> None:
        with sftp.open(temp, "wx") as handle:
            # Restrict permissions before any user content is written.
            sftp.chmod(temp, 0o600)
            offset = 0
            while offset < len(data):
                cls._raise_if_cancelled(cancel)
                chunk = data[offset : offset + CHUNK]
                handle.write(chunk)
                offset += len(chunk)
            flush = getattr(handle, "flush", None)
            if callable(flush):
                flush()

        if old_info is None:
            return
        old_uid = getattr(old_info, "st_uid", None)
        old_gid = getattr(old_info, "st_gid", None)
        chown = getattr(sftp, "chown", None)
        if old_uid is not None and old_gid is not None and callable(chown):
            temp_info = sftp.stat(temp)
            if (
                getattr(temp_info, "st_uid", None),
                getattr(temp_info, "st_gid", None),
            ) != (old_uid, old_gid):
                chown(temp, int(old_uid), int(old_gid))
        # chown may clear setuid/setgid bits, so restore the complete mode last.
        sftp.chmod(
            temp, stat.S_IMODE(int(getattr(old_info, "st_mode", 0) or 0))
        )

    @staticmethod
    def _commit_remote_text(
        sftp,
        temp: str,
        path: str,
        *,
        destination_exists: bool,
    ) -> None:
        if not destination_exists:
            # SFTP v3 rename is atomic and must refuse an existing destination.
            sftp.rename(temp, path)
            return

        posix_rename = getattr(sftp, "posix_rename", None)
        if not callable(posix_rename):
            raise RuntimeError(
                "Сервер не поддерживает атомарную замену posix-rename; файл не изменён"
            )
        try:
            posix_rename(temp, path)
        except Exception as exc:
            if SftpSession._is_connection_error(exc):
                raise
            err_no = getattr(exc, "errno", None)
            message = str(exc).lower()
            if err_no in (errno.ENOSYS, errno.EOPNOTSUPP) or any(
                marker in message
                for marker in (
                    "unsupported",
                    "not supported",
                    "operation unsupported",
                )
            ):
                raise RuntimeError(
                    "Сервер не поддерживает атомарную замену posix-rename; файл не изменён"
                ) from exc
            raise

    @staticmethod
    def _cleanup_remote_text_temps(sftp, paths: set[str]) -> None:
        for temp in tuple(paths):
            SftpSession._remove_remote_if_exists(sftp, temp)
            paths.discard(temp)

    def read_file(self, path: str) -> bytes:
        with self._lock:
            with self._s().open(path, "rb") as handle:
                return handle.read()

    def chmod(self, path: str, mode: int) -> None:
        with self._lock:
            self._s().chmod(path, mode)

    def exec_command(
        self,
        command: str,
        timeout: float = 30,
        input_data: str | None = None,
        retry_on_disconnect: bool = False,
        status: StatusFn | None = None,
        cancel: Callable[[], bool] | None = None,
        operation_id: str = "",
    ) -> tuple[int, str, str]:
        recovery_budget = [self.reconnect_attempts]
        while True:
            self._raise_if_cancelled(cancel)
            channel = None
            try:
                with self._lock:
                    self._s()
                    if self._transport is None or not self.connected:
                        raise RuntimeError("Нет подключения")
                    client = self._transport
                    transport = client.get_transport()
                    if transport is None or not transport.is_active():
                        raise ConnectionError("SSH-соединение потеряно")
                # Opening a session does not dispatch the command yet, so this
                # stage is safe to retry without executing it twice.
                open_timeout = min(self.io_timeout, max(1.0, float(timeout)))
                channel = transport.open_session(timeout=open_timeout)
                with self._lock:
                    if client is not self._transport or not self.connected:
                        raise ConnectionError(
                            "SSH-соединение изменилось перед запуском команды"
                        )
                    channel_key = self._register_channel(operation_id, channel)
            except TransferCancelled:
                if channel is not None:
                    try:
                        channel.close()
                    except Exception:
                        pass
                raise
            except Exception as exc:
                if channel is not None:
                    try:
                        channel.close()
                    except Exception:
                        pass
                if not retry_on_disconnect or not self._is_connection_error(exc):
                    raise
                self._recover_connection(
                    exc,
                    cancel=cancel,
                    status=status,
                    budget=recovery_budget,
                )
                continue
            break

        try:
            self._raise_if_cancelled(cancel)
            channel.settimeout(timeout)
            # From this point the command may already be running. Never retry
            # failures below: a replay could duplicate non-idempotent actions.
            channel.exec_command(command)
            stdin = channel.makefile_stdin("wb", -1)
            stdout = channel.makefile("rb", -1)
            stderr = channel.makefile_stderr("rb", -1)
            if input_data is not None:
                stdin.write(input_data)
                try:
                    stdin.flush()
                except Exception:
                    pass
                try:
                    channel.shutdown_write()
                except Exception:
                    pass
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            code = channel.recv_exit_status()
            return code, out, err
        finally:
            self._unregister_channel(channel_key, channel)
            try:
                channel.close()
            except Exception:
                pass


def walk_local(root: str) -> Iterable[tuple[str, str, bool]]:
    """Yields (abs_path, rel_posix, is_dir)."""
    root_path = Path(root)
    if root_path.is_file():
        yield str(root_path), root_path.name, False
        return
    base = root_path
    for dirpath, dirnames, filenames in os.walk(root_path):
        rel_dir = Path(dirpath).relative_to(base).as_posix()
        if rel_dir != ".":
            yield dirpath, rel_dir, True
        for name in filenames:
            full = Path(dirpath) / name
            rel = full.relative_to(base).as_posix()
            yield str(full), rel, False


def local_entries(path: str) -> list[FileEntry]:
    items: list[FileEntry] = []
    try:
        names = os.listdir(path)
    except OSError:
        return items
    for name in names:
        full = os.path.join(path, name)
        try:
            st = os.stat(full)
        except OSError:
            continue
        items.append(
            FileEntry(
                name=name,
                path=full,
                is_dir=os.path.isdir(full),
                size=int(st.st_size),
                mtime=float(st.st_mtime),
                mode=int(st.st_mode),
            )
        )
    items.sort(key=lambda e: (not e.is_dir, e.name.lower()))
    return items
