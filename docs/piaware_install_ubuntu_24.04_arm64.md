# PiAware Installation on Ubuntu 24.04 (Raspberry Pi 4, ARM64)

## Overview

This guide explains how to install and run PiAware on Ubuntu 24.04 (ARM64) using readsb as the ADS-B data source.

Architecture:
RTL-SDR → readsb → piaware → FlightAware (+ MLAT)

---

## Prerequisites

- Ubuntu 24.04 (ARM64)
- Raspberry Pi 4
- RTL-SDR working
- readsb installed and running

Check:
ss -tlnp | grep 30005

---

## 1. Install Dependencies

apt update
apt install -y tcl tcllib tcl-tls itcl3 tclx build-essential git netcat-openbsd ca-certificates

---

## 2. Install tcllauncher

git clone https://github.com/flightaware/tcllauncher.git
cd tcllauncher
make
make install

---

## 3. Install piaware

git clone https://github.com/flightaware/piaware.git
cd piaware
make install

---

## 4. Create user

useradd -r -M -s /usr/sbin/nologin piaware

---

## 5. Configure sudo

echo "piaware ALL=(ALL) NOPASSWD: ALL" > /etc/sudoers.d/piaware

---

## 6. Directories

mkdir -p /var/cache/piaware
chown -R piaware:piaware /var/cache/piaware

---

## 7. Config

cat <<EOF > /etc/piaware.conf
receiver-type other
receiver-host 127.0.0.1
receiver-port 30005
EOF

---

## 8. Install helpers

wget https://www.flightaware.com/adsb/piaware/files/packages/pool/piaware/p/piaware/piaware_10.2_arm64.deb
dpkg-deb -x piaware_10.2_arm64.deb piaware_pkg

cp piaware_pkg/usr/lib/piaware/helpers/faup1090 /usr/lib/piaware/helpers/
cp piaware_pkg/usr/lib/piaware/helpers/fa-mlat-client /usr/lib/piaware/helpers/
cp -r piaware_pkg/usr/lib/piaware/helpers/lib /usr/lib/piaware/helpers/

chmod +x /usr/lib/piaware/helpers/*

---

## 9. Install Python 3.11 runtime

wget http://deb.debian.org/debian/pool/main/p/python3.11/libpython3.11-minimal_3.11.2-6+deb12u6_arm64.deb
wget http://deb.debian.org/debian/pool/main/p/python3.11/libpython3.11-stdlib_3.11.2-6+deb12u6_arm64.deb
wget http://deb.debian.org/debian/pool/main/p/python3.11/libpython3.11_3.11.2-6+deb12u6_arm64.deb
wget http://deb.debian.org/debian/pool/main/libn/libnsl/libnsl2_1.3.0-2_arm64.deb

dpkg -i libnsl2_1.3.0-2_arm64.deb libpython3.11-minimal_3.11.2-6+deb12u6_arm64.deb libpython3.11-stdlib_3.11.2-6+deb12u6_arm64.deb libpython3.11_3.11.2-6+deb12u6_arm64.deb

---

## 10. Start

systemctl daemon-reload
systemctl enable piaware
systemctl start piaware

---

## 11. Verify

piaware-status

Expected:
- faup1090 running
- fa-mlat-client running
- connected to FlightAware

---

## 12. Claim feeder

https://flightaware.com/adsb/piaware/claim

---

## Notes

- Coordinates must be set via FlightAware website
- MLAT is enabled automatically after claim
