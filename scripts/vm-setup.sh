#!/usr/bin/env bash
# tw2qq Azure/Oracle VM one-shot setup (Ubuntu 22.04/24.04, amd64 or arm64)
# Usage: curl -sL https://raw.githubusercontent.com/amo-tx/tw2qq-push/main/scripts/vm-setup.sh | bash
set -e

# 1) swap FIRST: free-tier VMs have 1G RAM, docker build needs headroom
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo "/swapfile none swap sw 0 0" >> /etc/fstab
fi

# 2) docker
apt-get update -qq
apt-get install -y -qq docker.io git > /dev/null
systemctl enable --now docker

# 3) code
rm -rf /opt/tw2qq
git clone --depth 1 https://github.com/amo-tx/tw2qq-push.git /opt/tw2qq
mkdir -p /opt/tw2qq/state/data /opt/tw2qq/state/persist/home /opt/tw2qq/state/persist/Napcat

cat > /opt/tw2qq/docker-compose.yml <<'YML'
services:
  tw2qq:
    image: tw2qq:latest
    container_name: tw2qq
    restart: always
    ports: ["8080:8080"]
    environment: [PORT=8080]
    volumes:
      - /opt/tw2qq/state/data:/app/data
      - /opt/tw2qq/state/persist:/app/persist
    logging:
      driver: json-file
      options: {max-size: "20m", max-file: "3"}
YML

# 4) build image (native arch; QQ deb chosen at runtime by napcat_cloud.py)
docker build -f /opt/tw2qq/docker/Dockerfile -t tw2qq:latest /opt/tw2qq > /var/log/tw2qq-build.log 2>&1

# 5) run
docker compose -f /opt/tw2qq/docker-compose.yml up -d

# 6) local firewall (NSG handled outside)
iptables -C INPUT -p tcp --dport 8080 -j ACCEPT 2>/dev/null || iptables -I INPUT 5 -p tcp --dport 8080 -j ACCEPT

echo SETUP_DONE
