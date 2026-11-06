#!/bin/bash
set -e
mkdir -p /app/persist/home /app/persist/Napcat /app/data /app/logs
cd /app
exec python3 webui.py
