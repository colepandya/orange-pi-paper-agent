"""Hermetic, self-cleaning test environment (applies however the tests are started).

* every config/state/systemd path points into ONE temp directory, so tests never read or write the host's real /etc, /var/lib or systemd units
* all temp files (SQLite DBs etc.) live under that directory and are deleted when the run ends: nothing leaks (this machine is RAM-limited)
Must run BEFORE paper_agent is imported, which is why it lives here."""
import atexit
import os
import shutil
import tempfile

_ROOT = tempfile.mkdtemp(prefix="pa-tests-", dir=os.environ.get("PAPER_AGENT_TEST_TMP_BASE") or None)
for _k, _sub in (("PAPER_AGENT_CONFIG_DIR", "etc"), ("PAPER_AGENT_STATE_DIR", "state"), ("PAPER_AGENT_SETTINGS", "state/settings.json"), ("PAPER_AGENT_SECRETS", "state/secrets.env"),
                 ("PAPER_AGENT_ADMIN", "state/admin.json"), ("PAPER_AGENT_CONTROL", "state/control"), ("PAPER_AGENT_SYSTEMD_DIR", "systemd"), ("PAPER_AGENT_DB", "state/paper-agent.db")):
    os.environ[_k] = os.path.join(_ROOT, _sub)
tempfile.tempdir = os.path.join(_ROOT, "tmp")
os.makedirs(tempfile.tempdir, exist_ok=True)
atexit.register(shutil.rmtree, _ROOT, True)
