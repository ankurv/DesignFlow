#!/usr/bin/env python3
"""
DesignFlow — start the server.
Usage:  python run.py [--port 8000] [--host 0.0.0.0] [--debug-observer]
"""
import argparse
import os
import uvicorn

from backend.server import app
from backend.audit import audit_log
from backend.version import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    parser.add_argument("--version", action="version", version=f"DesignFlow {__version__}")
    parser.add_argument(
        "--debug-observer", action="store_true",
        help="passively record redacted workflow diagnostics under .designflow/debug",
    )
    parser.add_argument(
        "--log-prompts", action="store_true",
        help="Dump all raw LLM prompts and responses to the console",
    )
    return parser

if __name__ == "__main__":
    args = build_parser().parse_args()
    app.state.debug_observer_enabled = args.debug_observer
    
    if args.log_prompts:
        os.environ["DESIGNFLOW_LOG_PROMPTS"] = "1"

    print(f"\n🚀 DesignFlow {__version__} running at http://{args.host}:{args.port}\n")
    if args.debug_observer:
        print("🔎 Debug observer enabled; diagnostics will be stored per project in .designflow/debug\n")
        audit_log.record(
            action="debug_observer.enable", target="server", result="success",
            username="system", role="system", metadata={"port": args.port},
        )
    config = uvicorn.Config(
        app,
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
    server = uvicorn.Server(config)
    app.state.request_shutdown = lambda: setattr(server, "should_exit", True)
    server.run()
