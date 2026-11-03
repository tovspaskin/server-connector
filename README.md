# Server Connector

Windows-приложение для SSH/SFTP с MCP-адаптером и связанным навыком Codex. MCP использует сеанс открытого приложения: выбор сервера и состояние подключения общие для интерфейса и агента.

## Установка

Нужны Windows, Python 3.11 или новее с Tkinter и установленный Codex CLI.

```powershell
git clone https://github.com/tovspaskin/server-connector.git
cd server-connector
.\run.bat
```

`run.bat` создаёт локальную `.venv`, устанавливает зависимости из `requirements.txt` и запускает приложение.

В другом окне PowerShell, в той же папке, зарегистрируйте MCP и установите навык:

```powershell
.\.venv\Scripts\python.exe install_mcp.py
```

Установщик определяет путь к этой копии автоматически. В каталоге Codex (`CODEX_HOME` или `~/.codex`) он обновляет регистрацию `server-connector`, её таймаут и [связанный навык](skills/server-connector/SKILL.md). После установки перезапустите чат Codex и вызовите `get_state`.

## Состав

В репозитории девять модулей приложения и MCP, `requirements.txt`, `run.bat`, `install_mcp.py` и один навык `server-connector`. Приложение должно быть запущено для выполнения MCP-операций.

MCP предоставляет `get_state`, `connect_server`, `disconnect_server`, `execute`, `list_directory`, `read_file`, `write_file`, `upload_file` и `download_file`. Для серверных операций используется явный `server_id` из `get_state`.

## Локальные данные

Профили подключений и служебный токен моста хранятся в `%APPDATA%/ServerConnector/` и не входят в репозиторий. На другом компьютере профили создаются в приложении.

`.gitignore` разрешает публикацию только перечисленных исходников, навыка и служебных файлов репозитория. Пользовательский `commands.json`, окружение Python, тестовые результаты, кэши, ключи, профили и резервные копии остаются локально. MCP работает без `commands.json`.
