#!/bin/bash
set -e

# Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Create cache folder
mkdir -p ~/.cache/semantic-cache/cache

# Enable and start service
systemctl --user daemon-reload
systemctl --user enable semantic-cache
systemctl --user start semantic-cache

echo "Semantic cache service started!"
echo "Check status: systemctl --user status semantic-cache"
echo "View logs: journalctl --user -u semantic-cache -f"

