"""Сохранение профилей серверов. Пароли шифруются через Windows DPAPI."""

from __future__ import annotations

import base64
import json
import os
import uuid
from pathlib import Path
from typing import Any

APP_DIR = Path(os.environ.get("APPDATA", Path.home())) / "ServerConnector"
STATE_FILE = APP_DIR / "servers.json"
PREFIX = "dpapi:"


def _protect(text: str) -> str:
    if not text:
        return ""
    try:
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [
                ("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char)),
            ]

        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        raw = text.encode("utf-8")
        blob_in = DATA_BLOB(len(raw), ctypes.create_string_buffer(raw))
        blob_out = DATA_BLOB()
        if not crypt32.CryptProtectData(
            ctypes.byref(blob_in),
            None,
            None,
            None,
            None,
            0,
            ctypes.byref(blob_out),
        ):
            return text
        try:
            data = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            kernel32.LocalFree(blob_out.pbData)
        return PREFIX + base64.b64encode(data).decode("ascii")
    except Exception:
        return text


def _unprotect(text: str) -> str:
    if not text:
        return ""
    if not text.startswith(PREFIX):
        return text
    try:
        import ctypes
        from ctypes import wintypes

        class DATA_BLOB(ctypes.Structure):
            _fields_ = [
                ("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char)),
            ]

        crypt32 = ctypes.windll.crypt32
        kernel32 = ctypes.windll.kernel32
        raw = base64.b64decode(text[len(PREFIX) :])
        blob_in = DATA_BLOB(len(raw), ctypes.create_string_buffer(raw))
        blob_out = DATA_BLOB()
        if not crypt32.CryptUnprotectData(
            ctypes.byref(blob_in),
            None,
            None,
            None,
            None,
            0,
            ctypes.byref(blob_out),
        ):
            return ""
        try:
            data = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            kernel32.LocalFree(blob_out.pbData)
        return data.decode("utf-8")
    except Exception:
        return ""


def default_server() -> dict[str, Any]:
    return {
        "id": uuid.uuid4().hex,
        "name": "Новый сервер",
        "host": "",
        "port": 22,
        "username": "",
        "password": "",
        "key_path": "",
        "proxy_jump": "",
        "sftp_server_command": "",
        "remote_path": "/",
        "local_path": str(Path.home()),
    }


class ServerStore:
    def __init__(self, path: Path = STATE_FILE) -> None:
        self.path = path
        self.servers: list[dict[str, Any]] = []
        self.last_server_id: str = ""
        self.window: str = ""
        self.show_local: bool = False
        self.load()

    def load(self) -> None:
        self.servers = []
        self.last_server_id = ""
        self.window = ""
        self.show_local = False
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        items = data.get("servers") or []
        for item in items:
            if not isinstance(item, dict):
                continue
            row = default_server()
            row.update({k: item.get(k, row[k]) for k in row})
            row["password"] = _unprotect(str(row.get("password") or ""))
            try:
                row["port"] = int(row.get("port") or 22)
            except (TypeError, ValueError):
                row["port"] = 22
            if not row.get("id"):
                row["id"] = uuid.uuid4().hex
            self.servers.append(row)
        self.last_server_id = str(data.get("last_server_id") or "")
        self.window = str(data.get("window") or "")
        if "show_local" in data:
            self.show_local = bool(data.get("show_local"))

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "last_server_id": self.last_server_id,
            "window": self.window,
            "show_local": self.show_local,
            "servers": [
                {
                    **{k: v for k, v in s.items() if k != "password"},
                    "password": _protect(str(s.get("password") or "")),
                }
                for s in self.servers
            ],
        }
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def get(self, server_id: str) -> dict[str, Any] | None:
        for s in self.servers:
            if s["id"] == server_id:
                return s
        return None

    def upsert(self, server: dict[str, Any]) -> dict[str, Any]:
        if not server.get("id"):
            server["id"] = uuid.uuid4().hex
        existing = self.get(server["id"])
        if existing is None:
            self.servers.append(server)
        else:
            existing.update(server)
            server = existing
        self.save()
        return server

    def remove(self, server_id: str) -> None:
        self.servers = [s for s in self.servers if s["id"] != server_id]
        if self.last_server_id == server_id:
            self.last_server_id = self.servers[0]["id"] if self.servers else ""
        self.save()
