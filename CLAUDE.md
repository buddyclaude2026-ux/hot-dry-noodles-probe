# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 运行与部署

### 本地开发（直接运行）
```bash
pip install -r requirements.txt
python3 main.py  # 默认端口 8081
```

### Docker 部署（生产/测试）
```bash
docker compose up -d --build   # 首次构建并启动
docker compose up -d           # 已构建后直接启动
docker compose logs -f heihei-core  # 查看核心服务日志
```

### Agent 单独运行（被监控端）
```bash
python3 web/agent.py --server http://<面板IP>:8081/api/v1/report \
  --token <AGENT_TOKEN> --name "MyVPS" --country "hk"
```

## 架构概览

项目分为两个独立进程，通过 HTTP + WebSocket 通信：

**服务端 `main.py`（FastAPI）**
- 全局内存缓存：`server_cache: Dict[str, ServerStatus]`（所有服务器实时状态）和 `config_cache: Dict[str, ServerConfig]`（管理员配置覆盖）
- 两个后台 Task：`monitor_alerts_task()`（每 10s 检查离线/延迟/流量报警）、`history_recorder_task()`（每 60s 写入历史快照）
- 数据库 `heihei.db`（SQLite）：startup 时加载进内存缓存，写操作异步持久化
- 动态文件注入：`/agent.py` 和 `/install_agent.sh` 请求时注入实时 `AGENT_TOKEN`

**客户端 `web/agent.py`**
- 每 2 秒采集 CPU/内存/磁盘/网速，TCP 探测三网延迟（119.29.29.29:53、223.6.6.6:53、223.5.5.5:53）
- 用 `agent_uuid.txt` 持久化标识自身（跨 IP 迁移不丢失数据）
- 在 Docker 中通过 `--db-path` 从数据库直接读取 Token，无需硬编码

## 鉴权机制

- **两套 Token**：`SECRET_TOKEN`（admin，登录后返回）用于所有管理 API；`AGENT_TOKEN`（低权限）仅用于 Agent 上报 `/api/v1/report`
- Token 优先从环境变量 `ADMIN_TOKEN` / `AGENT_TOKEN` 读取，否则从 DB `system_config` 表加载，首次启动自动生成
- HTTP Header 传递：`Authorization: <token>`（注意不是 `Bearer` 格式）

## 数据库 Schema

表：`servers`（实时状态快照）、`server_configs`（管理员配置覆盖）、`server_history`（历史时序数据）、`alert_history`（报警记录，保留最近 1000 条）、`users`（仅 admin 账号 + TOTP secret）、`system_config`（K/V 配置，含 Token 和通知设置）

Schema 迁移策略：用 `try/except` 包裹 `ALTER TABLE` 语句，直接在 `load_cache()` 启动时执行，无独立迁移文件。

## 关键约定

- **Server ID**：前端使用 `md5(host)` 作为稳定 ID，真实 host/IP 不暴露给前端（返回时替换为 `x.x.*.*`）
- **上报地址**：`POST /api/v1/report`，Body 为 `ServerStatus` Pydantic 模型
- **WebSocket**：`/ws`，连接时推送全量 `init`，之后每次 report 触发 `update` 广播
- **静态文件**：`web/` 目录通过 `StaticFiles` 挂载到根路径，路由优先级高于静态文件（路由先注册）
- **服务离线判定**：`last_seen` 超过 90s 触发报警，超过 15s 前端展示为 offline

## Docker Compose 服务说明

| 服务 | 镜像/构建 | 作用 |
|---|---|---|
| `heihei-core` | 本地 Dockerfile | FastAPI 面板，端口 8081（仅 127.0.0.1） |
| `heihei-agent` | 本地 Dockerfile.agent | 监控宿主机，挂载 `/proc` `/sys` |
