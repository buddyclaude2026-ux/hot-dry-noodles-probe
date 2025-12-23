#!/bin/bash

# Configuration
SERVER_IP="185.186.147.118"
SERVER_USER="root"
REMOTE_DIR="~/services/heihei"
EXCLUDE_LIST="--exclude=__pycache__ --exclude=.git --exclude=.idea --exclude=venv --exclude=.DS_Store --exclude=*.tar.gz"

echo "🚀 Starting Deployment to $SERVER_IP..."

# 1. Package Files
echo "📦 Packaging files..."
tar $EXCLUDE_LIST -czf release.tar.gz .

# 2. Upload to Server
echo "Pw Uploading release.tar.gz..."
scp release.tar.gz $SERVER_USER@$SERVER_IP:$REMOTE_DIR/

# 3. Clean up Local Package
rm release.tar.gz

# 4. Remote Unzip and Restart
echo "🔄 Updating Server..."
ssh $SERVER_USER@$SERVER_IP << EOF
    cd $REMOTE_DIR
    # Backup config just in case (optional, database is persistent)
    
    # Extract (overwrite files)
    tar -xzf release.tar.gz
    rm release.tar.gz
    
    # Rebuild and Restart
    docker compose up -d --build
    
    # Cleanup unused images
    docker image prune -f
EOF

echo "✅ Deployment Complete!"
