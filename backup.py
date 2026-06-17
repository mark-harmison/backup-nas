#!/usr/bin/env python3
import sys
import os
import time
import fcntl
import re
import json
import logging
from pathlib import Path
from datetime import datetime
import urllib.request

# Start the global clock immediately to track the script's exact execution time
SCRIPT_START_TIME = time.monotonic()

# ==============================================================================
# CONFIGURATION
# ==============================================================================
MAX_ATTEMPTS = 30
DELAY_SECONDS = 2
THRESHOLD_MB = 51200
WRITE_SPEED_MBS = 66  # Calibrated directly from your June 13 archive benchmark log
DRY_RUN = False

WEBHOOK_URL = "https://ha.octabode.com/api/webhook/wfL7kWCER4R49ej4_nas_backup"

BACKUP_PAIRS = [
    ("/volume1/NetBackup", "NetBackup"),
    ("/volume1/photo", "photo"),
    ("/volume1/Media", "Media"),
    ("/volume1/homes", "homes"),
    ("/volume1/web-source", "web-source")
]

# ==============================================================================
# CONCURRENCY GUARD & INITIALIZATION
# ==============================================================================
# Global Singleton File Lock relocated to /tmp to safely allow user-level testing
lock_file_path = "/tmp/nas_usb_backup.lock"
lock_file = open(lock_file_path, "w")
try:
    fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
except IOError:
    print(f"[{datetime.now()}] Guard: Another instance of this backup script is already running. Exiting.")
    sys.exit(0)

# Dynamic Path Resolutions
SCRIPT_DIR = Path(__file__).resolve().parent
LOG_DIR = SCRIPT_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
LOG_FILE = LOG_DIR / f"backup_{timestamp}.log"

# Setup Native Logging Engine
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(message)s",
    datefmt="%a %b %d %H:%M:%S %Z %Y",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout)
    ]
)
logging.Formatter.converter = time.localtime


# ==============================================================================
# HELPER UTILITIES
# ==============================================================================
def send_ha_notification(message, status="progress"):
    """
    Sends structured JSON payload telemetry to Home Assistant Webhook URL.
    Includes a dedicated status classification field independent of the text.
    """
    logging.info(f"Sending to HA [{status}]: {message}")
    payload = json.dumps({
        "status": status,
        "message": message
    }).encode("utf-8")

    req = urllib.request.Request(
        WEBHOOK_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            pass
    except Exception as e:
        logging.error(f"Home Assistant Webhook Delivery Failed: {e}")


def run_command(cmd_list):
    """Executes a shell environment call capturing output line-by-line securely"""
    import subprocess
    result = subprocess.run(cmd_list, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return result.returncode, result.stdout


def format_duration(total_seconds):
    """Converts a raw count of seconds into a human-friendly string"""
    total_seconds = int(total_seconds)
    if total_seconds < 60:
        return f"{max(1, total_seconds)} seconds"
    else:
        total_minutes = total_seconds // 60
        hours = total_minutes // 60
        minutes = total_minutes % 60
        return f"{hours} hr {minutes} min" if hours > 0 else f"{minutes} min"


# ==============================================================================
# PHASE 1 & 1.5: DYNAMIC USB MOUNT & FLAG FILE DISCOVERY
# ==============================================================================
logging.info("USB insertion detected. Searching for backup volumes under /volumeUSB1...")

base_mount = None
backup_type = "UNKNOWN"

for attempt in range(1, MAX_ATTEMPTS + 1):
    parent_usb = Path("/volumeUSB1")
    if parent_usb.is_dir():
        weekly_flags = list(parent_usb.glob("*/WEEKLY.backup")) + list(parent_usb.glob("*/*/WEEKLY.backup"))
        longterm_flags = list(parent_usb.glob("*/LONGTERM.backup")) + list(parent_usb.glob("*/*/LONGTERM.backup"))

        if weekly_flags:
            base_mount = weekly_flags[0].parent
            backup_type = "WEEKLY"
            logging.info("Drive detected via flag file: WEEKLY rotation.")
            break
        elif longterm_flags:
            base_mount = longterm_flags[0].parent
            backup_type = "LONGTERM"
            logging.info("Drive detected via flag file: LONG-TERM archive.")
            break

    time.sleep(DELAY_SECONDS)

if not base_mount:
    error_msg = "USB Backup Failure: Timed out waiting for a drive containing a valid flag file under /volumeUSB1."
    logging.error(error_msg)
    send_ha_notification(error_msg, status="error")
    sys.exit(1)

# Resolve target destination path mutations
mount_path = base_mount
if backup_type == "LONGTERM":
    date_str = datetime.now().strftime("%Y-%m-%d")
    mount_path = base_mount / date_str
    logging.info(f"Active path resolved to: {base_mount} -> Target folder: /{date_str}")
else:
    logging.info(f"Active path resolved to: {base_mount}")

# --- EARLY MOUNT NOTIFICATION ---
# Fires immediately after paths match and before heavy pre-flight calculations start
send_ha_notification(
    message=f"USB Drive Detected: Identified {backup_type} backup target volume at '{base_mount.name}'. Initializing process tree...",
    status="progress"
)

# ==============================================================================
# PHASE 1.7: PRE-FLIGHT SPACE CALCULATION & TIME ESTIMATION
# ==============================================================================
logging.info("Running pre-flight space calculation across source pairs...")
total_required_bytes = 0
pair_size_map = {}
exclude_file_path = SCRIPT_DIR / "exclude-list.txt"

for src, dest_sub in BACKUP_PAIRS:
    src_path = Path(src)
    dest_path = mount_path / dest_sub

    if src_path.is_dir():
        rsync_stat_cmd = ["rsync", "-n", "-av", "--stats"]
        if exclude_file_path.is_file():
            rsync_stat_cmd.append(f"--exclude-from={exclude_file_path}")
        rsync_stat_cmd.extend([str(src_path) + "/", str(dest_path) + "/"])

        rc, output = run_command(rsync_stat_cmd)

        match = re.search(r"Total transferred file size:\s+([0-9,]+)", output)
        pair_bytes = int(match.group(1).replace(",", "")) if match else 0

        pair_size_map[src] = pair_bytes
        total_required_bytes += pair_bytes
    else:
        pair_size_map[src] = 0

required_mb = total_required_bytes // (1024 * 1024)
logging.info(f"Precise total required transfer size: {required_mb} MB")

# Calculate estimated duration
if required_mb == 0:
    duration_str = "0 seconds (No files changed)"
else:
    duration_str = format_duration(required_mb // WRITE_SPEED_MBS)

logging.info(f"Estimated backup runtime duration: {duration_str}")

# Notify Initial Startup Workload State
mode_tag = " (DRY-RUN mode)" if DRY_RUN else ""
send_ha_notification(
    message=f"Starting {backup_type} backup now{mode_tag}. Target: {base_mount.name}. Workload: {required_mb} MB. Est. Duration: {duration_str}.",
    status="progress"
)

# ==============================================================================
# LOW-SPACE AUTO-PRUNING LOGIC
# ==============================================================================
statvfs = os.statvfs(str(base_mount))
available_bytes = statvfs.f_bavail * statvfs.f_frsize

if total_required_bytes > available_bytes:
    if backup_type == "LONGTERM":
        logging.info(f"Storage threshold exceeded. Required: {required_mb} MB, Available: {available_bytes // (1024 * 1024)} MB. Initiating automated cleanup...")

        while total_required_bytes > available_bytes:
            archive_dirs = []
            for item in base_mount.iterdir():
                if item.is_dir() and re.match(r"^\d{4}-\d{2}-\d{2}$", item.name):
                    archive_dirs.append(item)

            archive_dirs.sort()

            if archive_dirs:
                oldest_dir = archive_dirs[0]
                logging.warning(f"Low disk space. Automatically purging oldest backup folder: {oldest_dir.name}")
                send_ha_notification(f"Low space on long-term drive. Automatically purging oldest backup: {oldest_dir.name}.", status="warning")

                if not DRY_RUN:
                    import shutil

                    shutil.rmtree(oldest_dir)

                statvfs = os.statvfs(str(base_mount))
                available_bytes = statvfs.f_bavail * statvfs.f_frsize
            else:
                error_msg = "USB Backup Failure: Out of space on drive, and no older versioned backups remain to prune."
                logging.error(error_msg)
                send_ha_notification(error_msg, status="error")
                sys.exit(1)
    else:
        logging.warning(f"Incoming storage requirement ({required_mb} MB) exceeds raw storage ceiling ({available_bytes // (1024 * 1024)} MB). Proceeding but drive space may exhaust.")
        send_ha_notification(message=f"Incoming storage requirement ({required_mb} MB) exceeds raw storage ceiling ({available_bytes // (1024 * 1024)} MB). Proceeding but drive space may exhaust.", status="warning")

# ==============================================================================
# PHASE 2: MULTI-DIRECTORY BACKUP (NAS -> USB)
# ==============================================================================
total_errors = 0
successful_dirs = []
total_pairs = len(BACKUP_PAIRS)
bytes_transferred_so_far = 0

for current_idx, (src, dest_sub) in enumerate(BACKUP_PAIRS, start=1):
    src_path = Path(src)
    full_dest_path = mount_path / dest_sub
    pair_bytes = pair_size_map[src]
    pair_mb = pair_bytes // (1024 * 1024)

    logging.info(f"Processing ({current_idx}/{total_pairs}): {src} [{pair_mb} MB] -> {full_dest_path}")

    if not src_path.is_dir():
        missing_msg = f"USB Backup Alert: Skipped folder '{src}' because it does not exist on the NAS."
        logging.error(missing_msg)
        total_errors += 1
        send_ha_notification(missing_msg, status="warning")
        continue

    if not DRY_RUN:
        full_dest_path.mkdir(parents=True, exist_ok=True)

    rsync_cmd = ["rsync", "-av", "--delete"]
    if exclude_file_path.is_file():
        rsync_cmd.append(f"--exclude-from={exclude_file_path}")
    if DRY_RUN:
        rsync_cmd.append("-n")
    rsync_cmd.extend([str(src_path) + "/", str(full_dest_path) + "/"])

    rc, output = run_command(rsync_cmd)

    with open(LOG_FILE, "a") as f:
        f.write(output)

    if rc != 0:
        rsync_msg = f"USB Backup Failure: rsync failed while backing up '{src}' (Exit code: {rc})."
        logging.error(rsync_msg)
        total_errors += 1
        send_ha_notification(rsync_msg, status="error")
    else:
        logging.info(f"Success: {src} exported to USB cleanly.")
        successful_dirs.append(src_path.name)
        bytes_transferred_so_far += pair_bytes

        # Granular Progress Telemetry Engine
        if required_mb > THRESHOLD_MB and current_idx < total_pairs:
            remaining_bytes = total_required_bytes - bytes_transferred_so_far
            remaining_mb = remaining_bytes // (1024 * 1024)
            progress_msg = f"Backup Progress: Finished sync of '{src_path.name}' ({pair_mb} MB). Progress: [{current_idx}/{total_pairs}] folders. Remaining data: {remaining_mb} MB."
            send_ha_notification(progress_msg, status="progress")

# ==============================================================================
# PHASE 3: FINAL STATUS AND LOG CLOSURE
# ==============================================================================
# Compute absolute elapsed run time 
elapsed_seconds = time.monotonic() - SCRIPT_START_TIME
actual_runtime_str = format_duration(elapsed_seconds)

logging.info(f"All sync pairs processed. Total errors encountered: {total_errors}")
logging.info(f"Backup script cycle complete. Total runtime: {actual_runtime_str}")

if total_errors == 0:
    prefix = f"USB {backup_type} Sync Dry-Run Successful" if DRY_RUN else f"USB {backup_type} Sync Successful"
    send_ha_notification(
        message=f"{prefix}: Archive generation concluded flawlessly. Total Runtime: {actual_runtime_str}. Successfully backed up: {', '.join(successful_dirs)}.",
        status="success"
    )
else:
    fail_msg = f"USB {backup_type} Sync Complete with Errors: {total_errors} error(s) recorded. Total Runtime: {actual_runtime_str}. Review newest file in logs folder."
    send_ha_notification(fail_msg, status="error")
