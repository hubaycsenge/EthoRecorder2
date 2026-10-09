#!/usr/bin/env bash
# EthoRecorder2 - one-time installation on Ubuntu.  Run:  bash install_ubuntu.sh
set -e
cd "$(dirname "$0")"

PY=python3
if ! $PY -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
    echo "Python 3.10 or newer is required (sudo apt install python3 python3-venv)."
    exit 1
fi

# System libraries needed by the window toolkit and the sound library.
missing=""
dpkg -s libportaudio2 >/dev/null 2>&1 || missing="$missing libportaudio2"
dpkg -s libxcb-cursor0 >/dev/null 2>&1 || missing="$missing libxcb-cursor0"
$PY -c 'import venv, ensurepip' 2>/dev/null || missing="$missing python3-venv"
if [ -n "$missing" ]; then
    echo "Installing system packages:$missing (asks for your password)"
    sudo apt-get install -y $missing
fi

[ -d .venv ] || $PY -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
chmod +x run_ubuntu.sh setup/ubuntu_network_tuning.sh

echo
echo "Installed.  Recommended once per PC:  sudo setup/ubuntu_network_tuning.sh"
echo "Start the program with:               ./run_ubuntu.sh"
