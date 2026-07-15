#!/usr/bin/env python3
import asyncio
import os
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent

app = Server("dummy-filesystem")

@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="list_directory",
            description="List files in a directory",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"}
                },
                "required": ["path"]
            }
        )
    ]

@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name != "list_directory":
        raise ValueError(f"Unknown tool: {name}")
    path = arguments.get("path", ".")
    try:
        files = os.listdir(path)
        return [TextContent(type="text", text="\n".join(files))]
    except Exception as e:
        return [TextContent(type="text", text=str(e))]

if __name__ == "__main__":
    async def main():
        async with stdio_server() as (read_stream, write_stream):
            await app.run(read_stream, write_stream, app.create_initialization_options())
    asyncio.run(main())
