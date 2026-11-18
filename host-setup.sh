#!/usr/bin/env bash
set -euo pipefail

FC_VERSION="${FC_VERSION:-v1.18.0}"
FC_ROOT="${FC_ROOT:-/srv/fc}"
FC_DEVICE="${FC_DEVICE:-}"
FC_LOOP_SIZE="${FC_LOOP_SIZE:-40G}"
HOST_USER="${SUDO_USER:-${HOST_USER:-root}}"
ARCH="$(uname -m)"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq xfsprogs squashfs-tools e2fsprogs jq curl acl python3 >/dev/null

usermod -aG kvm "$HOST_USER"
setfacl -m "u:${HOST_USER}:rw" /dev/kvm

if ! mountpoint -q "$FC_ROOT"; then
  mkdir -p "$FC_ROOT"
  if [ -z "$FC_DEVICE" ]; then
    FC_DEVICE=/var/lib/fc-cache.xfs
    [ -f "$FC_DEVICE" ] || { truncate -s "$FC_LOOP_SIZE" "$FC_DEVICE"; mkfs.xfs -q -m reflink=1 "$FC_DEVICE"; }
    grep -q "$FC_DEVICE" /etc/fstab || echo "$FC_DEVICE $FC_ROOT xfs loop,noatime 0 0" >> /etc/fstab
  else
    blkid "$FC_DEVICE" >/dev/null 2>&1 || mkfs.xfs -q -m reflink=1 "$FC_DEVICE"
    grep -q "$FC_DEVICE" /etc/fstab || echo "$FC_DEVICE $FC_ROOT xfs noatime 0 0" >> /etc/fstab
  fi
  mount "$FC_ROOT"
fi
xfs_info "$FC_ROOT" | grep -q "reflink=1" || { echo "reflink is off on $FC_ROOT"; exit 1; }
mkdir -p "$FC_ROOT"/{base,snapshots,vms}
chown -R "$HOST_USER" "$FC_ROOT"

if ! command -v firecracker >/dev/null || ! firecracker --version | grep -q "${FC_VERSION#v}"; then
  tmp="$(mktemp -d)"
  url="https://github.com/firecracker-microvm/firecracker/releases/download/${FC_VERSION}/firecracker-${FC_VERSION}-${ARCH}.tgz"
  curl -fsSL -o "$tmp/fc.tgz" "$url"
  curl -fsSL -o "$tmp/fc.tgz.sha256.txt" "$url.sha256.txt"
  (cd "$tmp" && sed "s|firecracker-${FC_VERSION}-${ARCH}.tgz|fc.tgz|" fc.tgz.sha256.txt | sha256sum -c -)
  tar -xzf "$tmp/fc.tgz" -C "$tmp"
  install -m 0755 "$tmp/release-${FC_VERSION}-${ARCH}/firecracker-${FC_VERSION}-${ARCH}" /usr/local/bin/firecracker
  install -m 0755 "$tmp/release-${FC_VERSION}-${ARCH}/jailer-${FC_VERSION}-${ARCH}" /usr/local/bin/jailer
  rm -rf "$tmp"
fi

base="$FC_ROOT/base"
if [ ! -f "$base/rootfs.ext4" ]; then
  S3="https://s3.amazonaws.com/spec.ccfc.min"
  prefix=$(curl -fsSL "$S3?list-type=2&prefix=firecracker-ci/&delimiter=/" \
    | grep -oP "(?<=<Prefix>)firecracker-ci/[0-9]{8}-[^/]+/(?=</Prefix>)" | sort | tail -1)
  kernel_key=$(curl -fsSL "$S3?list-type=2&prefix=${prefix}${ARCH}/vmlinux-" \
    | grep -oP "(?<=<Key>)(${prefix}${ARCH}/vmlinux-[0-9]+\.[0-9]+\.[0-9]{1,3})(?=</Key>)" | sort -V | tail -1)
  ubuntu_key=$(curl -fsSL "$S3?list-type=2&prefix=${prefix}${ARCH}/ubuntu-" \
    | grep -oP "(?<=<Key>)(${prefix}${ARCH}/ubuntu-[0-9]+\.[0-9]+\.squashfs)(?=</Key>)" | sort -V | tail -1)
  curl -fsSL -o "$base/vmlinux" "$S3/$kernel_key"
  work="$(mktemp -d)"
  curl -fsSL -o "$work/rootfs.squashfs" "$S3/$ubuntu_key"
  unsquashfs -q -d "$work/root" "$work/rootfs.squashfs"
  chown -R root:root "$work/root"
  truncate -s 1G "$base/rootfs.ext4"
  mkfs.ext4 -q -d "$work/root" -F "$base/rootfs.ext4"
  rm -rf "$work"
  printf '%s\n%s\n' "$kernel_key" "$ubuntu_key" > "$base/SOURCES"
  chown -R "$HOST_USER" "$base"
fi

echo "firecracker: $(firecracker --version | head -1)"
echo "cache: $FC_ROOT on $(findmnt -no SOURCE "$FC_ROOT") (reflink on)"
echo "base: $(cat "$base/SOURCES" | tr '\n' ' ')"
