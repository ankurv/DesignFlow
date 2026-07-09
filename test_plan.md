# DesignFlow Test Plan

This document outlines the testing strategies and instructions for verifying the various components of the DesignFlow system.

## 1. MCP Server System

### Overview
DesignFlow supports the Model Context Protocol (MCP) to allow agents to interact with external tools and systems. We must ensure that the MCP client can properly connect to both stdio and HTTP/SSE MCP servers, parse their tools, and allow Claude agents to invoke them.

### Testing a Local FastMCP Server (stdio)

A simple test server is provided in `test_mcp_server.py`. It uses the `FastMCP` framework to expose a `list_directory` tool.

**Steps to test:**
1. Start the DesignFlow application and open the UI in your browser.
2. Navigate to the **MCP Servers** tab in the top navigation bar.
3. In the "Add New MCP Server" section, configure the test server with the following details:
   - **Name**: `test-file-server`
   - **Command**: `python3`
   - **Arguments**: `test_mcp_server.py`
   - **Env Vars**: *(leave empty)*
4. Click **Add Server** to save the configuration.
5. Navigate to the **Live Feed** tab and submit a prompt to the coordinator agent, such as: *"List the contents of the current directory using your external tools."*
6. Verify that the agent successfully invokes the `test__list_directory` tool and returns the correct list of files from your current working directory.

### Testing an HTTP/SSE MCP Server
1. In the **MCP Servers** tab, enter a valid Server-Sent Events (SSE) endpoint URL directly into the **Command** field (e.g., `http://localhost:8000/sse`).
2. Verify that the system correctly routes this as an `sse_client` connection rather than a local subprocess, and that the tools are loaded correctly.
