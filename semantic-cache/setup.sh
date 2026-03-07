#!/bin/bash
# Setup script for Semantic Cache user-level systemd service

set -e

echo "=== Semantic Cache User Service Setup ==="
echo ""

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Step 1: Create cache directory
echo -e "${GREEN}[1/4]${NC} Creating cache directory..."
mkdir -p /home/bozz/.cache/semantic-cache/cache
echo "✓ Cache directory created: /home/bozz/.cache/semantic-cache/cache"
echo ""

# Step 2: Copy service file
echo -e "${GREEN}[2/4]${NC} Installing user-level systemd service..."
mkdir -p ~/.config/systemd/user
cp semantic-cache.service ~/.config/systemd/user/semantic-cache.service
echo "✓ Service file installed: ~/.config/systemd/user/semantic-cache.service"
echo ""

# Step 3: Reload systemd daemon
echo -e "${GREEN}[3/4]${NC} Reloading systemd daemon..."
systemctl --user daemon-reload
echo "✓ Systemd daemon reloaded"
echo ""

# Step 4: Enable and start service
echo -e "${GREEN}[4/4]${NC} Enabling and starting service..."
systemctl --user enable semantic-cache.service
systemctl --user start semantic-cache.service
echo "✓ Service enabled and started"
echo ""

# Wait a moment for service to start
sleep 5

# Check service status
echo ""
echo "=== Service Status ==="
systemctl --user status semantic-cache.service --no-pager
echo ""

# Show logs
echo ""
echo "=== Recent Logs ==="
journalctl --user -u semantic-cache.service -n 20 --no-pager
echo ""

echo -e "${GREEN}=== Setup Complete! ===${NC}"
echo ""
echo "Useful commands:"
echo "  - Check status:   systemctl --user status semantic-cache"
echo "  - View logs:      systemctl --user logs semantic-cache -f"
echo "  - Stop service:   systemctl --user stop semantic-cache"
echo "  - Restart service: systemctl --user restart semantic-cache"
echo "  - Disable service: systemctl --user disable semantic-cache"
echo ""
echo "To verify the service is working, check the health endpoint:"
echo "  curl http://127.0.0.1:7437/health"
echo ""
