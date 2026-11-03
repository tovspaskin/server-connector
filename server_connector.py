"""Server Connector — графический SFTP-клиент."""

from __future__ import annotations

import os
import queue
import shlex
import shutil
import tempfile
import threading
import time
import tkinter as tk
import uuid
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any, Callable

from clipboard_shortcuts import ClipboardMixin
from connection_attempt import ConnectionAttempt, close_in_background
from remote_tools import (
    COMMANDS_FILE,
    INSTALL_GIT,
    INSTALL_GIT_SUDO,
    extract_pubkey,
    is_archive,
    load_commands,
    local_pubkeys,
    looks_like_pubkey,
    pack_local_zip,
    remote_pack_command,
    remote_unpack_script,
    unpack_local,
)
from sftp_client import (
    FileEntry,
    SftpSession,
    TransferCancelled,
    human_size,
    local_entries,
    posix_join,
    posix_parent,
    walk_local,
)
from storage import ServerStore, default_server
from connector_bridge import ConnectorBridge

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    HAS_DND = True
except ImportError:
    DND_FILES = "DND_Files"
    TkinterDnD = None
    HAS_DND = False

BaseTk = TkinterDnD.Tk if HAS_DND else tk.Tk

APP_TITLE = "Server Connector"
ARCHIVE_THRESHOLD = 10
FONT = ("Segoe UI", 10)
FONT_SM = ("Segoe UI", 9)
FONT_B = ("Segoe UI", 10, "bold")
FONT_TITLE = ("Segoe UI Semibold", 14)

C = {
    "bg": "#16181d",
    "sidebar": "#1b1e25",
    "surface": "#22262e",
    "surface2": "#2a303a",
    "border": "#3a4150",
    "text": "#e8eaed",
    "muted": "#8b93a1",
    "accent": "#4c8bf5",
    "accent2": "#3b74d4",
    "success": "#3ecf8e",
    "danger": "#e85d6c",
    "warning": "#e6b450",
    "select": "#2c4a7c",
    "row": "#1c2028",
}


class HoverTip:
    def __init__(self, widget: tk.Misc, text: str) -> None:
        self.widget = widget
        self.text = (text or "").strip()
        self._after: str | None = None
        self._win: tk.Toplevel | None = None
        if self.text:
            widget.bind("<Enter>", self._schedule, add="+")
            widget.bind("<Leave>", self._hide, add="+")
            widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _event=None) -> None:
        self._cancel()
        self._after = self.widget.after(450, self._show)

    def _show(self) -> None:
        self._after = None
        if self._win is not None or not self.widget.winfo_exists():
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        win = tk.Toplevel(self.widget)
        win.wm_overrideredirect(True)
        win.wm_attributes("-topmost", True)
        win.configure(bg=C["border"])
        tk.Label(
            win,
            text=self.text,
            justify="left",
            wraplength=340,
            bg=C["surface2"],
            fg=C["text"],
            font=FONT_SM,
            padx=10,
            pady=7,
        ).pack(padx=1, pady=1)
        win.geometry(f"+{x}+{y}")
        self._win = win

    def _cancel(self) -> None:
        if self._after:
            try:
                self.widget.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None

    def _hide(self, _event=None) -> None:
        self._cancel()
        if self._win is not None:
            try:
                self._win.destroy()
            except tk.TclError:
                pass
            self._win = None


def pretty_error(exc: BaseException) -> str:
    text = str(exc) or exc.__class__.__name__
    low = text.lower()
    if "authentication" in low:
        return "Ошибка авторизации: проверьте логин, пароль или ключ"
    if "timed out" in low or "timeout" in low:
        return "Сервер не отвечает (таймаут)"
    if "getaddrinfo" in low or "name or service" in low or "11001" in text:
        return "Не удалось найти хост"
    if "connection refused" in low:
        return "Соединение отклонено. Проверьте хост и порт"
    if "no such file" in low:
        return "Файл или папка не найдены"
    if "permission denied" in low:
        return "Нет прав доступа"
    if "не установлен paramiko" in low:
        return text
    return text


def _is_inside_local(src: str, dest_dir: str) -> bool:
    try:
        src_abs = os.path.abspath(src)
        dest_abs = os.path.abspath(dest_dir)
        return os.path.commonpath([src_abs, dest_abs]) == src_abs
    except ValueError:
        return False


def _is_inside_remote(src: str, dest_dir: str) -> bool:
    src_n = (src or "/").rstrip("/") or "/"
    dest_n = (dest_dir or "/").rstrip("/") or "/"
    return dest_n == src_n or dest_n.startswith(src_n + "/")


def list_drives() -> list[FileEntry]:
    items: list[FileEntry] = []
    bitmask = 0
    try:
        bitmask = int(__import__("ctypes").windll.kernel32.GetLogicalDrives())
    except Exception:
        bitmask = 0
    if bitmask:
        for i in range(26):
            if bitmask & (1 << i):
                letter = chr(65 + i)
                path = f"{letter}:\\"
                items.append(
                    FileEntry(name=f"Диск {letter}:", path=path, is_dir=True, size=0, mtime=0)
                )
        return items
    home = str(Path.home())
    return [FileEntry(name=home, path=home, is_dir=True, size=0, mtime=0)]


def apply_theme(root: tk.Tk) -> None:
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    style.configure(".", background=C["bg"], foreground=C["text"], font=FONT, borderwidth=0)
    style.configure("TFrame", background=C["bg"])
    style.configure("Sidebar.TFrame", background=C["sidebar"])
    style.configure("Surface.TFrame", background=C["surface"])
    style.configure("Card.TFrame", background=C["surface2"])
    style.configure("TLabel", background=C["bg"], foreground=C["text"], font=FONT)
    style.configure("Sidebar.TLabel", background=C["sidebar"], foreground=C["text"])
    style.configure("Muted.TLabel", background=C["bg"], foreground=C["muted"], font=FONT_SM)
    style.configure("SurfaceMuted.TLabel", background=C["surface"], foreground=C["muted"], font=FONT_SM)
    style.configure("SidebarMuted.TLabel", background=C["sidebar"], foreground=C["muted"], font=FONT_SM)
    style.configure("Title.TLabel", background=C["bg"], foreground=C["text"], font=FONT_TITLE)
    style.configure("Section.TLabel", background=C["bg"], foreground=C["text"], font=FONT_B)
    style.configure("Status.TLabel", background=C["surface"], foreground=C["muted"], font=FONT_SM)
    style.configure("Ok.TLabel", background=C["bg"], foreground=C["success"], font=FONT_B)
    style.configure("Bad.TLabel", background=C["bg"], foreground=C["danger"], font=FONT_B)
    style.configure("PaneTitle.TLabel", background=C["surface"], foreground=C["muted"], font=FONT_B)

    style.configure(
        "TEntry",
        fieldbackground=C["surface2"],
        foreground=C["text"],
        insertcolor=C["text"],
        bordercolor=C["border"],
        lightcolor=C["border"],
        darkcolor=C["border"],
        padding=6,
    )
    style.map("TEntry", fieldbackground=[("focus", C["surface2"])], bordercolor=[("focus", C["accent"])])
    style.configure(
        "TCombobox",
        fieldbackground=C["surface2"],
        foreground=C["text"],
        background=C["surface2"],
        arrowcolor=C["text"],
        bordercolor=C["border"],
        padding=5,
    )
    style.configure(
        "TSpinbox",
        fieldbackground=C["surface2"],
        foreground=C["text"],
        background=C["surface2"],
        arrowcolor=C["text"],
        bordercolor=C["border"],
        padding=5,
    )

    style.configure(
        "TButton",
        background=C["surface2"],
        foreground=C["text"],
        bordercolor=C["border"],
        lightcolor=C["surface2"],
        darkcolor=C["surface2"],
        padding=(12, 7),
        font=FONT,
    )
    style.map(
        "TButton",
        background=[("active", C["border"]), ("pressed", C["select"])],
        foreground=[("disabled", C["muted"])],
    )
    style.configure(
        "Accent.TButton",
        background=C["accent"],
        foreground="#ffffff",
        bordercolor=C["accent2"],
        lightcolor=C["accent"],
        darkcolor=C["accent"],
        padding=(14, 7),
        font=FONT_B,
    )
    style.map(
        "Accent.TButton",
        background=[("active", C["accent2"]), ("disabled", C["border"])],
        foreground=[("disabled", C["muted"])],
    )
    style.configure(
        "Danger.TButton",
        background=C["surface2"],
        foreground=C["danger"],
        bordercolor=C["danger"],
        padding=(12, 7),
    )
    style.map("Danger.TButton", background=[("active", "#3a2226")])
    style.configure("Tool.TButton", padding=(8, 5), font=FONT_SM)
    style.configure("Chip.TButton", padding=(8, 4), font=FONT_SM)
    style.configure(
        "Transfer.TButton",
        padding=(10, 18),
        font=("Segoe UI", 12, "bold"),
        background=C["surface2"],
    )

    style.configure("TCheckbutton", background=C["bg"], foreground=C["text"], font=FONT)
    style.configure("TNotebook", background=C["bg"], borderwidth=0)
    style.configure(
        "TNotebook.Tab",
        background=C["surface"],
        foreground=C["muted"],
        padding=(16, 8),
        font=FONT,
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", C["surface2"])],
        foreground=[("selected", C["text"])],
    )
    style.configure(
        "Treeview",
        background=C["row"],
        fieldbackground=C["row"],
        foreground=C["text"],
        bordercolor=C["border"],
        rowheight=28,
        font=FONT,
    )
    style.configure(
        "Treeview.Heading",
        background=C["surface2"],
        foreground=C["muted"],
        relief="flat",
        font=FONT_SM,
        padding=6,
    )
    style.map(
        "Treeview",
        background=[("selected", C["select"])],
        foreground=[("selected", "#ffffff")],
    )
    style.map("Treeview.Heading", background=[("active", C["border"])])
    style.configure(
        "TProgressbar",
        background=C["accent"],
        troughcolor=C["surface2"],
        bordercolor=C["surface2"],
        lightcolor=C["accent"],
        darkcolor=C["accent"],
        thickness=8,
    )
    style.configure("TPanedwindow", background=C["bg"])
    style.configure("TSeparator", background=C["border"])
    style.configure("TScrollbar", background=C["surface2"], troughcolor=C["bg"], arrowcolor=C["muted"])

    root.option_add("*TCombobox*Listbox.background", C["surface2"])
    root.option_add("*TCombobox*Listbox.foreground", C["text"])
    root.option_add("*TCombobox*Listbox.selectBackground", C["select"])
    root.option_add("*Font", FONT)
    root.configure(bg=C["bg"])


class ServerDialog(tk.Toplevel, ClipboardMixin):
    def __init__(self, master: tk.Tk, server: dict[str, Any] | None = None) -> None:
        super().__init__(master)
        self.title("Сервер")
        self.configure(bg=C["bg"])
        self.resizable(False, False)
        self.transient(master)
        self.result: dict[str, Any] | None = None
        self.server = dict(server or default_server())
        self.show_pw = tk.BooleanVar(value=False)
        self._test_attempt: ConnectionAttempt | None = None

        body = ttk.Frame(self, padding=18)
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)

        fields = [
            ("name", "Имя"),
            ("host", "Хост"),
            ("port", "Порт"),
            ("username", "Пользователь"),
            ("password", "Пароль"),
            ("key_path", "Ключ SSH"),
            ("proxy_jump", "Промежуточный сервер SSH"),
            ("sftp_server_command", "Команда SFTP (необязательно)"),
            ("remote_path", "Папка на сервере"),
            ("local_path", "Локальная папка"),
        ]
        self.vars: dict[str, tk.Variable] = {}
        self.widgets: dict[str, ttk.Entry] = {}
        row = 0
        for key, label in fields:
            ttk.Label(body, text=label).grid(row=row, column=0, sticky="w", pady=5, padx=(0, 12))
            if key == "port":
                var = tk.IntVar(value=int(self.server.get("port") or 22))
                widget = ttk.Spinbox(body, from_=1, to=65535, textvariable=var, width=10)
            else:
                var = tk.StringVar(value=str(self.server.get(key) or ""))
                show = "*" if key == "password" else ""
                widget = ttk.Entry(body, textvariable=var, width=42, show=show)
            widget.grid(row=row, column=1, sticky="ew", pady=5)
            self.vars[key] = var
            self.widgets[key] = widget
            if key == "password":
                ttk.Checkbutton(
                    body, text="Показать", variable=self.show_pw, command=self._toggle_pw
                ).grid(row=row, column=2, padx=(8, 0))
            if key == "key_path":
                ttk.Button(body, text="…", width=3, command=self._browse_key, style="Tool.TButton").grid(
                    row=row, column=2, padx=(8, 0)
                )
            if key == "local_path":
                ttk.Button(body, text="…", width=3, command=self._browse_local, style="Tool.TButton").grid(
                    row=row, column=2, padx=(8, 0)
                )
            row += 1

        hint = ttk.Label(
            body,
            text="Промежуточный сервер: адрес или пользователь@адрес:порт. Пусто — прямое подключение.",
            style="Muted.TLabel",
        )
        hint.grid(row=row, column=0, columnspan=3, sticky="w", pady=(8, 12))
        row += 1

        btns = ttk.Frame(body)
        btns.grid(row=row, column=0, columnspan=3, sticky="ew")
        self.test_btn = ttk.Button(btns, text="Проверить", command=self._test)
        self.test_btn.pack(side="left")
        ttk.Button(btns, text="Отмена", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="Сохранить", style="Accent.TButton", command=self._ok).pack(
            side="right", padx=(0, 8)
        )

        self.setup_clipboard_shortcuts(*self.widgets.values())
        self.bind("<Return>", lambda _e: self._ok())
        self.bind("<Escape>", lambda _e: self.destroy())
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.update_idletasks()
        self.geometry(f"+{master.winfo_rootx() + 80}+{master.winfo_rooty() + 60}")
        self.grab_set()
        self.widgets["host"].focus_set()
        self.wait_window(self)

    def _toggle_pw(self) -> None:
        self.widgets["password"].configure(show="" if self.show_pw.get() else "*")

    def _browse_key(self) -> None:
        path = filedialog.askopenfilename(
            parent=self,
            title="Закрытый ключ SSH",
            filetypes=[("Ключи", "*"), ("Все файлы", "*.*")],
        )
        if path:
            self.vars["key_path"].set(path)

    def _browse_local(self) -> None:
        path = filedialog.askdirectory(parent=self, title="Локальная папка")
        if path:
            self.vars["local_path"].set(path)

    def _collect(self) -> dict[str, Any]:
        data = dict(self.server)
        data["name"] = str(self.vars["name"].get()).strip() or "Сервер"
        data["host"] = str(self.vars["host"].get()).strip()
        data["port"] = int(self.vars["port"].get() or 22)
        data["username"] = str(self.vars["username"].get()).strip()
        data["password"] = str(self.vars["password"].get())
        data["key_path"] = str(self.vars["key_path"].get()).strip()
        data["proxy_jump"] = str(self.vars["proxy_jump"].get()).strip()
        data["sftp_server_command"] = str(self.vars["sftp_server_command"].get()).strip()
        data["remote_path"] = str(self.vars["remote_path"].get()).strip() or "/"
        data["local_path"] = str(self.vars["local_path"].get()).strip() or str(Path.home())
        return data

    def _ok(self) -> None:
        data = self._collect()
        if not data["host"]:
            messagebox.showwarning("Сервер", "Укажите хост", parent=self)
            return
        if not data["username"]:
            messagebox.showwarning("Сервер", "Укажите пользователя", parent=self)
            return
        self.result = data
        self.destroy()

    def _test(self) -> None:
        if self._test_attempt is not None:
            self._test_attempt.cancel()
            self._test_attempt = None
            self.test_btn.configure(text="Проверить")
            return
        try:
            data = self._collect()
        except (ValueError, tk.TclError):
            messagebox.showwarning("Подключение", "Укажите корректный номер порта", parent=self)
            return
        self.test_btn.configure(text="Остановить проверку")
        self._test_attempt = ConnectionAttempt(
            self, data, self._test_ok, self._test_fail, format_error=pretty_error
        )
        self._test_attempt.start()

    def _test_ok(self, session: SftpSession, home: str, _entries) -> None:
        self._test_attempt = None
        self.test_btn.configure(text="Проверить")
        close_in_background(session)
        messagebox.showinfo("Подключение", f"Успешно.\nДомашняя папка: {home}", parent=self)

    def _test_fail(self, error: str) -> None:
        self._test_attempt = None
        self.test_btn.configure(text="Проверить")
        messagebox.showerror("Подключение", error, parent=self)

    def destroy(self) -> None:
        if self._test_attempt is not None:
            self._test_attempt.cancel()
            self._test_attempt = None
        super().destroy()


class AuthKeyDialog(tk.Toplevel, ClipboardMixin):
    def __init__(self, master: tk.Tk) -> None:
        super().__init__(master)
        self.title("Ключ авторизации")
        self.configure(bg=C["bg"])
        self.transient(master)
        self.result: str | None = None
        self.geometry("640x360")

        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Публичный ключ (строка ssh-ed25519 / ssh-rsa)").pack(anchor="w")
        self.text = tk.Text(
            body,
            height=10,
            wrap="word",
            bg=C["row"],
            fg=C["text"],
            insertbackground=C["text"],
            relief="flat",
            font=("Consolas", 10),
            padx=8,
            pady=8,
        )
        self.text.pack(fill="both", expand=True, pady=(8, 8))
        found = local_pubkeys()
        if found:
            try:
                self.text.insert("1.0", found[0].read_text(encoding="utf-8").strip() + "\n")
            except OSError:
                pass
            ttk.Label(body, text=f"Подставлен локальный ключ: {found[0]}", style="Muted.TLabel").pack(
                anchor="w"
            )

        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(10, 0))
        ttk.Button(btns, text="Из буфера", command=self._from_clip).pack(side="left")
        ttk.Button(btns, text="Из файла…", command=self._from_file).pack(side="left", padx=6)
        ttk.Button(btns, text="Отмена", command=self.destroy).pack(side="right")
        ttk.Button(btns, text="Добавить на сервер", style="Accent.TButton", command=self._ok).pack(
            side="right", padx=(0, 8)
        )
        self.setup_clipboard_shortcuts(self.text)
        self.bind("<Escape>", lambda _e: self.destroy())
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.update_idletasks()
        self.geometry(f"+{master.winfo_rootx() + 80}+{master.winfo_rooty() + 60}")
        self.grab_set()
        self.text.focus_set()
        self.wait_window(self)

    def _from_clip(self) -> None:
        try:
            data = self.clipboard_get()
        except tk.TclError:
            return
        self.text.delete("1.0", tk.END)
        self.text.insert("1.0", data.strip() + "\n")

    def _from_file(self) -> None:
        path = filedialog.askopenfilename(
            parent=self,
            title="Публичный ключ",
            initialdir=str(Path.home() / ".ssh"),
            filetypes=[("Публичные ключи", "*.pub"), ("Все файлы", "*.*")],
        )
        if not path:
            return
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            messagebox.showerror(APP_TITLE, pretty_error(exc), parent=self)
            return
        self.text.delete("1.0", tk.END)
        self.text.insert("1.0", text.strip() + "\n")

    def _ok(self) -> None:
        key = extract_pubkey(self.text.get("1.0", tk.END))
        if not looks_like_pubkey(key):
            messagebox.showwarning(
                APP_TITLE,
                "Это не похоже на публичный ключ.\nОжидается строка, начинающаяся с ssh-ed25519 или ssh-rsa.",
                parent=self,
            )
            return
        self.result = key
        self.destroy()


class RemoteTextEditor(tk.Toplevel, ClipboardMixin):
    """Small UTF-8 editor whose saves go directly to the captured server session."""

    def __init__(
        self,
        master: "App",
        session: SftpSession,
        server_id: str,
        entry: FileEntry,
        on_saved: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(master)
        # Tk may call destroy while a partially constructed Toplevel unwinds.
        self._closed = False
        self._busy = ""
        self._poll_after: str | None = None
        self._cancel_event: threading.Event | None = None
        self._operation_id = ""
        self.app = master
        self.session = session
        self.server_id = server_id
        self.connection_label = getattr(session, "label", "")
        self.entry = entry
        self.on_saved = on_saved
        self._document = None
        self.sha256 = ""
        self.baseline_text = ""
        self._dirty = False
        self._close_after_save = False
        self._results: queue.Queue = queue.Queue()

        self.title(f"Редактор — {entry.name}")
        self.geometry("900x640")
        self.minsize(620, 400)
        self.transient(master)
        self.configure(bg=C["bg"])

        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text=entry.path, style="Muted.TLabel").pack(fill="x", pady=(0, 8))

        text_frame = ttk.Frame(body, style="Surface.TFrame")
        text_frame.pack(fill="both", expand=True)
        self.text = tk.Text(
            text_frame,
            wrap="none",
            undo=True,
            maxundo=-1,
            state="disabled",
            bg=C["row"],
            fg=C["text"],
            insertbackground=C["text"],
            selectbackground=C["select"],
            relief="flat",
            font=("Consolas", 10),
            padx=10,
            pady=10,
        )
        sy = ttk.Scrollbar(text_frame, orient="vertical", command=self.text.yview)
        sx = ttk.Scrollbar(text_frame, orient="horizontal", command=self.text.xview)
        self.text.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        self.text.grid(row=0, column=0, sticky="nsew")
        sy.grid(row=0, column=1, sticky="ns")
        sx.grid(row=1, column=0, sticky="ew")
        text_frame.rowconfigure(0, weight=1)
        text_frame.columnconfigure(0, weight=1)

        footer = ttk.Frame(body)
        footer.pack(fill="x", pady=(10, 0))
        self.status_var = tk.StringVar(value="Загрузка с сервера…")
        ttk.Label(footer, textvariable=self.status_var, style="Muted.TLabel").pack(
            side="left", fill="x", expand=True
        )
        self.reload_btn = ttk.Button(
            footer, text="Перечитать", style="Tool.TButton", command=self.reload
        )
        close_btn = ttk.Button(footer, text="Закрыть", command=self.request_close)
        self.cancel_btn = ttk.Button(
            footer, text="Остановить", style="Tool.TButton", command=self.cancel_operation
        )
        self.save_btn = ttk.Button(
            footer, text="Сохранить", style="Accent.TButton", command=self.save
        )
        close_btn.pack(side="right", padx=(6, 0))
        self.save_btn.pack(side="right", padx=(6, 0))
        self.cancel_btn.pack(side="right", padx=(6, 0))
        self.reload_btn.pack(side="right", padx=(6, 0))

        self.setup_clipboard_shortcuts(self.text)
        self.text.bind("<<Modified>>", self._on_modified)
        self.bind("<Control-s>", self._save_shortcut)
        self.bind("<Control-S>", self._save_shortcut)
        self.bind("<KeyPress>", self._layout_safe_shortcut, add="+")
        self.bind("<Escape>", lambda _event: self.request_close())
        self.protocol("WM_DELETE_WINDOW", self.request_close)
        self._update_controls()
        self.after_idle(self._begin_load)

    @property
    def dirty(self) -> bool:
        return self._dirty

    @property
    def busy(self) -> bool:
        return bool(self._busy)

    def _same_connection(self) -> bool:
        return bool(
            self.session.connected
            and self.app.session is self.session
            and self.app.connected_server_id == self.server_id
        )

    def _adopt_reconnected_session(self) -> None:
        current = self.app.session
        if (
            current is not self.session
            and current.connected
            and self.app.connected_server_id == self.server_id
            and current.label == self.connection_label
        ):
            self.session = current

    def _start(
        self,
        kind: str,
        action: Callable[[Callable[[], bool], Callable[[str], None], str], Any],
    ) -> bool:
        if self._busy or self._closed:
            return False
        if getattr(self.app, "mcp_busy", False):
            self.status_var.set("Дождитесь завершения операции MCP")
            return False
        if (
            getattr(self.app, "busy", False) is True
            or getattr(self.app, "_command_running", False)
            or bool(getattr(self.app, "active_jobs", ()))
        ):
            self.status_var.set("Дождитесь завершения другой операции с сервером")
            return False
        self._busy = kind
        if kind == "load":
            self.text.configure(state="disabled")
        cancel_event = threading.Event()
        operation_id = f"editor-{uuid.uuid4().hex}"
        self._cancel_event = cancel_event
        self._operation_id = operation_id
        if hasattr(self.app, "active_jobs"):
            self.app.active_jobs.add(operation_id)
        self.status_var.set("Загрузка с сервера…" if kind == "load" else "Сохранение на сервере…")
        self._update_controls()
        self._poll_after = self.app.after(100, self._poll_result)

        def work() -> None:
            try:
                result = action(
                    cancel_event.is_set,
                    lambda message: self._results.put((kind, "status", message)),
                    operation_id,
                )
            except BaseException as exc:
                self._results.put((kind, "error", exc))
            else:
                self._results.put((kind, "success", result))

        threading.Thread(target=work, daemon=True).start()
        return True

    def _begin_load(self) -> None:
        if self._closed:
            return
        self._adopt_reconnected_session()
        if not self._same_connection():
            self.status_var.set("Подключение к серверу изменилось")
            self._update_controls()
            return
        self._start(
            "load",
            lambda cancel, status, operation_id: self.session.read_remote_text(
                self.entry.path,
                cancel=cancel,
                status=status,
                operation_id=operation_id,
            ),
        )

    def _finish_operation(self) -> None:
        if hasattr(self.app, "active_jobs") and self._operation_id:
            self.app.active_jobs.discard(self._operation_id)
        self._operation_id = ""
        self._cancel_event = None
        self._busy = ""

    def _poll_result(self) -> None:
        self._poll_after = None
        try:
            kind, outcome, payload = self._results.get_nowait()
        except queue.Empty:
            if self._busy:
                self._poll_after = self.app.after(100, self._poll_result)
            return
        if outcome == "status":
            if not self._closed:
                self.status_var.set(payload)
            self._poll_after = self.app.after(100, self._poll_result)
            return
        self._finish_operation()
        if self._closed:
            return
        if outcome == "error":
            if isinstance(payload, TransferCancelled):
                self._close_after_save = False
                if self._document is not None:
                    self.text.configure(state="normal")
                self.status_var.set("Операция остановлена")
                self._update_controls()
                return
            error = pretty_error(payload)
            self._close_after_save = False
            if self._document is not None:
                self.text.configure(state="normal")
            self.status_var.set(error)
            self._update_controls()
            messagebox.showerror("Редактор файла", error, parent=self)
            return
        if kind == "load":
            self._apply_loaded(payload)
        else:
            self._apply_saved(payload)

    def _apply_loaded(self, document: Any) -> None:
        self._document = document
        self.sha256 = document.sha256
        self.baseline_text = document.text
        self.text.configure(state="normal")
        self.text.delete("1.0", tk.END)
        self.text.insert("1.0", document.text)
        self.text.edit_reset()
        self.text.edit_modified(False)
        self._dirty = False
        self.status_var.set(f"Загружено: {document.size} байт · UTF-8 · Ctrl+S — сохранить")
        self._update_title()
        self._update_controls()
        self.text.focus_set()

    def save(self) -> bool:
        if self._busy or self._document is None:
            return False
        if not self._dirty:
            self.status_var.set("Изменений нет")
            return False
        self._adopt_reconnected_session()
        if not self._same_connection():
            messagebox.showerror(
                "Редактор файла",
                "Подключение изменилось. Подключитесь к исходному серверу и перечитайте файл.",
                parent=self,
            )
            self._close_after_save = False
            return False
        content = self.text.get("1.0", "end-1c")
        expected_sha256 = self.sha256
        self._saving_content = content
        started = self._start(
            "save",
            lambda cancel, status, operation_id: self.session.save_remote_text(
                self.entry.path,
                content,
                expected_sha256=expected_sha256,
                cancel=cancel,
                status=status,
                operation_id=operation_id,
            ),
        )
        if not started:
            self._close_after_save = False
        return started

    def _apply_saved(self, result: Any) -> None:
        if not result.saved:
            self._close_after_save = False
            self.status_var.set("Файл изменился на сервере — сохранение отменено")
            self._update_controls()
            messagebox.showerror(
                "Конфликт изменений",
                "Файл изменился на сервере после открытия. Ваш текст не перезаписан.\n\n"
                "Скопируйте нужные изменения или нажмите «Перечитать», чтобы загрузить новую версию.",
                parent=self,
            )
            return
        self.sha256 = result.sha256
        self.baseline_text = self._saving_content
        current = self.text.get("1.0", "end-1c")
        self._dirty = current != self._saving_content
        if not self._dirty:
            self.text.edit_modified(False)
        self.status_var.set(
            "Сохранено на сервере"
            if not self._dirty
            else "Версия сохранена; есть более новые несохранённые изменения"
        )
        self._update_title()
        self._update_controls()
        close_after_save = self._close_after_save
        self._close_after_save = False
        if self.on_saved is not None:
            self.on_saved()
        if close_after_save and not self._dirty:
            self.destroy()

    def reload(self) -> None:
        if self._busy:
            return
        if self._dirty and not messagebox.askyesno(
            "Перечитать файл",
            "Отменить несохранённые изменения и перечитать файл с сервера?",
            parent=self,
        ):
            return
        self._begin_load()

    def _on_modified(self, _event=None) -> None:
        if not self.text.edit_modified():
            return
        self.text.edit_modified(False)
        if self._document is None:
            return
        self._dirty = True
        self._update_title()
        self._update_controls()

    def _update_title(self) -> None:
        mark = " *" if self._dirty else ""
        self.title(f"Редактор — {self.entry.name}{mark}")

    def _update_controls(self) -> None:
        loaded = self._document is not None
        self.save_btn.configure(state="normal" if loaded and self._dirty and not self._busy else "disabled")
        self.reload_btn.configure(state="normal" if not self._busy else "disabled")
        self.cancel_btn.configure(state="normal" if self._busy else "disabled")

    def cancel_operation(self) -> None:
        if self._cancel_event is None or not self._operation_id:
            return
        self._cancel_event.set()
        self.status_var.set("Остановка операции…")
        self.cancel_btn.configure(state="disabled")
        operation_id = self._operation_id
        session = self.session

        def interrupt() -> None:
            try:
                session.interrupt_transfers(operation_id)
            except Exception:
                pass

        threading.Thread(target=interrupt, daemon=True).start()

    def _save_shortcut(self, _event=None) -> str:
        self.save()
        return "break"

    def _layout_safe_shortcut(self, event) -> str | None:
        if event.state & 0x4 and event.keycode == 83:
            return self._save_shortcut(event)
        return None

    def request_close(self) -> None:
        if self._busy == "save":
            messagebox.showinfo(
                "Редактор файла", "Дождитесь завершения сохранения на сервере.", parent=self
            )
            return
        if self._busy == "load" and self._dirty:
            messagebox.showinfo(
                "Редактор файла", "Дождитесь завершения перечитывания файла.", parent=self
            )
            return
        if self._dirty:
            answer = messagebox.askyesnocancel(
                "Несохранённые изменения",
                "Сохранить изменения на сервере перед закрытием?",
                parent=self,
            )
            if answer is None:
                return
            if answer:
                self._close_after_save = True
                if not self.save():
                    self._close_after_save = False
                return
        self.destroy()

    def destroy(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._cancel_event is not None:
            self._cancel_event.set()
            operation_id = self._operation_id
            session = self.session

            def interrupt() -> None:
                try:
                    session.interrupt_transfers(operation_id)
                except Exception:
                    pass

            if operation_id:
                threading.Thread(target=interrupt, daemon=True).start()
        if self._poll_after is not None and not self._busy:
            try:
                self.app.after_cancel(self._poll_after)
            except tk.TclError:
                pass
            self._poll_after = None
        super().destroy()


class FilePane(ttk.Frame):
    def __init__(self, master: tk.Misc, app: "App", kind: str, title: str) -> None:
        super().__init__(master, style="Surface.TFrame")
        self.app = app
        self.kind = kind
        self.path = str(Path.home()) if kind == "local" else "/"
        self._entries: dict[str, FileEntry] = {}
        self._sort = ("name", False)
        self._nav: list[str] = []

        head = ttk.Frame(self, style="Surface.TFrame")
        head.pack(fill="x", padx=8, pady=(8, 4))
        extra = "  ·  перетащите файлы сюда" if kind == "remote" else ""
        self.title_label = ttk.Label(head, text=title + extra, style="PaneTitle.TLabel")
        self.title_label.pack(side="left")
        self.count_var = tk.StringVar(value="")
        ttk.Label(head, textvariable=self.count_var, style="SurfaceMuted.TLabel").pack(side="right")

        nav = ttk.Frame(self, style="Surface.TFrame")
        nav.pack(fill="x", padx=8, pady=4)
        for text, cmd in (("↑", self.go_up), ("⌂", self.go_home), ("↻", self.refresh)):
            ttk.Button(nav, text=text, width=3, style="Tool.TButton", command=cmd).pack(side="left", padx=(0, 4))
        self.path_var = tk.StringVar(value=self.path)
        path_entry = ttk.Entry(nav, textvariable=self.path_var)
        path_entry.pack(side="left", fill="x", expand=True, padx=(4, 4))
        path_entry.bind("<Return>", lambda _e: self.go(self.path_var.get().strip()))
        ttk.Label(nav, text="фильтр", style="SurfaceMuted.TLabel").pack(side="left", padx=(8, 4))
        self.filter_var = tk.StringVar()
        filt = ttk.Entry(nav, textvariable=self.filter_var, width=16)
        filt.pack(side="left")
        self.filter_var.trace_add("write", lambda *_: self._apply_filter())
        self.path_entry = path_entry
        self.filter_entry = filt

        tree_wrap = ttk.Frame(self, style="Surface.TFrame")
        tree_wrap.pack(fill="both", expand=True, padx=8, pady=(4, 8))
        cols = ("size", "mtime")
        self.tree = ttk.Treeview(
            tree_wrap, columns=cols, show="tree headings", selectmode="extended"
        )
        self.tree.heading("#0", text="Имя", command=lambda: self.sort_by("name"))
        self.tree.heading("size", text="Размер", command=lambda: self.sort_by("size"))
        self.tree.heading("mtime", text="Изменён", command=lambda: self.sort_by("mtime"))
        self.tree.column("#0", width=280, stretch=True)
        self.tree.column("size", width=90, anchor="e", stretch=False)
        self.tree.column("mtime", width=130, anchor="center", stretch=False)
        sy = ttk.Scrollbar(tree_wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sy.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sy.pack(side="right", fill="y")
        self.tree.tag_configure("dir", foreground=C["accent"])
        self.tree.tag_configure("file", foreground=C["text"])
        self.tree.tag_configure("up", foreground=C["muted"])
        self.tree.tag_configure("hidden", foreground=C["muted"])
        self.tree.tag_configure("drop", background=C["select"])

        self.tree.bind("<Double-1>", self._on_open)
        self.tree.bind("<Return>", self._on_open)
        self.tree.bind("<BackSpace>", lambda _e: self.go_up())
        self.tree.bind("<Delete>", lambda _e: self.app.delete_selected(self.kind))
        self.tree.bind("<F2>", lambda _e: self.app.rename_selected(self.kind))
        self.tree.bind("<F5>", lambda _e: self.refresh())
        self.tree.bind("<Button-3>", self._menu)
        self.tree.bind("<ButtonPress-1>", self._on_press, add="+")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self.app.update_status())

        self.menu = tk.Menu(self, tearoff=0, bg=C["surface2"], fg=C["text"], activebackground=C["select"])

    def set_path(self, path: str, record: bool = True) -> None:
        if record and self.path and self.path != path:
            self._nav.append(self.path)
        self.path = path
        self.path_var.set(path)
        self.refresh()
        self.app.remember_paths()

    def go(self, path: str) -> None:
        path = (path or "").strip()
        if self.kind == "local":
            if not path or path.lower() in ("drives", "\\", "/"):
                self.set_path("")
                return
            resolved = os.path.abspath(os.path.expandvars(os.path.expanduser(path)))
            if not os.path.isdir(resolved):
                messagebox.showwarning(APP_TITLE, f"Папка не найдена:\n{resolved}", parent=self.app)
                self.path_var.set(self.path)
                return
            self.set_path(resolved)
            return
        if not self.app.session.connected:
            return
        self.set_path(path or "/")

    def go_up(self) -> None:
        if self.kind == "local":
            if not self.path:
                return
            parent = os.path.dirname(self.path.rstrip("\\/"))
            if not parent or parent == self.path:
                self.set_path("")
            else:
                self.set_path(parent)
            return
        self.set_path(posix_parent(self.path))

    def go_home(self) -> None:
        if self.kind == "local":
            self.set_path(str(Path.home()))
        elif self.app.session.connected:
            self.set_path(self.app.session.home or "/")

    def current_entries(self) -> list[FileEntry]:
        if self.kind == "local":
            if not self.path:
                return list_drives()
            return local_entries(self.path)
        if not self.app.session.connected:
            return []
        return self.app.session.listdir(self.path)

    def refresh(self) -> None:
        try:
            entries = self.current_entries()
        except Exception as exc:
            self.app.set_status(pretty_error(exc))
            messagebox.showerror(APP_TITLE, pretty_error(exc), parent=self.app)
            return
        self._all_entries = entries
        self._render(entries)
        self.app.update_status()

    def _apply_filter(self) -> None:
        entries = getattr(self, "_all_entries", [])
        self._render(entries)

    def _render(self, entries: list[FileEntry]) -> None:
        needle = (self.filter_var.get() or "").strip().lower()
        key, reverse = self._sort
        filtered = [e for e in entries if not needle or needle in e.name.lower()]

        def sort_key(e: FileEntry):
            if key == "size":
                return (not e.is_dir, e.size, e.name.lower())
            if key == "mtime":
                return (not e.is_dir, e.mtime, e.name.lower())
            return (not e.is_dir, e.name.lower())

        filtered.sort(key=sort_key, reverse=reverse)
        self.tree.delete(*self.tree.get_children())
        self._entries.clear()

        if self.path not in ("", "/") and not (self.kind == "remote" and not self.app.session.connected):
            self.tree.insert("", "end", iid="__up__", text="  ..", values=("", ""), tags=("up",))

        if self.kind == "remote" and not self.app.session.connected:
            self.tree.insert(
                "", "end", iid="__empty__", text="  Нет подключения", values=("", ""), tags=("up",)
            )
            self.count_var.set("")
            return

        for index, entry in enumerate(filtered):
            iid = f"i{index}"
            suffix = "/" if entry.is_dir else ""
            icon = "📁 " if entry.is_dir else "📄 "
            tags = ["dir" if entry.is_dir else "file"]
            if entry.name.startswith("."):
                tags.append("hidden")
            self.tree.insert(
                "",
                "end",
                iid=iid,
                text=f"  {icon}{entry.name}{suffix}",
                values=(entry.display_size, entry.display_mtime),
                tags=tuple(tags),
            )
            self._entries[iid] = entry

        folders = sum(1 for e in filtered if e.is_dir)
        files = len(filtered) - folders
        self.count_var.set(f"{folders} папок · {files} файлов")

    def sort_by(self, column: str) -> None:
        cur, rev = self._sort
        self._sort = (column, not rev if cur == column else False)
        self._render(getattr(self, "_all_entries", []))

    def selected(self) -> list[FileEntry]:
        out: list[FileEntry] = []
        for iid in self.tree.selection():
            if iid in self._entries:
                out.append(self._entries[iid])
        return out

    def selected_or_current(self) -> list[FileEntry]:
        items = self.selected()
        if items:
            return items
        if self.path:
            is_dir = True if self.kind == "local" else self.app.session.connected
            return [
                FileEntry(
                    name=os.path.basename(self.path.rstrip("\\/")) or self.path,
                    path=self.path,
                    is_dir=is_dir,
                    size=0,
                    mtime=0,
                )
            ]
        return []

    def _on_open(self, _event=None) -> None:
        self._open_selected()

    def _open_selected(self) -> None:
        focus = self.tree.focus()
        if focus == "__up__":
            self.go_up()
            return
        items = self.selected()
        if not items:
            return
        entry = items[0]
        if entry.is_dir:
            self.set_path(entry.path)
            return
        self.app.open_file(self.kind, entry)

    def _on_press(self, event) -> None:
        self.app.note_drag_press(self, event)

    def hit(self, x: int, y: int) -> FileEntry | None:
        row = self.tree.identify_row(y)
        if row == "__up__":
            if self.kind == "local":
                if not self.path:
                    return None
                parent = os.path.dirname(self.path.rstrip("\\/")) or ""
                return FileEntry(name="..", path=parent, is_dir=True, size=0, mtime=0)
            return FileEntry(name="..", path=posix_parent(self.path), is_dir=True, size=0, mtime=0)
        return self._entries.get(row)

    def clear_drop_highlight(self) -> None:
        for iid in self.tree.get_children():
            tags = [t for t in self.tree.item(iid, "tags") if t != "drop"]
            self.tree.item(iid, tags=tags)

    def highlight_drop(self, iid: str | None) -> None:
        self.clear_drop_highlight()
        if iid and self.tree.exists(iid):
            tags = list(self.tree.item(iid, "tags"))
            if "drop" not in tags:
                tags.append("drop")
            self.tree.item(iid, tags=tags)

    def _menu(self, event) -> None:
        row = self.tree.identify_row(event.y)
        if row and row not in self.tree.selection():
            self.tree.selection_set(row)
            self.tree.focus(row)
        items = self.selected()
        can_unpack = len(items) == 1 and not items[0].is_dir and is_archive(items[0].name)
        self.menu.delete(0, tk.END)
        if self.kind == "local":
            self.menu.add_command(label="Открыть", command=self._open_selected)
            self.menu.add_command(label="Загрузить на сервер →", command=lambda: self.app.transfer("upload"))
            self.menu.add_separator()
            self.menu.add_command(label="Новая папка", command=lambda: self.app.create_folder("local"))
            self.menu.add_command(label="Новый файл", command=lambda: self.app.create_file("local"))
            self.menu.add_command(label="Переименовать", command=lambda: self.app.rename_selected("local"))
            self.menu.add_command(label="Удалить", command=lambda: self.app.delete_selected("local"))
            if can_unpack:
                self.menu.add_command(
                    label="Распаковать архив", command=lambda: self.app.unpack_selected("local")
                )
            self.menu.add_separator()
            self.menu.add_command(label="Обновить", command=self.refresh)
            self.menu.add_command(label="Открыть в проводнике", command=self._reveal)
            self.menu.add_command(label="Копировать путь", command=self._copy_path)
        else:
            remote_open_label = (
                "Редактировать на сервере"
                if len(items) == 1 and not items[0].is_dir
                else "Открыть"
            )
            self.menu.add_command(label=remote_open_label, command=self._open_selected)
            self.menu.add_command(label="← Скачать", command=lambda: self.app.transfer("download"))
            self.menu.add_separator()
            self.menu.add_command(label="Новая папка", command=lambda: self.app.create_folder("remote"))
            self.menu.add_command(label="Новый файл", command=lambda: self.app.create_file("remote"))
            self.menu.add_command(label="Переименовать", command=lambda: self.app.rename_selected("remote"))
            self.menu.add_command(label="Удалить", command=lambda: self.app.delete_selected("remote"))
            if can_unpack:
                self.menu.add_command(
                    label="Распаковать архив", command=lambda: self.app.unpack_selected("remote")
                )
            self.menu.add_separator()
            self.menu.add_command(label="Обновить", command=self.refresh)
            self.menu.add_command(label="Копировать путь", command=self._copy_path)
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def _copy_path(self) -> None:
        items = self.selected()
        text = items[0].path if items else self.path
        if text:
            self.app.clipboard_clear()
            self.app.clipboard_append(text)
            self.app.set_status(f"Скопирован путь: {text}")

    def _reveal(self) -> None:
        items = self.selected()
        target = items[0].path if items else self.path
        if not target:
            return
        try:
            if os.path.isdir(target):
                os.startfile(target)
            else:
                os.startfile(os.path.dirname(target))
        except OSError as exc:
            messagebox.showerror(APP_TITLE, pretty_error(exc), parent=self.app)


class App(BaseTk, ClipboardMixin):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.minsize(1100, 680)
        apply_theme(self)

        self.store = ServerStore()
        self.session = SftpSession()
        self.current_id = ""
        self.connected_server_id = ""
        self.mcp_busy = False
        self._command_running = False
        self.mcp_bridge = None
        self.jobs: queue.Queue = queue.Queue()
        self.cancelled: set[str] = set()
        self.active_jobs: set[str] = set()
        self._remote_editors: set[RemoteTextEditor] = set()
        self.job_specs: dict[
            str,
            tuple[str, str, Callable[..., None], Callable[[], None] | None],
        ] = {}
        self.current_job = ""
        self.busy = False
        self._connect_attempt: ConnectionAttempt | None = None
        self.has_git = False
        self.common_buttons: list[ttk.Button] = []
        self.git_buttons: list[ttk.Button] = []
        self.key_buttons: list[ttk.Button] = []
        self._prog_ts = 0.0
        self._cmd_hist: list[str] = []
        self._cmd_idx = 0
        self._last_drop_ts = 0.0
        self._drag: dict[str, Any] = {
            "press": None,
            "active": False,
            "source": None,
            "items": [],
            "ghost": None,
            "hover": None,
        }
        self.show_local_var = tk.BooleanVar(value=self.store.show_local)
        self.command_groups = load_commands()

        self._build_menu()
        self._build_ui()
        self.setup_clipboard_shortcuts()
        self._setup_dnd()
        self._restore_geometry()
        self.reload_servers(select_id=self.store.last_server_id)
        self.local.refresh()
        self.remote.refresh()
        self._apply_local_visibility(save=False)
        self._update_cmd_buttons()

        self.bind("<F5>", lambda _e: self.refresh_all())
        self.bind_all("<B1-Motion>", self._on_drag_motion, add="+")
        self.bind_all("<ButtonRelease-1>", self._on_drag_release, add="+")
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        threading.Thread(target=self._worker, daemon=True).start()
        hint = "Готово. Локальную панель включает кнопка «Этот компьютер» справа вверху."
        if not HAS_DND:
            hint += "  Для перетаскивания из Проводника: pip install tkinterdnd2"
        self.set_status(hint)
        try:
            self.mcp_bridge = ConnectorBridge(self)
            self.mcp_bridge.start()
        except Exception as exc:
            self.set_status(f"MCP не запущен: {exc}")

    def _build_menu(self) -> None:
        menubar = tk.Menu(self)
        file_m = tk.Menu(menubar, tearoff=0)
        file_m.add_command(label="Добавить сервер", command=self.add_server)
        file_m.add_separator()
        file_m.add_command(label="Выход", command=self.on_close)
        menubar.add_cascade(label="Файл", menu=file_m)

        srv = tk.Menu(menubar, tearoff=0)
        srv.add_command(label="Подключить", command=self.connect_selected)
        srv.add_command(label="Отключить", command=self.disconnect)
        srv.add_separator()
        srv.add_command(label="Изменить…", command=self.edit_server)
        srv.add_command(label="Удалить", command=self.remove_server)
        menubar.add_cascade(label="Сервер", menu=srv)

        view = tk.Menu(menubar, tearoff=0)
        view.add_checkbutton(
            label="Этот компьютер",
            variable=self.show_local_var,
            command=self._apply_local_visibility,
        )
        menubar.add_cascade(label="Вид", menu=view)

        act = tk.Menu(menubar, tearoff=0)
        act.add_command(label="Обновить     F5", command=self.refresh_all)
        act.add_command(label="Загрузить на сервер", command=lambda: self.transfer("upload"))
        act.add_command(label="Скачать с сервера", command=lambda: self.transfer("download"))
        menubar.add_cascade(label="Действия", menu=act)

        help_m = tk.Menu(menubar, tearoff=0)
        help_m.add_command(label="О программе", command=self._about)
        help_m.add_command(label="MCP для Codex", command=lambda: messagebox.showinfo(
            APP_TITLE, "MCP server-connector управляет подключением и файлами этого приложения.\n"
            "Используются профили из списка слева.\n"
            "Состояние: " + ("работает" if self.mcp_bridge and self.mcp_bridge.server else "не запущен"), parent=self))
        menubar.add_cascade(label="Справка", menu=help_m)
        self.config(menu=menubar)

    def _build_ui(self) -> None:
        body = ttk.Panedwindow(self, orient="horizontal")
        body.pack(fill="both", expand=True)

        sidebar = ttk.Frame(body, style="Sidebar.TFrame", width=260)
        main = ttk.Frame(body)
        body.add(sidebar, weight=0)
        body.add(main, weight=1)
        try:
            body.pane(sidebar, minsize=220)
            body.pane(main, minsize=640)
        except tk.TclError:
            pass

        head = ttk.Frame(sidebar, style="Sidebar.TFrame", padding=(14, 14, 14, 8))
        head.pack(fill="x")
        ttk.Label(head, text="Серверы", style="Sidebar.TLabel").pack(anchor="w")
        ttk.Label(head, text="Сохранённые подключения", style="SidebarMuted.TLabel").pack(anchor="w", pady=(2, 0))

        list_wrap = ttk.Frame(sidebar, style="Sidebar.TFrame", padding=(10, 0, 10, 8))
        list_wrap.pack(fill="both", expand=True)
        self.servers_tree = ttk.Treeview(list_wrap, show="tree", selectmode="browse")
        self.servers_tree.pack(fill="both", expand=True)
        self.servers_tree.bind("<<TreeviewSelect>>", lambda _e: self._on_server_select())
        self.servers_tree.bind("<Double-1>", lambda _e: self.connect_selected())

        sbtns = ttk.Frame(sidebar, style="Sidebar.TFrame", padding=(10, 4, 10, 14))
        sbtns.pack(fill="x")
        ttk.Button(sbtns, text="＋  Добавить", command=self.add_server).pack(fill="x", pady=2)
        ttk.Button(sbtns, text="Изменить", command=self.edit_server).pack(fill="x", pady=2)
        ttk.Button(sbtns, text="Удалить", style="Danger.TButton", command=self.remove_server).pack(
            fill="x", pady=2
        )

        toolbar = ttk.Frame(main, padding=(12, 10, 12, 6))
        toolbar.pack(fill="x")
        self.conn_dot = ttk.Label(toolbar, text="●  Не подключено", style="Bad.TLabel")
        self.conn_dot.pack(side="left")
        self.conn_info = ttk.Label(toolbar, text="", style="Muted.TLabel")
        self.conn_info.pack(side="left", padx=(10, 0))
        ttk.Button(toolbar, text="Отключить", command=self.disconnect).pack(side="right")
        self.local_toggle = ttk.Checkbutton(
            toolbar,
            text="Этот компьютер",
            variable=self.show_local_var,
            command=self._apply_local_visibility,
        )
        self.local_toggle.pack(side="right", padx=(0, 12))
        HoverTip(self.local_toggle, "Показать или скрыть локальную панель файлов")
        self.connect_btn = ttk.Button(
            toolbar, text="Подключить", style="Accent.TButton", command=self.connect_selected
        )
        self.connect_btn.pack(side="right", padx=(0, 8))

        mid = ttk.Panedwindow(main, orient="vertical")
        mid.pack(fill="both", expand=True, padx=8, pady=(0, 4))

        notebook = ttk.Notebook(mid)
        files = ttk.Frame(notebook)
        term = ttk.Frame(notebook)
        notebook.add(files, text="  Файлы  ")
        notebook.add(term, text="  Команды  ")

        panes = ttk.Frame(files)
        panes.pack(fill="both", expand=True)
        panes.columnconfigure(0, weight=1)
        panes.columnconfigure(2, weight=1)
        panes.rowconfigure(0, weight=1)
        self.files_panes = panes

        self.local = FilePane(panes, self, "local", "Этот компьютер")
        self.remote = FilePane(panes, self, "remote", "Удалённый сервер")
        self.local.grid(row=0, column=0, sticky="nsew", padx=(0, 4), pady=6)
        self.mid_col = ttk.Frame(panes)
        self.mid_col.grid(row=0, column=1, sticky="ns", pady=80)
        ttk.Button(
            self.mid_col, text="→", style="Transfer.TButton", width=3, command=lambda: self.transfer("upload")
        ).pack(pady=6)
        ttk.Button(
            self.mid_col, text="←", style="Transfer.TButton", width=3, command=lambda: self.transfer("download")
        ).pack(pady=6)
        self.remote.grid(row=0, column=2, sticky="nsew", padx=(4, 0), pady=6)

        self._build_terminal(term)

        transfers = ttk.Frame(mid, style="Surface.TFrame")
        tbar = ttk.Frame(transfers, style="Surface.TFrame", padding=(8, 6))
        tbar.pack(fill="x")
        ttk.Label(tbar, text="Передачи", style="PaneTitle.TLabel").pack(side="left")
        ttk.Button(tbar, text="Очистить", style="Tool.TButton", command=self._clear_transfers).pack(
            side="right"
        )
        ttk.Button(tbar, text="Продолжить", style="Tool.TButton", command=self._continue_selected).pack(
            side="right", padx=(0, 6)
        )
        ttk.Button(tbar, text="Отменить текущую", style="Tool.TButton", command=self._cancel_current).pack(
            side="right", padx=(0, 6)
        )
        tw = ttk.Frame(transfers, style="Surface.TFrame")
        tw.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.transfers = ttk.Treeview(
            tw, columns=("dir", "progress", "status"), show="headings", height=5
        )
        self.transfers.heading("dir", text="")
        self.transfers.heading("progress", text="Прогресс")
        self.transfers.heading("status", text="Статус")
        self.transfers.column("dir", width=36, stretch=False, anchor="center")
        self.transfers.column("progress", width=220, stretch=False)
        self.transfers.column("status", width=360, stretch=True)
        sy = ttk.Scrollbar(tw, orient="vertical", command=self.transfers.yview)
        self.transfers.configure(yscrollcommand=sy.set)
        self.transfers.pack(side="left", fill="both", expand=True)
        sy.pack(side="right", fill="y")
        self.progress = ttk.Progressbar(transfers, mode="determinate")
        self.progress.pack(fill="x", padx=8, pady=(0, 8))

        mid.add(notebook, weight=4)
        mid.add(transfers, weight=1)

        status = ttk.Frame(self, style="Surface.TFrame")
        status.pack(fill="x")
        self.status_var = tk.StringVar(value="")
        ttk.Label(status, textvariable=self.status_var, style="Status.TLabel", padding=(12, 6)).pack(
            side="left", fill="x", expand=True
        )

    def _build_terminal(self, parent: ttk.Frame) -> None:
        palette = ttk.Frame(parent, padding=(10, 10, 10, 4))
        self.cwd_var = tk.StringVar(value="Каталог: нет подключения")
        ttk.Label(palette, textvariable=self.cwd_var, style="Muted.TLabel").pack(anchor="w", pady=(0, 6))

        common_head = ttk.Frame(palette)
        common_head.pack(fill="x")
        ttk.Label(common_head, text="Частые команды", style="Section.TLabel").pack(side="left")
        ttk.Button(
            common_head, text="commands.json", style="Tool.TButton", command=self.open_commands_file
        ).pack(side="right")
        HoverTip(
            common_head.winfo_children()[-1],
            "Открыть JSON: можно добавить свои кнопки (label, command, hint)",
        )
        ttk.Button(
            common_head, text="Обновить", style="Tool.TButton", command=self.reload_command_buttons
        ).pack(side="right", padx=(0, 6))
        self.common_host = ttk.Frame(palette)
        self.common_host.pack(fill="x", pady=(4, 10))

        mid = ttk.Frame(palette)
        mid.pack(fill="x")
        git_col = ttk.Frame(mid)
        git_col.pack(side="left", fill="x", expand=True)
        key_col = ttk.Frame(mid)
        key_col.pack(side="left", fill="x", expand=True, padx=(16, 0))

        git_head = ttk.Frame(git_col)
        git_head.pack(fill="x")
        ttk.Label(git_head, text="Git", style="Section.TLabel").pack(side="left")
        self.install_git_btn = ttk.Button(
            git_head, text="Установить Git", style="Accent.TButton", command=self.install_git
        )
        self.install_git_btn.pack(side="right")
        self.git_ver_var = tk.StringVar(value="")
        ttk.Label(git_head, textvariable=self.git_ver_var, style="Muted.TLabel").pack(
            side="right", padx=(0, 10)
        )
        self.git_host = ttk.Frame(git_col)
        self.git_host.pack(fill="x", pady=(4, 0))
        self.reload_command_buttons()

        ttk.Label(key_col, text="Ключ авторизации", style="Section.TLabel").pack(anchor="w")
        key_btns = ttk.Frame(key_col)
        key_btns.pack(fill="x", pady=(4, 0))
        key_actions = [
            ("Вставить публичный ключ…", self.insert_auth_key),
            ("Показать ~/.ssh", self.show_remote_ssh),
            ("Сгенерировать ключ на сервере", self.generate_server_key),
            ("Копировать pubkey сервера", self.copy_server_pubkey),
        ]
        for i, (label, cmd) in enumerate(key_actions):
            btn = ttk.Button(key_btns, text=label, style="Chip.TButton", command=cmd)
            btn.grid(row=i // 2, column=i % 2, padx=3, pady=3, sticky="ew")
            self.key_buttons.append(btn)
        key_btns.columnconfigure(0, weight=1)
        key_btns.columnconfigure(1, weight=1)

        self.term = tk.Text(
            parent,
            bg=C["row"],
            fg=C["text"],
            insertbackground=C["text"],
            relief="flat",
            font=("Consolas", 10),
            wrap="word",
            state="disabled",
            padx=10,
            pady=8,
        )
        self.term.tag_configure("cmd", foreground=C["accent"])
        self.term.tag_configure("err", foreground=C["danger"])
        self.term.tag_configure("ok", foreground=C["success"])
        row = ttk.Frame(parent)
        self.cmd_row = row
        self.cmd_var = tk.StringVar()
        cmd = ttk.Entry(row, textvariable=self.cmd_var)
        cmd.pack(side="left", fill="x", expand=True, padx=(0, 8))
        cmd.bind("<Return>", lambda _e: self.run_command())
        cmd.bind("<Up>", self._cmd_prev)
        cmd.bind("<Down>", self._cmd_next)
        ttk.Button(row, text="Выполнить", style="Accent.TButton", command=self.run_command).pack(side="right")
        self.cmd_entry = cmd

        # Reserve the command line before the taller palette and output widget.
        # Otherwise Pack may give the last row a zero-height parcel.
        row.pack(side="bottom", fill="x", padx=10, pady=(4, 10))
        palette.pack(side="top", fill="x")
        self.term.pack(side="top", fill="both", expand=True, padx=10, pady=4)

    def _command_grid(
        self, parent: ttk.Frame, items: list[dict[str, Any]], columns: int
    ) -> list[ttk.Button]:
        for child in parent.winfo_children():
            child.destroy()
        buttons: list[ttk.Button] = []
        for i, spec in enumerate(items):
            style = "Danger.TButton" if spec.get("danger") else "Chip.TButton"
            btn = ttk.Button(
                parent,
                text=spec["label"],
                style=style,
                command=lambda item=spec: self._run_preset(item),
            )
            btn.grid(row=i // columns, column=i % columns, padx=3, pady=3, sticky="ew")
            if spec.get("hint"):
                HoverTip(btn, spec["hint"])
            buttons.append(btn)
        for col in range(columns):
            parent.columnconfigure(col, weight=1)
        return buttons

    def _run_preset(self, spec: dict[str, Any]) -> None:
        if spec.get("confirm"):
            hint = spec.get("hint") or spec.get("command")
            if not messagebox.askyesno(
                APP_TITLE,
                f"{spec.get('label')}\n\n{hint}\n\nВыполнить?",
                parent=self,
                icon="warning" if spec.get("danger") else "question",
            ):
                return
        self.run_command(
            spec["command"],
            insert_only=bool(spec.get("insert_only")),
            use_cwd=bool(spec.get("use_cwd", True)),
        )

    def reload_command_buttons(self) -> None:
        self.command_groups = load_commands()
        if hasattr(self, "common_host"):
            self.common_buttons = self._command_grid(
                self.common_host, self.command_groups.get("common") or [], columns=7
            )
        if hasattr(self, "git_host"):
            self.git_buttons = self._command_grid(
                self.git_host, self.command_groups.get("git") or [], columns=5
            )
        self._update_cmd_buttons()

    def open_commands_file(self) -> None:
        path = COMMANDS_FILE
        if not path.exists():
            messagebox.showwarning(APP_TITLE, f"Файл не найден:\n{path}", parent=self)
            return
        try:
            os.startfile(str(path))
        except OSError as exc:
            messagebox.showerror(APP_TITLE, pretty_error(exc), parent=self)

    def _apply_local_visibility(self, save: bool = True) -> None:
        show = bool(self.show_local_var.get())
        if show:
            self.files_panes.columnconfigure(0, weight=1)
            self.files_panes.columnconfigure(1, weight=0)
            self.files_panes.columnconfigure(2, weight=1)
            self.local.grid(row=0, column=0, sticky="nsew", padx=(0, 4), pady=6)
            self.mid_col.grid(row=0, column=1, sticky="ns", pady=80)
            self.remote.grid(row=0, column=2, sticky="nsew", padx=(4, 0), pady=6)
        else:
            self.local.grid_remove()
            self.mid_col.grid_remove()
            self.files_panes.columnconfigure(0, weight=1)
            self.files_panes.columnconfigure(1, weight=0)
            self.files_panes.columnconfigure(2, weight=0)
            self.remote.grid(row=0, column=0, columnspan=3, sticky="nsew", padx=0, pady=6)
        if save:
            self.store.show_local = show
            self.store.save()

    def _restore_geometry(self) -> None:
        geo = self.store.window or "1280x800"
        try:
            self.geometry(geo)
        except tk.TclError:
            self.geometry("1280x800")

    def _about(self) -> None:
        messagebox.showinfo(
            APP_TITLE,
            "Server Connector\n\n"
            "Клиент для работы с удалёнными серверами по SFTP/SSH.\n"
            "Профили хранятся локально, пароли шифруются через Windows DPAPI.\n\n"
            "F5 — обновить   Delete — удалить   F2 — переименовать\n"
            "Enter — открыть или редактировать   Backspace — вверх\n"
            "Перетащите файлы между панелями или из Проводника на сервер.\n"
            "Локальную панель включает кнопка «Этот компьютер».\n"
            "Свои команды — в commands.json (есть подсказки при наведении).",
            parent=self,
        )

    def selected_server(self) -> dict[str, Any] | None:
        sel = self.servers_tree.selection()
        if not sel:
            return self.store.get(self.current_id) if self.current_id else None
        return self.store.get(sel[0])

    def reload_servers(self, select_id: str = "") -> None:
        self.servers_tree.delete(*self.servers_tree.get_children())
        for server in self.store.servers:
            label = f"{server.get('name') or server.get('host') or 'Сервер'}"
            host = f"{server.get('username', '')}@{server.get('host', '')}:{server.get('port', 22)}"
            self.servers_tree.insert("", "end", iid=server["id"], text=f"  {label}   ·   {host}")
        pick = select_id or self.store.last_server_id
        if pick and self.store.get(pick):
            self.servers_tree.selection_set(pick)
            self.servers_tree.focus(pick)
            self.current_id = pick
            self._apply_server_paths(self.store.get(pick))

    def _on_server_select(self) -> None:
        server = self.selected_server()
        if not server:
            return
        self.current_id = server["id"]
        if not self.session.connected:
            self._apply_server_paths(server)

    def _apply_server_paths(self, server: dict[str, Any] | None) -> None:
        if not server:
            return
        local = server.get("local_path") or str(Path.home())
        if os.path.isdir(local):
            self.local.path = local
            self.local.path_var.set(local)
            self.local.refresh()
        remote = server.get("remote_path") or "/"
        self.remote.path = remote
        self.remote.path_var.set(remote)
        if self.session.connected:
            self.remote.refresh()

    def remember_paths(self) -> None:
        self._update_cwd_label()
        server_id = self.connected_server_id if self.session.connected else self.current_id
        server = self.store.get(server_id) if server_id else self.selected_server()
        if not server:
            return
        server["local_path"] = self.local.path or server.get("local_path") or str(Path.home())
        server["remote_path"] = self.remote.path or server.get("remote_path") or "/"
        self.store.save()

    def add_server(self) -> None:
        dialog = ServerDialog(self)
        if not dialog.result:
            return
        saved = self.store.upsert(dialog.result)
        self.store.last_server_id = saved["id"]
        self.store.save()
        self.reload_servers(saved["id"])

    def edit_server(self) -> None:
        server = self.selected_server()
        if not server:
            messagebox.showinfo(APP_TITLE, "Выберите сервер", parent=self)
            return
        if any(
            editor.server_id == server["id"] and (editor.dirty or editor.busy)
            for editor in tuple(getattr(self, "_remote_editors", ()))
        ):
            messagebox.showinfo(
                APP_TITLE,
                "Сначала завершите работу с файлами этого сервера в редакторе.",
                parent=self,
            )
            return
        dialog = ServerDialog(self, server)
        if not dialog.result:
            return
        self.store.upsert(dialog.result)
        self.reload_servers(dialog.result["id"])

    def remove_server(self) -> None:
        server = self.selected_server()
        if not server:
            return
        if any(
            editor.server_id == server["id"] and (editor.dirty or editor.busy)
            for editor in tuple(getattr(self, "_remote_editors", ()))
        ):
            messagebox.showinfo(
                APP_TITLE,
                "Сначала завершите работу с файлами этого сервера в редакторе.",
                parent=self,
            )
            return
        name = server.get("name") or server.get("host")
        if not messagebox.askyesno(APP_TITLE, f"Удалить сервер «{name}»?", parent=self):
            return
        if self.session.connected and self.connected_server_id == server["id"]:
            if not self.disconnect():
                return
        self.store.remove(server["id"])
        self.current_id = self.store.last_server_id
        self.reload_servers(self.current_id)

    def connect_selected(self) -> None:
        if self.mcp_busy:
            self.set_status("Дождитесь завершения операции MCP")
            return
        if any(
            str(job_id).startswith("editor-")
            for job_id in getattr(self, "active_jobs", ())
        ):
            self.set_status("Дождитесь завершения чтения или сохранения в редакторе")
            return
        server = self.selected_server()
        if not server:
            if not self.store.servers:
                self.add_server()
                server = self.selected_server()
            if not server:
                messagebox.showinfo(APP_TITLE, "Сначала добавьте сервер", parent=self)
                return
        if self.busy:
            return
        self.busy = True
        self.connect_btn.configure(state="disabled")
        self.conn_dot.configure(text="●  Подключение…", style="Muted.TLabel")
        self.set_status(f"Подключение к {server.get('host')}…")

        def connected(session: SftpSession, home: str, entries: list[FileEntry]) -> None:
            previous = self.session
            self.session = session
            close_in_background(previous)
            self._connect_ok(server, home, entries)
            if attempt.directory_error is not None:
                messagebox.showerror("Папка сервера", attempt.directory_error, parent=self)

        attempt = ConnectionAttempt(
            self,
            server,
            connected,
            self._connect_fail,
            load_directory=True,
            format_error=pretty_error,
        )
        self._connect_attempt = attempt
        attempt.start()

    def _connect_ok(
        self, server: dict[str, Any], home: str, entries: list[FileEntry]
    ) -> None:
        self._connect_attempt = None
        self.busy = False
        self.connect_btn.configure(state="normal")
        self.current_id = server["id"]
        self.store.last_server_id = server["id"]
        self.connected_server_id = server["id"]
        if not server.get("remote_path") or server.get("remote_path") == "/":
            server["remote_path"] = home
        self.store.save()
        self.conn_dot.configure(text="●  Подключено", style="Ok.TLabel")
        self.conn_info.configure(text=self.session.label)
        self.remote.path = server.get("remote_path") or home
        self.remote.path_var.set(self.remote.path)
        self.remote._all_entries = entries
        self.remote._render(entries)
        self.set_status(f"Подключено: {self.session.label}")
        self._update_cwd_label()
        self._probe_tools()

    def _connect_fail(self, error: str) -> None:
        self._connect_attempt = None
        self.busy = False
        self.connect_btn.configure(state="normal")
        self._sync_connection_badge()
        self.set_status(error)
        messagebox.showerror("Подключение", error, parent=self)

    def disconnect(self) -> bool:
        if any(
            getattr(editor, "_busy", "") == "save"
            for editor in tuple(getattr(self, "_remote_editors", ()))
        ):
            self.set_status("Дождитесь завершения сохранения файла на сервере")
            return False
        self.connected_server_id = ""
        if self._connect_attempt is not None:
            self._connect_attempt.cancel()
            self._connect_attempt = None
        self.busy = False
        self.connect_btn.configure(state="normal")
        previous = self.session
        self.session = SftpSession()
        close_in_background(previous)
        self.conn_dot.configure(text="●  Не подключено", style="Bad.TLabel")
        self.conn_info.configure(text="")
        self.remote.refresh()
        self.has_git = False
        self._update_cmd_buttons()
        self._update_cwd_label()
        self.set_status("Отключено")
        return True

    def refresh_all(self) -> None:
        self.local.refresh()
        self.remote.refresh()

    def need_remote(self) -> bool:
        if self.session.connected:
            return True
        messagebox.showinfo(APP_TITLE, "Сначала подключитесь к серверу", parent=self)
        return False

    def _local_visible(self) -> bool:
        return bool(self.show_local_var.get())

    def _count_files(self, items: list[FileEntry], side: str) -> int:
        total = 0
        for item in items:
            if not item.is_dir:
                total += 1
                continue
            try:
                if side == "local":
                    total += sum(1 for _p, _r, is_dir in walk_local(item.path) if not is_dir)
                else:
                    total += sum(1 for _p, _r, is_dir in self.session.walk_remote(item.path) if not is_dir)
            except Exception:
                total += 1
        return total

    def _offer_archive(self, items: list[FileEntry], side: str) -> bool | None:
        self.configure(cursor="watch")
        try:
            self.update_idletasks()
            count = self._count_files(items, side)
        finally:
            self.configure(cursor="")
        if count <= ARCHIVE_THRESHOLD:
            return False
        answer = messagebox.askyesnocancel(
            APP_TITLE,
            f"Выбрано файлов: {count}.\n\n"
            "Передать одним архивом? Так быстрее: файлы упаковываются, "
            "едет один файл, на месте он распаковывается, временные архивы удаляются.\n\n"
            "Да — архивом,  Нет — по одному,  Отмена — ничего не делать.",
            parent=self,
        )
        return answer

    def transfer(
        self,
        direction: str,
        items: list[FileEntry] | None = None,
        dest_dir: str | None = None,
    ) -> None:
        if any(
            str(job_id).startswith("editor-")
            for job_id in getattr(self, "active_jobs", ())
        ):
            self.set_status("Дождитесь завершения чтения или сохранения в редакторе")
            return
        if not self.need_remote():
            return
        if direction == "upload":
            items = list(items) if items is not None else self.local.selected()
            dest_dir = dest_dir if dest_dir is not None else (self.remote.path or "/")
            if not items:
                if not self._local_visible():
                    paths = filedialog.askopenfilenames(parent=self, title="Файлы для загрузки")
                    if not paths:
                        return
                    self.upload_local_paths(list(paths), dest_dir)
                    return
                messagebox.showinfo(APP_TITLE, "Выберите файлы слева", parent=self)
                return
            packed = self._offer_archive(items, "local")
            if packed is None:
                return
            if packed:
                self._enqueue(
                    "↑",
                    f"архив ({len(items)} объект.)",
                    lambda job_id, batch=items, dest=dest_dir: self._do_upload_archive(batch, dest, job_id),
                    on_done=self.remote.refresh,
                )
                return
            for item in items:
                dest = posix_join(dest_dir, item.name)
                self._enqueue(
                    "↑",
                    item.name,
                    lambda job_id, item=item, dest=dest: self._do_upload(item, dest, job_id),
                    on_done=self.remote.refresh,
                )
            return
        items = list(items) if items is not None else self.remote.selected()
        dest_dir = dest_dir if dest_dir is not None else (self.local.path or str(Path.home()))
        if not self._local_visible() or not dest_dir or not os.path.isdir(dest_dir):
            chosen = filedialog.askdirectory(parent=self, title="Куда скачать")
            if not chosen:
                return
            dest_dir = chosen
        if not items:
            messagebox.showinfo(APP_TITLE, "Выберите файлы справа", parent=self)
            return
        packed = self._offer_archive(items, "remote")
        if packed is None:
            return
        if packed:
            self._enqueue(
                "↓",
                f"архив ({len(items)} объект.)",
                lambda job_id, batch=items, dest=dest_dir: self._do_download_archive(batch, dest, job_id),
                on_done=self.local.refresh,
            )
            return
        for item in items:
            dest = os.path.join(dest_dir, item.name)
            if not item.is_dir and os.path.exists(dest) and not self._confirm_overwrite(dest):
                continue
            self._enqueue(
                "↓",
                item.name,
                lambda job_id, item=item, dest=dest: self._do_download(item, dest, job_id),
                on_done=self.local.refresh,
            )

    def _confirm_overwrite(self, path: str) -> bool:
        if not os.path.exists(path):
            return True
        return messagebox.askyesno(
            APP_TITLE, f"«{os.path.basename(path)}» уже есть. Перезаписать?", parent=self
        )

    def _do_upload(self, item: FileEntry, dest: str, job_id: str = "") -> None:
        cancel = lambda: job_id in self.cancelled
        progress = lambda done, total: self._progress(job_id, done, total)
        status = lambda text: self._ui_status(job_id, text)
        if not item.is_dir:
            self.session.upload_file(
                item.path,
                dest,
                progress=progress,
                cancel=cancel,
                status=status,
                operation_id=job_id,
            )
            return
        self.session.ensure_dir(dest)
        entries = list(walk_local(item.path))
        total = max(1, sum(1 for _p, _r, is_dir in entries if not is_dir))
        done_files = 0
        for abs_path, rel, is_dir in entries:
            if cancel():
                raise RuntimeError("Передача отменена")
            remote = posix_join(dest, rel)
            if is_dir:
                self.session.ensure_dir(remote)
                continue
            done_files += 1
            self._ui_status(job_id, f"{done_files}/{total}  {rel}")
            self.session.upload_file(
                abs_path,
                remote,
                progress=progress,
                cancel=cancel,
                status=status,
                operation_id=job_id,
            )

    def _do_download(self, item: FileEntry, dest: str, job_id: str = "") -> None:
        cancel = lambda: job_id in self.cancelled
        progress = lambda done, total: self._progress(job_id, done, total)
        status = lambda text: self._ui_status(job_id, text)
        if not item.is_dir:
            self.session.download_file(
                item.path,
                dest,
                progress=progress,
                cancel=cancel,
                status=status,
                operation_id=job_id,
            )
            return
        Path(dest).mkdir(parents=True, exist_ok=True)
        entries = self.session.walk_remote(item.path)
        total = max(1, sum(1 for _p, _r, is_dir in entries if not is_dir))
        done_files = 0
        for remote, rel, is_dir in entries:
            if cancel():
                raise RuntimeError("Передача отменена")
            local = os.path.join(dest, *rel.split("/"))
            if is_dir:
                Path(local).mkdir(parents=True, exist_ok=True)
                continue
            done_files += 1
            Path(local).parent.mkdir(parents=True, exist_ok=True)
            self._ui_status(job_id, f"{done_files}/{total}  {rel}")
            self.session.download_file(
                remote,
                local,
                progress=progress,
                cancel=cancel,
                status=status,
                operation_id=job_id,
            )

    def _do_upload_archive(self, items: list[FileEntry], dest_dir: str, job_id: str = "") -> None:
        cancel = lambda: job_id in self.cancelled
        progress = lambda done, total: self._progress(job_id, done, total)
        status = lambda text: self._ui_status(job_id, text)
        archive_id = job_id or uuid.uuid4().hex
        zip_name = f"sc_pack_{archive_id}.zip"
        local_zip = Path(tempfile.gettempdir()) / "ServerConnector" / zip_name
        building_zip = local_zip.with_name(local_zip.name + ".building")
        if cancel():
            raise RuntimeError("Передача отменена")
        if not local_zip.is_file():
            self._ui_status(job_id, "упаковка…")
            try:
                pack_local_zip(
                    ((item.path, item.name, item.is_dir) for item in items),
                    building_zip,
                    cancel=cancel,
                )
            except Exception:
                building_zip.unlink(missing_ok=True)
                raise
            os.replace(building_zip, local_zip)
        else:
            self._ui_status(job_id, "готовый архив найден…")
        if cancel():
            raise RuntimeError("Передача отменена")
        remote_zip = posix_join(dest_dir, zip_name)
        self._ui_status(job_id, "загрузка архива…")
        self.session.upload_file(
            str(local_zip),
            remote_zip,
            progress=progress,
            cancel=cancel,
            status=status,
            resume_final=True,
            operation_id=job_id,
        )
        self._ui_status(job_id, "распаковка на сервере…")
        code, out, err = self.session.exec_command(
            remote_unpack_script(remote_zip),
            timeout=300,
            retry_on_disconnect=True,
            status=status,
            cancel=cancel,
            operation_id=job_id,
        )
        if code != 0:
            raise RuntimeError((err or out or "не удалось распаковать архив на сервере").strip())
        try:
            self.session.remove(remote_zip, False)
        except Exception:
            pass
        try:
            self.session.clear_upload_state(remote_zip, job_id)
        except Exception:
            pass
        try:
            local_zip.unlink(missing_ok=True)
        except OSError:
            pass

    def _do_download_archive(self, items: list[FileEntry], dest_dir: str, job_id: str = "") -> None:
        cancel = lambda: job_id in self.cancelled
        progress = lambda done, total: self._progress(job_id, done, total)
        status = lambda text: self._ui_status(job_id, text)
        archive_id = job_id or uuid.uuid4().hex
        archive_name = f"sc_pack_{archive_id}.tar.gz"
        remote_dir = posix_parent(items[0].path) if items else (self.remote.path or "/")
        if items and not items[0].path.startswith(remote_dir.rstrip("/") + "/") and items[0].path != remote_dir:
            remote_dir = self.remote.path or remote_dir
        remote_archive = posix_join(remote_dir, archive_name)
        if cancel():
            raise RuntimeError("Передача отменена")
        self._ui_status(job_id, "упаковка на сервере…")
        script = remote_pack_command(remote_dir, [item.name for item in items], archive_name)
        code, out, err = self.session.exec_command(
            script,
            timeout=300,
            cancel=cancel,
            operation_id=job_id,
        )
        if code != 0:
            raise RuntimeError((err or out or "не удалось упаковать файлы на сервере").strip())
        local_archive = Path(dest_dir) / archive_name
        self._ui_status(job_id, "скачивание архива…")
        self.session.download_file(
            remote_archive,
            str(local_archive),
            progress=progress,
            cancel=cancel,
            status=status,
            operation_id=job_id,
        )
        self._ui_status(job_id, "распаковка…")
        unpack_local(str(local_archive))
        try:
            self.session.remove(remote_archive, False)
        except Exception:
            pass
        try:
            local_archive.unlink(missing_ok=True)
        except OSError:
            pass

    def create_folder(self, side: str) -> None:
        name = simpledialog.askstring("Новая папка", "Имя папки:", parent=self)
        if not name:
            return
        name = name.strip()
        if not name or "/" in name or "\\" in name:
            messagebox.showwarning(APP_TITLE, "Некорректное имя", parent=self)
            return
        if side == "local":
            if not self.local.path:
                messagebox.showwarning(APP_TITLE, "Сначала откройте диск или папку", parent=self)
                return
            path = os.path.join(self.local.path, name)
            try:
                os.makedirs(path, exist_ok=False)
            except OSError as exc:
                messagebox.showerror(APP_TITLE, pretty_error(exc), parent=self)
                return
            self.local.refresh()
            return
        if not self.need_remote():
            return
        path = posix_join(self.remote.path, name)
        self._enqueue("·", name, lambda job_id: self.session.mkdir(path), on_done=self.remote.refresh)

    def create_file(self, side: str) -> None:
        name = simpledialog.askstring("Новый файл", "Имя файла:", parent=self)
        if not name:
            return
        name = name.strip()
        if not name or "/" in name or "\\" in name:
            messagebox.showwarning(APP_TITLE, "Некорректное имя", parent=self)
            return
        if side == "local":
            if not self.local.path:
                messagebox.showwarning(APP_TITLE, "Сначала откройте диск или папку", parent=self)
                return
            path = os.path.join(self.local.path, name)
            try:
                with open(path, "x", encoding="utf-8"):
                    pass
            except OSError as exc:
                messagebox.showerror(APP_TITLE, pretty_error(exc), parent=self)
                return
            self.local.refresh()
            return
        if not self.need_remote():
            return
        path = posix_join(self.remote.path, name)
        self._enqueue(
            "·", name, lambda job_id: self.session.write_file(path, b""), on_done=self.remote.refresh
        )

    def rename_selected(self, side: str) -> None:
        pane = self.local if side == "local" else self.remote
        items = pane.selected()
        if not items:
            return
        item = items[0]
        name = simpledialog.askstring("Переименовать", "Новое имя:", initialvalue=item.name, parent=self)
        if not name or name == item.name:
            return
        name = name.strip()
        if not name or "/" in name or "\\" in name:
            messagebox.showwarning(APP_TITLE, "Некорректное имя", parent=self)
            return
        if side == "local":
            dest = os.path.join(os.path.dirname(item.path), name)
            try:
                os.rename(item.path, dest)
            except OSError as exc:
                messagebox.showerror(APP_TITLE, pretty_error(exc), parent=self)
                return
            self.local.refresh()
            return
        if not self.need_remote():
            return
        dest = posix_join(posix_parent(item.path), name)
        self._enqueue(
            "·", name, lambda job_id: self.session.rename(item.path, dest), on_done=self.remote.refresh
        )

    def delete_selected(self, side: str) -> None:
        pane = self.local if side == "local" else self.remote
        items = pane.selected()
        if not items:
            return
        names = ", ".join(i.name for i in items[:5])
        extra = "" if len(items) <= 5 else f" и ещё {len(items) - 5}"
        if not messagebox.askyesno(APP_TITLE, f"Удалить {len(items)} объект(ов)?\n{names}{extra}", parent=self):
            return
        if side == "local":
            errors: list[str] = []
            for item in items:
                try:
                    if item.is_dir:
                        shutil.rmtree(item.path)
                    else:
                        os.remove(item.path)
                except OSError as exc:
                    errors.append(f"{item.name}: {pretty_error(exc)}")
            if errors:
                messagebox.showerror(APP_TITLE, "\n".join(errors), parent=self)
            self.local.refresh()
            return
        if not self.need_remote():
            return

        def work(job_id: str) -> None:
            for item in items:
                if job_id in self.cancelled:
                    raise RuntimeError("Передача отменена")
                self.session.remove(item.path, item.is_dir)

        self._enqueue("×", names, work, on_done=self.remote.refresh)

    def open_file(self, side: str, entry: FileEntry) -> None:
        if side == "local":
            try:
                os.startfile(entry.path)
            except OSError as exc:
                messagebox.showerror(APP_TITLE, pretty_error(exc), parent=self)
            return
        if not self.need_remote():
            return
        self.open_remote_editor(entry)

    def open_remote_editor(self, entry: FileEntry) -> RemoteTextEditor | None:
        if self.mcp_busy:
            self.set_status("Дождитесь завершения операции MCP")
            return None
        if not self.need_remote():
            return None
        for editor in tuple(self._remote_editors):
            try:
                if (
                    editor.winfo_exists()
                    and editor.server_id == self.connected_server_id
                    and editor.connection_label == self.session.label
                    and editor.entry.path == entry.path
                ):
                    editor.lift()
                    editor.focus_force()
                    return editor
            except tk.TclError:
                self._remote_editors.discard(editor)

        if self.busy or self._command_running or bool(self.active_jobs):
            self.set_status("Дождитесь завершения другой операции с сервером")
            return None

        session = self.session
        server_id = self.connected_server_id
        editor: RemoteTextEditor
        editor = RemoteTextEditor(
            self,
            session,
            server_id,
            entry,
            on_saved=lambda: self._refresh_remote_after_edit(editor.session, server_id),
        )
        self._remote_editors.add(editor)

        def forget(event, target=editor) -> None:
            if event.widget is target:
                self._remote_editors.discard(target)

        editor.bind("<Destroy>", forget, add="+")
        return editor

    def _refresh_remote_after_edit(self, session: SftpSession, server_id: str) -> None:
        if self.session is not session or self.connected_server_id != server_id:
            return
        path = self.remote.path
        results: queue.Queue = queue.Queue(maxsize=1)

        def work() -> None:
            try:
                entries = session.listdir(path)
            except BaseException as exc:
                results.put((False, exc))
            else:
                results.put((True, entries))

        def poll() -> None:
            if (
                self.session is not session
                or self.connected_server_id != server_id
                or self.remote.path != path
            ):
                return
            try:
                ok, payload = results.get_nowait()
            except queue.Empty:
                self.after(100, poll)
                return
            if not ok:
                self.set_status(pretty_error(payload))
                self._sync_connection_badge()
                return
            self.remote._all_entries = payload
            self.remote._render(payload)
            self.update_status()

        self.after(100, poll)
        threading.Thread(target=work, daemon=True).start()

    def _editor_operation_running(self) -> bool:
        return any(job_id.startswith("editor-") for job_id in self.active_jobs)

    def run_command(
        self,
        command: str | None = None,
        *,
        insert_only: bool = False,
        use_cwd: bool = True,
        timeout: float = 60,
        input_data: str | None = None,
    ) -> None:
        if self.mcp_busy:
            self.set_status("Дождитесь завершения операции MCP")
            return
        if any(
            str(job_id).startswith("editor-")
            for job_id in getattr(self, "active_jobs", ())
        ):
            self.set_status("Дождитесь завершения чтения или сохранения в редакторе")
            return
        cmd = (command if command is not None else self.cmd_var.get()).strip()
        if insert_only:
            self.cmd_var.set(cmd)
            self.cmd_entry.focus_set()
            self.cmd_entry.icursor(tk.END)
            return
        if not cmd:
            return
        if not self.need_remote():
            return
        if command is None:
            self.cmd_var.set("")
        self._cmd_hist.append(cmd)
        self._cmd_idx = len(self._cmd_hist)
        full = cmd
        if use_cwd and self.remote.path:
            full = f"cd {shlex.quote(self.remote.path)} && {cmd}"
        self._term_write(f"$ {cmd}\n", "cmd")
        self._command_running = True
        command_session = self.session

        def work() -> None:
            try:
                code, out, err = command_session.exec_command(full, timeout=timeout, input_data=input_data)
            except Exception as exc:
                self.after(0, lambda error=pretty_error(exc): self._term_write(error + "\n", "err"))
                return
            finally:
                self.after(0, lambda: setattr(self, "_command_running", False))
            text = out
            if err:
                text += (("\n" if text else "") + err)
            if not text.endswith("\n"):
                text += "\n"
            tag = "ok" if code == 0 else "err"
            self.after(0, lambda: self._term_write(text + f"[код {code}]\n", tag))

        threading.Thread(target=work, daemon=True).start()

    def _term_write(self, text: str, tag: str = "") -> None:
        self.term.configure(state="normal")
        self.term.insert("end", text, tag)
        self.term.see("end")
        self.term.configure(state="disabled")

    def _cmd_prev(self, _event=None):
        if not self._cmd_hist:
            return "break"
        self._cmd_idx = max(0, self._cmd_idx - 1)
        self.cmd_var.set(self._cmd_hist[self._cmd_idx])
        return "break"

    def _cmd_next(self, _event=None):
        if not self._cmd_hist:
            return "break"
        self._cmd_idx = min(len(self._cmd_hist), self._cmd_idx + 1)
        if self._cmd_idx >= len(self._cmd_hist):
            self.cmd_var.set("")
        else:
            self.cmd_var.set(self._cmd_hist[self._cmd_idx])
        return "break"

    def _update_cwd_label(self) -> None:
        if not hasattr(self, "cwd_var"):
            return
        if self.session.connected:
            self.cwd_var.set(f"Каталог: {self.remote.path}")
        else:
            self.cwd_var.set("Каталог: нет подключения")

    def _update_cmd_buttons(self) -> None:
        connected = bool(self.session.connected)
        for btn in self.common_buttons + self.key_buttons:
            btn.configure(state="normal" if connected else "disabled")
        for btn in self.git_buttons:
            btn.configure(state="normal" if connected and self.has_git else "disabled")
        if hasattr(self, "install_git_btn"):
            self.install_git_btn.configure(
                state="normal" if connected and not self.has_git else "disabled"
            )
            if not connected:
                self.git_ver_var.set("")
            elif self.has_git:
                self.git_ver_var.set("git установлен")
            else:
                self.git_ver_var.set("git не найден")

    def _probe_tools(self) -> None:
        def work() -> None:
            has = False
            version = ""
            try:
                code, out, err = self.session.exec_command("command -v git >/dev/null 2>&1 && git --version")
                has = code == 0
                version = (out or err).strip().splitlines()[0] if has else ""
            except Exception:
                has = False
            self.after(0, lambda: self._apply_git_state(has, version))

        threading.Thread(target=work, daemon=True).start()

    def _apply_git_state(self, has: bool, version: str = "") -> None:
        self.has_git = has
        if hasattr(self, "git_ver_var"):
            self.git_ver_var.set(version or ("git установлен" if has else "git не найден"))
        self._update_cmd_buttons()

    def install_git(self) -> None:
        if not self.need_remote():
            return
        self._term_write("$ установка git…\n", "cmd")

        def work() -> None:
            try:
                code, out, err = self.session.exec_command(
                    "bash -s", timeout=300, input_data=INSTALL_GIT
                )
            except Exception as exc:
                self.after(0, lambda error=pretty_error(exc): self._term_write(error + "\n", "err"))
                return
            text = ((out or "") + ("\n" + err if err else "")).strip()
            if code == 0:
                self.after(0, lambda: self._term_write(text + "\n[код 0]\n", "ok"))
                self.after(0, lambda: self._apply_git_state(True, (out or "").strip().splitlines()[0] if out else ""))
                return
            self.after(0, lambda: self._install_git_with_sudo(text))

        threading.Thread(target=work, daemon=True).start()

    def _install_git_with_sudo(self, prev: str) -> None:
        if prev:
            self._term_write(prev + "\n", "err")
        password = simpledialog.askstring(
            "Установка Git",
            "Нужны права sudo. Введите пароль (не сохранится):",
            show="*",
            parent=self,
        )
        if not password:
            self._term_write("Установка отменена. Можно выполнить вручную: apt-get install git\n", "err")
            return
        self._term_write("$ sudo … install git\n", "cmd")

        def work() -> None:
            try:
                code, out, err = self.session.exec_command(
                    INSTALL_GIT_SUDO, timeout=300, input_data=password + "\n"
                )
            except Exception as exc:
                self.after(0, lambda error=pretty_error(exc): self._term_write(error + "\n", "err"))
                return
            text = ((out or "") + ("\n" + err if err else "")).strip() + f"\n[код {code}]\n"
            tag = "ok" if code == 0 else "err"
            self.after(0, lambda: self._term_write(text, tag))
            if code == 0:
                self.after(0, self._probe_tools)

        threading.Thread(target=work, daemon=True).start()

    def insert_auth_key(self) -> None:
        if not self.need_remote():
            return
        dialog = AuthKeyDialog(self)
        if not dialog.result:
            return
        key = dialog.result
        home = self.session.home or "/"
        ssh_dir = posix_join(home, ".ssh")
        auth = posix_join(ssh_dir, "authorized_keys")

        def work(job_id: str) -> None:
            self.session.ensure_dir(ssh_dir)
            existing = ""
            try:
                existing = self.session.read_file(auth).decode("utf-8", errors="replace")
            except Exception:
                existing = ""
            if key in existing:
                return
            text = existing
            if text and not text.endswith("\n"):
                text += "\n"
            text += key + "\n"
            self.session.write_file(auth, text.encode("utf-8"))
            try:
                self.session.chmod(ssh_dir, 0o700)
                self.session.chmod(auth, 0o600)
            except Exception:
                pass

        def done() -> None:
            self.set_status("Ключ добавлен в ~/.ssh/authorized_keys")
            self._term_write("Ключ добавлен в ~/.ssh/authorized_keys\n", "ok")

        self._enqueue("·", "authorized_keys", work, on_done=done)

    def show_remote_ssh(self) -> None:
        self.run_command("ls -la ~/.ssh; echo '-----'; cat ~/.ssh/authorized_keys 2>/dev/null || true", use_cwd=False)

    def generate_server_key(self) -> None:
        if not self.need_remote():
            return
        if not messagebox.askyesno(
            APP_TITLE,
            "Сгенерировать ed25519-ключ на сервере (~/.ssh/id_ed25519),\nесли его ещё нет?",
            parent=self,
        ):
            return
        self.run_command(
            'mkdir -p ~/.ssh && chmod 700 ~/.ssh; '
            'if [ -f ~/.ssh/id_ed25519 ]; then echo "Ключ уже есть:"; '
            "else ssh-keygen -t ed25519 -N '' -f ~/.ssh/id_ed25519; fi; "
            "echo '-----'; cat ~/.ssh/id_ed25519.pub",
            use_cwd=False,
        )

    def copy_server_pubkey(self) -> None:
        if not self.need_remote():
            return

        def work() -> None:
            code, out, err = self.session.exec_command(
                "cat ~/.ssh/id_ed25519.pub 2>/dev/null || cat ~/.ssh/id_rsa.pub 2>/dev/null"
            )
            key = (out or "").strip()
            if code != 0 or not key:
                self.after(
                    0,
                    lambda: messagebox.showinfo(
                        APP_TITLE, "Публичный ключ на сервере не найден. Сначала сгенерируйте его.", parent=self
                    ),
                )
                return

            def apply() -> None:
                self.clipboard_clear()
                self.clipboard_append(key)
                self.set_status("Публичный ключ сервера скопирован")
                self._term_write(key + "\n", "ok")

            self.after(0, apply)

        threading.Thread(target=work, daemon=True).start()

    def unpack_selected(self, side: str) -> None:
        pane = self.local if side == "local" else self.remote
        items = pane.selected()
        if len(items) != 1 or items[0].is_dir or not is_archive(items[0].name):
            messagebox.showinfo(APP_TITLE, "Выберите архив (zip, tar.gz, 7z, rar…)", parent=self)
            return
        item = items[0]
        if side == "local":
            def work() -> None:
                try:
                    unpack_local(item.path)
                except Exception as exc:
                    self.after(0, lambda error=pretty_error(exc): messagebox.showerror(APP_TITLE, error, parent=self))
                    return
                self.after(0, self.local.refresh)
                self.after(0, lambda: self.set_status(f"Распаковано: {item.name}"))

            threading.Thread(target=work, daemon=True).start()
            return
        if not self.need_remote():
            return
        script = remote_unpack_script(item.path)

        def work(job_id: str) -> None:
            cancel = lambda: job_id in self.cancelled
            code, out, err = self.session.exec_command(
                script,
                timeout=300,
                retry_on_disconnect=True,
                status=lambda text: self._ui_status(job_id, text),
                cancel=cancel,
                operation_id=job_id,
            )
            if code != 0:
                raise RuntimeError((err or out or "не удалось распаковать").strip())

        self._enqueue("·", f"распаковать {item.name}", work, on_done=self.remote.refresh)

    def upload_local_paths(self, paths: list[str], dest_dir: str) -> None:
        entries: list[FileEntry] = []
        for raw in paths:
            path = os.path.abspath(raw)
            if not os.path.exists(path):
                continue
            entries.append(
                FileEntry(
                    name=os.path.basename(path.rstrip("\\/")) or path,
                    path=path,
                    is_dir=os.path.isdir(path),
                    size=os.path.getsize(path) if os.path.isfile(path) else 0,
                    mtime=0,
                )
            )
        if entries:
            self.transfer("upload", items=entries, dest_dir=dest_dir)

    def move_entries(self, side: str, items: list[FileEntry], dest_dir: str) -> None:
        if side == "local":
            if not dest_dir:
                return
            errors: list[str] = []
            for item in items:
                dest = os.path.join(dest_dir, item.name)
                if os.path.abspath(item.path) == os.path.abspath(dest):
                    continue
                if item.is_dir and _is_inside_local(item.path, dest_dir):
                    errors.append(f"{item.name}: нельзя перенести папку саму в себя")
                    continue
                try:
                    shutil.move(item.path, dest)
                except OSError as exc:
                    errors.append(f"{item.name}: {pretty_error(exc)}")
            if errors:
                messagebox.showerror(APP_TITLE, "\n".join(errors), parent=self)
            self.local.refresh()
            return
        if not self.need_remote():
            return

        def work(job_id: str) -> None:
            for item in items:
                dest = posix_join(dest_dir, item.name)
                if item.path.rstrip("/") == dest.rstrip("/"):
                    continue
                if item.is_dir and _is_inside_remote(item.path, dest_dir):
                    raise RuntimeError(f"Нельзя перенести «{item.name}» само в себя")
                self.session.rename(item.path, dest)

        names = ", ".join(item.name for item in items[:3])
        extra = "" if len(items) <= 3 else f" +{len(items) - 3}"
        self._enqueue("·", f"перенос {names}{extra}", work, on_done=self.remote.refresh)

    def _copy_os_to_local(self, paths: list[str], dest_dir: str) -> None:
        if not dest_dir:
            messagebox.showwarning(APP_TITLE, "Сначала откройте локальную папку", parent=self)
            return
        errors: list[str] = []
        for path in paths:
            name = os.path.basename(path.rstrip("\\/")) or path
            dest = os.path.join(dest_dir, name)
            try:
                if os.path.isdir(path):
                    shutil.copytree(path, dest, dirs_exist_ok=True)
                elif os.path.isfile(path):
                    shutil.copy2(path, dest)
            except OSError as exc:
                errors.append(f"{name}: {pretty_error(exc)}")
        if errors:
            messagebox.showerror(APP_TITLE, "\n".join(errors), parent=self)
        self.local.refresh()

    def _setup_dnd(self) -> None:
        if not HAS_DND:
            return
        for pane in (self.local, self.remote):
            try:
                pane.drop_target_register(DND_FILES)
                pane.dnd_bind("<<Drop>>", lambda event, p=pane: self._on_os_drop(event, p))
                pane.tree.drop_target_register(DND_FILES)
                pane.tree.dnd_bind("<<Drop>>", lambda event, p=pane: self._on_os_drop(event, p))
            except (tk.TclError, AttributeError):
                pass

    def _parse_drop_files(self, data: str) -> list[str]:
        try:
            parts = self.tk.splitlist(data)
        except tk.TclError:
            parts = [data]
        out: list[str] = []
        for part in parts:
            path = str(part).strip().strip("{}")
            if path.startswith("file:///"):
                path = path[8:]
                if os.name == "nt" and path.startswith("/"):
                    path = path[1:]
            if path:
                out.append(path)
        return out

    def _on_os_drop(self, event, pane: FilePane) -> None:
        now = time.monotonic()
        if now - self._last_drop_ts < 0.25:
            return
        self._last_drop_ts = now
        paths = self._parse_drop_files(getattr(event, "data", "") or "")
        if not paths:
            return
        dest_dir = pane.path
        try:
            tx = event.x_root - pane.tree.winfo_rootx()
            ty = event.y_root - pane.tree.winfo_rooty()
            hit = pane.hit(tx, ty)
            if hit and hit.is_dir:
                dest_dir = hit.path
        except tk.TclError:
            pass
        if pane.kind == "remote":
            self.upload_local_paths(paths, dest_dir or "/")
        else:
            self._copy_os_to_local(paths, dest_dir)

    def pane_at(self, x_root: int, y_root: int) -> FilePane | None:
        widget = self.winfo_containing(x_root, y_root)
        while widget is not None:
            if widget is self.local or widget is self.local.tree:
                return self.local
            if widget is self.remote or widget is self.remote.tree:
                return self.remote
            widget = getattr(widget, "master", None)
        return None

    def note_drag_press(self, pane: FilePane, event) -> None:
        if self._drag["active"]:
            return
        self._drag["press"] = (pane, event.x_root, event.y_root)

    def _on_drag_motion(self, event) -> str | None:
        press = self._drag.get("press")
        if not self._drag["active"]:
            if not press:
                return None
            if abs(event.x_root - press[1]) + abs(event.y_root - press[2]) < 8:
                return None
            pane: FilePane = press[0]
            items = pane.selected()
            if not items:
                self._drag["press"] = None
                return None
            self._start_internal_drag(pane.kind, items, event)
        self._update_internal_drag(event)
        return None

    def _start_internal_drag(self, source: str, items: list[FileEntry], event) -> None:
        self._drag.update({"active": True, "source": source, "items": items, "hover": None})
        text = items[0].name if len(items) == 1 else f"{len(items)} объектов"
        ghost = tk.Toplevel(self)
        ghost.overrideredirect(True)
        ghost.attributes("-topmost", True)
        try:
            ghost.attributes("-alpha", 0.88)
        except tk.TclError:
            pass
        tk.Label(
            ghost,
            text=f"  {text}  ",
            bg=C["accent"],
            fg="#ffffff",
            font=FONT_SM,
            padx=8,
            pady=4,
        ).pack()
        ghost.geometry(f"+{event.x_root + 14}+{event.y_root + 14}")
        self._drag["ghost"] = ghost
        self.configure(cursor="plus")

    def _update_internal_drag(self, event) -> None:
        ghost = self._drag.get("ghost")
        if ghost is not None:
            try:
                ghost.geometry(f"+{event.x_root + 14}+{event.y_root + 14}")
            except tk.TclError:
                pass
        pane = self.pane_at(event.x_root, event.y_root)
        self.local.clear_drop_highlight()
        self.remote.clear_drop_highlight()
        if pane is None:
            self._drag["hover"] = None
            return
        tx = event.x_root - pane.tree.winfo_rootx()
        ty = event.y_root - pane.tree.winfo_rooty()
        row = pane.tree.identify_row(ty)
        if row:
            pane.highlight_drop(row)
        self._drag["hover"] = (pane, pane.hit(tx, ty))

    def _on_drag_release(self, event) -> None:
        press = self._drag.get("press")
        active = self._drag["active"]
        self._drag["press"] = None
        if not active:
            return
        source = self._drag.get("source")
        items = list(self._drag.get("items") or [])
        hover = self._drag.get("hover")
        ghost = self._drag.get("ghost")
        self._drag.update({"active": False, "source": None, "items": [], "ghost": None, "hover": None})
        if ghost is not None:
            try:
                ghost.destroy()
            except tk.TclError:
                pass
        self.configure(cursor="")
        self.local.clear_drop_highlight()
        self.remote.clear_drop_highlight()
        dest_pane = hover[0] if hover else self.pane_at(event.x_root, event.y_root)
        dest_entry = hover[1] if hover else None
        if dest_pane is None or not items or not source:
            return
        dest_dir = dest_pane.path
        if dest_entry and dest_entry.is_dir:
            dest_dir = dest_entry.path
        if source == dest_pane.kind:
            if dest_dir == (self.local.path if source == "local" else self.remote.path) and not (
                dest_entry and dest_entry.is_dir and dest_entry.name != ".."
            ):
                return
            self.move_entries(source, items, dest_dir)
            return
        if source == "local" and dest_pane.kind == "remote":
            self.transfer("upload", items=items, dest_dir=dest_dir or "/")
        elif source == "remote" and dest_pane.kind == "local":
            self.transfer("download", items=items, dest_dir=dest_dir)

    def _enqueue(
        self,
        direction: str,
        title: str,
        fn: Callable[..., None],
        on_done: Callable[[], None] | None = None,
    ) -> None:
        if getattr(self, "mcp_busy", False):
            self.set_status("Дождитесь завершения операции MCP")
            return
        if any(
            str(job_id).startswith("editor-")
            for job_id in getattr(self, "active_jobs", ())
        ):
            self.set_status("Дождитесь завершения чтения или сохранения в редакторе")
            return
        job_id = uuid.uuid4().hex
        self.transfers.insert(
            "",
            "end",
            iid=job_id,
            values=(direction, "ожидание", title),
        )
        self.transfers.see(job_id)
        self.job_specs[job_id] = (direction, title, fn, on_done)
        self.active_jobs.add(job_id)
        self.jobs.put((job_id, title, fn, on_done))
        self.set_status(f"В очереди: {title}")

    def _finish_job(self, job_id: str) -> None:
        self.active_jobs.discard(job_id)
        if self.current_job == job_id:
            self.current_job = ""

    def _worker(self) -> None:
        while True:
            item = self.jobs.get()
            if item is None:
                return
            job_id, title, fn, on_done = item
            self.current_job = job_id
            if job_id in self.cancelled:
                self.after(0, lambda j=job_id: self._job_update(j, "—", "отменено"))
                self.after(0, lambda j=job_id: self._finish_job(j))
                continue
            self.after(0, lambda j=job_id: self._job_update(j, "0%", "выполняется"))
            try:
                fn(job_id)
            except Exception as exc:
                if job_id in self.cancelled:
                    self.after(0, lambda j=job_id: self._job_update(j, "—", "отменено"))
                    self.after(0, lambda: self.set_status("Передача отменена"))
                else:
                    err = pretty_error(exc)
                    self.after(0, lambda j=job_id, e=err: self._job_update(j, "ошибка", e))
                    self.after(0, lambda e=err: self.set_status(e))
                self.after(0, lambda j=job_id: self._finish_job(j))
                self.after(0, self._sync_connection_badge)
                continue
            self.job_specs.pop(job_id, None)
            self.cancelled.discard(job_id)
            self.after(0, lambda j=job_id: self._job_update(j, "100%", "готово"))
            self.after(0, lambda: self.progress.configure(value=0))
            self.after(0, lambda j=job_id: self._finish_job(j))
            if on_done:
                self.after(0, on_done)

    def _job_update(self, job_id: str, progress: str, status: str) -> None:
        if not self.transfers.exists(job_id):
            return
        direction = self.transfers.set(job_id, "dir")
        self.transfers.item(job_id, values=(direction, progress, status))

    def _progress(self, job_id: str, done: int, total: int) -> None:
        now = time.monotonic()
        if now - self._prog_ts < 0.05 and done < total:
            return
        self._prog_ts = now
        pct = 0 if not total else int(done * 100 / total)
        text = f"{pct}%  {human_size(done)} / {human_size(total)}"

        def apply() -> None:
            if self.transfers.exists(job_id):
                direction = self.transfers.set(job_id, "dir")
                status = self.transfers.set(job_id, "status")
                self.transfers.item(job_id, values=(direction, text, status or "выполняется"))
            self.progress.configure(maximum=max(total, 1), value=done)

        self.after(0, apply)

    def _ui_status(self, job_id: str, text: str) -> None:
        def apply() -> None:
            progress = self._safe_prog(job_id)
            self._job_update(job_id, progress, text)
            low = text.lower()
            if "переподключение" in low:
                self.conn_dot.configure(text="●  Переподключение…", style="Muted.TLabel")
            elif "соединение восстановлено" in low:
                self._sync_connection_badge()

        self.after(0, apply)

    def _sync_connection_badge(self) -> None:
        if self.session.connected:
            self.conn_dot.configure(text="●  Подключено", style="Ok.TLabel")
            self.conn_info.configure(text=self.session.label)
        else:
            self.conn_dot.configure(text="●  Не подключено", style="Bad.TLabel")
            self.conn_info.configure(text="")
        self._update_cmd_buttons()
        self._update_cwd_label()

    def _safe_prog(self, job_id: str) -> str:
        if self.transfers.exists(job_id):
            return self.transfers.set(job_id, "progress")
        return ""

    def _cancel_current(self) -> None:
        target = self.current_job
        if not target:
            for iid in self.transfers.get_children():
                if iid in self.active_jobs:
                    target = iid
                    break
        if not target:
            return
        self.cancelled.add(target)
        self.session.interrupt_transfers(target)
        if self.transfers.exists(target):
            progress = self.transfers.set(target, "progress")
            if progress == "ожидание":
                self._job_update(target, "—", "отменено")
            else:
                self._job_update(target, progress, "отмена…")
        self.set_status("Отмена передачи…")

    def _continue_selected(self) -> None:
        selected = self.transfers.selection()
        if not selected:
            self.set_status("Выберите отменённую или завершившуюся с ошибкой передачу")
            return
        job_id = selected[0]
        if job_id in self.active_jobs:
            self.set_status("Эта передача уже выполняется или ожидает в очереди")
            return
        spec = self.job_specs.get(job_id)
        if spec is None:
            self.set_status("Для этой передачи продолжение недоступно")
            return
        direction, title, fn, on_done = spec
        self.cancelled.discard(job_id)
        self.transfers.item(job_id, values=(direction, "ожидание", title))
        self.transfers.see(job_id)
        self.active_jobs.add(job_id)
        self.jobs.put((job_id, title, fn, on_done))
        self.set_status(f"Продолжение поставлено в очередь: {title}")

    def _clear_transfers(self) -> None:
        for iid in self.transfers.get_children():
            if iid not in self.active_jobs:
                self.transfers.delete(iid)
                self.job_specs.pop(iid, None)
                self.cancelled.discard(iid)
        for job_id in tuple(self.job_specs):
            if job_id not in self.active_jobs:
                self.job_specs.pop(job_id, None)
                self.cancelled.discard(job_id)

    def set_status(self, text: str) -> None:
        self.status_var.set(text)

    def update_status(self) -> None:
        loc = len(self.local.selected())
        rem = len(self.remote.selected())
        conn = self.session.label if self.session.connected else "нет подключения"
        extra = []
        if loc:
            extra.append(f"локально выбрано: {loc}")
        if rem:
            extra.append(f"на сервере: {rem}")
        bits = [conn] + extra
        self.status_var.set("  ·  ".join(bits))

    def on_close(self) -> None:
        editors = []
        saving = []
        for editor in tuple(getattr(self, "_remote_editors", ())):
            try:
                if editor.winfo_exists():
                    if getattr(editor, "_busy", "") == "save":
                        saving.append(editor)
                    elif editor.dirty or editor.busy:
                        editors.append(editor)
            except tk.TclError:
                pass
        if saving:
            messagebox.showinfo(
                APP_TITLE,
                "Дождитесь завершения сохранения файла на сервере.",
                parent=self,
            )
            return
        if editors and not messagebox.askyesno(
            APP_TITLE,
            f"В {len(editors)} редактор(ах) есть несохранённые изменения "
            "или незавершённые операции.\n\nЗакрыть приложение и прервать их?",
            parent=self,
        ):
            return
        if self.mcp_bridge:
            self.mcp_bridge.close()
        if self._connect_attempt is not None:
            self._connect_attempt.cancel()
            self._connect_attempt = None
        self.remember_paths()
        self.store.window = self.geometry()
        self.store.show_local = bool(self.show_local_var.get())
        self.store.save()
        close_in_background(self.session)
        self.jobs.put(None)
        self.destroy()


def _enable_dpi() -> None:
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            import ctypes

            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def main() -> None:
    _enable_dpi()
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
