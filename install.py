#!/usr/bin/env python3
"""Install this program and its system services; run with sudo."""
import grp
import json
import os
import pwd
import stat
import subprocess
import time
from pathlib import Path


def main():
    if os.geteuid() != 0:
        raise SystemExit("Run: sudo python3 install.py")
    source = Path(__file__).resolve().parent
    config = json.loads((source / "config.json").read_text())
    telegram_environment = Path(config["telegram_summary"]["environment_file"])
    if not telegram_environment.is_file():
        raise SystemExit(f"Telegram environment file is missing: {telegram_environment}")
    if telegram_environment.stat().st_uid != 0 or stat.S_IMODE(telegram_environment.stat().st_mode) & 0o077:
        raise SystemExit("Telegram environment file must be owned by root and mode 0600")
    # install_dir is a symlink to this checkout: one copy, so edits take effect on the next run without reinstalling.
    installed = Path(config["install_dir"])
    if installed.is_symlink():
        if installed.resolve() != source:
            installed.unlink()
    elif installed.exists():  # an older copied install: keep it aside rather than delete it
        retired = installed.with_name(installed.name + ".copy-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
        installed.rename(retired)
        print(f"Moved the previous copied install to {retired}; delete it once the symlinked install works.")
    if not installed.is_symlink():
        installed.parent.mkdir(parents=True, exist_ok=True)
        installed.symlink_to(source, target_is_directory=True)
    state = Path(config["state_dir"])
    download_user = pwd.getpwnam(config["download_user"])
    jellyfin = pwd.getpwnam(config["service_user"])
    group = grp.getgrnam(config["service_group"]).gr_gid
    # The services read config.json through the link: the download user keeps ownership (it edits the checkout), the service group may read it.
    os.chown(source / "config.json", download_user.pw_uid, group)
    (source / "config.json").chmod(0o640)
    for name in ("discover.py", "lyrics_bridge.py"):
        (source / name).chmod(0o644)
    mounts = f"{installed} {source}"  # the link alone does not make systemd wait for the checkout's filesystem
    for directory in (state, state / "handoff"):
        directory.mkdir(exist_ok=True)
        os.chown(directory, download_user.pw_uid, group)
        directory.chmod(0o2750)
    # Song requests: a container (GridShift) drops <id>.json here; the path unit below runs them on the host.
    requests_dir = Path(config.get("request_dir") or state / "requests")
    for directory, mode in ((requests_dir, 0o2770), (requests_dir / "results", 0o2750)):
        directory.mkdir(exist_ok=True)
        os.chown(directory, download_user.pw_uid, group)
        directory.chmod(mode)
    done = state / "lyrics-done"
    done.mkdir(exist_ok=True)
    os.chown(done, jellyfin.pw_uid, group)
    done.chmod(0o2750)
    units = {
        "music-discovery.service": f"""[Unit]
Description=Discover up to twenty missing EDM, 80s and 90s songs per run
Wants=network-online.target
After=network-online.target jellyfin.service
RequiresMountsFor={config['media_mount']} {Path(config['jellyfin_database']).parent} {mounts}
ConditionPathIsMountPoint={config['media_mount']}

[Service]
Type=oneshot
User={config['download_user']}
Group={config['service_group']}
WorkingDirectory={installed}
EnvironmentFile={telegram_environment}
ExecStart=/usr/bin/python3 {installed}/discover.py --telegram-summary
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
        "music-discovery-request.path": f"""[Unit]
Description=Watch for queued single-song music requests

[Path]
PathExistsGlob={requests_dir}/*.json
Unit=music-discovery-request.service

[Install]
WantedBy=paths.target
""",
        "music-discovery-request.service": f"""[Unit]
Description=Download queued single-song music requests
Wants=network-online.target
After=network-online.target jellyfin.service
RequiresMountsFor={config['media_mount']} {Path(config['jellyfin_database']).parent} {mounts}
ConditionPathIsMountPoint={config['media_mount']}

[Service]
Type=oneshot
User={config['download_user']}
Group={config['service_group']}
WorkingDirectory={installed}
ExecStart=/usr/bin/python3 {installed}/discover.py --process-requests
ExecStartPost=+/usr/bin/systemctl --no-block start music-discovery-lyrics.service
TimeoutStartSec=120min
TimeoutStopSec=30s
KillMode=control-group
UMask=0027
Nice=10
IOSchedulingClass=idle
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
ReadWritePaths={config['media_root']} {state} {requests_dir}
StandardOutput=journal
StandardError=journal
""",
        "music-discovery-lyrics.service": f"""[Unit]
Description=Queue newly imported music in the existing Finamp Genius worker
After=network-online.target jellyfin.service
Requires=jellyfin.service
RequiresMountsFor={config['media_mount']} {config['lyrics_install_dir']} {mounts}
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
    subprocess.run(["systemctl", "enable", "--now", "music-discovery-request.path"], check=True)
    print("Installed. Enable daily execution after the test: sudo systemctl start music-discovery.timer")


if __name__ == "__main__":
    main()
