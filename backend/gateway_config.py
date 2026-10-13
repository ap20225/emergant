"""
Gateway configuration utilities for writing dynamic environment variables.

This module handles writing secrets (tokens, API keys) to an environment file
that gets loaded by the supervised gateway wrapper script.
"""

import os
import stat
from pathlib import Path

# Path to the gateway environment file
GATEWAY_ENV_FILE = "/root/.openclaw/gateway.env"
GATEWAY_ENV_DIR = "/root/.openclaw"


def write_gateway_env(token: str, api_key: str = None, provider: str = "emergent") -> None:
    """
    Write secrets to env file before starting gateway.

    This allows the supervisor-managed gateway to load dynamic
    configuration that changes each time the gateway starts.

    Args:
        token: The gateway authentication token
        api_key: Optional API key for the provider
        provider: The provider name ("emergent", "anthropic", or "openai")
    """
    # Ensure directory exists
    os.makedirs(GATEWAY_ENV_DIR, exist_ok=True)

    # Build environment file content
    lines = [
        f'export OPENCLAW_GATEWAY_TOKEN="{token}"',
    ]

    # Add provider-specific API keys
    if api_key:
        if provider == "anthropic":
            lines.append(f'export ANTHROPIC_API_KEY="{api_key}"')
        elif provider == "openai":
            lines.append(f'export OPENAI_API_KEY="{api_key}"')
        # For emergent provider, the API key is in the config file, not env var

    # Write the file
    content = "\n".join(lines) + "\n"

    with open(GATEWAY_ENV_FILE, 'w') as f:
        f.write(content)

    # Set secure permissions (readable only by owner)
    os.chmod(GATEWAY_ENV_FILE, stat.S_IRUSR | stat.S_IWUSR)  # 0o600


def clear_gateway_env() -> None:
    """
    Clear the gateway environment file.

    Called when stopping the gateway to remove sensitive credentials.
    """
    if os.path.exists(GATEWAY_ENV_FILE):
        os.remove(GATEWAY_ENV_FILE)
