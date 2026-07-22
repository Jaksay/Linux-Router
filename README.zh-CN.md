[English](README.md) | [简体中文](README.zh-CN.md)

<h1>
  <img src="static/favicon.ico" alt="" width="32" height="32" align="center">
  Linux Router
</h1>

Linux Router 可以将 Debian 或 Armbian 设备变成路由器，并提供一个清晰的 Web 控制台，用于查看系统状态、管理有线网络和 Wi-Fi、开启热点共享、查看接入设备以及执行维护操作。

项目基于 Flask、NetworkManager 和 systemd 构建。Web 服务以普通用户运行，系统查询和网络变更由独立的 root Agent 通过 Unix Socket 执行白名单操作。

![Linux Router Web 管理界面](docs/assets/linux-router-web-console.png)

## 功能亮点

- 系统概览：硬件信息、IP 地址、活动连接、存储、内存和运行状态
- 依赖检查与修复：NetworkManager、dnsmasq、iptables、`iw` 等运行环境
- 有线和 Wi-Fi 管理：扫描、连接、断开、配置绑定和忘记网络
- 热点创建：支持独占 AP，以及网卡能力允许时的 AP+STA 并发模式
- 热点设备：查看客户端、DHCP 租约、无线信号和 LAN 网段配置
- 热点保活：热点异常断线后自动尝试恢复
- 工具能力：Tailscale 登录辅助、服务监控、密码修改和设备重启

## 网络变更风险

安装、卸载和网络栈修复会修改宿主机网络配置，可能重载 NetworkManager、应用 netplan、启停 `dhcpcd`，以及删除项目创建的热点和虚拟接口。相关操作可能导致网络连接中断。

建议在本地控制台或维护窗口执行，并提前备份宿主机网络配置。SSH 环境默认延迟网络变更；需要立即应用时使用 `--apply-network-now`。

安装选项：

```bash
# 跳过项目 NetworkManager/netplan 配置的写入和应用
sudo bash /tmp/linux-router-install.sh install --no-network-config

# 写入网络配置但延迟应用
sudo bash /tmp/linux-router-install.sh install --defer-network-restart
```

`--no-network-config` 仍会启用 NetworkManager 并开启 IPv4 转发。延迟模式卸载时不会立即恢复网络运行状态，应在维护窗口使用 `--apply-network-now` 再次执行；该模式不允许与 `--purge-data` 同时使用。

## 安装要求

目标系统为 Debian 13 或基于 Debian 的 Armbian，并使用 systemd 和 apt。网络接口由 NetworkManager 管理。

安装器会安装或检查以下主要依赖：

- Python 3、Flask 和 Gunicorn
- NetworkManager
- dnsmasq-base
- iptables
- iw
- iproute2、udev、curl 和 tar

安装器会从 GitHub 下载源码压缩包，目标设备无需安装 Git。

## 安装、更新和卸载

### 安装

下载安装脚本并执行全新安装：

```bash
curl -fsSL https://raw.githubusercontent.com/Jaksay/Linux-Router/main/install.sh \
  -o /tmp/linux-router-install.sh
sudo bash /tmp/linux-router-install.sh install
```

安装器会安装依赖、部署程序、创建 `router-panel` 服务账号、生成管理员初始密码、安装两个 systemd 服务，并配置 IPv4 转发和 NetworkManager 网络栈。

默认安装位置：

- 程序目录：`/opt/linux-router`
- 数据目录：`/var/lib/linux-router`
- Web 服务：`router-panel.service`
- root Agent：`router-panel-agent.service`

### 更新

```bash
sudo bash /tmp/linux-router-install.sh upgrade
```

更新只替换程序文件和 systemd 服务配置，保留管理员账号、密钥、LAN 网段及其他运行数据，不会重新配置 NetworkManager、netplan、IPv4 转发或 `dhcpcd`。更新完成后会执行健康检查，失败时自动恢复旧版本。

### 卸载

默认卸载会删除程序、systemd 服务、运行时 Socket，并尝试删除 `DebianRouterHotspot` 连接和匹配 `ap-*` 的无线虚拟接口；同时恢复安装前保存的 NetworkManager、netplan、IPv4 转发和 `dhcpcd` 状态。管理员账号、密钥和 LAN 配置会被保留，方便之后重新安装。

```bash
sudo bash /tmp/linux-router-install.sh uninstall
```

如需删除所有运行数据，显式添加 `--purge-data`。该操作不可恢复：

```bash
sudo bash /tmp/linux-router-install.sh uninstall --purge-data
```

## 运行架构

项目由两个 systemd 服务组成：普通用户运行的 Web 服务和 root 权限 Agent。Web 服务负责页面、登录、CSRF 防护和操作提交，Agent 通过 Unix Socket 执行白名单内的系统查询和网络变更。

网络变更由 Agent 串行执行，完成后 Web 页面会查询操作结果并刷新状态。

## 首次登录

安装器会创建管理员账号：

- 用户名：`admin`
- 密码：安装过程中随机生成，并在安装结束时输出

初始密码同时保存到：

```text
/var/lib/linux-router/initial_password.txt
```

首次登录后请立即修改密码。密码哈希、Flask 密钥、LAN 配置和热点保活配置均保存在数据目录，不应提交到 Git 仓库。

## 开发

开发环境需要先启动 root Agent，再启动 Web 服务：

```bash
cd /opt/linux-router

# 终端一
sudo env \
  LINUX_ROUTER_DATA_DIR=/var/lib/linux-router \
  LINUX_ROUTER_AGENT_SOCKET=/run/linux-router/agent.sock \
  python3 agent.py

# 终端二
python3 app.py
```

默认监听地址为 `http://127.0.0.1:80` 和 `http://<设备IP>:80`。生产环境应使用项目提供的 Gunicorn systemd 服务。

修改代码后：

- Web 代码或模板：重启 `router-panel.service`
- Agent、系统查询或网络操作：重启 `router-panel-agent.service`
- `static/style.css`：同时递增 `templates/base.html` 中 CSS URL 的 `v` 参数

运行测试：

```bash
python3 -m unittest tests.test_application
```
