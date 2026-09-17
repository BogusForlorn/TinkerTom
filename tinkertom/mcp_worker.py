"""One cancellable MCP tool execution, isolated from the protocol reader."""
import json
from pathlib import Path
import sys

from .mcp import response


if __name__ == "__main__":
    request = json.load(sys.stdin)
    try:
        result = response(request, Path(sys.argv[1]))
    except Exception as exc:
        result = {"jsonrpc": "2.0", "id": request.get("id"), "result": {
            "isError": True, "content": [{"type": "text", "text": str(exc)}]}}
    print(json.dumps(result), flush=True)
