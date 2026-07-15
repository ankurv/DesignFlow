# DesignFlow release versioning

DesignFlow uses semantic versions in `x.y.z` form. The repository-root `VERSION`
file is authoritative for the Python server, command-line startup, API metadata,
and release tooling.

- Increment `z` for backward-compatible fixes.
- Increment `y` for backward-compatible features or additive database migrations.
- Increment `x` for incompatible API, project-data, or workflow changes.

The VS Code extension manifest must use the same release number. Automated tests
reject invalid or divergent versions before a release is built.

## Current baseline

`0.1.0` is the first productization baseline. Versions below `1.0.0` may still
evolve quickly, but stored project data must only change through explicit,
forward-tested migrations.
