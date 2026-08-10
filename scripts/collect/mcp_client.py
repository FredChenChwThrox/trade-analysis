"""kimi-datasource MCP stdio 直调客户端（采集辅助，非入库路径）。

用法：
    uv run python scripts/collect/mcp_client.py <tool_name> '<json_arguments>'

在 stdout 输出 tools/call 的 result（JSON）。失败时退出码非 0。
本脚本只搬运原始响应，不做任何加工（skills/stock-collect 约定）。
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading

PLUGIN_DIR = "/Users/fred_chen/.kimi-code/plugins/managed/kimi-datasource"


class McpClient:
    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            ["node", "bin/kimi-datasource.mjs"],
            cwd=PLUGIN_DIR,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self._lock = threading.Lock()
        self._id = 0

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _read_response(self, req_id: int, timeout_s: float = 120.0) -> dict:
        result: dict = {}

        def _reader() -> None:
            assert self.proc.stdout is not None
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("id") == req_id:
                    result.update(obj)
                    return

        t = threading.Thread(target=_reader, daemon=True)
        t.start()
        t.join(timeout_s)
        if not result:
            raise TimeoutError(f"no response for id={req_id} within {timeout_s}s")
        return result

    def _send(self, payload: dict) -> None:
        assert self.proc.stdin is not None
        with self._lock:
            self.proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()

    def initialize(self) -> None:
        req_id = self._next_id()
        self._send(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "stock-collect", "version": "0.1"},
                },
            }
        )
        resp = self._read_response(req_id)
        if "error" in resp:
            raise RuntimeError(f"initialize failed: {resp['error']}")
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call_tool(self, name: str, arguments: dict) -> dict:
        req_id = self._next_id()
        self._send(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        )
        resp = self._read_response(req_id)
        if "error" in resp:
            raise RuntimeError(f"tools/call error: {resp['error']}")
        return resp["result"]

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
        except Exception:
            pass

    def __enter__(self) -> "McpClient":
        self.initialize()
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def extract_text(result: dict) -> str:
    """tools/call result -> 首个 text content。"""
    for item in result.get("content", []):
        if item.get("type") == "text":
            return item.get("text", "")
    raise RuntimeError(f"no text content in result: {json.dumps(result)[:500]}")


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: mcp_client.py <tool_name> '<json_arguments>'", file=sys.stderr)
        return 2
    tool, args_json = sys.argv[1], sys.argv[2]
    with McpClient() as client:
        result = client.call_tool(tool, json.loads(args_json))
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
