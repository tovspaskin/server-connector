"""Register this project's MCP adapter and install its companion Codex skill."""
from pathlib import Path
import os
import re
import shutil
import subprocess
import sys
import tomllib

root = Path(__file__).resolve().parent
python = root / '.venv/Scripts/python.exe'
if not python.exists():
    raise SystemExit('Run run.bat first to create the project virtual environment')
codex = shutil.which('codex')
if not codex:
    raise SystemExit('Codex CLI was not found in PATH')
subprocess.run([codex, 'mcp', 'add', 'server-connector', '--', str(python), '-u', str(root/'connector_mcp.py')], check=True)
codex_home = Path(os.environ.get('CODEX_HOME') or Path.home()/'.codex')
config = codex_home/'config.toml'
text = config.read_text(encoding='utf-8')
pattern = r'(\[mcp_servers\.(?:"server-connector"|server-connector)\]\s*\n)(.*?)(?=\n\[|\Z)'
match = re.search(pattern, text, re.S)
if not match:
    raise SystemExit('MCP registered, but timeout section was not found; inspect config.toml')
block = re.sub(r'(?m)^tool_timeout_sec\s*=.*\n?', '', match.group(2)).rstrip()+'\ntool_timeout_sec = 330\n'
updated = text[:match.start(2)]+block+text[match.end(2):]
assert tomllib.loads(updated)['mcp_servers']['server-connector']['tool_timeout_sec'] == 330
config.write_text(updated, encoding='utf-8')
skill = codex_home/'skills/server-connector'
skill.mkdir(parents=True, exist_ok=True)
shutil.copy2(root/'skills/server-connector/SKILL.md', skill/'SKILL.md')
print('Server Connector MCP and skill installed from '+str(root))
