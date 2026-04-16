import asyncio
import json
import logging
import sqlite3
import time
import io
import base64
import hashlib
from contextlib import asynccontextmanager
from typing import Dict, List, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Header, Depends, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import uvicorn
import aiosqlite
import secrets
import pyotp
import qrcode
import urllib.request

import os

# --- Configuration ---
PORT = int(os.environ.get("PORT", 8081))
DB_PATH = "heihei.db"
# Tokens will be loaded from DB
SECRET_TOKEN = "" 
AGENT_TOKEN = ""

# --- Dynamic Token Injection ---
def get_agent_py_content():
    with open("web/agent.py", "r") as f:
        content = f.read()
    # Replace the placeholder or the variable line
    # We look for: SECRET_TOKEN = "..."
    import re
    content = re.sub(r'SECRET_TOKEN = ".*?"', f'SECRET_TOKEN = "{AGENT_TOKEN}"', content)
    return content

def get_install_sh_content(host_url):
    with open("web/install_agent.sh", "r") as f:
        content = f.read()
    
    # Extract host form URL (e.g. http://1.2.3.4:8081 -> 1.2.3.4:8081)
    # Actually host_url passed from request is base_url
    
    # Inject Host and Token
    # We stripped protocol in the script? No, script expects SERVER_HOST like ip:port
    # Let's just blindly replace.
    from urllib.parse import urlparse
    parsed = urlparse(str(host_url))
    host_only = parsed.netloc # ip:port
    
    content = content.replace("{{SERVER_HOST}}", host_only)
    content = content.replace("{{AGENT_TOKEN}}", AGENT_TOKEN)
    return content

# --- Logging ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("heihei-core")

# --- Utils ---
def hash_pw(password: str):
    return hashlib.sha256(password.encode()).hexdigest()

def fetch_ip_country(ip: str) -> Optional[str]:
    try:
        # Use ip-api.com (free, no key, 45 requests/min)
        with urllib.request.urlopen(f"http://ip-api.com/json/{ip}?fields=countryCode", timeout=3) as url:
            data = json.loads(url.read().decode())
            return data.get("countryCode", "").lower()
    except:
        return None

# --- Data Models ---
class ServerStatus(BaseModel):
    id: str = "" # Hashed ID for frontend unique key
    name: str = ""
    host: str = "" # Use host/ip as ID
    uuid: Optional[str] = None
    ip_address: str = "" # Actual IP
    hostname: Optional[str] = None # Original Hostname (e.g. ubuntu-s-1vcpu)
    type: str = "KVM"
    alert_status: str = "up" # 'up' or 'down'
    last_alert_ts: int = 0 # Throttle timestamp
    latency_status: str = "normal" # 'normal' or 'high'
    online: bool = True
    uptime: int = 0
    load: float = 0.0
    network_in: int = 0
    network_out: int = 0
    net_in_speed: int = 0
    net_out_speed: int = 0
    rate_status: str = "normal" # 'normal' or 'high'
    last_rate_alert_ts: int = 0
    cpu: float = 0.0
    memory_total: int = 0
    memory_used: int = 0
    disk_total: int = 0
    disk_used: int = 0
    last_seen: int = 0
    country_code: Optional[str] = None
    # New HW Info
    cpu_count: int = 1
    os_release: str = "Linux"
    # Latency
    ping_189: float = 0.0
    ping_10010: float = 0.0
    ping_10086: float = 0.0
    # Config Overrides (Merged at runtime)
    alias: Optional[str] = None
    public_note: Optional[str] = None # e.g. Price
    expiry: Optional[str] = None 
    display_order: int = 0 
    traffic_rate_threshold: Optional[float] = None # Per-server override 

class NotificationConfig(BaseModel):
    tg_token: str
    tg_chat_id: str
    bark_server: str
    bark_key: str
    enabled: bool
    latency_enable: bool = False
    latency_threshold: int = 210
    latency_isp_ct: bool = True
    latency_isp_cu: bool = True
    latency_isp_cm: bool = True
    traffic_rate_enable: bool = False
    traffic_rate_threshold: float = 30.0 # Mbps (Float for Kbps support)



# ... (Updating report_status for New Server Alert)

# ...



 

class ServerConfig(BaseModel):
    host: str
    alias: Optional[str] = None
    public_note: Optional[str] = None
    expiry: Optional[str] = None
    country_code: Optional[str] = None
    display_order: int = 0
    traffic_rate_threshold: Optional[float] = None

class LoginRequest(BaseModel):
    username: str
    password: str
    code: Optional[str] = None # 2FA Code

class ContentChangePassword(BaseModel):
    old_password: str
    new_password: str

class AlertHistoryItem(BaseModel):
    id: int
    ts: int  # UTC timestamp
    svid: str # Server ID
    type: str # TRAFFIC, OFFLINE, SYSTEM
    title: str
    content: str
    context: str # JSON Dump


class AlertHistoryItem(BaseModel):
    id: int
    ts: int  # UTC timestamp
    svid: str # Server ID
    type: str # TRAFFIC, OFFLINE, SYSTEM
    title: str
    content: str
    context: str # JSON Dump


class ContentEnable2FA(BaseModel):
    secret: str
    code: str

# --- Global In-Memory Cache ---
server_cache: Dict[str, ServerStatus] = {}
config_cache: Dict[str, ServerConfig] = {}

# --- WebSocket Manager (Moved Up) ---
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        msg_str = json.dumps(message)
        for connection in self.active_connections:
            try:
                await connection.send_text(msg_str)
            except:
                pass

manager = ConnectionManager()

# --- Notification System ---

system_settings: Dict[str, str] = {}

async def load_system_settings():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("CREATE TABLE IF NOT EXISTS system_config (key TEXT PRIMARY KEY, value TEXT)")
        async with db.execute("SELECT key, value FROM system_config") as cursor:
            async for row in cursor:
                system_settings[row[0]] = row[1]
    logger.info(f"Loaded System Settings: {system_settings}")

async def save_system_setting(key: str, value: str):
    system_settings[key] = value
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO system_config (key, value) VALUES (?, ?)", (key, value))
        await db.commit()

async def send_telegram_msg(text: str):
    token = system_settings.get("notify_tg_token")
    chat_id = system_settings.get("notify_tg_chat_id")
    if not token or not chat_id: return
    
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        import requests
        def _post():
            resp = requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=5)
            resp.raise_for_status()
        await asyncio.to_thread(_post)
    except Exception as e:
        logger.error(f"TG Error: {e}")

async def send_bark_msg(title: str, content: str):
    server_url = system_settings.get("notify_bark_server", "http://bark-server:8080")
    key = system_settings.get("notify_bark_key")
    if not key: return
    
    server_url = server_url.rstrip("/")
    # Use POST for better encoding support
    url = f"{server_url}/push"
    try:
        import requests
        def _post():
            payload = {
                "body": content,
                "title": title,
                "device_key": key,
                "icon": "https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/server.png"
            }
            resp = requests.post(url, json=payload, timeout=5)
            if resp.status_code != 200:
                logger.error(f"Bark Failed {resp.status_code}: {resp.text}")
            resp.raise_for_status()
        
        # Fire and forget bark, but record it first!
        # Note: We record explicitly in monitor_alerts now to capture context
        await asyncio.to_thread(_post)
    except Exception as e:
        logger.error(f"Bark Error: {e}")

async def send_notification(title: str, content: str):
    if system_settings.get("notify_enabled") != "true": return
    await send_telegram_msg(f"*{title}*\n{content}")
    await send_bark_msg(title, content)

async def monitor_alerts_task():
    logger.info("Alert Monitor Started")
    while True:
        try:
            await asyncio.sleep(10)
            now = int(time.time())
            
            for sid, s in list(server_cache.items()):
                # 35s timeout
                is_offline = (now - s.last_seen) > 35
                
                if is_offline:
                    if s.alert_status != 'down':
                        s.alert_status = 'down'
                        s.online = False
                        logger.warning(f"Server Down: {s.name or s.host}")
                        await send_notification("🔴 服务器报警", f"服务器离线: {s.name or s.host}\nIP: {s.ip_address}\n时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
                else:
                    if s.alert_status == 'down':
                        s.alert_status = 'up'
                        s.online = True
                        logger.info(f"Server Up: {s.name or s.host}")
                        await send_notification("🟢服务器恢复", f"服务器上线: {s.name or s.host}\nIP: {s.ip_address}\n时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
                    else:
                        if s.alert_status != 'up': s.alert_status = 'up'

        except Exception as e:
            logger.error(f"Monitor Task Error: {e}")
            await asyncio.sleep(5)

# --- Database ---
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        # Servers Table
        await db.execute('''
            CREATE TABLE IF NOT EXISTS servers (
                host TEXT PRIMARY KEY,
                data TEXT,
                last_seen INTEGER
            )
        ''')
        # Alert History Table
        await db.execute('''
            CREATE TABLE IF NOT EXISTS alert_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER,
                svid TEXT,
                type TEXT,
                title TEXT,
                content TEXT,
                context TEXT
            )
        ''')

        # Configs Table
        await db.execute('''
            CREATE TABLE IF NOT EXISTS server_configs (
                host TEXT PRIMARY KEY,
                alias TEXT,
                public_note TEXT,
                expiry TEXT,
                country_code TEXT,
                display_order INTEGER
            )
        ''')
        # History Table
        await db.execute('''
            CREATE TABLE IF NOT EXISTS server_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                host TEXT,
                ts INTEGER,
                cpu REAL,
                mem_usage REAL,
                net_in_speed INTEGER,
                net_out_speed INTEGER
            )
        ''')
        # Users Table
        await db.execute("CREATE TABLE IF NOT EXISTS users (username TEXT PRIMARY KEY, password_hash TEXT)")
        # Try add totp col
        try:
             await db.execute("ALTER TABLE users ADD COLUMN totp_secret TEXT")
        except:
             pass 

        # Check if admin exists - use random unguessable password, setup wizard will set the real one
        async with db.execute("SELECT username FROM users WHERE username = 'admin'") as cursor:
            if not await cursor.fetchone():
                await db.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)", ("admin", hash_pw(secrets.token_hex(32))))
        
        # System Config Table
        await db.execute('''
            CREATE TABLE IF NOT EXISTS system_config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        
        await db.commit()
    logger.info("Database initialized.")

async def load_tokens():
    global SECRET_TOKEN, AGENT_TOKEN
    
    # Priority 1: Environment Variables (Preferred for Docker/One-Click)
    env_admin = os.environ.get("ADMIN_TOKEN")
    env_agent = os.environ.get("AGENT_TOKEN")
    
    async with aiosqlite.connect(DB_PATH) as db:
        # Load Admin Token
        if env_admin:
            SECRET_TOKEN = env_admin
            # Sync to DB for consistency (optional, effectively overwrite DB if ENV is set)
            await db.execute("INSERT OR REPLACE INTO system_config (key, value) VALUES (?, ?)", ('admin_token', SECRET_TOKEN))
            logger.info(f"Loaded ADMIN_TOKEN from Environment")
        else:
            # Fallback to DB
            async with db.execute("SELECT value FROM system_config WHERE key='admin_token'") as cursor:
                row = await cursor.fetchone()
                if row:
                    SECRET_TOKEN = row[0]
                else:
                    SECRET_TOKEN = secrets.token_hex(16)
                    await db.execute("INSERT INTO system_config (key, value) VALUES (?, ?)", ('admin_token', SECRET_TOKEN))
                    logger.warning("=" * 60)
                    logger.warning(f"  NEW ADMIN TOKEN GENERATED: {SECRET_TOKEN}")
                    logger.warning("=" * 60)

        # Load Agent Token
        if env_agent:
            AGENT_TOKEN = env_agent
            await db.execute("INSERT OR REPLACE INTO system_config (key, value) VALUES (?, ?)", ('agent_token', AGENT_TOKEN))
            logger.info(f"Loaded AGENT_TOKEN from Environment")
        else:
            async with db.execute("SELECT value FROM system_config WHERE key='agent_token'") as cursor:
                row = await cursor.fetchone()
                if row:
                    AGENT_TOKEN = row[0]
                else:
                    AGENT_TOKEN = secrets.token_hex(16)
                    await db.execute("INSERT INTO system_config (key, value) VALUES (?, ?)", ('agent_token', AGENT_TOKEN))
                    logger.warning("=" * 60)
                    logger.warning(f"  NEW AGENT TOKEN GENERATED: {AGENT_TOKEN}")
                    logger.warning("=" * 60)
        
        
        await db.commit()

async def load_cache():
    # Load Status
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT host, data FROM servers") as cursor:
            async for row in cursor:
                try:
                    host, data_json = row
                    server_cache[host] = ServerStatus(**json.loads(data_json))
                except Exception as e:
                    logger.error(f"Failed to load server {row[0]}: {e}")
        
        # Load Configs
        # Load Configs
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                # Migration: Add traffic_rate_threshold if missing
                try:
                    await db.execute("ALTER TABLE server_configs ADD COLUMN traffic_rate_threshold REAL")
                    await db.commit()
                    logger.info("DB Migration: Added traffic_rate_threshold column")
                except:
                    pass

                async with db.execute("SELECT host, alias, public_note, expiry, country_code, display_order, traffic_rate_threshold FROM server_configs") as cursor:
                    async for row in cursor:
                        try:
                            # Safely handle potentially missing columns if select matches outdated schema? 
                            # No, we selected 7 columns.
                            host, alias, note, expiry, cc, order, thresh = row
                            config_cache[host] = ServerConfig(
                                host=host,
                                alias=alias,
                                public_note=note,
                                expiry=expiry,
                                country_code=cc,
                                display_order=row[5] if row[5] is not None else 0,
                                traffic_rate_threshold=thresh
                            )
                        except Exception as e:
                            logger.error(f"Config Load Error {row[0]}: {e}")
        except Exception as e:
             logger.error(f"Load Configs Failed: {e}")
    logger.info(f"Loaded {len(server_cache)} servers and {len(config_cache)} configs.")

# --- Log History Helper (Sherlock) ---
def record_alert(svid: str, alert_type: str, title: str, content: str, context: dict):
    try:
        import json
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            c.execute("INSERT INTO alert_history (ts, svid, type, title, content, context) VALUES (?, ?, ?, ?, ?, ?)",
                      (int(time.time()), svid, alert_type, title, content, json.dumps(context)))
            conn.commit()
            # Keep history slim (keep last 1000)
            c.execute("DELETE FROM alert_history WHERE id NOT IN (SELECT id FROM alert_history ORDER BY id DESC LIMIT 1000)")
            conn.commit()
    except Exception as e:
        logger.error(f"Failed to record alert history: {e}")


# --- Background Tasks ---
async def history_recorder_task():
    while True:
        await asyncio.sleep(60) # Run every minute
        now = int(time.time())
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                for host, s in list(server_cache.items()):
                    # Only record online servers
                    if s.online and (now - s.last_seen < 60):
                        mem_usage_pct = (s.memory_used / s.memory_total * 100) if s.memory_total > 0 else 0
                        await db.execute('''
                            INSERT INTO server_history (host, ts, cpu, mem_usage, net_in_speed, net_out_speed)
                            VALUES (?, ?, ?, ?, ?, ?)
                        ''', (host, now, s.cpu, mem_usage_pct, s.net_in_speed, s.net_out_speed))
                await db.commit()
        except Exception as e:
            logger.error(f"History recorder failed: {e}")

async def persist_server(data: ServerStatus):
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute(
                "INSERT OR REPLACE INTO servers (host, data, last_seen) VALUES (?, ?, ?)",
                (data.host, json.dumps(data.model_dump()), data.last_seen)
            )
            await db.commit()
    except Exception as e:
        logger.error(f"DB Persist Error: {e}")

# --- Synchronous DB Init (Fallback) ---
def ensure_db_sync():
    try:
        import sqlite3
        with sqlite3.connect(DB_PATH) as conn:
            # Alert History
            conn.execute('''
                CREATE TABLE IF NOT EXISTS alert_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER,
                    svid TEXT,
                    type TEXT,
                    title TEXT,
                    content TEXT,
                    context TEXT
                )
            ''')
            # Servers (Just in case)
            conn.execute('''
                CREATE TABLE IF NOT EXISTS servers (
                    host TEXT PRIMARY KEY,
                    data TEXT,
                    last_seen INTEGER
                )
            ''')
            conn.commit()
            logger.info("Sync DB Schema check passed.")
    except Exception as e:
        logger.error(f"Sync DB Init Failed: {e}")

# --- App Lifecycle ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await asyncio.to_thread(ensure_db_sync) # Run sync init first
    await init_db() # Run async init next (for others)
    await load_cache()
    await load_tokens() # Load tokens
    await load_system_settings() # Load Notify Config
    asyncio.create_task(history_recorder_task())
    asyncio.create_task(monitor_alerts_task()) # Start Monitor
    yield
    # Shutdown
    logger.info("Server shutting down...")

app = FastAPI(lifespan=lifespan)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Routes ---

# 1. Server List (Merged)
@app.get("/api/v1/server/list")
async def get_server_list():
    now = int(time.time())
    result = []
    for s_orig in server_cache.values():
        s = s_orig.model_copy()
        if s.host in config_cache:
            cfg = config_cache[s.host]
            if cfg.alias: s.name = cfg.alias
            s.alias = cfg.alias
            s.public_note = cfg.public_note
            s.expiry = cfg.expiry
            s.display_order = cfg.display_order
            s.traffic_rate_threshold = cfg.traffic_rate_threshold
            if cfg.country_code: s.country_code = cfg.country_code

        if now - s.last_seen > 15:
            s.online = False
        else:
            s.online = True
            
        # --- Backend Masking & ID Generation ---
        # 1. Generate stable ID from real host/ip
        s.id = hashlib.md5(s.host.encode()).hexdigest()
        
        # 2. Mask IP for security
        def mask_ip(ip_str):
            if not ip_str: return ""
            # IPv4
            if '.' in ip_str:
                parts = ip_str.split('.')
                if len(parts) == 4:
                    return f"{parts[0]}.{parts[1]}.*.*"
            # IPv4
            if '.' in ip_str:
                parts = ip_str.split('.')
                if len(parts) == 4:
                    return f"{parts[0]}.{parts[1]}.*.*"
            
            return "Hidden"
            
        real_ip = s.ip_address if s.ip_address else s.host
        masked = mask_ip(real_ip)
        
        # Override fields sent to frontend
        s.host = masked
        s.ip_address = masked
        
        result.append(s.model_dump())
    
    # Sort by display_order (Ascending: 0, 1, 2...)
    result.sort(key=lambda x: x.get('display_order', 0))
    
    return {"result": result}

# 2. Agent Report
@app.post("/api/v1/report")
async def report_status(data: ServerStatus, request: Request, token: str = Header(None, alias="Authorization")):
    if token not in [SECRET_TOKEN, AGENT_TOKEN]:
         raise HTTPException(status_code=401, detail="Unauthorized")
         
    identity_key = data.host
    if data.uuid: identity_key = data.uuid
    
    # Capture original hostname from agent before overwriting host with UUID
    if not data.hostname:
        data.hostname = data.host

    # Trust Nginx Header for IP
    client_ip = request.headers.get("x-real-ip") or request.client.host
    if not data.ip_address: data.ip_address = client_ip
    
    # Auto-detect Country for New Servers (or if missing)
    # Trigger if: 1. Not in cache (New) OR 2. In cache but has default/null country
    should_lookup = False
    if identity_key not in server_cache:
        should_lookup = True
    elif server_cache[identity_key].country_code in [None, 'cn', '']:
        should_lookup = True
        
    # But trusting "cn" from agent might be wrong if defaults are 'cn'.
    # If data.country_code is 'cn', and we really want to check:
    if should_lookup and (not data.country_code or data.country_code == 'cn'):
         # Run in thread to allow async
         real_cc = await asyncio.to_thread(fetch_ip_country, data.ip_address)
         if real_cc: 
             data.country_code = real_cc
             logger.info(f"Auto-detected Country: {real_cc} for {data.ip_address}")

    # Check if New Server (Alert)
    if identity_key not in server_cache:
        logger.info(f"New Server Detected: {data.name} ({data.ip_address})")
        # Mask IP for Notification
        notify_ip = "Hidden" 
        if data.ip_address and '.' in data.ip_address:
             parts = data.ip_address.split('.')
             if len(parts) == 4:
                 notify_ip = f"{parts[0]}.{parts[1]}.*.*"

        asyncio.create_task(send_notification(
            "🆕 新机上线", 
            f"名称: {data.alias or data.name}\nIP: {notify_ip}\n系统: {data.os_release}\n时间: {time.strftime('%Y-%m-%d %H:%M:%S')}"
        ))
    # If client provides a Public IP, use it. 
    # Otherwise fallback to connection IP.
    if not data.ip_address:
         data.ip_address = client_ip
         
    # Ensure data.host is set to the identity key for DB persistence
    data.host = identity_key
    
    data.last_seen = int(time.time())
    data.online = True
    
    # Preserve runtime statuses if updating
    # Preserve runtime statuses if updating
    if identity_key in server_cache:
        old_s = server_cache[identity_key]
        data.alert_status = old_s.alert_status
        data.latency_status = old_s.latency_status
        data.last_alert_ts = old_s.last_alert_ts # Preserve throttle timestamp
        data.last_rate_alert_ts = old_s.last_rate_alert_ts # Preserve rate alert timestamp
        # Preserve country code if we have a better one in cache
        if (not data.country_code or data.country_code == 'cn') and old_s.country_code and old_s.country_code != 'cn':
            data.country_code = old_s.country_code
        # Attempt to re-add hostname if missing in new data but present in old
        if not data.hostname and old_s.hostname:
            data.hostname = old_s.hostname

    # Merge Alias from Config Cache if exists
    if identity_key in config_cache:
        data.alias = config_cache[identity_key].alias
        data.traffic_rate_threshold = config_cache[identity_key].traffic_rate_threshold

    server_cache[identity_key] = data
    
    await manager.broadcast({
        "type": "update",
        "data": data.model_dump()
    })
    
    asyncio.create_task(persist_server(data))
    return {"status": "ok"}


# 3. WebSocket Endpoint
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        now = int(time.time())
        init_list = []
        for s in server_cache.values():
            if now - s.last_seen > 15: s.online = False
            else: s.online = True
            init_list.append(s.model_dump())
            
        await websocket.send_json({
            "type": "init",
            "data": init_list
        })
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

# 4. Config Update
class ConfigUpdatePayload(BaseModel):
    id: str # Use ID to identify server (since host is masked)
    alias: Optional[str] = None
    public_note: Optional[str] = None
    expiry: Optional[str] = None
    country_code: Optional[str] = None
    display_order: int = 0
    traffic_rate_threshold: Optional[float] = None

@app.post("/api/v1/server/config")
async def update_config(payload: ConfigUpdatePayload, token: str = Header(None, alias="Authorization")):
    if token != SECRET_TOKEN:
         raise HTTPException(status_code=401, detail="Unauthorized")

    logger.info(f"DEBUG: Received Config Update: {payload}")

    # 1. Find Real Host by ID
    target_host = None
    for s in server_cache.values():
        if hashlib.md5(s.host.encode()).hexdigest() == payload.id:
            target_host = s.host
            break
            
    if not target_host:
        raise HTTPException(status_code=404, detail="Server not found (ID mismatch)")

    # 2. Update Cache & DB
    if target_host in config_cache:
        cfg = config_cache[target_host]
    else:
        cfg = ServerConfig(host=target_host)
        
    cfg.alias = payload.alias
    cfg.public_note = payload.public_note
    cfg.expiry = payload.expiry
    cfg.country_code = payload.country_code
    cfg.display_order = payload.display_order
    cfg.traffic_rate_threshold = payload.traffic_rate_threshold
    
    config_cache[target_host] = cfg
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
        INSERT OR REPLACE INTO server_configs (host, alias, public_note, expiry, country_code, display_order, traffic_rate_threshold) 
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (target_host, cfg.alias, cfg.public_note, cfg.expiry, cfg.country_code, cfg.display_order, cfg.traffic_rate_threshold))
        await db.commit()
    return {"status": "ok"}

 # --- Save Config ---
def save_server_config(svid: str, cfg: ServerConfig):
    with sqlite3.connect(DB_PATH) as conn:
        c = conn.cursor()
        c.execute("INSERT OR REPLACE INTO servers (id, host, alias, public_note, expiry, country_code, display_order, traffic_rate_threshold) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                  (svid, cfg.host, cfg.alias, cfg.public_note, cfg.expiry, cfg.country_code, cfg.display_order, cfg.traffic_rate_threshold))
        conn.commit()
    return {"status": "ok"}

# 5. Admin Delete
@app.get("/api/v1/admin/delete")
async def admin_delete(id: str = Query(..., description="Server ID"), token: str = Header(None, alias="Authorization")):
    if token != SECRET_TOKEN:
         raise HTTPException(status_code=401, detail="Unauthorized")

    # Lookup Host by ID
    target_host = None
    # 1. Check Server Cache
    for s in server_cache.values():
         if hashlib.md5(s.host.encode()).hexdigest() == id:
             target_host = s.host
             break
    
    # 2. If not in server_cache (offline?), check config_cache?
    # IDs are generated from s.host. If s.host is in config_cache, we can try to re-hash keys?
    if not target_host:
        for host in config_cache.keys():
            if hashlib.md5(host.encode()).hexdigest() == id:
                target_host = host
                break
    
    if not target_host:
        # If we still can't find it, maybe the user passed a host directly? (Legacy fallback?)
        # For now, just return not found/ok to avoid frontend blocking
        return {"status": "not_found", "detail": "Could not verify ID mapping"}

    if target_host in server_cache: del server_cache[target_host]
    if target_host in config_cache: del config_cache[target_host]
        
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM servers WHERE host = ?", (target_host,))
        await db.execute("DELETE FROM server_configs WHERE host = ?", (target_host,))
        await db.commit()
    return {"status": "deleted", "host": target_host}

# 6. Auth Endpoints
@app.post("/api/v1/login")
async def login(creds: LoginRequest):
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT password_hash, totp_secret FROM users WHERE username = ?", (creds.username,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    stored_hash, totp_secret = row
                    if stored_hash == hash_pw(creds.password):
                        # Check 2FA
                        if totp_secret:
                            if not creds.code:
                                 return JSONResponse(status_code=403, content={"detail": "2FA Required", "require_2fa": True})
                            
                            if not isinstance(totp_secret, str):
                                totp_secret = str(totp_secret)
                                
                            totp = pyotp.TOTP(totp_secret)
                            if not totp.verify(creds.code):
                                 raise HTTPException(status_code=401, detail="Invalid 2FA Code")
    
                        return {"token": SECRET_TOKEN} 
        raise HTTPException(status_code=401, detail="Invalid credentials")
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"detail": f"Login Error: {str(e)}"})

class SetupRequest(BaseModel):
    password: str

@app.get("/api/v1/setup/status")
async def setup_status():
    initialized = system_settings.get("initialized") == "true"
    return {"initialized": initialized}

@app.post("/api/v1/setup")
async def setup(req: SetupRequest):
    if system_settings.get("initialized") == "true":
        raise HTTPException(status_code=400, detail="Already initialized")
    if len(req.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET password_hash = ? WHERE username = 'admin'", (hash_pw(req.password),))
        await db.execute("INSERT OR REPLACE INTO system_config (key, value) VALUES (?, ?)", ("initialized", "true"))
        await db.commit()
    system_settings["initialized"] = "true"
    logger.info("System initialized by setup wizard.")
    return {"status": "ok"}

@app.post("/api/v1/user/password")
async def change_password(req: ContentChangePassword, token: str = Header(None, alias="Authorization")):
    if token != SECRET_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT password_hash FROM users WHERE username = 'admin'") as cursor:
            row = await cursor.fetchone()
            if not row or row[0] != hash_pw(req.old_password):
                 raise HTTPException(status_code=400, detail="Old password incorrect")
        
        await db.execute("UPDATE users SET password_hash = ? WHERE username = 'admin'", (hash_pw(req.new_password),))
        await db.commit()
    return {"status": "ok"}

@app.get("/api/v1/alerts/history")
async def get_alert_history(limit: int = 50, start_ts: Optional[int] = None, end_ts: Optional[int] = None, token: str = Header(None, alias="Authorization")):
    if token != SECRET_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        import json
        with sqlite3.connect(DB_PATH) as conn:
            c = conn.cursor()
            query = "SELECT id, ts, svid, type, title, content, context FROM alert_history WHERE 1=1"
            params = []
            if start_ts:
                query += " AND ts >= ?"
                params.append(start_ts)
            if end_ts:
                query += " AND ts <= ?"
                params.append(end_ts)
            
            query += " ORDER BY id DESC LIMIT ?"
            params.append(limit)
            
            c.execute(query, tuple(params))
            rows = c.fetchall()
            ret = []
            for r in rows:
                try:
                    ctx = json.loads(r[6])
                except:
                    ctx = {}
                ret.append({
                    "id": r[0],
                    "ts": r[1],
                    "svid": r[2],
                    "type": r[3],
                    "title": r[4],
                    "content": r[5],
                    "context": ctx
                })
            return ret
    except Exception as e:
        logger.error(f"DB Error: {e}")
        return []


@app.get("/api/v1/user/me")
async def get_user_info(token: str = Header(None, alias="Authorization")):
    if token != SECRET_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT totp_secret FROM users WHERE username = 'admin'") as cursor:
            row = await cursor.fetchone()
            has_2fa = bool(row and row[0])
            
    return {"username": "admin", "has_2fa": has_2fa, "agent_token": AGENT_TOKEN}

@app.post("/api/v1/auth/2fa/disable")
async def disable_2fa(token: str = Header(None, alias="Authorization")):
    if token != SECRET_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET totp_secret = NULL WHERE username = 'admin'")
        await db.commit()
    
    return {"status": "ok"}

@app.post("/api/v1/auth/2fa/generate")
async def generate_2fa(token: str = Header(None, alias="Authorization")):
    if token != SECRET_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

    secret = pyotp.random_base32()
    totp = pyotp.TOTP(secret)
    uri = totp.provisioning_uri(name="admin", issuer_name="HeiHei Probe")
    
    # Generate QR
    img = qrcode.make(uri)
    buf = io.BytesIO()
    img.save(buf)
    b64 = base64.b64encode(buf.getvalue()).decode()
    
    return {"secret": secret, "qr_code": b64}

@app.post("/api/v1/auth/2fa/enable")
async def enable_2fa(req: ContentEnable2FA, token: str = Header(None, alias="Authorization")):
    if token != SECRET_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

    totp = pyotp.TOTP(req.secret)
    if not totp.verify(req.code):
         raise HTTPException(status_code=400, detail="Invalid Code")
         
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET totp_secret = ? WHERE username = 'admin'", (req.secret,))
        await db.commit()
    
    return {"status": "ok"}

# --- Notification API ---
# --- Notification API ---





@app.post("/api/v1/settings/notify/test")
async def test_notify(token: str = Header(None, alias="Authorization")):
    if token != SECRET_TOKEN: raise HTTPException(401)
    await send_notification("🔔 测试消息", "这是一条来自探针的测试报警消息。\n如果收到此消息，说明配置成功！")
    return {"status": "ok"}

@app.get("/api/v1/settings/notify")
async def get_notify_settings(token: str = Header(None, alias="Authorization")):
    if token != SECRET_TOKEN: raise HTTPException(401)
    return {
        "tg_token": system_settings.get("notify_tg_token", ""),
        "tg_chat_id": system_settings.get("notify_tg_chat_id", ""),
        "bark_server": system_settings.get("notify_bark_server", "http://bark-server:8080"),
        "bark_key": system_settings.get("notify_bark_key", ""),
        "enabled": system_settings.get("notify_enabled") == "true",
        "latency_enable": system_settings.get("notify_latency_enable") == "true",
        "latency_threshold": int(system_settings.get("notify_latency_threshold", "210")),
        "latency_isp_ct": system_settings.get("notify_latency_isp_ct", "true") == "true",
        "latency_isp_cu": system_settings.get("notify_latency_isp_cu", "true") == "true",
        "latency_isp_cm": system_settings.get("notify_latency_isp_cm", "true") == "true",
        "traffic_rate_enable": system_settings.get("notify_traffic_rate_enable") == "true",
        "traffic_rate_threshold": float(system_settings.get("notify_traffic_rate_threshold", "30.0")),
    }

@app.post("/api/v1/settings/notify")
async def save_notify_settings(cfg: NotificationConfig, token: str = Header(None, alias="Authorization")):
    if token != SECRET_TOKEN: raise HTTPException(401)
    await save_system_setting("notify_tg_token", cfg.tg_token)
    await save_system_setting("notify_tg_chat_id", cfg.tg_chat_id)
    await save_system_setting("notify_bark_server", cfg.bark_server)
    await save_system_setting("notify_bark_key", cfg.bark_key)
    await save_system_setting("notify_enabled", "true" if cfg.enabled else "false")
    await save_system_setting("notify_latency_enable", "true" if cfg.latency_enable else "false")
    await save_system_setting("notify_latency_threshold", str(cfg.latency_threshold))
    await save_system_setting("notify_latency_isp_ct", "true" if cfg.latency_isp_ct else "false")
    await save_system_setting("notify_latency_isp_cu", "true" if cfg.latency_isp_cu else "false")
    await save_system_setting("notify_latency_isp_cm", "true" if cfg.latency_isp_cm else "false")
    await save_system_setting("notify_traffic_rate_enable", "true" if cfg.traffic_rate_enable else "false")
    await save_system_setting("notify_traffic_rate_threshold", str(cfg.traffic_rate_threshold))
    return {"status": "ok"}

# --- Monitor Task (Updated) ---
async def monitor_alerts_task():
    logger.info("Alert Monitor Started")
    while True:
        try:
            await asyncio.sleep(10)
            now = int(time.time())
            
            lat_enable = system_settings.get("notify_latency_enable") == "true"
            lat_threshold = int(system_settings.get("notify_latency_threshold", "210"))
            check_ct = system_settings.get("notify_latency_isp_ct", "true") == "true"
            check_cu = system_settings.get("notify_latency_isp_cu", "true") == "true"
            check_cm = system_settings.get("notify_latency_isp_cm", "true") == "true"
            rate_enable = system_settings.get("notify_traffic_rate_enable") == "true"
            rate_threshold_mbps = float(system_settings.get("notify_traffic_rate_threshold", "30.0"))

            for sid, s in list(server_cache.items()):
                # Helper for ID Masking in Notifications
                def notify_mask_ip(ip_str):
                    if not ip_str: return "Unknown"
                    if '.' in ip_str:
                         parts = ip_str.split('.')
                         if len(parts) == 4:
                             return f"{parts[0]}.{parts[1]}.*.*"
                    return "Hidden"

                # 1. Offline Check
                is_offline = (now - s.last_seen) > 90
                if is_offline:
                    if s.alert_status != 'down':
                        s.alert_status = 'down'
                        s.online = False
                        logger.warning(f"Server Down: {s.name or s.host}")
                        msg = f"服务器离线: {s.alias or s.name or s.host}\nIP: {notify_mask_ip(s.ip_address)}\n时间: {time.strftime('%Y-%m-%d %H:%M:%S')}"
                        
                        record_alert(sid, "OFFLINE", "🔴 服务器报警", msg, {
                            "last_seen": s.last_seen,
                            "now": now,
                            "diff": now - s.last_seen,
                            "ip": s.ip_address
                        })
                        await send_notification("🔴 服务器报警", msg)
                else:
                    if s.alert_status == 'down':
                        s.alert_status = 'up'
                        s.online = True
                        logger.info(f"Server Up: {s.name or s.host}")
                        msg = f"服务器上线: {s.alias or s.name or s.host}\nIP: {notify_mask_ip(s.ip_address)}\n时间: {time.strftime('%Y-%m-%d %H:%M:%S')}"
                        
                        record_alert(sid, "ONLINE", "🟢服务器恢复", msg, {
                            "last_seen": s.last_seen,
                            "now": now,
                            "ip": s.ip_address
                        })
                        await send_notification("🟢服务器恢复", msg)

                    else:
                        if s.alert_status != 'up': s.alert_status = 'up'
                
                # 2. Latency Check (Only if Online and Enabled)
                if lat_enable and s.online:
                    # Filter Pings based on Settings
                    targets = []
                    if check_ct and s.ping_189 > 0: targets.append(s.ping_189)
                    if check_cu and s.ping_10010 > 0: targets.append(s.ping_10010)
                    if check_cm and s.ping_10086 > 0: targets.append(s.ping_10086)
                    
                    if targets:
                        max_ping = max(targets)
                        if max_ping > lat_threshold:
                            # Alert if: 1. Status Changed OR 2. Last Alert > 300s (5min)
                            should_alert = False
                            if s.latency_status != 'high': should_alert = True
                            if (now - s.last_alert_ts) > 300: should_alert = True
                            
                            if should_alert:
                                s.latency_status = 'high'
                                s.last_alert_ts = now
                                logger.warning(f"High Latency: {s.name or s.host} ({max_ping}ms)")
                                # Ensure display name doesn't leak IP if no alias/name
                                safe_name = s.alias or s.name or notify_mask_ip(s.ip_address)
                                msg = f"服务器: {safe_name}\nIP: {notify_mask_ip(s.ip_address)}\n最大延迟: {max_ping}ms (阈值 {lat_threshold}ms)\n三网: CT{s.ping_189}|CU{s.ping_10010}|CM{s.ping_10086}"
                                
                                record_alert(sid, "LATENCY", "⚠️ 高延迟报警", msg, {
                                     "ping_max": max_ping,
                                     "threshold": lat_threshold,
                                     "ct": s.ping_189,
                                     "cu": s.ping_10010,
                                     "cm": s.ping_10086
                                })
                                await send_notification("⚠️ 高延迟报警", msg)
                        else:
                            if s.latency_status == 'high':
                                s.latency_status = 'normal'
                                # Optional: Clear throttle or send recovery?
                                # Usually better to just resolve.
                                logger.info(f"Latency Normal: {s.name or s.host}")

                # 3. Traffic Rate Alert
                if rate_enable and s.online:
                    # Convert bytes/s to MB/s (Binary): / 1024 / 1024
                    in_mbs = s.net_in_speed / 1024 / 1024
                    out_mbs = s.net_out_speed / 1024 / 1024
                    max_mbs = max(in_mbs, out_mbs)
                    
                    # Threshold logic: User Per-Server or Global
                    # Priority: s.traffic_rate_threshold > rate_threshold_mbps (Global)
                    current_threshold = rate_threshold_mbps
                    if s.traffic_rate_threshold is not None and s.traffic_rate_threshold > 0:
                        current_threshold = s.traffic_rate_threshold

                    # Threshold is now treated as MB/s
                    if max_mbs > current_threshold:
                         # Always update status to high
                         s.rate_status = 'high'
                         
                         # Alert purely on Cooldown (Strict 10 mins)
                         if (now - s.last_rate_alert_ts) > 600:
                             s.last_rate_alert_ts = now
                             
                             # Format Human Readable
                             if max_mbs < 1:
                                 disp_val = f"{max_mbs * 1024:.2f} KB/s"
                                 disp_thresh = f"{current_threshold * 1024:.2f} KB/s"
                             else:
                                 disp_val = f"{max_mbs:.2f} MB/s"
                                 disp_thresh = f"{current_threshold:.2f} MB/s"
                                 
                                 
                             logger.warning(f"High Traffic Rate: {s.name or s.host} ({disp_val})")
                             safe_name = s.alias or s.name or notify_mask_ip(s.ip_address)
                             msg = f"服务器: {safe_name}\nIP: {notify_mask_ip(s.ip_address)}\n当前速率: {disp_val} (阈值 {disp_thresh})\n方向: {'上传' if out_mbs > in_mbs else '下载'}"
                             
                             record_alert(sid, "TRAFFIC", "🚀 异常流量报警", msg, {
                                 "rate_mbps": max_mbs,
                                 "threshold_mbps": current_threshold,
                                 "in_mbps": in_mbs,
                                 "out_mbps": out_mbs,
                                 "disp_val": disp_val
                             })
                             await send_notification("🚀 异常流量报警", msg)
                    else:
                        s.rate_status = 'normal'

        except Exception as e:
            logger.error(f"Monitor Task Error: {e}")
            await asyncio.sleep(5)

# --- Dynamic File Serving ---
@app.get("/agent.py")
async def serve_agent_py():
    return HTMLResponse(content=get_agent_py_content(), media_type="text/x-python")

@app.get("/install_agent.sh")
async def serve_install_sh(request: Request):
    try:
        with open("web/install_agent.sh", "r") as f:
            content = f.read()
        
        host = request.headers.get("host") or "localhost:8081"
        content = content.replace("{{SERVER_HOST}}", host)
        content = content.replace("{{AGENT_TOKEN}}", AGENT_TOKEN)
        
        return Response(content=content, media_type="text/x-shellscript")
    except Exception as e:
        logger.error(f"Install Script Error: {e}")
        return Response(content="# Error generating script", media_type="text/plain", status_code=500)

# --- Static Files ---
# Mount static but exclude agent.py/install.sh if they collide (FastAPI order matters)
# Since we defined specific routes above, they take precedence.
app.mount("/", StaticFiles(directory="web", html=True), name="static")

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=PORT, reload=False)
