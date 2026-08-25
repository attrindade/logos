"""Background service and auto-startup manager (Windows and Linux)."""
import os
import shutil
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _install_windows():
    appdata = os.getenv("APPDATA")
    if not appdata:
        print("[ERROR] APPDATA environment variable not found on Windows.")
        return False

    startup_dir = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    startup_dir.mkdir(parents=True, exist_ok=True)
    dest_bat = startup_dir / "Logos.bat"
    source_bat = BASE_DIR / "Launch_Logos.bat"

    if not source_bat.exists():
        # Create default Launch_Logos.bat if missing
        pythonw = BASE_DIR / ".venv" / "Scripts" / "pythonw.exe"
        if not pythonw.exists():
            pythonw = "pythonw"
        content = f'@echo off\ncd /d "%~dp0"\nstart "" "{pythonw}" main.py\nexit\n'
        source_bat.write_text(content, encoding="utf-8")

    shutil.copy2(source_bat, dest_bat)
    print(f"[OK] Windows startup entry created:\n     {dest_bat}")
    return True


def _uninstall_windows():
    appdata = os.getenv("APPDATA")
    if not appdata:
        return False
    dest_bat = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Logos.bat"
    if dest_bat.exists():
        dest_bat.unlink()
        print(f"[OK] Removed from Windows Startup: {dest_bat}")
    else:
        print("No startup entry found to remove.")
    return True


def _install_linux():
    systemd_user_dir = Path.home() / ".config" / "systemd" / "user"
    systemd_user_dir.mkdir(parents=True, exist_ok=True)
    service_file = systemd_user_dir / "logos.service"

    python_bin = BASE_DIR / ".venv" / "bin" / "python"
    if not python_bin.exists():
        python_bin = Path(sys.executable)

    service_content = f"""[Unit]
Description=Logos Audio Inbox Watcher Daemon
After=network.target

[Service]
Type=simple
WorkingDirectory={BASE_DIR}
ExecStart={python_bin} {BASE_DIR / 'main.py'}
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
"""
    service_file.write_text(service_content, encoding="utf-8")
    print(f"[OK] systemd user service created at: {service_file}")
    print("\nTo enable and start now on Linux, run:")
    print("  systemctl --user daemon-reload")
    print("  systemctl --user enable --now logos.service")
    return True


def _uninstall_linux():
    service_file = Path.home() / ".config" / "systemd" / "user" / "logos.service"
    if service_file.exists():
        service_file.unlink()
        print(f"[OK] Removed {service_file}")
        print("Run to disable from systemd:")
        print("  systemctl --user disable logos.service")
        print("  systemctl --user daemon-reload")
    else:
        print("No systemd service found to remove.")
    return True


def install():
    if sys.platform == "win32":
        return _install_windows()
    else:
        return _install_linux()


def uninstall():
    if sys.platform == "win32":
        return _uninstall_windows()
    else:
        return _uninstall_linux()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "uninstall":
        uninstall()
    else:
        install()

