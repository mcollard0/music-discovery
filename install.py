#!/usr/bin/env python3
"""Install this program and its system services; run with sudo."""
import grp
import json
import os
import pwd
import shutil
import subprocess
from pathlib import Path


def main():
    if os.geteuid() != 0:
        raise SystemExit("Run: sudo python3 install.py")
    source = Path(__file__).resolve().parent
    config = json.loads((source / "config.json").read_text())
    installed = Path(config["install_dir"])
    installed.mkdir(parents=True, exist_ok=True)
    for name in ("discover.py", "lyrics_bridge.py", "config.json", "README.md", "requirements.txt"):
        shutil.copy2(source / name, installed / name)
        (installed / name).chmod(0o640 if name == "config.json" else 0o644)
    config = json.loads((installed / "config.json").read_text())
    state = Path(config["state_dir"])
    download_user = pwd.getpwnam(config["download_user"])
    jellyfin = pwd.getpwnam(config["service_user"])
    group = grp.getgrnam(config["service_group"]).gr_gid
    os.chown(installed / "config.json", 0, group)
    for directory in (state, state / "handoff"):
        directory.mkdir(exist_ok=True)
        os.chown(directory, download_user.pw_uid, group)
        directory.chmod(0o2750)
    done = state / "lyrics-done"
    done.mkdir(exist_ok=True)
    os.chown(done, jellyfin.pw_uid, group)
    done.chmod(0o2750)
    units = {
        "music-discovery.service": f"""[Unit]
Description=Discover up to twenty missing EDM, 80s and 90s songs per run
Wants=network-online.target
After=network-online.target jellyfin.service
RequiresMountsFor={config['media_mount']} {Path(config['jellyfin_database']).parent} {installed}
ConditionPathIsMountPoint={config['media_mount']}

[Service]
Type=oneshot
User={config['download_user']}
Group={config['service_group']}
WorkingDirectory={installed}
ExecStart=/usr/bin/python3 {installed}/discover.py
ExecStartPost=+/usr/bin/systemctl --no-block start music-discovery-lyrics.service
TimeoutStartSec=60min
TimeoutStopSec=30s
KillMode=control-group
UMask=0027
Nice=10
IOSchedulingClass=idle
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths={config['media_root']} {state}
StandardOutput=journal
StandardError=journal
""",
        "music-discovery.timer": f"""[Unit]
Description=Discover missing music daily at the configured local time

[Timer]
OnCalendar=*-*-* {config['schedule_time']} {config['timezone']}
RandomizedDelaySec=5min
AccuracySec=1min
Persistent=true
Unit=music-discovery.service

[Install]
WantedBy=timers.target
""",
        "music-discovery-lyrics.service": f"""[Unit]
Description=Queue newly imported music in the existing Finamp Genius worker
After=network-online.target jellyfin.service
Requires=jellyfin.service
RequiresMountsFor={config['media_mount']} {config['lyrics_install_dir']} {installed}
ConditionPathIsMountPoint={config['media_mount']}

[Service]
Type=oneshot
User={config['service_user']}
Group={config['service_group']}
WorkingDirectory={installed}
ExecStart=/usr/bin/python3 {installed}/lyrics_bridge.py
ExecStartPost=+/usr/bin/python3 {installed}/lyrics_bridge.py --kick
TimeoutStartSec=5min
UMask=0027
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths={state}/lyrics-done {config['lyrics_install_dir']}/state
StandardOutput=journal
StandardError=journal
""",
        "music-discovery-lyrics.timer": """[Unit]
Description=Retry lyric handoff after Jellyfin indexes new music

[Timer]
OnBootSec=3min
OnUnitActiveSec=15min
AccuracySec=1min
Unit=music-discovery-lyrics.service

[Install]
WantedBy=timers.target
"""
    }
    for name, content in units.items():
        (Path("/etc/systemd/system") / name).write_text(content)
    subprocess.run(["systemd-analyze", "verify", *[str(Path("/etc/systemd/system") / name) for name in units]], check=True)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    # Avoid an immediate discovery catch-up during installation: test one song first.
    subprocess.run(["systemctl", "enable", "music-discovery.timer"], check=True)
    subprocess.run(["systemctl", "enable", "--now", "music-discovery-lyrics.timer"], check=True)
    print("Installed. Enable daily execution after the test: sudo systemctl start music-discovery.timer")


if __name__ == "__main__":
    main()
