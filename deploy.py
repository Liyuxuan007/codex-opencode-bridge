"""Install the runtime into Linux storage, leaving credentials and task history in WSL."""
import argparse
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import time
import bridge


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--target', default=str(Path.home() / '.local/share/codex-opencode-bridge'))
    args = parser.parse_args()
    source = Path(__file__).resolve().parent
    target = Path(args.target).resolve()
    with socket.socket() as probe:
        listening = probe.connect_ex(('127.0.0.1', bridge.CONFIG['broker_port'])) == 0
    if listening:
        status = bridge.broker_call('bridge_health')
        if not status.get('ok'):
            raise RuntimeError('Existing broker is unhealthy; inspect it before replacing the runtime.')
        counts = status['data']['counts']
        if any(counts.get(key, 0) for key in bridge.ACTIVE | {'queued'}):
            raise RuntimeError('Finish or cancel active/queued jobs before deploying.')
        pid = int((bridge.STATE / 'broker-pid').read_text())
        command = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
        allowed = [str(source / 'bridge.py').encode(), str(target / 'bridge.py').encode()]
        if not any(path in command for path in allowed) or b'broker' not in command:
            raise RuntimeError('PID does not belong to this bridge; nothing was stopped.')
        os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            time.sleep(.1)
            with socket.socket() as probe:
                if probe.connect_ex(('127.0.0.1', bridge.CONFIG['broker_port'])) != 0:
                    break
    target.mkdir(parents=True, exist_ok=True)
    for name in ['bridge.py', 'control_plane.py', 'artifacts.py', 'test_bridge.py',
                 'test_control_plane.py', 'test_artifacts.py', 'test_integration.py', 'verify_live.py', 'deploy.py']:
        destination = target / name
        if destination.resolve() != (source / name).resolve():
            shutil.copy2(source / name, destination)
    # The local config stays private and is never committed.
    if bridge.CONFIG_PATH.resolve() != (target / 'config.json').resolve():
        shutil.copy2(bridge.CONFIG_PATH, target / 'config.json')
    (target / 'scripts').mkdir(exist_ok=True)
    telemetry_source = source / 'scripts/measure_usage.py'
    if telemetry_source.is_file() and telemetry_source.resolve() != (target / 'scripts/measure_usage.py').resolve():
        shutil.copy2(telemetry_source, target / 'scripts/measure_usage.py')
    binary = Path.home() / '.local/bin/codex-opencode'
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text('#!/bin/sh\nexec /usr/bin/python3 ' + __import__('shlex').quote(str(target / 'bridge.py')) + ' "$@"\n')
    binary.chmod(0o755)
    result = subprocess.run([sys.executable, str(target / 'bridge.py'), 'doctor'], check=True)
    print(json.dumps({'installed': str(target), 'command': str(binary)}))


if __name__ == '__main__':
    main()
