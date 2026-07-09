#!/usr/bin/env python3
"""
DesignFlow — start the server.
Usage:  python run.py [--port 8000] [--host 0.0.0.0]
"""
import argparse
import uvicorn

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args()

    print(f"\n🚀 DesignFlow running at http://{args.host}:{args.port}\n")
    uvicorn.run(
        "backend.server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
