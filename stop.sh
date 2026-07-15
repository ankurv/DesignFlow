#!/bin/bash
# Stop the DesignFlow server
cd "$(dirname "$0")"

if [ -f server.pid ]; then
    PID=$(cat server.pid)
    if ps -p $PID > /dev/null; then
        echo "Stopping DesignFlow server (PID: $PID)..."
        kill $PID
        
        # Wait up to 5 seconds for it to exit
        for i in {1..5}; do
            if ! ps -p $PID > /dev/null; then
                break
            fi
            sleep 1
        done
        
        # Force kill if still running
        if ps -p $PID > /dev/null; then
            echo "Server did not stop gracefully. Forcing kill..."
            kill -9 $PID
        fi
        
        echo "Server stopped successfully."
    else
        echo "Server (PID: $PID) is not running."
    fi
    rm server.pid
else
    echo "No server.pid found. Server might not be running in the background."
    # Optional: try to find and kill it anyway if it was started manually
    PIDS=$(pgrep -f "python3 run.py")
    if [ ! -z "$PIDS" ]; then
        echo "Found running processes without pidfile. Killing..."
        pkill -f "python3 run.py"
        echo "Processes stopped."
    fi
fi
