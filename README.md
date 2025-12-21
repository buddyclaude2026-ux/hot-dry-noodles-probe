# Hot Dry Noodles Probe (热干面探针)

Clean, lightweight, Python-based server monitoring probe with a beautiful glassmorphism UI.

## 📂 Project Structure
- `install.sh`: One-click server deployment script.
- `main.py`: Backend server (FastAPI + WebSocket).
- `agent.py`: Client agent (runs on monitored VPS).
- `web/`: Frontend static files.
- `probe.conf`: Nginx configuration template.

## 3. 服务端安装 (面板)

### 方式 A: 常规安装 (Recommended for Dev)
1. 准备 Python 3.9+ 环境
2. 安装依赖: `pip install -r requirements.txt`
3. 运行: `python3 main.py`

### 方式 B: Docker 部署 (Clean)
如果您重装了 HostDare 系统，想用纯净的 Docker 方式运行面板：

1. **上传代码** 到服务器 `/root/heihei` 目录。
2. **启动**:
   ```bash
   cd /root/heihei
   docker compose up -d
   ```
   *即使删除容器，只要 `heihei.db` 文件还在，您的数据就不会丢。*
   默认端口: 8081

---

## 4. Agent 安装 (被监控端)
## 4. Agent 安装 (被监控端)

您可以选择 **Shell 脚本** (简单) 或 **Docker** (干净) 两种方式安装。

### 方式 A: Shell 脚本 (Simple)

适合大多数场景，直接在 VPS 上运行。

```bash
# 请将 URL 替换为您面板的地址
bash <(curl -sL http://YOUR_PANEL_IP:8081/install_agent.sh) --name "MyVPS" --country "us"
```

### 方式 B: Docker (Clean)

适合不想污染宿主机环境的用户。Agent 运行在容器内，**删除容器即卸载**。

```bash
docker run -d --name heihei-agent \
  --restart always \
  --network host \
  -v /proc:/proc \
  -v /sys:/sys \
  python:3-slim \
  /bin/sh -c "pip install psutil requests && curl -sL http://YOUR_PANEL_IP:8081/agent.py | python3 - --name 'MyVPS' --country 'us' --server 'http://YOUR_PANEL_IP:8081/api/v1/report' --token 'HEIHEI_REPORT_VX'"
```

*注意：必须映射 (-v) `/proc` 和 `/sys`，否则无法读取宿主机的真实 CPU/内存使用率。*

---

## 5. 卸载 Agent

### 如果是 Shell 安装
```bash
systemctl stop heihei-agent
systemctl disable heihei-agent
rm /etc/systemd/system/heihei-agent.service
rm -rf /opt/heihei-agent  # 假设安装目录在此
systemctl daemon-reload
```

### 如果是 Docker 安装
```bash
# 1. 删除容器 (所有配置瞬间消失)
docker rm -f heihei-agent

# 2. (可选) 删除镜像
docker rmi python:3-slim
```
此操作后，系统恢复如初。


**参数说明:**
- `--server`: 面板上报地址 (e.g. `http://YOUR_IP:8081/api/v1/report`)
- `--name`: 面板显示的名称
- `--country`: 国旗代码 (us, hk, cn ...)
- `--token`: 上报密钥 (默认 `HEIHEI_REPORT_VX`)

---

## 🌐 Nginx Domain Configuration
1. Edit `/etc/nginx/sites-available/probe.conf`:
   - Change `server_name example.com;` to your domain.
2. Get SSL (Certbot):
   ```bash
   certbot --nginx -d yourdomain.com
   ```

## 🛠 Features
- **Real-time Status**: CPU/Mem/Disk/Network flow.
- **Latency Monitoring**: CT/CU/CM backbone latency.
- **2FA Security**: Login protected by TOTP.
- **Visuals**: Dynamic gradient background + Glassmorphism.
