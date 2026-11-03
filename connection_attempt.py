"""Cancellable SSH connection attempts; only the Tk thread delivers results."""

from __future__ import annotations

import queue
import threading
import time
from typing import Callable

from sftp_client import SftpSession


def close_in_background(session: SftpSession) -> None:
    # Closing may wait for another thread holding the session's IO lock.
    def close() -> None:
        try:
            session.close()
        except Exception:
            pass

    threading.Thread(target=close, daemon=True).start()


class ConnectionAttempt:
    def __init__(
        self,
        owner,
        server: dict,
        on_success: Callable,
        on_error: Callable[[str], None],
        *,
        load_directory: bool = False,
        timeout: float = 30,
        format_error: Callable[[Exception], str] = str,
    ) -> None:
        self.owner = owner
        self.server = dict(server)
        self.session = SftpSession()
        self.on_success = on_success
        self.on_error = on_error
        self.load_directory = load_directory
        self.timeout = timeout
        self.format_error = format_error
        self.directory_error: str | None = None
        self.cancelled = threading.Event()
        self.results: queue.Queue = queue.Queue()
        self.finished = False
        self._after = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self.deadline = time.monotonic() + self.timeout
        self._after = self.owner.after(100, self._poll)
        self._thread.start()

    def _run(self) -> None:
        succeeded = False
        try:
            if self.cancelled.is_set():
                return
            data = self.server
            home = self.session.connect(
                data["host"],
                int(data.get("port") or 22),
                data["username"],
                data.get("password") or "",
                data.get("key_path") or "",
                timeout=min(20, self.timeout),
                proxy_jump=data.get("proxy_jump") or "",
                sftp_server_command=data.get("sftp_server_command") or "",
            )
            if self.cancelled.is_set():
                return
            entries = None
            if self.load_directory:
                path = data.get("remote_path")
                try:
                    entries = self.session.listdir(home if not path or path == "/" else path)
                except (FileNotFoundError, PermissionError, NotADirectoryError) as exc:
                    # The SSH session is usable even if the saved folder is not.
                    # Let the user navigate home instead of forcing a reconnect.
                    entries = []
                    self.directory_error = self.format_error(exc)
            self.results.put((home, entries, None))
            succeeded = True
        except Exception as exc:
            # Materialize the message here: Python clears 'exc' after except.
            self.results.put((None, None, self.format_error(exc)))
        finally:
            if not succeeded or self.cancelled.is_set():
                try:
                    self.session.close()
                except Exception:
                    pass

    def _poll(self) -> None:
        self._after = None
        if self.finished:
            return
        try:
            home, entries, error = self.results.get_nowait()
        except queue.Empty:
            if time.monotonic() >= self.deadline:
                self.cancel()
                self.on_error(
                    f"Сервер не ответил за {self.timeout:g} с. "
                    "Проверьте адрес и параметры подключения и повторите попытку."
                )
            else:
                self._after = self.owner.after(100, self._poll)
            return
        self.finished = True
        if error is not None:
            self.on_error(error)
        else:
            self.on_success(self.session, home, entries)

    def cancel(self) -> None:
        if self.finished:
            return
        self.finished = True
        self.cancelled.set()
        if self._after is not None:
            self.owner.after_cancel(self._after)
            self._after = None
        close_in_background(self.session)
