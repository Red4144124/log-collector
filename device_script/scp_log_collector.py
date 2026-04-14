#!/usr/bin/env python3

import os
import sys
import time
import signal
import threading
import subprocess
import json
import gzip
import shutil
import tempfile
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
        
        # Файл для хранения позиций отправки
        self.state_file = self.state_dir / 'send_positions.json'
        self.send_positions = self.load_send_positions()
        
        self.current_files = {}   # source_name -> file object
        self.current_sizes = {}   # source_name -> size
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
                'send_interval_minutes': 5
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
    
    def load_send_positions(self):
        if self.state_file.exists():
            try:
                with open(self.state_file, 'r') as f:
                    return json.load(f)
            except:
                return {}
        return {}
    
    def save_send_positions(self):
        with open(self.state_file, 'w') as f:
            json.dump(self.send_positions, f, indent=2)
    
    def setup_handlers(self):
        def handler(signum, frame):
            print(f"\nSignal {signum}, shutting down...")
            self.save_send_positions()
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
            if src not in self.send_positions:
                self.send_positions[src] = 0
            print(f"Init {src} -> {filepath} (size {self.current_sizes[src]} bytes, send_pos {self.send_positions[src]})")
    
    def rotate_file(self, source_name):
        old_path = self.local_live_dir / f"{source_name}.log"
        if not old_path.exists():
            return
        
        # Закрываем текущий файл
        if source_name in self.current_files:
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
            with open(new_rotated, 'rb') as f_in, gzip.open(f"{new_rotated}.gz", 'wb') as f_out:
                shutil.copyfileobj(f_in, f_out)
            new_rotated.unlink()
        
        # Открываем новый файл
        self.current_files[source_name] = open(old_path, 'a')
        self.current_sizes[source_name] = 0
        # При ротации сбрасываем позицию отправки
        self.send_positions[source_name] = 0
        self.stats['rotations'] += 1
        print(f"Rotated {source_name}, reset send position")
    
    def write_log(self, source_name, line, timestamp=None):
        if timestamp is None:
            timestamp = datetime.now()
        log_line = f"[{timestamp.isoformat()}] {line}\n"
        self.ring_buffer.append(log_line)
        
        fd = self.current_files.get(source_name)
        if fd and not fd.closed:
            fd.write(log_line)
            self.current_sizes[source_name] += len(log_line)
            self.stats['lines_written'] += 1
            if self.stats['lines_written'] % 100 == 0:
                fd.flush()
            
            max_size = self.config['buffer']['max_file_size_mb'] * 1024 * 1024
            if self.current_sizes[source_name] > max_size:
                self.rotate_file(source_name)
        else:
            # Файл закрыт – переоткрываем
            filename = f"{source_name}.log"
            filepath = self.local_live_dir / filename
            self.current_files[source_name] = open(filepath, 'a')
            self.current_sizes[source_name] = filepath.stat().st_size if filepath.exists() else 0
            self.write_log(source_name, line, timestamp)
        
        self.stats['lines_received'] += 1
    
    def send_via_scp(self):
        if not self.config['remote']['enabled']:
            return
        
        try:
            target = self.config['remote']['scp_target'].rstrip('/')
            if ':' not in target:
                print("ERROR: invalid scp_target format")
                return
            host_part, path_part = target.split(':', 1)
            key = self.config['remote']['ssh_key_path']
            device_name = os.uname().nodename
            
            any_sent = False
            
            for src, cfg in self.config['sources'].items():
                if not cfg.get('enabled', True):
                    continue
                local_file = self.local_live_dir / f"{src}.log"
                if not local_file.exists():
                    continue
                
                current_size = local_file.stat().st_size
                last_pos = self.send_positions.get(src, 0)
                
                # Если файл уменьшился (ротация внешняя) – сбросить позицию
                if current_size < last_pos:
                    last_pos = 0
                
                if current_size <= last_pos:
                    continue  # Нет новых данных
                
                # Читаем новые данные
                with open(local_file, 'r') as f:
                    f.seek(last_pos)
                    new_data = f.read()
                
                if not new_data:
                    continue
                
                # Создаём локальный временный файл только с новыми данными
                with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.log') as tmp:
                    tmp.write(new_data)
                    tmp_path = tmp.name
                
                # Целевая директория и файл на сервере
                target_dir = f"{path_part}/{device_name}"
                target_file = f"{target_dir}/{src}.log"
                
                # Копируем временный файл на сервер во временное имя
                temp_remote = f"{path_part}/temp_{device_name}_{src}_{int(time.time())}.log"
                scp_cmd = ['scp', '-i', key, '-o', 'ConnectTimeout=10',
                           tmp_path, f"{host_part}:{temp_remote}"]
                res = subprocess.run(scp_cmd, capture_output=True, timeout=30)
                os.unlink(tmp_path)
                
                if res.returncode != 0:
                    print(f"SCP copy failed for {src}: {res.stderr.decode()}")
                    self.stats['scp_failed'] += 1
                    continue
                
                # Дописываем временный файл в целевой и удаляем его
                ssh_cmd = ['ssh', '-i', key, '-o', 'ConnectTimeout=10', host_part,
                           f'mkdir -p {target_dir} && cat {temp_remote} >> {target_file} && rm {temp_remote}']
                res = subprocess.run(ssh_cmd, capture_output=True, timeout=30)
                
                if res.returncode == 0:
                    print(f"Sent {len(new_data)} new bytes to {device_name}/{src}.log (pos {last_pos} -> {current_size})")
                    self.send_positions[src] = current_size
                    self.stats['scp_sent'] += 1
                    any_sent = True
                else:
                    print(f"Append failed for {src}: {res.stderr.decode()}")
                    self.stats['scp_failed'] += 1
            
            if any_sent:
                self.save_send_positions()
            
        except Exception as e:
            self.stats['scp_failed'] += 1
            print(f"SCP error: {e}")
    
    def emergency_flush(self):
        for fd in self.current_files.values():
            if fd and not fd.closed:
                fd.flush()
        self.save_send_positions()
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
            # Прочитать уже существующие строки (для начального заполнения live-файла)
            for line in f:
                if line.strip():
                    self.write_log(src, line.strip())
            f.seek(0, os.SEEK_END)
            while self.running:
                line = f.readline()
                if line:
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
        def flush_worker():
            while self.running:
                time.sleep(self.config['buffer']['flush_interval_seconds'])
                for src, fd in self.current_files.items():
                    if fd and not fd.closed:
                        try:
                            fd.flush()
                        except Exception as e:
                            print(f"Flush error for {src}: {e}")
        threading.Thread(target=flush_worker, daemon=True).start()
        
        def scp_worker():
            while self.running:
                interval = self.config['remote'].get('send_interval_minutes', 5) * 60
                time.sleep(interval)
                if self.config['remote']['enabled']:
                    self.send_via_scp()
        threading.Thread(target=scp_worker, daemon=True).start()
        
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
        print("=== SCP Log Collector (incremental) Started ===")
        print(f"Live dir: {self.local_live_dir}")
        print(f"SCP target: {self.config['remote']['scp_target']}")
        last_status = time.time()
        try:
            while self.running:
                time.sleep(1)
                if time.time() - last_status > 60:
                    print(f"[Status] lines: {self.stats['lines_received']} recv, rotations: {self.stats['rotations']}, SCP ok/fail: {self.stats['scp_sent']}/{self.stats['scp_failed']}")
                    last_status = time.time()
        except KeyboardInterrupt:
            pass
        finally:
            self.save_send_positions()
            self.emergency_flush()
            for fd in self.current_files.values():
                if fd and not fd.closed:
                    fd.close()

if __name__ == "__main__":
    collector = AppendSCPLogCollector()
    collector.run()