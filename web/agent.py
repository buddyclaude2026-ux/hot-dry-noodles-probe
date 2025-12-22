import os
import time
import psutil
import requests
import platform
import subprocess
import argparse
import socket
import sys

# --- Configuration ---
SERVER_URL = "http://185.186.147.118:8081/api/v1/report"
SECRET_TOKEN = "HEIHEI_REPORT_VX" # Low Privilege Token
INTERVAL = 2
PING_TIMEOUT = 3
CACHED_IP = None

# Argparse for override
# Argparse for override
parser = argparse.ArgumentParser()
parser.add_argument("--server", help="Server URL", default=SERVER_URL)
parser.add_argument("--token", help="Auth Token", default=None)
parser.add_argument("--db-path", help="Path to SQLite DB for auto-config", default=None) # New: Read from DB
parser.add_argument("--name", help="Server Name", default=None)
parser.add_argument("--country", help="Country Code", default=None)
args = parser.parse_args()

def get_token_from_db(db_path):
    import sqlite3
    try:
        # Wait a bit for DB to be created by Core
        if not os.path.exists(db_path):
            time.sleep(5) 
            
        con = sqlite3.connect(db_path)
        cur = con.cursor()
        cur.execute("SELECT value FROM system_config WHERE key='agent_token'")
        row = cur.fetchone()
        con.close()
        if row:
            print(f"Loaded Token from DB: {row[0]}")
            return row[0]
    except Exception as e:
        print(f"DB Read Error: {e}")
    return SECRET_TOKEN

# Resolve Token
if args.token:
    token = args.token
elif args.db_path:
    # Loop wait for DB to be ready
    while True:
        token = get_token_from_db(args.db_path)
        if token != "HEIHEI_REPORT_VX": # successfully loaded valid token
            break
        print("Waiting for database initialization...")
        time.sleep(3)
else:
    token = SECRET_TOKEN

args.token = token # Assign back to args for main loop

def get_os_info():
    # 1. Try Host OS (Mapped Volume)
    try:
        if os.path.exists("/host/etc/os-release"):
            with open("/host/etc/os-release") as f:
                for line in f:
                    if line.startswith("PRETTY_NAME="):
                        return line.split("=", 1)[1].strip().strip('"')
    except:
        pass

    # 2. Try Local Container/System OS
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except:
        pass
    
    # 3. Try /etc/issue (e.g. Debian 12 \n \l)
    try:
        with open("/etc/issue") as f:
            return f.read().split("\\")[0].strip()
    except:
        pass
        
    return platform.platform()

def get_ping(host):
    try:
        # Linux/Mac ping -c 1 -W 1 (1 second timeout)
        # Check OS
        param = '-n' if platform.system().lower() == 'windows' else '-c'
        timeout_param = '-w' if platform.system().lower() == 'windows' else '-W'
        
        # 1 packet, N second timeout
        cmd = ['ping', param, '1', timeout_param, str(PING_TIMEOUT), host]
        
        # Run
        start = time.time()
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        end = time.time()
        
        return round((end - start) * 1000, 1) # ms
    except:
        return 0.0

def get_status():
    # CPU
    cpu = psutil.cpu_percent(interval=0.1)
    cpu_count = psutil.cpu_count(logical=True)
    
    # Mem
    mem = psutil.virtual_memory()
    
    # Disk
    disk = psutil.disk_usage('/')
    
    # Network Speed (Diff - Excluding Localhost/Docker)
    def get_network_io():
        io = psutil.net_io_counters(pernic=True)
        sent = 0
        recv = 0
        for nic, stats in io.items():
            # Filter out Loopback, Docker Bridge, veth
            if nic == 'lo' or 'docker' in nic or 'veth' in nic or 'br-' in nic:
                continue
            sent += stats.bytes_sent
            recv += stats.bytes_recv
        return sent, recv

    s1, r1 = get_network_io()
    time.sleep(0.5)
    s2, r2 = get_network_io()
    
    sent_speed = (s2 - s1) / 0.5
    recv_speed = (r2 - r1) / 0.5
    
    # Pings (CT, CU, CM)
    # Using reliable Backbone IPs to avoid DNS issues/timeouts
    p_189 = get_ping("202.97.29.133")   # CT Backbone (BJ)
    p_cu = get_ping("219.158.3.69")     # CU Backbone (Reliable) 
    p_cm = get_ping("221.179.155.161")  # CM Backbone (BJ)
    
    # Try Public IP (Cached)
    global CACHED_IP
    if not CACHED_IP:
        try:
             CACHED_IP = requests.get("http://ipv4.icanhazip.com", timeout=3).text.strip()
        except:
             pass
    public_ip = CACHED_IP

    return {
        "host": socket.gethostname(), # API will overwrite with real IP for security
        "uuid": get_uuid(), # Persistent ID
        "name": args.name if args.name else socket.gethostname(),
        "country_code": args.country if args.country else None,
        "ip_address": public_ip, # Send explicit IP
        "type": platform.system(),
        "online": True,
        "uptime": int(time.time() - psutil.boot_time()),
        "load": psutil.getloadavg()[0] if hasattr(psutil, "getloadavg") else 0.0,
        "network_in": int(r2),
        "network_out": int(s2),
        "net_in_speed": int(recv_speed),
        "net_out_speed": int(sent_speed),
        "cpu": cpu,
        "memory_total": mem.total,
        "memory_used": mem.used,
        "disk_total": disk.total,
        "disk_used": disk.used,
        "cpu_count": cpu_count,
        "os_release": get_os_info(), # Use new Logic
        "ping_189": p_189,
        "ping_10010": p_cu,
        "ping_10086": p_cm
    }

def get_uuid():
    # Persistent UUID to identify this agent uniquely (even if IP changes)
    uuid_file = "agent_uuid.txt"
    if args.db_path: # If running in Docker with mapped DB, store UUID next to it
         uuid_file = os.path.dirname(args.db_path) + "/agent_uuid.txt"
         
    if os.path.exists(uuid_file):
        try:
            with open(uuid_file, 'r') as f:
                return f.read().strip()
        except:
            pass
            
    # Generate new
    import uuid
    new_id = str(uuid.uuid4())
    try:
        with open(uuid_file, 'w') as f:
            f.write(new_id)
    except Exception as e:
        print(f"Warning: Could not save UUID: {e}")
        
    return new_id

def main():
    print(f"Agent started. Reporting to {args.server}")
    while True:
        try:
            data = get_status()
            headers = {"Authorization": args.token}
            requests.post(args.server, json=data, headers=headers, timeout=5)
            # print("Report sent.", end='\r')
        except Exception as e:
            print(f"\nError: {e}")
        
        time.sleep(INTERVAL)

if __name__ == "__main__":
    main()
