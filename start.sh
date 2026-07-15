#!/bin/bash
# Start the DesignFlow server in the background
cd "$(dirname "$0")"

# Check if already running
if [ -f server.pid ]; then
    if ps -p $(cat server.pid) > /dev/null; then
        echo "Server is already running with PID $(cat server.pid)"
        exit 1
    else
        echo "Found stale server.pid, cleaning it up..."
        rm server.pid
    fi
fi

echo "Starting DesignFlow server..."
nohup python3 run.py "$@" > server.log 2>&1 &
PID=$!
echo $PID > server.pid

echo "Server started successfully with PID $PID"
echo "Logs are being written to server.log"
