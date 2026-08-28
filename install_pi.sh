#!/usr/bin/env bash
# install_pi.sh - set up the label watcher on Raspberry Pi OS (Bookworm).
# Run with: sudo bash install_pi.sh
set -euo pipefail

DEST=/opt/mplabel
RUN_USER=${SUDO_USER:-pi}
HOME_DIR=$(getent passwd "$RUN_USER" | cut -d: -f6)
DATA_DIR="$HOME_DIR/marketplace"

echo "==> installing for user $RUN_USER, data in $DATA_DIR"

apt-get update
# cups + printer-driver-* covers most USB label printers.
# libopenjp2-7 and libjpeg are Pillow's runtime deps on Bookworm.
apt-get install -y python3 python3-venv python3-pip \
    cups cups-client usbutils \
    libopenjp2-7 libjpeg62-turbo zlib1g

echo "==> adding $RUN_USER to lp and lpadmin"
usermod -aG lp,lpadmin "$RUN_USER"

install -d -o "$RUN_USER" -g "$RUN_USER" "$DEST" "$DATA_DIR/labels"
cp -r src pyproject.toml requirements.txt "$DEST"/
chown -R "$RUN_USER":"$RUN_USER" "$DEST"

echo "==> python venv"
sudo -u "$RUN_USER" python3 -m venv "$DEST/venv"
sudo -u "$RUN_USER" "$DEST/venv/bin/pip" install --upgrade pip
# All four have prebuilt aarch64 wheels, so no compiling on the Pi.
sudo -u "$RUN_USER" "$DEST/venv/bin/pip" install "$DEST[sheets]"

if [ ! -f /etc/mplabel.conf ]; then
    cp mplabel.conf.example /etc/mplabel.conf
    sed -i "s|/home/pi/marketplace|$DATA_DIR|" /etc/mplabel.conf
    chown "$RUN_USER":"$RUN_USER" /etc/mplabel.conf
    chmod 600 /etc/mplabel.conf
    echo "==> wrote /etc/mplabel.conf - EDIT IT before starting the service"
fi

sed -e "s|^User=.*|User=$RUN_USER|" \
    -e "s|^WorkingDirectory=.*|WorkingDirectory=$DEST|" \
    -e "s|^ExecStart=.*|ExecStart=$DEST/venv/bin/python -m mplabel run --loop|" \
    -e "s|^ReadWritePaths=.*|ReadWritePaths=$DATA_DIR|" \
    -e "s|^ProtectHome=.*|ProtectHome=false|" \
    systemd/mplabel.service > /etc/systemd/system/mplabel.service
systemctl daemon-reload

# The raw tspl backend needs usblp, which CUPS unbinds when it claims a
# printer. Load it at boot and keep CUPS off the label printer.
if ! grep -q '^usblp' /etc/modules 2>/dev/null; then
    echo "usblp" >> /etc/modules
fi
modprobe usblp 2>/dev/null || true

cp udev/99-clabel-g4.rules /etc/udev/rules.d/
udevadm control --reload-rules && udevadm trigger || true

cat <<EOF

==> done. Next:

  1. Log out and back in, so the lp group membership takes effect.
  2. Plug in the G4 and turn it on, then:
                                     $DEST/venv/bin/python -m mplabel probe
     Look for "speaks TSPL" and note the /dev/usb/lpN node.
  3. Tiny text-only test print:      $DEST/venv/bin/python -m mplabel selftest
     This is a few dozen bytes. If it prints, the language is right.
  4. Edit /etc/mplabel.conf          (IMAP credentials; device node if not lp0)
  5. Dry run, no printing:           $DEST/venv/bin/python -m mplabel check
  6. Real label:                     $DEST/venv/bin/python -m mplabel test-print
  7. Start it:                       sudo systemctl enable --now mplabel
  8. Watch it:                       journalctl -u mplabel -f

EOF
