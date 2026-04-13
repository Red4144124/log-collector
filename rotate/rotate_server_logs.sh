#!/bin/bash
LOG_BASE="/home/loguser/device_logs"
MAX_SIZE_MB=100
MAX_BACKUPS=5

find "$LOG_BASE" -type f -name "*.log" | while read logfile; do
    size=$(stat -c%s "$logfile")
    max_size=$((MAX_SIZE_MB * 1024 * 1024))
    if [ $size -gt $max_size ]; then
        dir=$(dirname "$logfile")
        base=$(basename "$logfile" .log)
        for i in $(seq $MAX_BACKUPS -1 1); do
            [ -f "$dir/${base}.log.$i.gz" ] && mv "$dir/${base}.log.$i.gz" "$dir/${base}.log.$((i+1)).gz"
        done
        gzip -c "$logfile" > "$dir/${base}.log.1.gz"
        > "$logfile"
        echo "Rotated $logfile"
    fi
done