#!/bin/bash
# Moltbot Dependencies Installation Script
# Installs Node.js and openclaw to /root for persistence across restarts

set -e

LOGFILE="/tmp/moltbot_deps.log"
LOCKFILE="/tmp/moltbot_deps.lock"
NODE_DIR="/root/nodejs"
OPENCLAW_DIR="/root/.openclaw-bin"
NODE_VERSION="22.22.0"

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') - $1" | tee -a "$LOGFILE"
}

# Check if already running
if [ -f "$LOCKFILE" ]; then
    log "Another installation is already in progress"
    exit 0
fi

trap "rm -f $LOCKFILE" EXIT
touch "$LOCKFILE"

log "Starting Moltbot dependencies check..."

# Ensure PATH includes our custom directories
export PATH="$NODE_DIR/bin:$OPENCLAW_DIR:$PATH"

# Add to .bashrc if not already there
if ! grep -q "NODE_DIR=/root/nodejs" /root/.bashrc 2>/dev/null; then
    cat >> /root/.bashrc << 'EOF'

# Moltbot dependencies paths
export NODE_DIR=/root/nodejs
export OPENCLAW_DIR=/root/.openclaw-bin
export PATH="$NODE_DIR/bin:$OPENCLAW_DIR:$PATH"
EOF
    log "Added paths to .bashrc"
fi

# Check if Node.js is installed in persistent location
if [ -f "$NODE_DIR/bin/node" ]; then
    CURRENT_VERSION=$("$NODE_DIR/bin/node" -v 2>/dev/null | cut -d'v' -f2 || echo "0")
    log "Node.js found at $NODE_DIR: v$CURRENT_VERSION"
else
    log "Node.js not found in $NODE_DIR. Installing..."
    
    # Download and extract Node.js binary
    mkdir -p "$NODE_DIR"
    cd /tmp
    
    # Detect architecture
    ARCH=$(uname -m)
    if [ "$ARCH" = "x86_64" ]; then
        NODE_ARCH="x64"
    elif [ "$ARCH" = "aarch64" ]; then
        NODE_ARCH="arm64"
    else
        NODE_ARCH="x64"
    fi
    
    NODE_TARBALL="node-v${NODE_VERSION}-linux-${NODE_ARCH}.tar.xz"
    NODE_URL="https://nodejs.org/dist/v${NODE_VERSION}/${NODE_TARBALL}"
    
    log "Downloading Node.js v${NODE_VERSION} for ${NODE_ARCH}..."
    curl -fsSL "$NODE_URL" -o "$NODE_TARBALL"
    
    log "Extracting Node.js..."
    tar -xJf "$NODE_TARBALL"
    cp -r "node-v${NODE_VERSION}-linux-${NODE_ARCH}"/* "$NODE_DIR/"
    rm -rf "$NODE_TARBALL" "node-v${NODE_VERSION}-linux-${NODE_ARCH}"
    
    log "Node.js installed: $("$NODE_DIR/bin/node" -v)"
fi

# Update PATH for npm operations
export PATH="$NODE_DIR/bin:$PATH"

# Check if openclaw is installed in persistent location
if [ -f "$OPENCLAW_DIR/openclaw" ]; then
    log "OpenClaw found at $OPENCLAW_DIR: $("$OPENCLAW_DIR/openclaw" --version 2>/dev/null || echo 'unknown')"
else
    log "OpenClaw not found in $OPENCLAW_DIR. Installing..."
    
    mkdir -p "$OPENCLAW_DIR"
    
    # Install openclaw globally using our Node.js
    log "Installing openclaw via npm..."
    "$NODE_DIR/bin/npm" install -g openclaw@latest 2>&1 | tee -a "$LOGFILE" || true
    
    # The npm global bin is in $NODE_DIR/bin, create symlink
    if [ -f "$NODE_DIR/bin/openclaw" ]; then
        ln -sf "$NODE_DIR/bin/openclaw" "$OPENCLAW_DIR/openclaw"
        log "OpenClaw symlinked to $OPENCLAW_DIR"
    fi
    
    # Verify
    if [ -f "$OPENCLAW_DIR/openclaw" ] || [ -f "$NODE_DIR/bin/openclaw" ]; then
        VERSION=$("$NODE_DIR/bin/openclaw" --version 2>/dev/null || echo 'installed')
        log "OpenClaw installed: $VERSION"
    else
        log "WARNING: OpenClaw installation may have issues, trying alternative method..."
        # Try using the installer script but capture the binary
        curl -fsSL https://molt.bot/install.sh -o /tmp/install_moltbot.sh
        chmod +x /tmp/install_moltbot.sh
        
        # Run installer (it will install to /usr/bin)
        bash /tmp/install_moltbot.sh || true
        
        # Copy to our persistent location if it exists
        if [ -f "/usr/bin/openclaw" ]; then
            cp /usr/bin/openclaw "$OPENCLAW_DIR/openclaw"
            chmod +x "$OPENCLAW_DIR/openclaw"
            log "OpenClaw copied to persistent location"
        fi
    fi
fi

# Create a wrapper script that the backend can use
cat > /root/run_openclaw.sh << 'WRAPPER'
#!/bin/bash
export NODE_DIR=/root/nodejs
export OPENCLAW_DIR=/root/.openclaw-bin
export PATH="$NODE_DIR/bin:$OPENCLAW_DIR:$PATH"

# Find openclaw
if [ -f "$OPENCLAW_DIR/openclaw" ]; then
    exec "$OPENCLAW_DIR/openclaw" "$@"
elif [ -f "$NODE_DIR/bin/openclaw" ]; then
    exec "$NODE_DIR/bin/openclaw" "$@"
elif command -v openclaw &> /dev/null; then
    exec openclaw "$@"
else
    echo "ERROR: openclaw not found" >&2
    exit 1
fi
WRAPPER
chmod +x /root/run_openclaw.sh

log "Created wrapper script at /root/run_openclaw.sh"
log "Moltbot dependencies check complete!"

# Print final status
echo ""
echo "=== Installation Summary ==="
echo "Node.js: $("$NODE_DIR/bin/node" -v 2>/dev/null || echo 'not found')"
echo "npm: $("$NODE_DIR/bin/npm" -v 2>/dev/null || echo 'not found')"
echo "openclaw: $(/root/run_openclaw.sh --version 2>/dev/null || echo 'not found')"
echo "Paths added to /root/.bashrc"
echo ""
