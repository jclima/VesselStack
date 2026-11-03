# shellcheck shell=bash
# Interactive remediation primitives for VesselStack scripts. SOURCED, never executed.
#
# Offers the owner a choice at the prompt instead of stopping on a problem the owner can fix,
# while unattended runs (no terminal, VESSELSTACK_NONINTERACTIVE, --yes) still stop safely.
# Everything is vs_-prefixed and safe under `set -euo pipefail`. Prompts go to stderr, the
# answers are read from a dedicated file descriptor (never from stdin, which a pipe may own).
#
# vs_prompt_open               0 when answers can be read; 1 when the run is non-interactive.
#                              Order: VESSELSTACK_NONINTERACTIVE=1|true -> 1; VS_PROMPT_FD already
#                              open -> 0; VESSELSTACK_PROMPT_ANSWERS (regular file, test hook) ->
#                              open it; else /dev/tty; no terminal -> 1.
# vs_prompt_choice VAR VALID   Reads a menu answer into VAR (lower-cased) from the words in VALID
#                              (for example "1 2"). "c" always cancels.
# vs_prompt_input VAR [secret] Reads a value into VAR. "secret" turns the echo off. Empty input is
#                              refused. Values are never printed.
# vs_prompt_close              Closes the prompt descriptor again (call it when the questions are done).
# vs_prompt_cancel             Prints "Cancelled. Nothing was changed." and exits 1.
# vs_is_secret_key KEY         0 for names ending _TOKEN, _PASSWORD, _SECRET or _KEY.
# vs_printable VALUE           Prints VALUE with control characters replaced by "?".
# EOF, a timeout (VESSELSTACK_PROMPT_TIMEOUT seconds, default 300) and "c" all cancel.

# vs_backup_destination_fix DEST CONTEXT [require-mounted]
#                              Offered when DEST is under /media or /mnt and that mount point is not
#                              mounted. CONTEXT is update, backup or bootstrap. Returns 0 when the
#                              drive is now mounted, or when the owner chose to back up to the system
#                              disk once (VS_BACKUP_ALLOW_ONCE=1). Returns 1 when non-interactive (after
#                              printing the refusal and the fix). Cancelling exits 1. Inputs, all optional:
#                              VS_BACKUP_ROOT, VS_BACKUP_DATA (sizes the backup), VS_BACKUP_ENV_FILE (the
#                              installed env file; VESSELSTACK_BACKUP_ALLOW_UNMOUNTED=false there hides
#                              the system-disk choice), VS_BACKUP_UPDATE_DIR (shown in the update hint).
#                              The third argument offers only choices 1 to 3. fstab is never written.

VS_PROMPT_FD=${VS_PROMPT_FD:-}
VS_PROMPT_OWNED=${VS_PROMPT_OWNED:-0}
VS_BACKUP_ALLOW_ONCE=${VS_BACKUP_ALLOW_ONCE:-0}

vs_prompt_cancel() {
    echo "${VS_CANCEL_MESSAGE:-Cancelled. Nothing was changed.}" >&2
    exit 1
}

vs_prompt_open() {
    case "${VESSELSTACK_NONINTERACTIVE:-}" in
        1|true) return 1 ;;
    esac
    if [ -n "$VS_PROMPT_FD" ]; then
        if [[ $VS_PROMPT_FD =~ ^[0-9]+$ ]] && { : <&"$VS_PROMPT_FD"; } 2>/dev/null; then
            return 0
        fi
        VS_PROMPT_FD=
    fi
    if [ -n "${VESSELSTACK_PROMPT_ANSWERS:-}" ]; then
        # Honoured only for a regular file owned by this user that nobody else can write.
        if [ -f "$VESSELSTACK_PROMPT_ANSWERS" ] && [ ! -L "$VESSELSTACK_PROMPT_ANSWERS" ] \
            && [ "$(stat -c '%u' -- "$VESSELSTACK_PROMPT_ANSWERS" 2>/dev/null)" = "$(id -u)" ] \
            && [[ $(stat -c '%a' -- "$VESSELSTACK_PROMPT_ANSWERS" 2>/dev/null) =~ ^[0-7]*[0145][0145]$ ]] \
            && { : <"$VESSELSTACK_PROMPT_ANSWERS"; } 2>/dev/null; then
            exec {VS_PROMPT_FD}<"$VESSELSTACK_PROMPT_ANSWERS"
            VS_PROMPT_OWNED=1
            return 0
        fi
        return 1
    fi
    # Without a controlling terminal (cron, systemd, the Control Panel) the open fails.
    if { : </dev/tty; } 2>/dev/null; then
        exec {VS_PROMPT_FD}</dev/tty
        VS_PROMPT_OWNED=1
        return 0
    fi
    return 1
}

vs_prompt_close() {
    # A descriptor the caller supplied (VS_PROMPT_FD) stays open; only our own is closed.
    if [ -n "$VS_PROMPT_FD" ] && [ "$VS_PROMPT_OWNED" -eq 1 ]; then
        exec {VS_PROMPT_FD}<&-
    fi
    VS_PROMPT_FD=
    VS_PROMPT_OWNED=0
    return 0
}

vs_printable() {
    local value=$1
    printf '%s' "${value//[![:print:]]/?}"
}

vs_is_secret_key() {
    case $1 in
        *_TOKEN|*_PASSWORD|*_SECRET|*_KEY) return 0 ;;
    esac
    return 1
}

# vs_read_answer VAR [secret]: one line from the prompt descriptor. EOF with no data and a
# timeout cancel; a final line without a newline is accepted.
vs_read_answer() {
    local __var=$1 __answer='' __status=0
    if [ "${2:-}" = secret ]; then
        IFS= read -r -s -t "${VESSELSTACK_PROMPT_TIMEOUT:-300}" -u "$VS_PROMPT_FD" __answer || __status=$?
        echo >&2
    else
        IFS= read -r -t "${VESSELSTACK_PROMPT_TIMEOUT:-300}" -u "$VS_PROMPT_FD" __answer || __status=$?
        # A terminal echoes the typed line and its Enter; scripted answers do not.
        if [ ! -t "$VS_PROMPT_FD" ]; then echo >&2; fi
    fi
    if [ "$__status" -gt 128 ] || { [ "$__status" -ne 0 ] && [ -z "$__answer" ]; }; then
        echo >&2
        vs_prompt_cancel
    fi
    printf -v "$__var" '%s' "${__answer%$'\r'}"
}

vs_prompt_choice() {
    local __var=$1 __valid=" $2 " __reply='' __tries=0 __ok
    while :; do
        printf 'Choice: ' >&2
        vs_read_answer __reply
        __reply=${__reply#"${__reply%%[![:space:]]*}"}
        __reply=${__reply%"${__reply##*[![:space:]]}"}
        __reply=${__reply,,}
        [ "$__reply" != c ] || vs_prompt_cancel
        # Exactly one listed token: "1 2" or "1 b" is a bad answer, never a guess.
        __ok=0
        if [ -n "$__reply" ] && [[ $__reply != *[[:space:]]* ]]; then
            case $__valid in
                *" $__reply "*) __ok=1 ;;
            esac
        fi
        [ "$__ok" -eq 0 ] || break
        __tries=$((__tries + 1))
        [ "$__tries" -lt 10 ] || vs_prompt_cancel
        echo "Please enter one of: ${2// /, }, or c to cancel." >&2
    done
    printf -v "$__var" '%s' "$__reply"
}

vs_prompt_input() {
    local __var=$1 __mode=${2:-} __reply=''
    while :; do
        if [ "$__mode" = secret ]; then
            printf 'New value (typing is hidden; Ctrl-D cancels): ' >&2
        else
            printf 'New value (Ctrl-D cancels): ' >&2
        fi
        vs_read_answer __reply "$__mode"
        [ -n "$__reply" ] && break
        echo "The value cannot be empty." >&2
    done
    printf -v "$__var" '%s' "$__reply"
}

# ---------------------------------------------------------------- backup drive

# Reads lsblk JSON on stdin; argument 1 lists device paths that hold the system (one per line).
# Argument 2 lists fstab entries as "source<TAB>mountpoint" lines (other mount points only).
# Prints "OK<US>path<US>size<US>label<US>fstype<US>uuid" (US = byte 0x1f, so an empty label keeps its
# column) for a drive that can be offered, "NO<US>...<US>reason" for a removable drive that is refused
# and "MOUNTED<US>path<US>size<US>label<US>fstype<US>mountpoint" for a removable ext4/exFAT partition
# that is already mounted elsewhere. Missing fields mean "not removable".
VS_LSBLK_PY='
import json, sys

def flag(v):
    return v is True or str(v).lower() in ("1", "true")

def clean(v):
    s = "" if v is None else str(v)
    return "".join(c if c.isprintable() else "?" for c in s)

try:
    data = json.load(sys.stdin)
except ValueError:
    sys.exit(1)
system_paths = {l for l in sys.argv[1].split("\n") if l}
system_mounts = {"/", "/boot", "/boot/firmware"}
fstab = {}
for l in (sys.argv[2] if len(sys.argv) > 2 else "").split("\n"):
    src, _, mp = l.partition("\t")
    if src and mp:
        fstab[src] = mp
rows = []
mounted = []

def fstab_mount(node):
    for key in (node.get("path"), "UUID=" + str(node.get("uuid") or ""), "LABEL=" + str(node.get("label") or ""),
                "/dev/disk/by-uuid/" + str(node.get("uuid") or ""), "/dev/disk/by-label/" + str(node.get("label") or "")):
        if key in fstab:
            return fstab[key]
    return None

def subtree(node):
    yield node
    for child in node.get("children") or []:
        yield from subtree(child)

def walk(node, removable, system):
    removable = removable or flag(node.get("rm")) or flag(node.get("hotplug")) or node.get("tran") == "usb"
    children = node.get("children") or []
    kind = node.get("type")
    if (kind == "part" or (kind == "disk" and not children)) and node.get("fstype") and not node.get("mountpoint"):
        rows.append((node, removable, system))
    elif (kind == "part" or (kind == "disk" and not children)) and node.get("mountpoint") and node.get("fstype") in ("ext4", "exfat"):
        mounted.append((node, removable, system))
    for child in children:
        walk(child, removable, system)

for top in data.get("blockdevices") or []:
    system = any(n.get("path") in system_paths or n.get("mountpoint") in system_mounts for n in subtree(top))
    walk(top, False, system)

for node, removable, system in sorted(rows, key=lambda r: str(r[0].get("path"))):
    if system or not removable:
        continue
    fs = clean(node.get("fstype"))
    fields = [clean(node.get("path")), clean(node.get("size")), clean(node.get("label")), fs]
    elsewhere = fstab_mount(node)
    if elsewhere:
        print("\x1f".join(["NO"] + fields + ["listed in /etc/fstab for " + clean(elsewhere) + "; not offered here"]))
    elif fs in ("ext4", "exfat"):
        print("\x1f".join(["OK"] + fields + [clean(node.get("uuid"))]))
    else:
        print("\x1f".join(["NO"] + fields + [fs + " is not supported for backups; use ext4 (recommended) or exFAT"]))

for node, removable, system in sorted(mounted, key=lambda r: str(r[0].get("path"))):
    if system or not removable:
        continue
    print("\x1f".join(["MOUNTED", clean(node.get("path")), clean(node.get("size")), clean(node.get("label")),
                       clean(node.get("fstype")), clean(node.get("mountpoint"))]))
'

# vs_mib KIB: whole MiB with a thousands-free digit string.
vs_mib() {
    printf '%s' "$(($1 / 1024))"
}

# Sets VS_BACKUP_OFFER_SYSTEM=1 when a backup to the system disk is allowed and fits, and prints why not.
vs_backup_system_disk_check() {
    local dest=$1 ancestor free_kib used_kib=0 size_kib du_status=0 du_out
    VS_BACKUP_OFFER_SYSTEM=0
    if [ -n "${VS_BACKUP_ENV_FILE:-}" ] && [ -r "$VS_BACKUP_ENV_FILE" ] \
        && grep -Eq '^(export[[:space:]]+)?VESSELSTACK_BACKUP_ALLOW_UNMOUNTED=["'"'"']?false' "$VS_BACKUP_ENV_FILE"; then
        return 0
    fi
    ancestor=$dest
    while [ ! -e "$ancestor" ] && [ "$ancestor" != / ]; do ancestor=$(dirname -- "$ancestor"); done
    free_kib=$(df -Pk -- "$ancestor" 2>/dev/null | awk 'NR == 2 {print $4}')
    case $free_kib in ''|*[!0-9]*) free_kib=0 ;; esac
    du_out=$(timeout 60 du -skx -- "${VS_BACKUP_ROOT:-/opt/vesselstack}" "${VS_BACKUP_DATA:-/opt/vesselstack-data}" 2>/dev/null) || du_status=$?
    if [ "$du_status" -eq 124 ]; then
        echo "  The size of the VesselStack data could not be measured in 60 seconds (size unknown)." >&2
        VS_BACKUP_OFFER_SYSTEM=1
        return 0
    fi
    while read -r size_kib _; do
        case $size_kib in ''|*[!0-9]*) continue ;; esac
        used_kib=$((used_kib + size_kib))
    done <<<"$du_out"
    if [ "$free_kib" -lt "$used_kib" ]; then
        echo "  The system disk has $(vs_mib "$free_kib") MiB free but the backup needs about $(vs_mib "$used_kib") MiB," >&2
        echo "  so a backup to the system disk is not offered." >&2
        return 0
    fi
    VS_BACKUP_OFFER_SYSTEM=1
}

# vs_backup_mount_device MOUNT_DIR DEVICE FSTYPE UUID: mounts the chosen drive. 0 mounted, 1 failed.
vs_backup_mount_device() {
    local mount_dir=$1 device=$2 fs=$3 uuid=$4 created=0 options fstab_options pass=2
    if [[ ! $device =~ ^/dev/[A-Za-z0-9._/+-]+$ ]]; then
        echo "Refusing to mount an unexpected device name." >&2
        return 1
    fi
    if [ -L "$mount_dir" ] || { [ -e "$mount_dir" ] && [ ! -d "$mount_dir" ]; }; then
        echo "$(vs_printable "$mount_dir") exists but is not a plain folder; move it away first." >&2
        return 1
    fi
    if [ -d "$mount_dir" ] && [ -n "$(ls -A -- "$mount_dir" 2>/dev/null)" ]; then
        echo "$(vs_printable "$mount_dir") is not empty (files hidden under a mount would be unreachable); move its contents first." >&2
        return 1
    fi
    if [ ! -d "$mount_dir" ]; then
        install -d -m 0700 -o root -g root -- "$mount_dir" || return 1
        created=1
    fi
    case $fs in
        ext4)
            options=nodev,nosuid,noexec
            fstab_options=nofail,noatime,nodev,nosuid,noexec
            ;;
        *)
            options=nodev,nosuid,noexec,uid=0,gid=0,umask=0077
            fstab_options=nofail,noatime,nodev,nosuid,noexec,uid=0,gid=0,umask=0077
            pass=0
            ;;
    esac
    if ! mount -t "$fs" -o "$options" "$device" "$mount_dir" || ! mountpoint -q "$mount_dir"; then
        echo "Could not mount $device at $(vs_printable "$mount_dir")." >&2
        if [ "$created" -eq 1 ]; then rmdir -- "$mount_dir" 2>/dev/null || true; fi
        return 1
    fi
    echo "Mounted $device at $(vs_printable "$mount_dir")." >&2
    if [[ $uuid =~ ^[A-Za-z0-9-]+$ ]]; then
        echo "It stays mounted until reboot. To mount it at boot, add this line to /etc/fstab:" >&2
        echo "UUID=$uuid $(vs_printable "$mount_dir") $fs $fstab_options 0 $pass" >&2
    fi
    return 0
}

# vs_backup_pick_drive MOUNT_DIR: lists connected USB drives and mounts the chosen one.
# 0 mounted, 1 nothing mounted (the caller shows the menu again).
vs_backup_pick_drive() {
    local mount_dir=$1 json='' sources='' target src listing kind path size label fs uuid reason shown label_text pick
    local fstab_file=${VS_FSTAB_FILE:-/etc/fstab} fstab_src fstab_mp fstab_lines='' elsewhere=''
    local -a paths=() fss=() uuids=()
    if ! command -v lsblk >/dev/null 2>&1 || ! command -v python3 >/dev/null 2>&1; then
        echo "Listing drives needs lsblk and python3." >&2
        return 1
    fi
    json=$(lsblk -J -p -o PATH,TYPE,SIZE,LABEL,FSTYPE,UUID,MOUNTPOINT,RM,HOTPLUG,TRAN,PKNAME 2>/dev/null) || json=
    if [ -z "$json" ]; then
        echo "Could not list the connected drives." >&2
        return 1
    fi
    for target in / /boot/firmware "${VS_BACKUP_DATA:-}"; do
        [ -n "$target" ] || continue
        src=$(findmnt -n -o SOURCE --target "$target" 2>/dev/null | head -n 1) || src=
        src=${src%%\[*}
        case $src in /dev/*) sources+="$src"$'\n' ;; esac
    done
    # Drives that /etc/fstab assigns to another mount point (a data SSD that failed to mount) are not offered.
    if [ -r "$fstab_file" ]; then
        while read -r fstab_src fstab_mp _; do
            case $fstab_src in ''|'#'*) continue ;; esac
            [ "$fstab_mp" != "$mount_dir" ] || continue
            fstab_lines+="$fstab_src"$'\t'"$fstab_mp"$'\n'
        done <"$fstab_file"
    fi
    listing=$(printf '%s' "$json" | python3 -I -c "$VS_LSBLK_PY" "$sources" "$fstab_lines") || listing=
    echo "Connected USB drives that are not mounted:" >&2
    shown=0
    while IFS=$'\x1f' read -r kind path size label fs uuid; do
        [ -n "$kind" ] || continue
        label_text='(no label)'
        [ -z "$label" ] || label_text="\"$label\""
        if [ "$kind" = MOUNTED ]; then
            elsewhere+="  $path  $size  $label_text  $fs, mounted at $uuid"$'\n'
            continue
        elif [ "$kind" = OK ]; then
            paths+=("$path"); fss+=("$fs"); uuids+=("$uuid")
            echo "  [${#paths[@]}] $path  $size  $label_text  $fs" >&2
        else
            reason=$uuid
            echo "      $path  $size  $label_text  $fs -- $reason" >&2
        fi
        shown=1
    done <<<"$listing"
    if [ "$shown" -eq 0 ]; then
        echo "  (none found)" >&2
    fi
    if [ "${#paths[@]}" -eq 0 ]; then
        echo "No unmounted USB drive was found. If your desktop already mounted the drive (for example under /media/<user>/...), unmount it first: sudo umount <path>." >&2
        if [ -n "$elsewhere" ]; then
            echo "Already mounted elsewhere:" >&2
            printf '%s' "$elsewhere" >&2
        fi
        echo "Plug in a drive formatted as ext4 (recommended) or exFAT, then choose [3] to check again." >&2
        return 1
    fi
    echo "  [b] Back" >&2
    pick=
    vs_prompt_choice pick "$(seq -s ' ' 1 "${#paths[@]}") b"
    [ "$pick" != b ] || return 1
    vs_backup_mount_device "$mount_dir" "${paths[pick - 1]}" "${fss[pick - 1]}" "${uuids[pick - 1]}"
}

vs_backup_menu() {
    local dest=$1 context=$2 strict=$3 mount_dir=$4 cancel_what=the\ update choice valid
    local has_fstab=0
    [ "$context" != backup ] || cancel_what='the backup'
    if command -v findmnt >/dev/null 2>&1 && findmnt --fstab -n --mountpoint "$mount_dir" >/dev/null 2>&1; then
        has_fstab=1
    fi
    VS_BACKUP_OFFER_SYSTEM=0
    [ -n "$strict" ] || vs_backup_system_disk_check "$dest"
    while :; do
        {
            echo
            echo "The backup folder $(vs_printable "$dest") is on $mount_dir, which is not mounted."
            echo "Backing up now would put the archive on the system disk (SD card)."
            echo "Nothing has been changed yet."
        } >&2
        valid="2 3"
        if [ "$has_fstab" -eq 1 ]; then
            echo "  [1] Mount $(vs_printable "$mount_dir") now (it is listed in /etc/fstab)" >&2
            valid="1 $valid"
        fi
        echo "  [2] Choose a connected drive to mount at $(vs_printable "$mount_dir")" >&2
        echo "  [3] I have plugged the drive in; check again" >&2
        if [ "$VS_BACKUP_OFFER_SYSTEM" -eq 1 ]; then
            echo "  [4] Back up to the system disk this once (not recommended)" >&2
            valid="$valid 4"
        fi
        echo "  [c] Cancel $cancel_what" >&2
        choice=
        vs_prompt_choice choice "$valid"
        case $choice in
            1)
                if mount -- "$mount_dir" && mountpoint -q "$mount_dir"; then
                    echo "Mounted $(vs_printable "$mount_dir")." >&2
                    return 0
                fi
                echo "Could not mount $(vs_printable "$mount_dir") from /etc/fstab." >&2
                ;;
            2)
                if vs_backup_pick_drive "$mount_dir"; then return 0; fi
                ;;
            3)
                if mountpoint -q "$mount_dir"; then
                    echo "$(vs_printable "$mount_dir") is mounted." >&2
                    return 0
                fi
                echo "$(vs_printable "$mount_dir") is still not mounted." >&2
                ;;
            4)
                echo "WARNING: the backup will be written to the system disk (SD card) this once. It will not survive loss of that disk, and the next backup asks again." >&2
                VS_BACKUP_ALLOW_ONCE=1
                return 0
                ;;
        esac
    done
}

vs_backup_destination_fix() {
    local dest=$1 context=${2:-update} strict=${3:-} mount_dir rc=0 update_dir=${VS_BACKUP_UPDATE_DIR:-<DIR>}
    mount_dir=$(printf '%s' "$dest" | cut -d/ -f1-3)
    VS_BACKUP_ALLOW_ONCE=0
    if ! vs_prompt_open; then
        {
            echo "Refusing to back up: $(vs_printable "$mount_dir") is not mounted, so the archive would land on the system disk (SD card). Nothing was changed."
            if [ "$context" = backup ] && [ -n "$strict" ]; then
                echo "To fix: mount the backup drive at $(vs_printable "$mount_dir") and run again."
            elif [ "$context" = backup ]; then
                echo "To fix: mount the backup drive at $(vs_printable "$mount_dir") and run again, or pass --allow-unmounted."
            else
                echo "To fix: mount the backup drive at $(vs_printable "$mount_dir") and run again, or back up to the system disk this once: sudo env VESSELSTACK_BACKUP_ALLOW_UNMOUNTED=true vesselstackctl update $update_dir"
            fi
        } >&2
        return 1
    fi
    vs_backup_menu "$dest" "$context" "$strict" "$mount_dir" || rc=$?
    vs_prompt_close
    return "$rc"
}
