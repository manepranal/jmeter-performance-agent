#!/bin/bash
# One-time setup for JMeter Agent

set -e

echo "========================================"
echo "  JMeter Agent - First Time Setup"
echo "========================================"

# 1. Check Python
if ! command -v python3 &>/dev/null; then
  echo "ERROR: Python 3 is required. Install from https://python.org"
  exit 1
fi
echo "Python: $(python3 --version)"

# 2. Install Python dependencies
echo ""
echo "Installing Python dependencies..."
pip3 install -r requirements.txt

# 3. Check/set API key
echo ""
if [ -z "$ANTHROPIC_API_KEY" ]; then
  echo "ANTHROPIC_API_KEY is not set."
  read -p "Enter your Anthropic API key: " key
  echo ""
  echo "Add this to your shell profile (~/.zshrc or ~/.bash_profile):"
  echo "  export ANTHROPIC_API_KEY=$key"
  echo ""
  export ANTHROPIC_API_KEY="$key"
  echo "Key set for this session."
else
  echo "ANTHROPIC_API_KEY: already set"
fi

# 4. Make agent executable
chmod +x jmeter_agent.py

echo ""
echo "========================================"
echo "  Setup complete!"
echo "========================================"
echo ""
echo "To run the agent:"
echo "  python3 jmeter_agent.py"
echo ""
echo "JMeter will be auto-installed on first run."
