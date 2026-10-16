"""Gracefully signal only an isolated, confirmed project server console."""
import ctypes
import os
import subprocess
import sys
from pathlib import Path

if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    if sys.argv[1] == 'stop':
        target = int(sys.argv[2])
        allowed = {target, int(sys.argv[3]), os.getpid(), *(int(pid) for pid in sys.argv[4:])}
        kernel.FreeConsole()
        if not kernel.AttachConsole(target):
            raise RuntimeError(f'Cannot attach server console: {ctypes.get_last_error()}')
        kernel.SetConsoleCtrlHandler(None, True)
        ids = (ctypes.c_uint32 * 64)()
        count = kernel.GetConsoleProcessList(ids, 64)
        members = set(ids[:count])
        if not count or count > 64 or not members <= allowed:
            kernel.FreeConsole()
            raise RuntimeError(f'Console shared; refusing signal. Member PIDs: {sorted(members)}')
        ok = kernel.GenerateConsoleCtrlEvent(0, 0)
        kernel.FreeConsole()
        if not ok:
            raise RuntimeError(f'Graceful signal failed: {ctypes.get_last_error()}')
        print('Graceful CTRL_C delivered to isolated project console')
    elif sys.argv[1] == 'start':
        with (root / 'test/web_restart_stdout.log').open('ab') as out, (root / 'test/web_restart_stderr.log').open('ab') as err:
            proc = subprocess.Popen([str(root / '.venv/Scripts/python.exe'), '-m', 'gpt_signup_hybrid', 'web', '--host', '127.0.0.1', '--port', '8083'], cwd=root, stdin=subprocess.DEVNULL, stdout=out, stderr=err, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NEW_CONSOLE)
        print('Persistent launcher PID:', proc.pid)
