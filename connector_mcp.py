"""Stdio MCP adapter; all server work is dispatched to the running desktop app."""
import asyncio
import json
from pathlib import Path
import urllib.request

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from connector_bridge import RUNTIME_FILE

mcp = FastMCP('server-connector', instructions='Controls the actual Server Connector desktop app and its saved profiles. Use get_state and server IDs; never substitute OpenSSH aliases.')
READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)


def call_app(operation, arguments):
    try:
        config = json.loads(RUNTIME_FILE.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f'Server Connector bridge is not running. Start/restart {Path(__file__).resolve().with_name("run.bat")}.') from exc
    # Never use environment HTTP proxies for the authenticated local control endpoint.
    req = urllib.request.Request('http://127.0.0.1:' + str(int(config['port'])) + '/rpc',
        data=json.dumps({'operation': operation, 'arguments': arguments}).encode(),
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + config['token']})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=330) as response:
        return json.load(response)


async def call(operation, **arguments):
    return await asyncio.to_thread(call_app, operation, arguments)


@mcp.tool(annotations=READ)
async def get_state() -> dict:
    """Get actual desktop app PID, saved server IDs/names, active connection and remote folder. No credentials."""
    return await call('state')


@mcp.tool(annotations=WRITE)
async def connect_server(server_id: str) -> dict:
    """Connect a saved profile in the desktop app using its stored credentials, without dialogs. Switches the app connection."""
    return await call('connect', server_id=server_id)


@mcp.tool(annotations=WRITE)
async def disconnect_server(server_id: str) -> dict:
    """Disconnect the specified active server in the desktop app."""
    return await call('disconnect', server_id=server_id)


@mcp.tool(annotations=WRITE)
async def execute(server_id: str, command: str, cwd: str | None = None,
                  timeout_seconds: int = 30, max_output_bytes: int = 24000) -> dict:
    """Run a shell command through the app's active SSH session. cwd defaults to its visible remote folder.
    Timeout 1..300 seconds. Streams bounded separately. No automatic mutation retry.
    Timeout closes the SSH channel but remote process completion is unknown; inspect before retrying.
    """
    return await call('execute', server_id=server_id, command=command, cwd=cwd,
                      timeout_seconds=timeout_seconds, max_output_bytes=max_output_bytes)


@mcp.tool(annotations=READ)
async def list_directory(server_id: str, path: str | None = None) -> dict:
    """List remote directory through the active SFTP session; defaults to visible remote folder."""
    return await call('list_directory', server_id=server_id, path=path)


@mcp.tool(annotations=READ)
async def read_file(server_id: str, path: str) -> dict:
    """Read regular UTF-8 file up to 1 MiB; returns SHA256 and mode. Refuses symlinks."""
    return await call('read_file', server_id=server_id, path=path)


@mcp.tool(annotations=WRITE)
async def write_file(server_id: str, path: str, content: str, expected_sha256: str) -> dict:
    """Write UTF-8 up to 1 MiB with backup and optimistic SHA256 check; use 'missing' for a new file.
    Existing writes require SFTP posix-rename. ACLs/xattrs/hardlinks are not preserved.
    """
    return await call('write_file', server_id=server_id, path=path, content=content, expected_sha256=expected_sha256)


@mcp.tool(annotations=WRITE)
async def upload_file(server_id: str, local_path: str, path: str, overwrite: bool = False) -> dict:
    """Upload one local file using the app's resumable transfer implementation. Explicit overwrite required."""
    return await call('upload_file', server_id=server_id, local_path=local_path, path=path, overwrite=overwrite)


@mcp.tool(annotations=WRITE)
async def download_file(server_id: str, path: str, local_path: str, overwrite: bool = False) -> dict:
    """Download one remote file using the app's resumable transfer implementation. Explicit local overwrite required."""
    return await call('download_file', server_id=server_id, path=path, local_path=local_path, overwrite=overwrite)


if __name__ == '__main__':
    mcp.run(transport='stdio')
