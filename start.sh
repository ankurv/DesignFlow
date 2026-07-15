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

# Check if --port is provided in the arguments
if [[ "$*" != *"--port"* ]]; then
    ARGS="--port 8010 $@"
else
    ARGS="$@"
fi

echo "Starting DesignFlow server with arguments: $ARGS"
nohup python3 run.py $ARGS > server.log 2>&1 &
PID=$!
echo $PID > server.pid

echo "Server started successfully with PID $PID"
echo "Logs are being written to server.log"
