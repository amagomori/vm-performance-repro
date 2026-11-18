#!/usr/bin/env bash
set -euo pipefail

FC_VERSION="${FC_VERSION:-v1.18.0}"
FC_CI="${FC_CI:-https://s3.amazonaws.com/spec.ccfc.min/firecracker-ci/20261111-778a26c0594c-0}"
FC_KERNEL="${FC_KERNEL:-vmlinux-6.18.62}"
FC_ROOT="${FC_ROOT:-/srv/fc}"
BLOCKDIFF_REV="${BLOCKDIFF_REV:-8d10a60bfb31a9dd773843395e9ab3d7bb6dff9c}"
HOST_USER="${SUDO_USER:-root}"
ARCH="$(uname -m)"

export DEBIAN_FRONTEND=noninteractive
systemctl stop apt-daily.timer apt-daily-upgrade.timer
systemd-run --quiet --wait --property=After=apt-daily.service --property=After=apt-daily-upgrade.service /bin/true
apt-get update -qq
apt-get -o DPkg::Lock::Timeout=300 install -y -qq xfsprogs squashfs-tools e2fsprogs curl acl python3 cargo docker.io git rsync >/dev/null

usermod -aG kvm "$HOST_USER"
setfacl -m "u:${HOST_USER}:rw" /dev/kvm

cache=/var/lib/fc-cache.xfs
if ! mountpoint -q "$FC_ROOT"; then
  [ -f "$cache" ] || { truncate -s 40G "$cache"; mkfs.xfs -q -m reflink=1 "$cache"; }
  grep -q "$cache" /etc/fstab || echo "$cache $FC_ROOT xfs loop,noatime 0 0" >> /etc/fstab
  mkdir -p "$FC_ROOT" && mount "$FC_ROOT"
fi
mkdir -p "$FC_ROOT"/{base,snapshots,vms}

if ! firecracker --version 2>/dev/null | grep -q "${FC_VERSION#v}" || ! command -v snapshot-editor >/dev/null; then
  tmp="$(mktemp -d)"
  release="firecracker-${FC_VERSION}-${ARCH}"
  url="https://github.com/firecracker-microvm/firecracker/releases/download/${FC_VERSION}/${release}.tgz"
  curl -fsSL --output-dir "$tmp" -O "$url" -O "$url.sha256.txt"
  (cd "$tmp" && sha256sum -c "${release}.tgz.sha256.txt")
  tar -xzf "$tmp/${release}.tgz" -C "$tmp"
  for bin in firecracker snapshot-editor; do
    install -m 0755 "$tmp/release-${FC_VERSION}-${ARCH}/${bin}-${FC_VERSION}-${ARCH}" "/usr/local/bin/$bin"
  done
  rm -rf "$tmp"
fi

cargo install --quiet --locked --root /usr/local --git https://github.com/CognitionAI/blockdiff --rev "$BLOCKDIFF_REV"

base="$FC_ROOT/base"
if [ ! -f "$base/rootfs.ext4" ]; then
  work="$(mktemp -d)"
  curl -fsSL -o "$base/vmlinux" "$FC_CI/$ARCH/$FC_KERNEL"
  curl -fsSL -o "$work/rootfs.squashfs" "$FC_CI/$ARCH/ubuntu-24.04.squashfs"
  unsquashfs -q -d "$work/root" "$work/rootfs.squashfs"
  chown -R root:root "$work/root"
  truncate -s 1G "$base/rootfs.ext4"
  mkfs.ext4 -q -d "$work/root" -F "$base/rootfs.ext4"
  rm -rf "$work"
  echo "$FC_CI/$ARCH/$FC_KERNEL + ubuntu-24.04.squashfs" > "$base/SOURCES"
fi
chown -R "$HOST_USER" "$FC_ROOT"

echo "firecracker: $(firecracker --version | head -1)"
echo "blockdiff: CognitionAI/blockdiff@${BLOCKDIFF_REV:0:7}"
echo "cache: $FC_ROOT on $(findmnt -no SOURCE "$FC_ROOT") (reflink on)"
echo "base: $(cat "$base/SOURCES")"
