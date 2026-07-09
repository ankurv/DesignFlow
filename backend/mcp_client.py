import asyncio
from typing import Any
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.sse import sse_client

class MCPManager:
    """Manages connections to multiple MCP stdio servers and namespaces their tools."""
    
    def __init__(self, configs: list[dict]):
        self.configs = configs
        self.sessions: dict[str, ClientSession] = {}
        self.exits = []

    async def start(self):
        for conf in self.configs:
            cmd = conf["command"]
            try:
                if cmd.startswith("http://") or cmd.startswith("https://"):
                    ctx = sse_client(cmd)
                else:
                    server_params = StdioServerParameters(
                        command=cmd,
                        args=conf.get("args", []),
                        env=conf.get("env", None)
                    )
                    ctx = stdio_client(server_params)
                    
                read, write = await ctx.__aenter__()
                self.exits.append(ctx)
                
                session = ClientSession(read, write)
                await session.__aenter__()
                self.exits.append(session)
                
                await session.initialize()
                self.sessions[conf["name"]] = session
            except Exception as e:
                print(f"Failed to start MCP server {conf['name']}: {e}")

    async def stop(self):
        for ctx in reversed(self.exits):
            try:
                await ctx.__aexit__(None, None, None)
            except Exception:
                pass
        self.exits.clear()
        self.sessions.clear()

    async def list_tools(self) -> list[dict]:
        tools = []
        for name, session in self.sessions.items():
            try:
                result = await session.list_tools()
                for t in result.tools:
                    tools.append({
                        "server": name,
                        "name": f"{name}__{t.name}",
                        "description": t.description or "",
                        "inputSchema": t.inputSchema,
                    })
            except Exception as e:
                print(f"Failed to list tools for {name}: {e}")
        return tools

    async def call_tool(self, tool_name: str, args: dict) -> Any:
        if "__" not in tool_name:
            raise ValueError(f"Invalid tool name format: {tool_name}")
        server_name, actual_tool = tool_name.split("__", 1)
        session = self.sessions.get(server_name)
        if not session:
            raise ValueError(f"MCP Server not found: {server_name}")
        
        result = await session.call_tool(actual_tool, arguments=args)
        if result.isError:
            raise RuntimeError(f"Tool error: {result.content}")
            
        output = []
        for content in result.content:
            if content.type == "text":
                output.append(content.text)
        return "\n".join(output)
