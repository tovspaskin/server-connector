"""Windows clipboard helpers for Tkinter (Russian layout safe)."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk


class ClipboardMixin:
    """Expects self to be a tk.Tk / tk.Toplevel (or expose .root)."""

    def _root(self):
        return getattr(self, "root", self)

    def setup_clipboard_shortcuts(self, *widgets: tk.Misc) -> None:
        root = self._root()
        self.context_menu = tk.Menu(root, tearoff=0)
        self.context_menu.add_command(
            label="Копировать (Ctrl+C)", command=self.context_copy
        )
        self.context_menu.add_command(
            label="Вставить (Ctrl+V)", command=self.context_paste
        )
        self.context_menu.add_command(
            label="Вырезать (Ctrl+X)", command=self.context_cut
        )
        self.context_menu.add_separator()
        self.context_menu.add_command(
            label="Выделить всё (Ctrl+A)", command=self.context_select_all
        )

        if not widgets:
            editable: list[tk.Misc] = []
            pending = list(root.winfo_children())
            while pending:
                widget = pending.pop()
                pending.extend(widget.winfo_children())
                if isinstance(widget, (tk.Entry, ttk.Entry, tk.Text)):
                    editable.append(widget)
            widgets = tuple(editable)

        def on_paste(event):
            event.widget.event_generate("<<Paste>>")
            return "break"

        def on_copy(event):
            event.widget.event_generate("<<Copy>>")
            return "break"

        def on_cut(event):
            event.widget.event_generate("<<Cut>>")
            return "break"

        def on_select_all(event):
            w = event.widget
            if isinstance(w, (tk.Entry, ttk.Entry)):
                w.select_range(0, tk.END)
                w.icursor(tk.END)
            elif isinstance(w, tk.Text):
                w.tag_add(tk.SEL, "1.0", tk.END)
                w.mark_set(tk.INSERT, "1.0")
            return "break"

        def universal_key_handler(event):
            if not (event.state & 0x4):
                return
            if event.keycode == 86:
                return on_paste(event)
            if event.keycode == 67:
                return on_copy(event)
            if event.keycode == 88:
                return on_cut(event)
            if event.keycode == 65:
                return on_select_all(event)

        for w in widgets:
            w.bind("<Button-3>", self.show_context_menu, add="+")
            w.bind("<KeyPress>", universal_key_handler, add="+")
            w.bind("<Control-v>", on_paste)
            w.bind("<Control-V>", on_paste)
            w.bind("<Control-c>", on_copy)
            w.bind("<Control-C>", on_copy)
            w.bind("<Control-x>", on_cut)
            w.bind("<Control-X>", on_cut)
            w.bind("<Control-a>", on_select_all)
            w.bind("<Control-A>", on_select_all)
            w.bind("<Shift-Insert>", on_paste)

    def show_context_menu(self, event):
        event.widget.focus_set()
        try:
            self.context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.context_menu.grab_release()

    def context_copy(self):
        w = self._root().focus_get()
        if w:
            w.event_generate("<<Copy>>")

    def context_paste(self):
        w = self._root().focus_get()
        if w:
            w.event_generate("<<Paste>>")

    def context_cut(self):
        w = self._root().focus_get()
        if w:
            w.event_generate("<<Cut>>")

    def context_select_all(self):
        w = self._root().focus_get()
        if isinstance(w, tk.Text):
            w.tag_add(tk.SEL, "1.0", tk.END)
        elif isinstance(w, (tk.Entry, ttk.Entry)):
            w.select_range(0, tk.END)
            w.icursor(tk.END)
