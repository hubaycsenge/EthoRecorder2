#!/usr/bin/env bash
# Start EthoRecorder2.  Extra options are passed on, e.g.  ./run_ubuntu.sh --demo
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
    echo "Not installed yet - run:  bash install_ubuntu.sh"
    exit 1
fi
exec .venv/bin/python -m ethorecorder2 "$@"
