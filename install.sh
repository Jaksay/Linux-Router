#!/usr/bin/env bash
set -Eeuo pipefail

REPO_URL="${LINUX_ROUTER_REPO_URL:-https://github.com/Jaksay/Linux-Router.git}"
BRANCH="${LINUX_ROUTER_BRANCH:-main}"
ARCHIVE_URL="${LINUX_ROUTER_ARCHIVE_URL:-}"
INSTALL_DIR="${LINUX_ROUTER_INSTALL_DIR:-/opt/linux-router}"
DATA_DIR="${LINUX_ROUTER_DATA_DIR:-/var/lib/linux-router}"
INSTALL_STATE_DIR="$DATA_DIR/.linux-router-installer"
SERVICE_NAME="router-panel.service"
AGENT_SERVICE_NAME="router-panel-agent.service"
SERVICE_USER="router-panel"
CONFIGURE_NETWORK=1
NETWORK_APPLY_MODE="auto"
ACTION=""
PURGE_DATA=0
NEW_PASSWORD=""
INITIAL_PASSWORD="${LINUX_ROUTER_INITIAL_PASSWORD:-}"
WORK_DIR=""
STAGING_DIR=""
BACKUP_DIR=""
ROLLBACK_DIR=""
INSTALL_STARTED=0
INSTALL_COMMITTED=0
INSTALL_DIR_EXISTED=0
INSTALL_SWAP_STARTED=0
DATA_DIR_EXISTED=0
SERVICE_USER_CREATED=0
SERVICE_GROUP_CREATED=0
NETWORK_APPLIED=0
NETPLAN_GENERATED=0
DATA_TOUCHED=0
ORIGINAL_IP_FORWARD=""
NETWORKMANAGER_WAS_ACTIVE=0
NETWORKMANAGER_WAS_ENABLED=0
DHCPCD_WAS_ACTIVE=0
DHCPCD_WAS_ENABLED=0
WEB_WAS_ACTIVE=0
WEB_WAS_ENABLED=0
AGENT_WAS_ACTIVE=0
AGENT_WAS_ENABLED=0

log() {
  printf '[linux-router] %s\n' "$*"
}

die() {
  printf '[linux-router] ERROR: %s\n' "$*" >&2
  exit 1
}

service_is_active() {
  systemctl is-active --quiet "$1" 2>/dev/null
}

service_is_enabled() {
  systemctl is-enabled --quiet "$1" 2>/dev/null
}

backup_file() {
  local path="$1"
  local name="$2"
  if [[ -e "$path" ]]; then
    cp -a "$path" "$ROLLBACK_DIR/$name"
  else
    : > "$ROLLBACK_DIR/$name.missing"
  fi
}

restore_file() {
  local path="$1"
  local name="$2"
  if [[ -e "$ROLLBACK_DIR/$name" ]]; then
    install -d -m 0755 "$(dirname "$path")"
    cp -a "$ROLLBACK_DIR/$name" "$path"
  elif [[ -e "$ROLLBACK_DIR/$name.missing" ]]; then
    rm -f "$path"
  fi
}

persist_install_state() {
  local name
  local state_tmp="${INSTALL_STATE_DIR}.new.$$"
  local state_previous="${INSTALL_STATE_DIR}.previous"
  rm -rf "$state_tmp" "$state_previous"
  install -d -o root -g root -m 0700 "$state_tmp"
  printf 'version=1\ndhcpcd_was_active=%s\ndhcpcd_was_enabled=%s\nip_forward=%s\n' \
    "$DHCPCD_WAS_ACTIVE" "$DHCPCD_WAS_ENABLED" "$ORIGINAL_IP_FORWARD" \
    > "$state_tmp/state.base"
  for name in networkmanager.conf netplan.yaml sysctl.conf; do
    if [[ -e "$ROLLBACK_DIR/$name" ]]; then
      cp -a "$ROLLBACK_DIR/$name" "$state_tmp/$name"
    else
      : > "$state_tmp/$name.missing"
    fi
  done
  {
    cat "$state_tmp/state.base"
    printf 'complete=1\n'
  } > "$state_tmp/state.new"
  chmod 0600 "$state_tmp/state.new"
  mv "$state_tmp/state.new" "$state_tmp/state"
  rm -f "$state_tmp/state.base"
  if [[ -e "$INSTALL_STATE_DIR" ]]; then
    mv "$INSTALL_STATE_DIR" "$state_previous"
  fi
  mv "$state_tmp" "$INSTALL_STATE_DIR"
  rm -rf "$state_previous"
}

state_file_is_valid() {
  local directory="$1"
  [[ -f "$directory/state" ]] || return 1
  grep -q '^version=1$' "$directory/state" || return 1
  if grep -q '^complete=' "$directory/state"; then
    grep -q '^complete=1$' "$directory/state"
  fi
}

state_value() {
  local key="$1"
  [[ -f "$INSTALL_STATE_DIR/state" ]] || return 1
  awk -F= -v key="$key" '$1 == key { print substr($0, index($0, "=") + 1); exit }' \
    "$INSTALL_STATE_DIR/state"
}

restore_persisted_file() {
  local path="$1"
  local name="$2"
  if [[ -e "$INSTALL_STATE_DIR/$name" ]]; then
    install -d -m 0755 "$(dirname "$path")"
    cp -a "$INSTALL_STATE_DIR/$name" "$path"
    return 0
  fi
  if [[ -e "$INSTALL_STATE_DIR/$name.missing" ]]; then
    rm -f "$path"
    return 0
  fi
  return 1
}

restore_service_state() {
  local service_name="$1"
  local was_enabled="$2"
  local was_active="$3"
  if [[ "$was_enabled" -eq 1 ]]; then
    systemctl enable "$service_name" >/dev/null 2>&1 || true
  else
    systemctl disable "$service_name" >/dev/null 2>&1 || true
  fi
  if [[ "$was_active" -eq 1 ]]; then
    systemctl restart "$service_name" >/dev/null 2>&1 || true
  else
    systemctl stop "$service_name" >/dev/null 2>&1 || true
  fi
}

rollback_install() {
  log "Installation failed; restoring the previous system state"
  systemctl stop "$SERVICE_NAME" >/dev/null 2>&1 || true
  systemctl stop "$AGENT_SERVICE_NAME" >/dev/null 2>&1 || true

  if [[ "$INSTALL_SWAP_STARTED" -eq 1 ]]; then
    if [[ "$INSTALL_DIR_EXISTED" -eq 1 && -d "$BACKUP_DIR" ]]; then
      rm -rf "$INSTALL_DIR"
      mv "$BACKUP_DIR" "$INSTALL_DIR"
    elif [[ "$INSTALL_DIR_EXISTED" -eq 0 ]]; then
      rm -rf "$INSTALL_DIR"
    fi
  fi

  if [[ "$DATA_TOUCHED" -eq 1 ]]; then
    if [[ "$DATA_DIR_EXISTED" -eq 1 && -d "$ROLLBACK_DIR/data-dir" ]]; then
      rm -rf "$DATA_DIR"
      cp -a "$ROLLBACK_DIR/data-dir" "$DATA_DIR"
    else
      rm -rf "$DATA_DIR"
    fi
  fi

  restore_file "/etc/systemd/system/$SERVICE_NAME" "web.service"
  restore_file "/etc/systemd/system/$AGENT_SERVICE_NAME" "agent.service"
  if [[ "$ACTION" == "install" ]]; then
    restore_file "/etc/sysctl.d/90-router-panel.conf" "sysctl.conf"
    restore_file "/etc/NetworkManager/conf.d/90-linux-router.conf" "networkmanager.conf"
    restore_file "/etc/netplan/90-linux-router.yaml" "netplan.yaml"
  fi

  systemctl daemon-reload >/dev/null 2>&1 || true
  if [[ "$ACTION" == "install" ]]; then
    if [[ "$NETPLAN_GENERATED" -eq 1 ]]; then
      command -v netplan >/dev/null 2>&1 && netplan generate >/dev/null 2>&1 || true
    fi
    if [[ "$NETWORK_APPLIED" -eq 1 ]]; then
      command -v netplan >/dev/null 2>&1 && netplan apply >/dev/null 2>&1 || true
    fi
    if [[ -n "$ORIGINAL_IP_FORWARD" ]]; then
      sysctl -w "net.ipv4.ip_forward=$ORIGINAL_IP_FORWARD" >/dev/null 2>&1 || true
    fi
    restore_service_state "NetworkManager.service" "$NETWORKMANAGER_WAS_ENABLED" "$NETWORKMANAGER_WAS_ACTIVE"
    restore_service_state "dhcpcd.service" "$DHCPCD_WAS_ENABLED" "$DHCPCD_WAS_ACTIVE"
  fi
  restore_service_state "$AGENT_SERVICE_NAME" "$AGENT_WAS_ENABLED" "$AGENT_WAS_ACTIVE"
  restore_service_state "$SERVICE_NAME" "$WEB_WAS_ENABLED" "$WEB_WAS_ACTIVE"

  if [[ "$SERVICE_USER_CREATED" -eq 1 ]]; then
    userdel "$SERVICE_USER" >/dev/null 2>&1 || true
  fi
  if [[ "$SERVICE_GROUP_CREATED" -eq 1 ]]; then
    groupdel "$SERVICE_USER" >/dev/null 2>&1 || true
  fi
}

install_service_units() {
  sed \
    -e "s|@INSTALL_DIR@|$INSTALL_DIR|g" \
    -e "s|@DATA_DIR@|$DATA_DIR|g" \
    "$INSTALL_DIR/router-panel-agent.service" > "$WORK_DIR/$AGENT_SERVICE_NAME"
  sed \
    -e "s|@INSTALL_DIR@|$INSTALL_DIR|g" \
    -e "s|@DATA_DIR@|$DATA_DIR|g" \
    "$INSTALL_DIR/router-panel.service" > "$WORK_DIR/$SERVICE_NAME"
  systemd-analyze verify "$WORK_DIR/$AGENT_SERVICE_NAME" "$WORK_DIR/$SERVICE_NAME"
  install -o root -g root -m 0644 "$WORK_DIR/$AGENT_SERVICE_NAME" "/etc/systemd/system/$AGENT_SERVICE_NAME"
  install -o root -g root -m 0644 "$WORK_DIR/$SERVICE_NAME" "/etc/systemd/system/$SERVICE_NAME"

  systemctl daemon-reload
  systemctl enable "$AGENT_SERVICE_NAME" >/dev/null
  systemctl enable "$SERVICE_NAME" >/dev/null
  systemctl restart "$AGENT_SERVICE_NAME"
  systemctl restart "$SERVICE_NAME"

  log "Checking service health"
  if ! curl --fail --silent --retry 10 --retry-delay 1 --retry-connrefused \
    http://127.0.0.1/healthz >/dev/null 2>&1; then
    systemctl --no-pager --full status "$SERVICE_NAME" || true
    systemctl --no-pager --full status "$AGENT_SERVICE_NAME" || true
    die "service health check failed"
  fi
}

should_apply_network_now() {
  if [[ "$NETWORK_APPLY_MODE" == "now" ]]; then
    return 0
  fi
  if [[ "$NETWORK_APPLY_MODE" == "defer" ]]; then
    return 1
  fi
  [[ -z "${SSH_CONNECTION:-}" ]]
}

uninstall_router_panel() {
  local interface_path
  local netplan_changed=0
  local networkmanager_changed=0
  local has_install_state=0
  local original_dhcpcd_active=0
  local original_dhcpcd_enabled=0
  local original_ip_forward=""
  log "Uninstalling Linux Router"

  if ! state_file_is_valid "$INSTALL_STATE_DIR" && state_file_is_valid "${INSTALL_STATE_DIR}.previous"; then
    INSTALL_STATE_DIR="${INSTALL_STATE_DIR}.previous"
  fi
  if state_file_is_valid "$INSTALL_STATE_DIR"; then
    has_install_state=1
    original_dhcpcd_active="$(state_value dhcpcd_was_active || printf '0')"
    original_dhcpcd_enabled="$(state_value dhcpcd_was_enabled || printf '0')"
    original_ip_forward="$(state_value ip_forward || true)"
    [[ "$original_dhcpcd_active" =~ ^[01]$ ]] || original_dhcpcd_active=0
    [[ "$original_dhcpcd_enabled" =~ ^[01]$ ]] || original_dhcpcd_enabled=0
  fi

  if command -v nmcli >/dev/null 2>&1; then
    nmcli connection down id DebianRouterHotspot >/dev/null 2>&1 || true
    nmcli connection delete id DebianRouterHotspot >/dev/null 2>&1 || true
  fi
  if command -v iw >/dev/null 2>&1; then
    for interface_path in /sys/class/net/ap-*; do
      [[ -e "$interface_path" ]] || continue
      iw dev "$(basename "$interface_path")" del >/dev/null 2>&1 || true
    done
  fi

  systemctl disable --now "$SERVICE_NAME" >/dev/null 2>&1 || true
  systemctl disable --now "$AGENT_SERVICE_NAME" >/dev/null 2>&1 || true
  rm -f "/etc/systemd/system/$SERVICE_NAME"
  rm -f "/etc/systemd/system/$AGENT_SERVICE_NAME"
  systemctl daemon-reload
  systemctl reset-failed "$SERVICE_NAME" "$AGENT_SERVICE_NAME" >/dev/null 2>&1 || true

  rm -f /etc/sysctl.d/90-router-panel.conf
  if [[ "$has_install_state" -eq 1 ]]; then
    if restore_persisted_file "/etc/NetworkManager/conf.d/90-linux-router.conf" "networkmanager.conf"; then
      networkmanager_changed=1
    fi
    if restore_persisted_file "/etc/netplan/90-linux-router.yaml" "netplan.yaml"; then
      netplan_changed=1
      networkmanager_changed=1
    fi
    if ! restore_persisted_file "/etc/sysctl.d/90-router-panel.conf" "sysctl.conf"; then
      log "WARNING: original sysctl configuration backup is missing"
    fi
  else
    if [[ -e /etc/NetworkManager/conf.d/90-linux-router.conf ]]; then
      rm -f /etc/NetworkManager/conf.d/90-linux-router.conf
      networkmanager_changed=1
    fi
    if [[ -e /etc/netplan/90-linux-router.yaml ]]; then
      rm -f /etc/netplan/90-linux-router.yaml
      netplan_changed=1
      networkmanager_changed=1
    fi
  fi

  if [[ "$networkmanager_changed" -eq 1 ]] && should_apply_network_now; then
    if [[ "$netplan_changed" -eq 1 ]] && command -v netplan >/dev/null 2>&1; then
      if ! netplan generate || ! netplan apply; then
        log "WARNING: netplan configuration removal could not be applied automatically"
      fi
    fi
    systemctl restart NetworkManager.service >/dev/null 2>&1 || true
  elif [[ "$networkmanager_changed" -eq 1 ]]; then
    log "Network configuration removal was not applied; apply netplan and restart NetworkManager during a maintenance window"
  fi

  if [[ "$has_install_state" -eq 1 ]] && should_apply_network_now; then
    if [[ -n "$original_ip_forward" ]]; then
      sysctl -w "net.ipv4.ip_forward=$original_ip_forward" >/dev/null 2>&1 || \
        log "WARNING: could not restore net.ipv4.ip_forward"
    fi
    restore_service_state dhcpcd.service "$original_dhcpcd_enabled" "$original_dhcpcd_active"
    rm -rf "$DATA_DIR/.linux-router-installer" "$DATA_DIR/.linux-router-installer.previous"
  elif [[ "$has_install_state" -eq 1 ]]; then
    log "dhcpcd and IPv4 forwarding restoration was deferred; rerun uninstall with --apply-network-now"
  fi

  rm -rf "$INSTALL_DIR"
  rm -rf /run/linux-router

  if [[ "$PURGE_DATA" -eq 1 ]]; then
    rm -rf "$DATA_DIR"
    log "Removed application data from $DATA_DIR"
  elif [[ -e "$DATA_DIR" ]]; then
    chown -R root:root "$DATA_DIR"
    chmod 0700 "$DATA_DIR"
    find "$DATA_DIR" -type f -exec chmod 0600 {} +
    log "Preserved application data in $DATA_DIR"
  fi

  if getent passwd "$SERVICE_USER" >/dev/null; then
    userdel "$SERVICE_USER" || log "WARNING: could not remove user $SERVICE_USER"
  fi
  if getent group "$SERVICE_USER" >/dev/null; then
    groupdel "$SERVICE_USER" || log "WARNING: could not remove group $SERVICE_USER"
  fi

  log "Linux Router has been uninstalled"
  log "Installed apt packages and the current runtime IPv4 forwarding value were left unchanged"
}

cleanup() {
  local status=$?
  trap - EXIT
  set +e

  if [[ "$status" -ne 0 && "$INSTALL_COMMITTED" -eq 0 && "$INSTALL_STARTED" -eq 1 ]]; then
    rollback_install
  fi

  [[ -z "$STAGING_DIR" || ! -e "$STAGING_DIR" ]] || rm -rf "$STAGING_DIR"
  [[ -z "$WORK_DIR" || ! -e "$WORK_DIR" ]] || rm -rf "$WORK_DIR"
  if [[ "$status" -eq 0 && -n "$BACKUP_DIR" && -e "$BACKUP_DIR" ]]; then
    rm -rf "$BACKUP_DIR"
  fi
  exit "$status"
}

trap cleanup EXIT

usage() {
  cat <<'EOF'
Install, upgrade, or uninstall Linux Router on Debian/Armbian.

Usage: sudo bash install.sh <command> [options]

Commands:
  install                    Perform a new installation
  upgrade                    Upgrade an existing installation
  uninstall                  Remove the installation and preserve user data

Options:
  --purge-data               With uninstall, also remove credentials and LAN settings
  --repo URL                 GitHub repository URL
  --branch NAME              GitHub branch (default: main)
  --archive-url URL          Download a specific source archive instead
  --install-dir PATH         Application directory (default: /opt/linux-router)
  --data-dir PATH            Persistent data directory (default: /var/lib/linux-router)
  --no-network-config        Do not write NetworkManager or netplan configuration
  --apply-network-now        Apply network configuration even over SSH
  --defer-network-restart    Write network configuration without applying it now
  -h, --help                 Show this help

Environment variables with the LINUX_ROUTER_ prefix can also set repo, branch,
install directory, and data directory. The installer never reboots the system.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    install|upgrade|uninstall)
      [[ -z "$ACTION" ]] || die "only one command may be specified"
      ACTION="$1"
      shift
      ;;
    --repo)
      [[ $# -ge 2 ]] || die "--repo requires a value"
      REPO_URL="$2"
      shift 2
      ;;
    --purge-data)
      PURGE_DATA=1
      shift
      ;;
    --branch)
      [[ $# -ge 2 ]] || die "--branch requires a value"
      BRANCH="$2"
      shift 2
      ;;
    --archive-url)
      [[ $# -ge 2 ]] || die "--archive-url requires a value"
      ARCHIVE_URL="$2"
      shift 2
      ;;
    --install-dir)
      [[ $# -ge 2 ]] || die "--install-dir requires a value"
      INSTALL_DIR="${2%/}"
      shift 2
      ;;
    --data-dir)
      [[ $# -ge 2 ]] || die "--data-dir requires a value"
      DATA_DIR="${2%/}"
      shift 2
      ;;
    --no-network-config)
      CONFIGURE_NETWORK=0
      shift
      ;;
    --apply-network-now)
      NETWORK_APPLY_MODE="now"
      shift
      ;;
    --defer-network-restart)
      NETWORK_APPLY_MODE="defer"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown option: $1"
      ;;
  esac
done

[[ -n "$ACTION" ]] || die "specify one command: install, upgrade, or uninstall"
[[ "$PURGE_DATA" -eq 0 || "$ACTION" == "uninstall" ]] || die "--purge-data requires the uninstall command"
[[ "$CONFIGURE_NETWORK" -eq 1 || "$ACTION" == "install" ]] || die "--no-network-config is only valid with install"
[[ "$BRANCH" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]] || die "invalid GitHub branch: $BRANCH"
[[ "$REPO_URL" =~ ^https://github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+(\.git)?$ ]] || die "invalid GitHub repository URL"

for path in "$INSTALL_DIR" "$DATA_DIR"; do
  [[ "$path" == /* ]] || die "paths must be absolute: $path"
  [[ "$path" =~ ^/[A-Za-z0-9._/-]+$ ]] || die "paths contain unsupported characters: $path"
  [[ ! "$path" =~ /\.\.?(/|$) ]] || die "paths must not contain . or .. segments: $path"
done

[[ "$INSTALL_DIR" != "$DATA_DIR" ]] || die "install and data directories must differ"
[[ "$DATA_DIR/" != "$INSTALL_DIR/"* ]] || die "data directory must not be inside install directory"
[[ "$INSTALL_DIR/" != "$DATA_DIR/"* ]] || die "install directory must not be inside data directory"
for path in "$INSTALL_DIR" "$DATA_DIR"; do
  case "$path" in
    /|/bin|/boot|/dev|/etc|/home|/lib|/opt|/proc|/root|/run|/sbin|/sys|/tmp|/usr|/var)
      die "refusing to use a system directory directly: $path"
      ;;
  esac
done
[[ ! -L "$INSTALL_DIR" ]] || die "install directory must not be a symbolic link"
[[ ! -L "$DATA_DIR" ]] || die "data directory must not be a symbolic link"
[[ ! -e "$INSTALL_DIR" || -d "$INSTALL_DIR" ]] || die "install path exists but is not a directory: $INSTALL_DIR"
[[ ! -e "$DATA_DIR" || -d "$DATA_DIR" ]] || die "data path exists but is not a directory: $DATA_DIR"

if [[ "$ACTION" == "install" ]]; then
  [[ ! -e "$INSTALL_DIR" ]] || die "an installation already exists at $INSTALL_DIR; use upgrade"
  [[ ! -e "/etc/systemd/system/$SERVICE_NAME" ]] || die "$SERVICE_NAME already exists; use upgrade"
  [[ ! -e "/etc/systemd/system/$AGENT_SERVICE_NAME" ]] || die "$AGENT_SERVICE_NAME already exists; use upgrade"
elif [[ "$ACTION" == "upgrade" ]]; then
  [[ -f "$INSTALL_DIR/app.py" ]] || die "no installation found at $INSTALL_DIR; use install"
  [[ -f "/etc/systemd/system/$SERVICE_NAME" ]] || die "$SERVICE_NAME is not installed"
  [[ -f "/etc/systemd/system/$AGENT_SERVICE_NAME" ]] || die "$AGENT_SERVICE_NAME is not installed"
  [[ -d "$DATA_DIR" ]] || die "application data directory not found: $DATA_DIR"
  getent passwd "$SERVICE_USER" >/dev/null || die "service user not found: $SERVICE_USER"
  getent group "$SERVICE_USER" >/dev/null || die "service group not found: $SERVICE_USER"
fi

[[ ${EUID} -eq 0 ]] || die "run this installer as root"
[[ -d /run/systemd/system ]] || die "systemd is required"
command -v apt-get >/dev/null 2>&1 || die "only apt-based Debian/Armbian systems are supported"

if [[ "$ACTION" == "uninstall" && "$PURGE_DATA" -eq 1 ]] && ! should_apply_network_now; then
  die "--purge-data requires --apply-network-now when network changes are deferred"
fi

if [[ "$ACTION" == "uninstall" ]]; then
  uninstall_router_panel
  INSTALL_COMMITTED=1
  exit 0
fi

if [[ -z "$ARCHIVE_URL" ]]; then
  ARCHIVE_URL="${REPO_URL%.git}/archive/refs/heads/$BRANCH.tar.gz"
fi
[[ "$ARCHIVE_URL" =~ ^https:// ]] || die "archive URL must use HTTPS"

if [[ "$ACTION" == "install" ]]; then
  export DEBIAN_FRONTEND=noninteractive
  log "Installing system packages"
  apt-get update
  apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    dnsmasq-base \
    gunicorn \
    iproute2 \
    iptables \
    iw \
    network-manager \
    python3 \
    python3-flask \
    tar \
    udev
else
  for command_name in curl tar python3 gunicorn systemd-analyze; do
    command -v "$command_name" >/dev/null 2>&1 || die "required command is missing: $command_name"
  done
fi

log "Resolving $BRANCH branch build"
REPO_PATH="${REPO_URL#https://github.com/}"
REPO_PATH="${REPO_PATH%.git}"
REF_JSON="$(curl --fail --location --silent --show-error --retry 3 \
  "https://api.github.com/repos/$REPO_PATH/git/ref/heads/$BRANCH")" || \
  die "could not resolve branch: $BRANCH"
REMOTE_COMMIT="$(REF_JSON="$REF_JSON" python3 -c 'import json, os; print(json.loads(os.environ["REF_JSON"])["object"]["sha"])')" || \
  die "could not parse branch metadata: $BRANCH"
[[ -n "$REMOTE_COMMIT" ]] || die "could not resolve branch: $BRANCH"
BUILD_ID="${REMOTE_COMMIT:0:7}"

log "Downloading application archive"
WORK_DIR="$(mktemp -d /tmp/linux-router-install.XXXXXX)"
install -d -m 0755 "$WORK_DIR/source"
curl --fail --location --silent --show-error --retry 3 \
  --output "$WORK_DIR/source.tar.gz" "$ARCHIVE_URL"
tar -xzf "$WORK_DIR/source.tar.gz" -C "$WORK_DIR/source" --strip-components=1

[[ -f "$WORK_DIR/source/app.py" ]] || die "downloaded archive does not contain app.py"
[[ -f "$WORK_DIR/source/router-panel.service" ]] || die "downloaded archive does not contain router-panel.service"
[[ -f "$WORK_DIR/source/router-panel-agent.service" ]] || die "downloaded archive does not contain router-panel-agent.service"
[[ -f "$WORK_DIR/source/agent.py" ]] || die "downloaded archive does not contain agent.py"
[[ -d "$WORK_DIR/source/router_panel" ]] || die "downloaded archive does not contain router_panel"
[[ -d "$WORK_DIR/source/templates" ]] || die "downloaded archive does not contain templates"
python3 -m py_compile "$WORK_DIR/source/app.py" "$WORK_DIR/source/agent.py" "$WORK_DIR/source/router_panel/"*.py

ROLLBACK_DIR="$WORK_DIR/rollback"
install -d -m 0700 "$ROLLBACK_DIR"
INSTALL_SWAP_STARTED=1
if [[ -e "$INSTALL_DIR" ]]; then
  INSTALL_DIR_EXISTED=1
fi
if [[ "$ACTION" == "install" && -e "$DATA_DIR" ]]; then
  DATA_DIR_EXISTED=1
  cp -a "$DATA_DIR" "$ROLLBACK_DIR/data-dir"
fi
service_is_active "$SERVICE_NAME" && WEB_WAS_ACTIVE=1
service_is_enabled "$SERVICE_NAME" && WEB_WAS_ENABLED=1
service_is_active "$AGENT_SERVICE_NAME" && AGENT_WAS_ACTIVE=1
service_is_enabled "$AGENT_SERVICE_NAME" && AGENT_WAS_ENABLED=1
backup_file "/etc/systemd/system/$SERVICE_NAME" "web.service"
backup_file "/etc/systemd/system/$AGENT_SERVICE_NAME" "agent.service"
if [[ "$ACTION" == "install" ]]; then
  service_is_active NetworkManager.service && NETWORKMANAGER_WAS_ACTIVE=1
  service_is_enabled NetworkManager.service && NETWORKMANAGER_WAS_ENABLED=1
  service_is_active dhcpcd.service && DHCPCD_WAS_ACTIVE=1
  service_is_enabled dhcpcd.service && DHCPCD_WAS_ENABLED=1
  ORIGINAL_IP_FORWARD="$(sysctl -n net.ipv4.ip_forward 2>/dev/null || true)"
  backup_file "/etc/sysctl.d/90-router-panel.conf" "sysctl.conf"
  backup_file "/etc/NetworkManager/conf.d/90-linux-router.conf" "networkmanager.conf"
  backup_file "/etc/netplan/90-linux-router.yaml" "netplan.yaml"
fi
INSTALL_STARTED=1

install -d -m 0755 "$(dirname "$INSTALL_DIR")"
STAGING_DIR="${INSTALL_DIR}.new.$$"
BACKUP_DIR="${INSTALL_DIR}.previous"
rm -rf "$STAGING_DIR" "$BACKUP_DIR"
install -d -m 0755 "$STAGING_DIR"
cp -a "$WORK_DIR/source/." "$STAGING_DIR/"
printf 'branch=%s\nbuild=%s\n' "$BRANCH" "$BUILD_ID" > "$STAGING_DIR/BUILD_INFO"
chown -R root:root "$STAGING_DIR"

# Runtime secrets always live in DATA_DIR, never in downloaded application files.
rm -f \
  "$STAGING_DIR/data/auth.json" \
  "$STAGING_DIR/data/initial_password.txt" \
  "$STAGING_DIR/data/secret_key" \
  "$STAGING_DIR/data/network.json"

if [[ -e "$INSTALL_DIR" ]]; then
  mv "$INSTALL_DIR" "$BACKUP_DIR"
fi
mv "$STAGING_DIR" "$INSTALL_DIR"
STAGING_DIR=""

[[ -f "$INSTALL_DIR/app.py" ]] || die "app.py was not found in $INSTALL_DIR"
if [[ "$ACTION" == "upgrade" ]]; then
  log "Installing upgraded services"
  install_service_units
  INSTALL_COMMITTED=1
  log "Linux Router upgrade completed"
  exit 0
fi

DATA_TOUCHED=1
if ! getent group "$SERVICE_USER" >/dev/null; then
  groupadd --system "$SERVICE_USER"
  SERVICE_GROUP_CREATED=1
fi
if ! getent passwd "$SERVICE_USER" >/dev/null; then
  useradd --system --gid "$SERVICE_USER" --home-dir "$DATA_DIR" --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
  SERVICE_USER_CREATED=1
fi
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0700 "$DATA_DIR"

if [[ ! -f "$DATA_DIR/auth.json" ]]; then
  NEW_PASSWORD="${INITIAL_PASSWORD:-$(python3 -c 'import secrets; print(secrets.token_urlsafe(15))')}"
  log "Creating initial administrator credentials"
fi

LINUX_ROUTER_DATA_DIR="$DATA_DIR" \
LINUX_ROUTER_INITIAL_PASSWORD="$NEW_PASSWORD" \
python3 -c "import sys; sys.path.insert(0, '$INSTALL_DIR'); import app"

chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR"
chmod 0700 "$DATA_DIR"
find "$DATA_DIR" -type f -exec chmod 0600 {} +
persist_install_state

log "Enabling IPv4 forwarding"
printf 'net.ipv4.ip_forward=1\n' > /etc/sysctl.d/90-router-panel.conf
sysctl -w net.ipv4.ip_forward=1 >/dev/null

systemctl enable NetworkManager.service >/dev/null
systemctl start NetworkManager.service

if [[ "$CONFIGURE_NETWORK" -eq 1 ]]; then
  log "Writing NetworkManager configuration"
  install -d -m 0755 /etc/NetworkManager/conf.d
  cat > /etc/NetworkManager/conf.d/90-linux-router.conf <<'EOF'
[ifupdown]
managed=true
EOF

  netplan_available=0
  if command -v netplan >/dev/null 2>&1 && [[ -d /etc/netplan ]]; then
    netplan_available=1
    log "Setting netplan renderer to NetworkManager"
    cat > /etc/netplan/90-linux-router.yaml <<'EOF'
network:
  version: 2
  renderer: NetworkManager
EOF
    chmod 0600 /etc/netplan/90-linux-router.yaml
    NETPLAN_GENERATED=1
    netplan generate
  fi

  apply_network=1
  if [[ "$NETWORK_APPLY_MODE" == "defer" ]]; then
    apply_network=0
  elif [[ "$NETWORK_APPLY_MODE" == "auto" && -n "${SSH_CONNECTION:-}" ]]; then
    apply_network=0
  fi

  if [[ "$apply_network" -eq 1 ]]; then
    log "Applying network configuration"
    NETWORK_APPLIED=1
    if [[ "$DHCPCD_WAS_ACTIVE" -eq 1 || "$DHCPCD_WAS_ENABLED" -eq 1 ]]; then
      log "Disabling dhcpcd to prevent duplicate interface management"
      systemctl disable --now dhcpcd.service
    fi
    if [[ "$netplan_available" -eq 1 ]]; then
      netplan apply
    fi
    systemctl restart NetworkManager.service
  else
    log "Network configuration written but not applied; apply it during a maintenance window"
  fi
fi

log "Installing systemd services"
install_service_units
INSTALL_COMMITTED=1

log "Linux Router is running at http://<device-ip>/"
if [[ -n "$NEW_PASSWORD" ]]; then
  printf '\nInitial login:\n  Username: admin\n  Password: %s\n' "$NEW_PASSWORD"
  printf 'Credentials are also stored in %s/initial_password.txt\n' "$DATA_DIR"
fi
if [[ "$CONFIGURE_NETWORK" -eq 1 && "$NETWORK_APPLY_MODE" != "now" && -n "${SSH_CONNECTION:-}" ]]; then
  printf '\nNetwork changes were deferred because the installer detected an SSH session.\n'
  printf 'During a maintenance window, disable dhcpcd, run "netplan apply" (when available), and restart NetworkManager.\n'
fi
