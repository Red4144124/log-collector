#!/usr/bin/env python3
# SCP Log Collector - дозапись + ротация + отправка по SCP с Dropbear-ключом

import os
import sys
import time
import signal
import threading
import subprocess
import json
import gzip
import shutil
from pathlib import Path
from datetime import datetime
from collections import deque

class AppendSCPLogCollector:
    def __init__(self, config_path='/etc/log-collector/config.json'):
        self.config = self.load_config(config_path)
        
        self.local_live_dir = Path(self.config['storage']['live_dir'])
        self.local_archive_dir = Path(self.config['storage']['archive_dir'])
        self.state_dir = Path(self.config['storage']['state_dir'])
        for d in [self.local_live_dir, self.local_archive_dir, self.state_dir]:
            d.mkdir(parents=True, exist_ok=True)
        
        self.current_files = {}
        self.current_sizes = {}
        self.ring_buffer = deque(maxlen=self.config['buffer']['ring_buffer_lines'])
        
        self.running = True
        self.stats = {
            'lines_received': 0, 'lines_written': 0,
            'scp_sent': 0, 'scp_failed': 0, 'rotations': 0,
            'start_time': time.time()
        }
        
        self.setup_handlers()
        self.init_source_files()
        self.start_background_tasks()
    
    def load_config(self, path):
        default = {
            'sources': {},
            'storage': {
                'live_dir': '/var/log/live',
                'archive_dir': '/var/log/collected',
                'state_dir': '/var/lib/log-collector'
            },
            'buffer': {
                'ring_buffer_lines': 5000,
                'flush_interval_seconds': 5,
                'max_file_size_mb': 10
            },
            'rotation': {
                'max_rotated_files': 5,
                'compress_rotated': True
            },
            'remote': {
                'enabled': False,
                'scp_target': '',
                'ssh_key_path': '',
                'send_interval_minutes': 5,
                'cleanup_after_send': False
            }
        }
        if os.path.exists(path):
            with open(path, 'r') as f:
                user = json.load(f)
                self._merge(default, user)
        return default
    
    def _merge(self, base, override):
        for k, v in override.items():
            if k in base and isinstance(base[k], dict) and isinstance(v, dict):
                self._merge(base[k], v)
            else:
                base[k] = v
    
    def setup_handlers(self):
        def handler(signum, frame):
            print(f"\nSignal {signum}, shutting down...")
            self.emergency_flush()
            self.running = False
            sys.exit(0)
        signal.signal(signal.SIGTERM, handler)
        signal.signal(signal.SIGINT, handler)
    
    def init_source_files(self):
        for src, cfg in self.config['sources'].items():
            if not cfg.get('enabled', True):
                continue
            filename = f"{src}.log"
            filepath = self.local_live_dir / filename
            self.current_files[src] = open(filepath, 'a')
            self.current_sizes[src] = filepath.stat().st_size if filepath.exists() else 0
            print(f"Init {src} -> {filepath} (size {self.current_sizes[src]} bytes)")
    
    def rotate_file(self, source_name):
        old_path = self.local_live_dir / f"{source_name}.log"
        if not old_path.exists():
            return
        
        self.current_files[source_name].close()
        
        max_files = self.config['rotation']['max_rotated_files']
        rotated = sorted(self.local_live_dir.glob(f"{source_name}.log.*"))
        while len(rotated) >= max_files:
            oldest = rotated.pop(0)
            oldest.unlink()
        
        for i in range(max_files-1, 0, -1):
            src = self.local_live_dir / f"{source_name}.log.{i}"
            dst = self.local_live_dir / f"{source_name}.log.{i+1}"
            if src.exists():
                src.rename(dst)
        
        new_rotated = self.local_live_dir / f"{source_name}.log.1"
        old_path.rename(new_rotated)
        
        if self.config['rotation']['compress_rotated']:
            with open(new_rotated, 'rb') as f_in:
                with gzip.open(f"{new_rotated}.gz", 'wb') as f_out:
                    shutil.copyfileobj(f_in, f_out)
            new_rotated.unlink()
        
        self.current_files[source_name] = open(old_path, 'a')
        self.current_sizes[source_name] = 0
        self.stats['rotations'] += 1
        print(f"Rotated {source_name}")
    
    def write_log(self, source_name, line, timestamp=None):
        if timestamp is None:
            timestamp = datetime.now()
        log_line = f"[{timestamp.isoformat()}] {line}\n"
        self.ring_buffer.append(log_line)
        
        fd = self.current_files.get(source_name)
        if fd:
            fd.write(log_line)
            self.current_sizes[source_name] += len(log_line)
            self.stats['lines_written'] += 1
            if self.stats['lines_written'] % 100 == 0:
                fd.flush()
            
            max_size = self.config['buffer']['max_file_size_mb'] * 1024 * 1024
            if self.current_sizes[source_name] > max_size:
                self.rotate_file(source_name)
        
        self.stats['lines_received'] += 1
    
    def send_via_scp(self):
        if not self.config['remote']['enabled']:
            return
    
        try:
            target = self.config['remote']['scp_target'].rstrip('/')
            # Разделяем строку "user@host:/path" на host и путь
            if ':' not in target:
                print("ERROR: invalid scp_target format, expected user@host:/path")
                return
            host_part, path_part = target.split(':', 1)
            key = self.config['remote']['ssh_key_path']
            device_name = os.uname().nodename
    
            for src, cfg in self.config['sources'].items():
                if not cfg.get('enabled', True):
                    continue
                local_file = self.local_live_dir / f"{src}.log"
                if not local_file.exists() or local_file.stat().st_size == 0:
                    continue
                
                # Путь к временному файлу на сервере (только путь, без хоста)
                temp_remote_path = f"{path_part}/temp_{device_name}_{src}.log"
                # Целевая директория и файл
                target_dir = f"{path_part}/{device_name}"
                target_file = f"{target_dir}/{src}.log"
    
                # Копируем локальный файл на сервер во временный
                scp_cmd = ['scp', '-i', key, '-o', 'ConnectTimeout=10',
                           str(local_file), f"{host_part}:{temp_remote_path}"]
                result = subprocess.run(scp_cmd, capture_output=True, timeout=30)
                if result.returncode != 0:
                    print(f"SCP copy failed for {src}: {result.stderr.decode()}")
                    self.stats['scp_failed'] += 1
                    continue
                
                # Создаём целевую директорию, дописываем временный файл в целевой, удаляем временный
                ssh_cmd = [
                    'ssh', '-i', key, '-o', 'ConnectTimeout=10', host_part,
                    f'mkdir -p {target_dir} && cat {temp_remote_path} >> {target_file} && rm {temp_remote_path}'
                ]
                result = subprocess.run(ssh_cmd, capture_output=True, timeout=30)
                if result.returncode == 0:
                    print(f"Appended {local_file.stat().st_size} bytes to {device_name}/{src}.log")
                    self.stats['scp_sent'] += 1
                else:
                    print(f"Append failed for {src}: {result.stderr.decode()}")
                    self.stats['scp_failed'] += 1
    
                # Если нужно очистить локальный файл после отправки
                if self.config['remote'].get('cleanup_after_send', False):
                    self.current_files[src].close()
                    local_file.unlink()
                    self.current_files[src] = open(local_file, 'a')
                    self.current_sizes[src] = 0
    
        except Exception as e:
            self.stats['scp_failed'] += 1
            print(f"SCP error: {e}")
    
    def emergency_flush(self):
        for fd in self.current_files.values():
            fd.flush()
        emergency = self.local_archive_dir / f"EMERGENCY_{int(time.time())}.log"
        with open(emergency, 'w') as f:
            f.write(f"=== EMERGENCY {datetime.now()} ===\n")
            for line in self.ring_buffer:
                f.write(line)
        print(f"Emergency saved to {emergency}")
        if self.config['remote']['enabled']:
            self.send_via_scp()
    
    def watch_file(self, src, path):
        print(f"Watching {src} -> {path}")
        try:
            f = open(path, 'r')
            # Сначала прочитаем все существующие строки
            f.seek(0, os.SEEK_SET)
            for line in f:
                if line.strip():
                    self.write_log(src, line.strip())
            # Затем перейдём в конец для отслеживания новых
            f.seek(0, os.SEEK_END)
            while self.running:
                line = f.readline()
                if line:
                    print(f"DEBUG: read line from {src}: {line[:50]}")
                    self.write_log(src, line.strip())
                else:
                    time.sleep(0.1)
                    if not os.path.exists(path):
                        break
                    if f.tell() > os.path.getsize(path):
                        print(f"File rotated: {path}")
                        f.close()
                        f = open(path, 'r')
                        f.seek(0, os.SEEK_END)
            f.close()
        except Exception as e:
            print(f"Error watching {path}: {e}")
    
    def watch_command(self, src, cmd):
        proc = subprocess.Popen(cmd.split(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        for line in proc.stdout:
            if not self.running:
                break
            self.write_log(src, line.decode().strip())
    
    def start_background_tasks(self):
        # Flush thread
        def flush_worker():
            while self.running:
                time.sleep(self.config['buffer']['flush_interval_seconds'])
                for fd in self.current_files.values():
                    fd.flush()
        threading.Thread(target=flush_worker, daemon=True).start()
        
        # SCP sender thread
        def scp_worker():
            while self.running:
                interval = self.config['remote'].get('send_interval_minutes', 5) * 60
                time.sleep(interval)
                if self.config['remote']['enabled']:
                    self.send_via_scp()
        threading.Thread(target=scp_worker, daemon=True).start()
        
        # Start watchers
        for src, cfg in self.config['sources'].items():
            if not cfg.get('enabled', True):
                continue
            if 'file' in cfg:
                path = cfg['file']
                if os.path.exists(path):
                    threading.Thread(target=self.watch_file, args=(src, path), daemon=True).start()
            elif cfg.get('type') == 'command':
                cmd = cfg.get('command', src)
                threading.Thread(target=self.watch_command, args=(src, cmd), daemon=True).start()
    
    def run(self):
        print("=== SCP Log Collector Started ===")
        print(f"Live dir: {self.local_live_dir}")
        print(f"SCP target: {self.config['remote']['scp_target']}")
        last_status = time.time()
        try:
            while self.running:
                time.sleep(1)
                if time.time() - last_status > 60:
                    print(f"[Status] lines: {self.stats['lines_received']} recv, "
                          f"rotations: {self.stats['rotations']}, "
                          f"SCP ok/fail: {self.stats['scp_sent']}/{self.stats['scp_failed']}")
                    last_status = time.time()
        except KeyboardInterrupt:
            pass
        finally:
            self.emergency_flush()
            for fd in self.current_files.values():
                fd.close()

if __name__ == "__main__":
    collector = AppendSCPLogCollector()
    collector.run()