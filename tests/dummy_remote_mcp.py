import asyncio
import secrets
from contextlib import asynccontextmanager

import uvicorn
from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.authentication import (
    AuthenticationBackend, AuthenticationError, SimpleUser, AuthCredentials
)
import base64

from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import Tool, TextContent

mcp_server = Server("dummy-remote")

@mcp_server.list_tools()
async def handle_list_tools() -> list[Tool]:
    return [
        Tool(
            name="remote_ping",
            description="Ping the remote server",
            inputSchema={
                "type": "object",
                "properties": {},
                "required": []
            }
        )
    ]

@mcp_server.call_tool()
async def handle_call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "remote_ping":
        return [TextContent(type="text", text="Remote pong from auth server!")]
    raise ValueError(f"Unknown tool: {name}")

class BasicAuthBackend(AuthenticationBackend):
    async def authenticate(self, request):
        if "Authorization" not in request.headers:
            return None
        auth = request.headers["Authorization"]
        try:
            scheme, credentials = auth.split()
            if scheme.lower() != 'basic':
                return None
            decoded = base64.b64decode(credentials).decode("ascii")
            username, _, password = decoded.partition(":")
            if username == "admin" and password == "password":
                return AuthCredentials(["authenticated"]), SimpleUser(username)
        except Exception:
            raise AuthenticationError("Invalid basic auth credentials")
        raise AuthenticationError("Invalid basic auth credentials")

sse = SseServerTransport("/messages")

async def handle_sse(request: Request):
    if not request.user.is_authenticated:
        return JSONResponse({"error": "Unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Basic"})
    async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
        await mcp_server.run(streams[0], streams[1], mcp_server.create_initialization_options())

async def handle_messages(request: Request):
    if not request.user.is_authenticated:
        return JSONResponse({"error": "Unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Basic"})
    await sse.handle_post_message(request.scope, request.receive, request._send)

app = Starlette(
    routes=[
        Route("/sse", endpoint=handle_sse),
        Route("/messages", endpoint=handle_messages, methods=["POST"]),
    ]
)
app.add_middleware(AuthenticationMiddleware, backend=BasicAuthBackend())

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8001)
