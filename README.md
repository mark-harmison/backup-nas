Here is a comprehensive, production-ready `README.md` file designed specifically for your project. It walks through the architecture, configuration steps, and file structural paradigms of your automated Python backup environment.

---

# NAS to USB Automated Backup Solution

A robust, self-discovering Python 3 automation framework designed for Synology DSM environments. This solution automatically intercepts physical external USB hard drive insertions, dynamically discovers the target volume path, profiles the incoming workload, handles fail-safe storage space auto-pruning, and executes a zero-dependency data replication strategy while streaming real-time telemetry back to Home Assistant.

## Core Pillars & Philosophy

* **Data Sovereignty:** Backups are written as plain-text, uncompressed, unencrypted directories on standard filesystems (`exFAT` or `ext4`). In a total catastrophic infrastructure failure, your data is accessible on virtually any device without proprietary recovery software.
* **Concurrency Guarded:** Prevents race conditions or dual-sync overhead by using a non-blocking hardware file lock via the Linux kernel.
* **Event-Driven Execution:** Relies natively on `udev` kernel state shifts to eliminate resource-heavy polling or rigid cron timelines.

---

## Configuration Variables

The top section of `backup.py` exposes parameters to adjust performance and thresholds:

| Variable | Default | Description |
| --- | --- | --- |
| `MAX_ATTEMPTS` | `30` | Number of retries to wait for the storage volume table to mount. |
| `DELAY_SECONDS` | `2` | Wait interval between volume validation retries. |
| `THRESHOLD_MB` | `51200` | Size threshold (50 GB) required to trigger real-time progress notifications. |
| `WRITE_SPEED_MBS` | `66` | Sustained write speed benchmark (MB/s) utilized for the time estimation engine. |
| `DRY_RUN` | `False` | When `True`, processes stats and runs calculations without writing to the disk. |
| `WEBHOOK_URL` | `https://ha...` | Your Home Assistant secret REST API Webhook endpoint receiver. |
| `BACKUP_PAIRS` | List | Mapping layout array matching `("NAS_SOURCE", "USB_SUBDIR")`. |

---

## Backup Rotation & Structural Framework

The script dynamically adapts its behavior based on a hidden root flag file sitting on the root of your external drive. It looks for either `WEEKLY.backup` or `LONGTERM.backup`.

### 1. The Weekly Drive Strategy (`WEEKLY.backup`)

Designed for rapid, low-overhead incremental parity.

* **Behavior:** Mirrors the NAS precisely. Files deleted on the NAS are deleted on the USB drive (`--delete`).
* **File Layout:**
```text
/volumeUSB1/usbshare/              <-- Dynamically discovered mount point
├── WEEKLY.backup                  <-- Target identity flag file
├── NetBackup/                     <-- Mirrored shared folders
├── photo/
├── Media/
├── homes/
└── web-source/

```



### 2. The Long-Term Archive Strategy (`LONGTERM.backup`)

Designed for an indestructible, sequential history of your system snapshots.

* **Behavior:** Generates an isolated, immutable folder named after the execution date (`YYYY-MM-DD`). Because it writes to a clean folder on an `exFAT` file table, it executes a complete sequential copy of all files every time.
* **Storage Constraints:** Includes automatic low-space mitigation. If the projected workload exceeds available sectors, it chronologically sorts older date directories and purges the oldest archival block entirely until space is cleared.
* **File Layout:**
```text
/volumeUSB1/usbshare1-1/           <-- Dynamically discovered mount point
├── LONGTERM.backup                <-- Target identity flag file
├── 2026-05-15/                     <-- Historic snapshot capsule
│   ├── NetBackup/
│   └── photo/
├── 2026-06-13/                     <-- Next sequential snapshot capsule
│   ├── NetBackup/
│   ├── photo/
│   └── Media/
└── logs/                          <-- Persistent diagnostic runtime execution tables

```



---

## Synology DSM Deployment Guide

### Step 1: Install and Permissioning

1. Connect to your Synology NAS via SSH as an administrator.
2. Place `backup.py` and your optional `exclude-list.txt` inside a safe scripts directory (e.g., `/volume1/NetBackup/scripts/`).
3. Apply global execution permissions to the Python script:
```bash
chmod +x /volume1/NetBackup/scripts/backup.py

```



### Step 2: Configure the Kernel Event Trigger (`udev`)

To map the script to physical hardware insertion events, you must add a custom `udev` instruction table:

1. Create a local rules definition file using the terminal editor:
```bash
sudo vi /etc/udev/rules.d/99-nas-usb-backup.rules

```


2. Paste the following rule line, which isolates events strictly to physical block storage disks (filtering out duplicate partition sub-events) and forks the process into a detached background daemon to bypass kernel execution timeouts:
```text
ACTION=="add", SUBSYSTEM=="block", ENV{DEVTYPE}=="disk", RUN+="/bin/sh -c '/usr/bin/python3 /volume1/NetBackup/scripts/backup.py &'"

```


3. Command the system `udev` daemon to rebuild its configuration table:
```bash
sudo udevadm control --reload-rules

```



### Step 3: Flash Drive Configuration

Format your backup drives using **exFAT** (for universal cross-compatibility) or native Linux filesystems. Use your computer or Synology's External Devices tool to create an empty tracking file on the absolute root of the disk directory matching your chosen rotation type:

* For your weekly rotational arrays: `touch WEEKLY.backup`
* For your immutable milestone archives: `touch LONGTERM.backup`

---

## Home Assistant Telemetry Integration

The script sends structured telemetry via a standard JSON payload format containing independent string status identifiers:

```json
{
  "status": "success",
  "message": "USB WEEKLY Sync Successful: Archive generation concluded flawlessly..."
}

```

### Classifications Table (`status`)

Your automation templates can read the exact key value to categorize notifications:

* `mounted`: Sent the exact millisecond a volume matches paths, before slow calculations run.
* `progress`: Used for workload notifications and sequential step tracking updates.
* `warning`: Sent if low disk space forces old archive prunes or file thresholds are stressed.
* `success`: Final completion state indication containing total operation runtimes.
* `error`: Dispatched if an explicit failure occurs, containing error codes or missing resource logs.
