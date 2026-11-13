#!/bin/bash
set -e

# Start seekdb
seekdb &
SEEKDB_PID=$!

# Wait for seekdb to be ready
sleep 3

# Start node backend
cd /app/backend
exec node server.js
