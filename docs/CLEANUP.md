# Cleaning up what old builds left behind

    sudo bash scripts/cleanup-legacy.sh            # DRY RUN: shows everything, changes nothing
    sudo bash scripts/cleanup-legacy.sh --apply    # does it (refuses during the US session; --force overrides)

1. `db-maintain` (existing): archives then removes the legacy v4 tables (44, ~6 MB) and the 4,962 old v4 decisions, expires old news, VACUUMs. A verified backup is taken first.
2. `cleanup`: old re-forecasts nothing points at; finished candidate-queue rows; the price-provider call log beyond a week; stale options caches; the old v4 app folder inside /opt/paper-agent and stale systemd unit copies (both ARCHIVED to /var/backups/paper-agent first); install backups beyond the newest 3; old unpacked builds and zips in your home folder (the newest is kept); old temporary files; and keys in /etc/paper-agent/config.json the program does not know (the original is kept and the effective settings are verified identical).
Never touched: the live database file, secrets and keys, settings.json, the current install, the newest backups; nothing outside an allow-list of folders; symlinks are removed, never followed.
On a copy of the owner's real database: 31.9 MB -> 20.5 MB, 113 tables -> 69, 5,212 decisions -> the 250 real ones, 15 dead config keys removed.
