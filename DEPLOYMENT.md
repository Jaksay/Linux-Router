# Linux Router 部署与故障排查

本文说明 Linux Router 的手动部署、网络配置、服务管理和常见故障排查。全新设备优先使用 [README.zh-CN.md](README.zh-CN.md) 中的安装器；手动部署适合开发、定制目录或需要逐步检查系统配置的场景。

下文使用以下示例路径：

```bash
INSTALL_DIR=/home/router-panel
DATA_DIR=/home/router-panel/data
```

如使用其他路径，必须同步修改环境变量、systemd unit 和后续命令。

## 1. 系统要求

目标系统为 Debian 13 或基于 Debian 的 Armbian，并使用 systemd、apt 和 NetworkManager。

安装依赖：

```bash
sudo apt-get update
sudo apt-get install -y \
  python3 \
  python3-flask \
  gunicorn \
  network-manager \
  dnsmasq-base \
  iptables \
  iw \
  iproute2 \
  udev \
  curl
```

组件用途：

- `python3-flask`、`gunicorn`：运行 Web 服务；
- `network-manager`：管理有线、Wi-Fi 和热点连接；
- `dnsmasq-base`：提供 NetworkManager shared 热点所需的 DHCP/DNS 能力；
- `iptables`：NetworkManager shared 模式使用的系统转发后端；
- `iw`：读取无线接口、PHY 和 AP+STA 能力；
- `iproute2`、`udev`：系统网络和硬件信息查询。

项目不要求目标设备安装 Git。安装器和手动部署都可以使用源码目录或 GitHub 源码压缩包。

## 2. 部署程序文件

将项目文件部署到目标目录，并确保包含以下内容：

```text
$INSTALL_DIR/app.py
$INSTALL_DIR/agent.py
$INSTALL_DIR/router_panel/
$INSTALL_DIR/templates/
$INSTALL_DIR/static/
$INSTALL_DIR/router-panel.service
$INSTALL_DIR/router-panel-agent.service
```

例如从当前源码目录复制：

```bash
sudo install -d -m 0755 "$INSTALL_DIR"
sudo cp -a ./. "$INSTALL_DIR/"
sudo chown -R root:root "$INSTALL_DIR"
```

不要把运行时账号、密钥或网络配置放入源码目录。运行数据应保存在 `$DATA_DIR`。

## 3. 创建运行账号和数据目录

Web 服务以普通用户运行，Agent 以 root 运行并使用 `router-panel` 组限制 Unix Socket 访问：

```bash
sudo groupadd --system router-panel
sudo useradd --system \
  --gid router-panel \
  --home-dir "$DATA_DIR" \
  --no-create-home \
  --shell /usr/sbin/nologin \
  router-panel

sudo install -d -o router-panel -g router-panel -m 0700 "$DATA_DIR"
```

如果用户或组已经存在，不要重复创建；确认数据目录最终归属为 `router-panel:router-panel`。

初始化应用数据并生成开发环境默认账号：

```bash
sudo env \
  LINUX_ROUTER_DATA_DIR="$DATA_DIR" \
  LINUX_ROUTER_INITIAL_PASSWORD='CHANGE_THIS_PASSWORD' \
  python3 -c "import sys; sys.path.insert(0, '$INSTALL_DIR'); import app"

# 将 CHANGE_THIS_PASSWORD 替换为实际初始密码
sudo chown -R router-panel:router-panel "$DATA_DIR"
sudo chmod 0700 "$DATA_DIR"
sudo find "$DATA_DIR" -type f -exec chmod 0600 {} +
```

如果未设置 `LINUX_ROUTER_INITIAL_PASSWORD`，开发环境默认密码为 `password`。生产部署应显式设置初始密码并在首次登录后立即修改。

## 4. 配置 NetworkManager 和 netplan

启用 NetworkManager：

```bash
sudo systemctl enable --now NetworkManager.service
```

确认传统接口由 NetworkManager 管理。`/etc/NetworkManager/NetworkManager.conf` 至少应包含：

```ini
[ifupdown]
managed=true
```

如果系统使用 netplan，应将 renderer 设置为 `NetworkManager`。建议先备份现有配置：

```bash
sudo cp -a /etc/netplan "/etc/netplan.backup.$(date +%Y%m%d%H%M%S)"
```

项目生成的最小配置示例：

```yaml
network:
  version: 2
  renderer: NetworkManager
```

保存后，在本地控制台或维护窗口执行：

```bash
sudo netplan generate
sudo netplan apply
sudo systemctl restart NetworkManager.service
```

检查 renderer 和网卡状态：

```bash
grep -R "^[[:space:]]*renderer:" /etc/netplan 2>/dev/null
nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device status
```

如果系统使用 `dhcpcd` 管理同一批接口，应在维护窗口确认后停止并禁用：

```bash
sudo systemctl disable --now dhcpcd.service
```

不要让 `dhcpcd`、`systemd-networkd` 和 NetworkManager 同时管理同一接口。

## 5. 配置 IPv4 转发

热点共享需要 IPv4 转发：

```bash
printf 'net.ipv4.ip_forward=1\n' | sudo tee /etc/sysctl.d/90-router-panel.conf
sudo sysctl --system
sudo sysctl -n net.ipv4.ip_forward
```

期望输出：

```text
1
```

## 6. 安装并启动 systemd 服务

服务模板包含 `@INSTALL_DIR@` 和 `@DATA_DIR@` 占位符。生成实际 unit：

```bash
sudo sed \
  -e "s|@INSTALL_DIR@|$INSTALL_DIR|g" \
  -e "s|@DATA_DIR@|$DATA_DIR|g" \
  "$INSTALL_DIR/router-panel-agent.service" \
  | sudo tee /etc/systemd/system/router-panel-agent.service >/dev/null

sudo sed \
  -e "s|@INSTALL_DIR@|$INSTALL_DIR|g" \
  -e "s|@DATA_DIR@|$DATA_DIR|g" \
  "$INSTALL_DIR/router-panel.service" \
  | sudo tee /etc/systemd/system/router-panel.service >/dev/null

sudo chmod 0644 \
  /etc/systemd/system/router-panel-agent.service \
  /etc/systemd/system/router-panel.service

sudo systemd-analyze verify \
  /etc/systemd/system/router-panel-agent.service \
  /etc/systemd/system/router-panel.service

sudo systemctl daemon-reload
sudo systemctl enable --now router-panel-agent.service router-panel.service
```

检查服务：

```bash
sudo systemctl status router-panel-agent.service --no-pager
sudo systemctl status router-panel.service --no-pager
curl -sS http://127.0.0.1/healthz
```

健康检查应返回：

```json
{"status":"ok"}
```

Web 服务默认监听 TCP `80` 端口。服务需要绑定低端口能力，unit 中已配置 `CAP_NET_BIND_SERVICE`。

## 7. 首次登录和运行数据

安装或初始化完成后：

- 用户名：`admin`；
- 初始密码：由 `LINUX_ROUTER_INITIAL_PASSWORD` 指定，或使用开发环境默认值；
- 密码提示文件：`$DATA_DIR/initial_password.txt`。

以下文件属于运行数据，不应提交到 Git：

```text
$DATA_DIR/auth.json
$DATA_DIR/secret_key
$DATA_DIR/network.json
$DATA_DIR/hotspot_keepalive.json
```

## 8. 热点和共享网络检查

热点使用 NetworkManager 的 `ipv4.method shared`，项目不直接维护一套独立的 NAT 规则。启动热点前确认：

```bash
systemctl is-active NetworkManager.service
command -v nmcli
command -v iw
command -v iptables
dpkg-query -W dnsmasq-base
sysctl -n net.ipv4.ip_forward
```

查看活动连接和热点配置：

```bash
nmcli -f NAME,TYPE,DEVICE connection show --active
nmcli connection show DebianRouterHotspot
nmcli -g ipv4.method connection show id DebianRouterHotspot
```

热点共享正常时，最后一条命令应返回：

```text
shared
```

查看无线接口和驱动能力：

```bash
iw dev
iw phy
```

并发 AP+STA 是否可用取决于无线驱动。许多设备只支持 AP 与 STA 使用同一频段或同一信道；连接不同信道的上游 Wi-Fi 时，热点可能无法启动或连接可能失败。

## 9. 常见故障排查

### Web 页面无法打开

```bash
sudo systemctl status router-panel.service --no-pager
sudo journalctl -u router-panel.service -n 100 --no-pager
sudo ss -ltnp | grep ':80 '
curl -v http://127.0.0.1/healthz
```

### Agent 不可用

```bash
sudo systemctl status router-panel-agent.service --no-pager
sudo journalctl -u router-panel-agent.service -n 100 --no-pager
sudo ls -l /run/linux-router/agent.sock
```

确认 Web 服务和 Agent 使用相同的 `LINUX_ROUTER_DATA_DIR`、Socket 路径以及 `router-panel` 组。

### 网卡显示为 unmanaged

```bash
nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device status
grep -R "^[[:space:]]*renderer:" /etc/netplan 2>/dev/null
sudo udevadm info -q property -p /sys/class/net/<接口名> | grep NM_UNMANAGED
```

确认 NetworkManager 使用 `managed=true`，netplan renderer 为 `NetworkManager`，并且没有其他服务接管同一接口。修改后在维护窗口执行 `netplan generate`、`netplan apply` 和 NetworkManager 重启。

### 热点能连接但无法上网

依次确认：

1. 上游 Wi-Fi 或有线接口已连接并拥有默认路由；
2. `DebianRouterHotspot` 的 IPv4 方法为 `shared`；
3. `dnsmasq-base`、`iptables` 已安装；
4. `net.ipv4.ip_forward` 为 `1`。

```bash
ip route
nmcli -g ipv4.method connection show id DebianRouterHotspot
systemctl status NetworkManager.service --no-pager
```

### 修改配置后未生效

```bash
sudo systemctl restart router-panel.service
sudo systemctl restart router-panel-agent.service
```

修改 `static/style.css` 后，还需递增 `templates/base.html` 中 CSS URL 的 `v` 参数。

## 10. 卸载和恢复

如果程序由安装器部署，推荐使用安装器卸载：

```bash
sudo bash install.sh uninstall \
  --install-dir /home/router-panel \
  --data-dir /home/router-panel/data
```

默认卸载保留账号、密钥和 LAN 配置，并恢复安装器记录的 NetworkManager、netplan、sysctl、IPv4 转发和 `dhcpcd` 状态。使用 `--purge-data` 会删除全部运行数据，且不可恢复。

通过 SSH 卸载时，网络运行状态可能被延迟恢复。确认维护窗口后，使用：

```bash
sudo bash install.sh uninstall \
  --install-dir /home/router-panel \
  --data-dir /home/router-panel/data \
  --apply-network-now
```

如果安装或升级在健康检查前失败，安装器会尝试恢复应用目录、数据目录、项目网络配置和服务状态。恢复后仍应检查 `systemctl`、`nmcli` 和默认路由。

## 11. 开发验证

开发环境可分别启动 Agent 和 Web：

```bash
cd "$INSTALL_DIR"
sudo env \
  LINUX_ROUTER_DATA_DIR="$DATA_DIR" \
  LINUX_ROUTER_AGENT_SOCKET=/run/linux-router/agent.sock \
  python3 agent.py

# 另一个终端
python3 app.py
```

运行测试：

```bash
python3 -m unittest tests.test_application
```

修改 Web 代码或模板后重启 `router-panel.service`；修改 Agent、系统查询或网络操作后重启 `router-panel-agent.service`。生产环境应使用 systemd 管理的 Gunicorn 服务。
