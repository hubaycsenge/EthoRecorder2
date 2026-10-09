#!/usr/bin/env bash
# EthoRecorder2 - network tuning for GigE cameras on Ubuntu (run once per PC):
#     sudo setup/ubuntu_network_tuning.sh
#
# Raises the kernel's network receive buffer.  The Ubuntu default (208 kB) is
# smaller than a single camera image, so under load packets get lost and have
# to be re-sent.  This is the setting Basler recommends for pylon.
set -e
if [ "$(id -u)" -ne 0 ]; then
    echo "Please run with sudo:  sudo $0"
    exit 1
fi
cat > /etc/sysctl.d/90-ethorecorder2-gige.conf <<'EOF'
# Larger receive buffers for GigE Vision cameras (EthoRecorder2 / Basler pylon)
net.core.rmem_max = 33554432
net.core.rmem_default = 33554432
EOF
sysctl --system >/dev/null
echo "Receive buffer is now $(sysctl -n net.core.rmem_max) bytes (kept after reboot)."
