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

# --- Slice 2 (backup drive): vs_backup_destination_fix DEST CONTEXT is added here. ---

VS_PROMPT_FD=${VS_PROMPT_FD:-}

vs_prompt_cancel() {
    echo "Cancelled. Nothing was changed." >&2
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
        if [ -f "$VESSELSTACK_PROMPT_ANSWERS" ] && [ ! -L "$VESSELSTACK_PROMPT_ANSWERS" ] \
            && { : <"$VESSELSTACK_PROMPT_ANSWERS"; } 2>/dev/null; then
            exec {VS_PROMPT_FD}<"$VESSELSTACK_PROMPT_ANSWERS"
            return 0
        fi
        return 1
    fi
    # Without a controlling terminal (cron, systemd, the Control Panel) the open fails.
    if { : </dev/tty; } 2>/dev/null; then
        exec {VS_PROMPT_FD}</dev/tty
        return 0
    fi
    return 1
}

vs_prompt_close() {
    if [ -n "$VS_PROMPT_FD" ]; then
        exec {VS_PROMPT_FD}<&-
        VS_PROMPT_FD=
    fi
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
    local __var=$1 __valid=" $2 " __reply='' __tries=0
    while :; do
        printf 'Choice: ' >&2
        vs_read_answer __reply
        __reply=${__reply#"${__reply%%[![:space:]]*}"}
        __reply=${__reply%"${__reply##*[![:space:]]}"}
        __reply=${__reply,,}
        [ "$__reply" != c ] || vs_prompt_cancel
        case $__valid in
            *" $__reply "*) [ -z "$__reply" ] || break ;;
        esac
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
