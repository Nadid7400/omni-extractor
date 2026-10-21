#!/usr/bin/env bash
set -e

echo "=========================================================="
echo " OmniExtractor — Oracle Cloud Automated Installer "
echo "=========================================================="

# Update and install Docker + dependencies
echo ">>> Installing Docker, Docker Compose, and tools..."
sudo apt-get update -y
sudo apt-get install -y --no-install-recommends docker.io docker-compose-v2 curl git ca-certificates iptables-persistent

# Start and enable Docker
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER" || true

# Open local Ubuntu firewall for port 8000, 80, 443
echo ">>> Configuring VM firewall for incoming traffic..."
sudo iptables -I INPUT 1 -p tcp --dport 8000 -j ACCEPT
sudo iptables -I INPUT 1 -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 1 -p tcp --dport 443 -j ACCEPT

# Save iptables rules so they persist across reboots
if command -v netfilter-persistent >/dev/null 2>&1; then
    sudo netfilter-persistent save || true
fi

# Clone repo if not already inside omni-extractor directory
if [ ! -f "docker-compose.yml" ]; then
    echo ">>> Cloning OmniExtractor repository..."
    git clone https://github.com/Nadid7400/omni-extractor.git
    cd omni-extractor
fi

# Build and start OmniExtractor
echo ">>> Building and starting OmniExtractor container..."
sudo docker compose down 2>/dev/null || true
sudo docker compose up -d --build

# Detect public IP
PUBLIC_IP=$(curl -s https://api.ipify.org || curl -s ifconfig.me || echo "YOUR_SERVER_IP")

echo ""
echo "=========================================================="
echo " [SUCCESS] OmniExtractor is now running 24/7 on Oracle Cloud!"
echo " URL: http://${PUBLIC_IP}:8000"
echo "=========================================================="
echo ""
echo "IMPORTANT: In Oracle Cloud Console, make sure you added an"
echo "Ingress Rule for port 8000 in your Virtual Cloud Network (VCN)"
echo "Security List so external visitors can access it."
echo ""
