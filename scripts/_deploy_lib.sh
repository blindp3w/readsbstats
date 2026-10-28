# shellcheck shell=bash
# Shared helpers for install.sh and update.sh (sourced, never executed).
#
# Resolves the data directory (RSBS_DATA_DIR) and renders the systemd units
# from systemd/*.service|*.timer, substituting the @DATA_DIR@ placeholder.
# Written for bash 3.2+ and safe under `set -euo pipefail`. Paths are
# overridable through the environment so tests can run it unprivileged.

RSBS_ENV_FILE="${RSBS_ENV_FILE:-/etc/readsbstats/readsbstats.env}"
UNIT_DEST="${UNIT_DEST:-/etc/systemd/system}"
DEFAULT_DATA_DIR="${DEFAULT_DATA_DIR:-/var/lib/readsbstats}"

# env_value NAME — print the value of the LAST uncommented NAME=... line of
# $RSBS_ENV_FILE (surrounding quotes stripped); nothing if absent. The file is
# operator config plus secrets, so it is parsed, never sourced.
env_value() {
  local name="$1" line value result=""
  [[ -r "$RSBS_ENV_FILE" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line#"${line%%[![:space:]]*}"}"
    case "$line" in
      "$name="*)
        value="${line#"$name="}"
        # Trailing whitespace / CR (CRLF-edited file).
        value="${value%"${value##*[![:space:]]}"}"
        case "$value" in
          \"*\") value="${value#\"}"; value="${value%\"}" ;;
          \'*\') value="${value#\'}"; value="${value%\'}" ;;
        esac
        result="$value"
        ;;
    esac
  done < "$RSBS_ENV_FILE"
  if [[ -n "$result" ]]; then printf '%s\n' "$result"; fi
  return 0
}

# valid_data_dir DIR — absolute path of safe characters only, no `..` segment.
# The value is substituted into unit files: a newline, space, `|` or `@` must
# never get through.
valid_data_dir() {
  local dir="${1-}"
  [[ "$dir" =~ ^/[A-Za-z0-9._/-]+$ ]] || return 1
  case "/$dir/" in
    */../*) return 1 ;;
  esac
  return 0
}

# _installed_data_dir — ReadWritePaths= of the already-installed collector
# unit (first such line, single path); nothing if absent or still templated.
_installed_data_dir() {
  local unit="$UNIT_DEST/readsbstats-collector.service" line value=""
  [[ -r "$unit" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    case "$line" in
      ReadWritePaths=*) value="${line#ReadWritePaths=}"; break ;;
    esac
  done < "$unit"
  value="${value%"${value##*[![:space:]]}"}"
  case "$value" in
    *@*|*[[:space:]]*) return 0 ;;
  esac
  if [[ -n "$value" ]]; then printf '%s\n' "$value"; fi
  return 0
}

# _persist_data_dir DIR — append RSBS_DATA_DIR to the env file (created 0600
# if missing; chown root only when running as root).
_persist_data_dir() {
  local dir="$1" created=0
  if [[ ! -e "$RSBS_ENV_FILE" ]]; then
    created=1
    mkdir -p "$(dirname "$RSBS_ENV_FILE")"
    ( umask 077; : > "$RSBS_ENV_FILE" )
  fi
  # Keep the block on its own lines even if the file lacks a final newline.
  # Checked before the append, not inside it (SC2094: same file read+written).
  local lead=""
  if [[ -s "$RSBS_ENV_FILE" && -n "$(tail -c1 "$RSBS_ENV_FILE")" ]]; then lead=$'\n'; fi
  {
    printf '%s' "$lead"
    echo "# Added by update.sh (v2.32.0): data directory migrated from the installed units."
    echo "RSBS_DATA_DIR=$dir"
  } >> "$RSBS_ENV_FILE"
  if [[ $created -eq 1 && ${EUID:-$(id -u)} -eq 0 ]]; then
    chown root:root "$RSBS_ENV_FILE"
  fi
  return 0
}

# resolve_data_dir — sets DATA_DIR and DATA_DIR_SOURCE (env|migrated|default).
#   1. RSBS_DATA_DIR in the env file
#   2. migration: ReadWritePaths= of the installed collector unit (persisted
#      once into the env file, so later deploys take branch 1)
#   3. DEFAULT_DATA_DIR
resolve_data_dir() {
  local dir src
  dir="$(env_value RSBS_DATA_DIR)"
  if [[ -n "$dir" ]]; then
    src="env"
  else
    dir="$(_installed_data_dir)"
    if [[ -n "$dir" ]]; then
      src="migrated"
      if ! valid_data_dir "$dir"; then
        echo "ERROR: data directory '$dir' (from the installed collector unit) is not a plain absolute path" >&2
        return 1
      fi
      _persist_data_dir "$dir"
    else
      dir="$DEFAULT_DATA_DIR"
      src="default"
    fi
  fi
  if ! valid_data_dir "$dir"; then
    echo "ERROR: invalid RSBS_DATA_DIR '$dir': must be an absolute path of [A-Za-z0-9._/-] without '..'" >&2
    return 1
  fi
  # shellcheck disable=SC2034  # read by install.sh / update.sh
  DATA_DIR="$dir"
  # shellcheck disable=SC2034
  DATA_DIR_SOURCE="$src"
  return 0
}

# render_units DATA_DIR SRC_DIR — write every unit of SRC_DIR into $UNIT_DEST
# with @DATA_DIR@ substituted (atomic: temp file in the destination, then mv).
render_units() {
  local data_dir="${1-}" src_dir="${2-}" src name tmp
  if ! valid_data_dir "$data_dir"; then
    echo "ERROR: refusing to render units: invalid data dir '$data_dir'" >&2
    return 1
  fi
  mkdir -p "$UNIT_DEST"
  for src in "$src_dir"/*.service "$src_dir"/*.timer; do
    [[ -f "$src" ]] || continue
    name="$(basename "$src")"
    tmp="$(mktemp "$UNIT_DEST/.${name}.XXXXXX")"
    if ! sed "s|@DATA_DIR@|$data_dir|g" "$src" > "$tmp"; then
      rm -f "$tmp"
      return 1
    fi
    if grep -q '@DATA_DIR@' "$tmp"; then
      echo "ERROR: $name still contains @DATA_DIR@ after rendering" >&2
      rm -f "$tmp"
      return 1
    fi
    chmod 644 "$tmp"
    mv "$tmp" "$UNIT_DEST/$name"
  done
  return 0
}

# keep_backups_value — print how many history.db backups update.sh keeps:
# RSBS_KEEP_BACKUPS from the env file, an integer 1..9999, default 3. Anything
# else (0, negative, text, a leading zero) warns on stderr and yields 3. An
# empty/absent value is the default, silently.
keep_backups_value() {
  local default=3 raw
  raw="$(env_value RSBS_KEEP_BACKUPS)"
  if [[ -z "$raw" ]]; then
    echo "$default"
  elif [[ "$raw" =~ ^[1-9][0-9]{0,3}$ ]]; then
    echo "$raw"
  else
    echo "WARNING: invalid RSBS_KEEP_BACKUPS '$raw' (want an integer >= 1); keeping $default backups" >&2
    echo "$default"
  fi
}

# prune_backups DB_FILE KEEP — delete all but the KEEP newest
# DB_FILE.backup.YYYYmmdd_HHMMSS files. "Newest" is by name: the timestamp
# suffix sorts lexicographically = chronologically, and (unlike mtime) that
# needs no GNU-only `find -printf`/`stat`. Files with any other suffix
# (e.g. a hand-made .backup.pre-v6) and other databases' backups are never
# touched. The glob expands in sorted order; bash 3.2 safe (no empty-array
# expansion under `set -u`).
prune_backups() {
  local db="${1-}" keep="${2-}" f n=0 i=0
  local -a found=()
  if [[ -z "$db" || ! "$keep" =~ ^[1-9][0-9]{0,3}$ ]]; then
    echo "ERROR: prune_backups needs DB_FILE and an integer KEEP >= 1" >&2
    return 1
  fi
  for f in "$db".backup.*; do
    [[ -f "$f" ]] || continue
    [[ "${f##*.backup.}" =~ ^[0-9]{8}_[0-9]{6}$ ]] || continue
    found[n]="$f"
    n=$((n + 1))
  done
  while (( i < n - keep )); do
    rm -- "${found[i]}"
    i=$((i + 1))
  done
  return 0
}
