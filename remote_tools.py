"""Шаблоны команд, распаковка архивов и установка git на сервере."""

from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path
from typing import Any, Callable, Iterable

COMMANDS_FILE = Path(__file__).resolve().parent / "commands.json"

ARCHIVE_SUFFIXES = (
    ".tar.gz",
    ".tar.bz2",
    ".tar.xz",
    ".tar.zst",
    ".tgz",
    ".tbz2",
    ".txz",
    ".zip",
    ".tar",
    ".7z",
    ".rar",
    ".gz",
    ".bz2",
    ".xz",
)

def _normalize_command(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    label = str(item.get("label") or "").strip()
    command = str(item.get("command") or "")
    if not label or not command:
        return None
    return {
        "label": label,
        "command": command,
        "insert_only": bool(item.get("insert_only")),
        "confirm": bool(item.get("confirm")),
        "danger": bool(item.get("danger")),
        "hint": str(item.get("hint") or ""),
        "use_cwd": bool(item.get("use_cwd", True)),
    }


def load_commands(path: Path = COMMANDS_FILE) -> dict[str, list[dict[str, Any]]]:
    data: dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            data = {}
    groups = {
        "common": [_normalize_command(x) for x in (data.get("common") or [])],
        "git": [_normalize_command(x) for x in (data.get("git") or [])],
    }
    return {
        "common": [item for item in groups["common"] if item],
        "git": [item for item in groups["git"] if item],
    }


def _write_zip_file(
    archive: zipfile.ZipFile,
    source: Path,
    archive_name: str,
    cancel: Callable[[], bool] | None,
) -> None:
    info = zipfile.ZipInfo.from_file(source, archive_name, strict_timestamps=False)
    info.compress_type = zipfile.ZIP_DEFLATED
    with source.open("rb") as input_file, archive.open(
        info, "w", force_zip64=True
    ) as output_file:
        while True:
            if cancel is not None and cancel():
                raise RuntimeError("Передача отменена")
            chunk = input_file.read(1024 * 1024)
            if not chunk:
                return
            output_file.write(chunk)


def pack_local_zip(
    entries: Iterable[tuple[str, str, bool]],
    zip_path: Path,
    cancel: Callable[[], bool] | None = None,
) -> None:
    """entries: (abs_path, archive_name, is_dir)."""
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for abs_path, name, is_dir in entries:
            if cancel is not None and cancel():
                raise RuntimeError("Передача отменена")
            if is_dir:
                base = Path(abs_path)
                if not base.exists():
                    continue
                wrote = False
                for child in base.rglob("*"):
                    if child.is_dir():
                        continue
                    rel = Path(name) / child.relative_to(base)
                    _write_zip_file(archive, child, rel.as_posix(), cancel)
                    wrote = True
                if not wrote:
                    archive.writestr(f"{name.rstrip('/')}/", "")
            else:
                _write_zip_file(archive, Path(abs_path), name, cancel)


def remote_pack_command(remote_dir: str, names: list[str], archive_name: str) -> str:
    quoted_dir = remote_dir.replace("'", "'\"'\"'")
    quoted_archive = archive_name.replace("'", "'\"'\"'")
    quoted_names = " ".join("'" + name.replace("'", "'\"'\"'") + "'" for name in names)
    return (
        f"set -e; cd '{quoted_dir}'; tar -czf '{quoted_archive}' {quoted_names}; echo packed"
    )

INSTALL_GIT = r"""
set -e
if command -v git >/dev/null 2>&1; then
  git --version
  exit 0
fi
if [ "$(id -u)" -eq 0 ]; then
  S=""
else
  S="sudo -n"
fi
if command -v apt-get >/dev/null 2>&1; then
  $S apt-get update -y
  DEBIAN_FRONTEND=noninteractive $S apt-get install -y git
elif command -v dnf >/dev/null 2>&1; then
  $S dnf install -y git
elif command -v yum >/dev/null 2>&1; then
  $S yum install -y git
elif command -v apk >/dev/null 2>&1; then
  $S apk add --no-cache git
elif command -v pacman >/dev/null 2>&1; then
  $S pacman -Sy --noconfirm git
elif command -v zypper >/dev/null 2>&1; then
  $S zypper --non-interactive install git
else
  echo "Не найден пакетный менеджер. Установите git вручную." >&2
  exit 1
fi
git --version
"""

INSTALL_GIT_SUDO = r"""
sudo -S -p '' /bin/bash -c "set -e
if command -v git >/dev/null 2>&1; then git --version; exit 0; fi
if command -v apt-get >/dev/null 2>&1; then
  apt-get update -y
  DEBIAN_FRONTEND=noninteractive apt-get install -y git
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y git
elif command -v yum >/dev/null 2>&1; then
  yum install -y git
elif command -v apk >/dev/null 2>&1; then
  apk add --no-cache git
elif command -v pacman >/dev/null 2>&1; then
  pacman -Sy --noconfirm git
elif command -v zypper >/dev/null 2>&1; then
  zypper --non-interactive install git
else
  echo 'Не найден пакетный менеджер. Установите git вручную.' >&2
  exit 1
fi
git --version
"
"""


def is_archive(name: str) -> bool:
    lower = (name or "").lower()
    return any(lower.endswith(suffix) for suffix in ARCHIVE_SUFFIXES)


def archive_dest_dir(path: str) -> str:
    return str(Path(path).parent)


def unpack_local(path: str) -> str:
    dest = archive_dest_dir(path)
    lower = path.lower()
    if lower.endswith(".zip"):
        with zipfile.ZipFile(path) as zf:
            zf.extractall(dest)
        return dest
    shutil.unpack_archive(path, dest)
    return dest


def remote_unpack_script(archive_path: str) -> str:
    path = archive_path.replace("'", "'\"'\"'")
    return f"""
set -e
f='{path}'
d=$(dirname "$f")
cd "$d"
base=$(basename "$f")
low=$(printf '%s' "$base" | tr '[:upper:]' '[:lower:]')
unpack_zip() {{
  if command -v unzip >/dev/null 2>&1; then
    unzip -o "$f"
  elif command -v python3 >/dev/null 2>&1; then
    python3 -m zipfile -e "$f" .
  elif command -v python >/dev/null 2>&1; then
    python -m zipfile -e "$f" .
  else
    echo "Нет unzip/python для распаковки zip" >&2
    exit 1
  fi
}}
case "$low" in
  *.tar.gz|*.tgz) tar -xzf "$f" ;;
  *.tar.bz2|*.tbz2) tar -xjf "$f" ;;
  *.tar.xz|*.txz) tar -xJf "$f" ;;
  *.tar.zst) tar --zstd -xf "$f" ;;
  *.tar) tar -xf "$f" ;;
  *.zip) unpack_zip ;;
  *.7z)
    if command -v 7z >/dev/null 2>&1; then 7z x -y "$f"
    elif command -v 7za >/dev/null 2>&1; then 7za x -y "$f"
    else echo "7z не установлен" >&2; exit 1; fi
    ;;
  *.rar)
    if command -v unrar >/dev/null 2>&1; then unrar x -o+ "$f"
    elif command -v unar >/dev/null 2>&1; then unar -f -o . "$f"
    else echo "unrar не установлен" >&2; exit 1; fi
    ;;
  *.gz) gzip -dk "$f" ;;
  *.bz2) bunzip2 -k "$f" ;;
  *.xz) xz -dk "$f" ;;
  *)
    if command -v python3 >/dev/null 2>&1; then
      python3 -c "import shutil,sys; shutil.unpack_archive(sys.argv[1])" "$f"
    else
      echo "Неизвестный тип архива: $base" >&2
      exit 1
    fi
    ;;
esac
echo "Распаковано: $base"
"""


def looks_like_pubkey(text: str) -> bool:
    line = ""
    for raw in (text or "").splitlines():
        candidate = raw.strip()
        if candidate and not candidate.startswith("#"):
            line = candidate
            break
    prefixes = ("ssh-rsa", "ssh-ed25519", "ssh-dss", "ecdsa-sha2-", "sk-ssh-", "sk-ecdsa-")
    return any(line.startswith(prefix) for prefix in prefixes)


def extract_pubkey(text: str) -> str:
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and looks_like_pubkey(line):
            return line
    return (text or "").strip()


def local_pubkeys() -> list[Path]:
    ssh = Path.home() / ".ssh"
    names = ("id_ed25519.pub", "id_rsa.pub", "id_ecdsa.pub", "id_dsa.pub")
    return [ssh / name for name in names if (ssh / name).is_file()]
