#!/bin/bash
# HeiHei Probe Agent Installer

echo "Checking environment..."

# 1. Install Python3 & Pip if missing
if ! command -v python3 &> /dev/null; then
    echo "Installing Python3..."
    if command -v apt-get &> /dev/null; then
        apt-get update && apt-get install -y python3 python3-pip
    elif command -v yum &> /dev/null; then
        yum install -y python3 python3-pip
    fi
fi

# 2. Install Ping (Critical for monitoring)
if ! command -v ping &> /dev/null; then
    echo "Installing Ping..."
    if command -v apt-get &> /dev/null; then
        apt-get update && apt-get install -y iputils-ping
    elif command -v yum &> /dev/null; then
        yum install -y iputils
    fi
fi

# 3. Install Python Dependencies
echo "Installing dependencies..."
pip3 install requests psutil

# 4. Download and Run Agent
SERVER_URL="" # Injected or passed via args? The shell command usually pipes curl.
# Actually the one-liner is: bash <(curl -sL host/install_agent.sh) --name ...
# So this script is executed directly.
# We need to fetch agent.py. 
# Since we don't know the server URL inside the script easily unless injected,
# we rely on the fact that the USER puts the hostname in the one-liner command 
# BUT wait, the one-liner in admin.html is: bash <(curl -sL ${host}/install_agent.sh)
# Logic: We can try to deduce the server URL from where we downloaded this script if we use curl -J? No.
# Simpler: main.py `get_install_sh_content` doesn't currently inject the host.
# Let's rely on the admin.html generating a slightly smarter one-liner OR 
# Update `get_install_sh_content` in main.py to Inject the host URL.

# Let's assume for now this script is just a wrapper for dependencies, 
# and the agent download happens via a separate command OR we inject the URL.

# Re-reading admin.html one-liner:
# cmdShell = `bash <(curl -sL ${host}/install_agent.sh) --name "${name}" --country "${cc}"`;
# This passes --name and --country to THIS script.
# So this script needs to run agent.py.
# Accessing the SERVER HOST is tricky if not injected.
# Best approach: Modify main.py to INJECT the server URL into this script before serving.

# Let's write a placeholder variable for injection
SERVER_HOST="{{SERVER_HOST}}"
SERVER_URL="http://${SERVER_HOST}/api/v1/report"
TOKEN="{{AGENT_TOKEN}}"

# Download agent.py
curl -sL "http://${SERVER_HOST}/agent.py" > agent.py

# Run it
python3 agent.py --server "$SERVER_URL" --token "$TOKEN" "$@"
