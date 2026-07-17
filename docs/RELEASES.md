# DesignFlow release versioning

DesignFlow uses semantic versions in `x.y.z` form. The repository-root `VERSION`
file is authoritative for the Python server, command-line startup, API metadata,
and release tooling.

- Increment `z` for backward-compatible fixes.
- Increment `y` for backward-compatible features or additive database migrations.
- Increment `x` for incompatible API, project-data, or workflow changes.

The VS Code extension manifest must use the same release number. Automated tests
reject invalid or divergent versions before a release is built.

## Unreleased

- Added a standard Streamable HTTP MCP endpoint at `/mcp/` for coding agents.
- Added scoped project-status, artifact, implementation-context, validation, and recent-activity tools.
- Added constrained implementation-report write-back backed by the project SQLite database.
- Added localhost-by-default MCP access, admin-managed one-time token generation, immediate regeneration/revocation, and optional `DESIGNFLOW_MCP_TOKEN` authentication.
- Moved third-party MCP server configuration endpoints to `/mcp/servers`.

## Current baseline

`0.1.0` is the first productization baseline. Versions below `1.0.0` may still
evolve quickly, but stored project data must only change through explicit,
forward-tested migrations.
