"""The release scripts themselves. A rollback that restores a database and settings but not the program is worse than no rollback: it leaves a machine that cannot start. These run the REAL rollback.sh in a sandbox."""
import os
import re
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def sandbox(has_code_backup: bool):
    d = tempfile.mkdtemp(prefix="pa-tests-rb-")
    for p in ("opt/paper-agent/paper_agent", "etc/paper-agent", "etc/systemd", "var/lib/paper-agent", "bin", "bk/etc-paper-agent", "bk/units"):
        os.makedirs(os.path.join(d, p), exist_ok=True)
    w = lambda p, t: open(os.path.join(d, p), "w").write(t)  # noqa: E731
    w("opt/paper-agent/paper_agent/new_code.py", "# build 43\n"); w("opt/paper-agent/BUILD", "43\n"); w("var/lib/paper-agent/paper-agent.db", "CURRENT DB"); w("etc/paper-agent/config.json", "{\"now\": 1}")
    w("etc/systemd/paper-agent-cycle.timer", "NEW UNIT"); w("bk/paper-agent.db", "BACKED UP DB"); w("bk/etc-paper-agent/config.json", "{\"backed\": 1}"); w("bk/units/paper-agent-cycle.timer", "OLD UNIT")
    if has_code_backup:
        os.makedirs(os.path.join(d, "bk/opt-paper-agent/paper_agent"))
        w("bk/opt-paper-agent/paper_agent/old_code.py", "# build 42\n"); w("bk/opt-paper-agent/BUILD", "42\n")
    for c in ("systemctl", "chown", "chgrp"):
        w("bin/" + c, "#!/bin/bash\nexit 0\n"); os.chmod(os.path.join(d, "bin", c), 0o755)
    s = open(os.path.join(ROOT, "scripts", "rollback.sh")).read()
    s = re.sub(r"\[\[ \$EUID -eq 0 \]\].*", "true", s)
    for a, b in (("/etc/systemd/system", d + "/etc/systemd"), ("/opt/paper-agent", d + "/opt/paper-agent"), ("/var/lib/paper-agent", d + "/var/lib/paper-agent"), ("/etc/paper-agent", d + "/etc/paper-agent")):
        s = s.replace(a, b)
    s = s.replace(" -o paperagent -g paperagent", "")
    w("rollback_test.sh", s)
    return d


def run(d):
    env = dict(os.environ, PATH=d + "/bin:" + os.environ["PATH"])
    r = subprocess.run(["bash", d + "/rollback_test.sh", d + "/bk"], capture_output=True, text=True, env=env)
    return r.returncode, r.stdout + r.stderr


def read(d, p):
    return open(os.path.join(d, p)).read()


class TestRollback(unittest.TestCase):
    def test_a_backup_without_the_program_is_refused_and_nothing_at_all_is_changed(self):
        d = sandbox(has_code_backup=False)
        rc, out = run(d)
        self.assertEqual(rc, 1)
        self.assertIn("REFUSING TO ROLL BACK", out)
        self.assertIn("NOTHING HAS BEEN CHANGED", out)
        self.assertTrue(os.path.exists(d + "/opt/paper-agent/paper_agent/new_code.py"))                      # the program is still there
        self.assertEqual(read(d, "opt/paper-agent/BUILD"), "43\n")
        self.assertEqual(read(d, "var/lib/paper-agent/paper-agent.db"), "CURRENT DB")                         # the database was not replaced
        self.assertEqual(read(d, "etc/paper-agent/config.json"), "{\"now\": 1}")
        self.assertEqual(read(d, "etc/systemd/paper-agent-cycle.timer"), "NEW UNIT")                          # and the units were not deleted

    def test_a_backup_with_the_program_restores_program_database_config_and_units(self):
        d = sandbox(has_code_backup=True)
        rc, out = run(d)
        self.assertEqual(rc, 0, out)
        self.assertEqual(read(d, "opt/paper-agent/BUILD"), "42\n")
        self.assertTrue(os.path.exists(d + "/opt/paper-agent/paper_agent/old_code.py"))
        self.assertFalse(os.path.exists(d + "/opt/paper-agent/paper_agent/new_code.py"))                      # the newer program is gone
        self.assertEqual(read(d, "var/lib/paper-agent/paper-agent.db"), "BACKED UP DB")
        self.assertEqual(read(d, "etc/paper-agent/config.json"), "{\"backed\": 1}")
        self.assertEqual(read(d, "etc/systemd/paper-agent-cycle.timer"), "OLD UNIT")
        self.assertIn("BUILD 42", out)

    def test_the_installer_saves_the_current_program_before_replacing_it(self):
        s = open(os.path.join(ROOT, "scripts", "install.sh")).read()
        i = s.index('echo "[2/7] Backup')
        j = s.index('echo "[3/7]')
        block = s[i:j]
        self.assertIn("-d /opt/paper-agent/paper_agent", block)                                              # the v5 layout, not only the old v4 folder
        self.assertIn('cp -a /opt/paper-agent "$B/opt-paper-agent"', block)
        self.assertNotIn('cp -a /opt/paper-agent "$B/opt-paper-agent" || true', block)                       # a failed copy must stop the install, not be ignored
        self.assertIn("exit 1", block)


if __name__ == "__main__":
    unittest.main()
