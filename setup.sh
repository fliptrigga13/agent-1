#!/bin/bash
# setup.sh — run ONCE on the fresh VM as root. Installs the agent as a systemd service.
set -euo pipefail

apt-get update -qq
apt-get install -y -qq python3 sqlite3

id agent >/dev/null 2>&1 || useradd -m -s /bin/bash agent
mkdir -p /home/agent/workspace
cp /tmp/agent-pkg/agent.py /home/agent/agent.py
chown -R agent:agent /home/agent
chmod 700 /home/agent

# env file (owner fills in once)
if [ ! -f /home/agent/.env ]; then
  cat > /home/agent/.env <<'EOF'
GEMINI_API_KEY=PASTE_YOUR_KEY_HERE
TELEGRAM_BOT_TOKEN=PASTE_BOT_TOKEN_HERE
TELEGRAM_OWNER_ID=PASTE_YOUR_TELEGRAM_ID_HERE
GEMINI_MODEL=gemini-3.8-flash
AGENT_WORKDIR=/home/agent/workspace
EOF
  chmod 600 /home/agent/.env
  chown agent:agent /home/agent/.env
fi

cat > /etc/systemd/system/agent-1.service <<'EOF'
[Unit]
Description=Agent-1 personal agent daemon
After=network-online.target
Wants=network-online.target

[Service]
User=agent
WorkingDirectory=/home/agent
EnvironmentFile=/home/agent/.env
ExecStart=/usr/bin/python3 /home/agent/agent.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable agent-1.service
echo "Installed. Fill in /home/agent/.env, then: systemctl start agent-1"
