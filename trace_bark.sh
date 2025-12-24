#!/bin/bash

# Default to checking last 5 messages
COUNT=${1:-5}

echo "🔍 正在追踪最近 $COUNT 条 Bark 推送背后的真相..."
echo "💡 注意：时间显示为 UTC (比北京时间慢8小时)，但逻辑已自动对齐。"
echo "==================================================="

# 1. Start Loop over recent Bark logs
# We use -t to get Docker's native timestamp for precise correlation
docker compose logs -t --no-log-prefix bark 2>/dev/null | grep "/push" | tail -n "$COUNT" | while read -r line; do
    
    # 2. Extract Info
    # TS Format: 2025-12-24T17:32:33.425...Z
    TS=$(echo "$line" | awk '{print $1}')
    
    # Display friendly time
    DISPLAY_TIME=$(echo "$TS" | sed 's/T/ /;s/Z//;s/\..*//')
    
    # Generate Search Pattern (YYYY-MM-DDTHH:MM) - Minute Precision
    SEARCH_PATTERN=$(echo "$TS" | cut -d: -f1,2)
    
    echo ""
    echo "📨 [推送时间] $DISPLAY_TIME (UTC)"
    echo "   [Bark日志] $line"
    echo "   ⬇️ 核心程序(heihei-core) 在这一分钟发生了什么："
    echo "   ---------------------------------------------------"
    
    # 3. Search Core Logs
    # First try to find "Smoking Gun" (Warnings/Errors)
    CRITICAL_LOGS=$(docker compose logs -t --no-log-prefix heihei-core 2>/dev/null | grep "$SEARCH_PATTERN" | grep -iE "WARNING|ERROR|OFFLINE|ONLINE")
    
    if [ -n "$CRITICAL_LOGS" ]; then
        # Found critical logs, show them
        echo "$CRITICAL_LOGS" | sed 's/^/   🔥 /'
    else
        # No critical logs, show context (INFO)
        # Maybe it was a test message or normal operation
        CONTEXT_LOGS=$(docker compose logs -t --no-log-prefix heihei-core 2>/dev/null | grep "$SEARCH_PATTERN" | tail -n 5)
        if [ -n "$CONTEXT_LOGS" ]; then
            echo "   (未发现明显报错，显示上下文...)"
            echo "$CONTEXT_LOGS" | sed 's/^/   ℹ️ /'
        else
            echo "   (❌ 这一分钟没有核心日志，可能是日志已轮替或容器刚重启)"
        fi
    fi
    echo "   ---------------------------------------------------"
done

echo ""
echo "✅ 追踪完成。"
