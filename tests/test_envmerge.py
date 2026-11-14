import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from paper_agent import envmerge  # noqa: E402


class TestEnvMerge(unittest.TestCase):
    def test_a_deploy_never_overwrites_a_value_you_already_have(self):
        """The rule you asked for: deployments preserve local changes."""
        local = "TWELVE_DATA_API_KEY=mine\nANTHROPIC_API_KEY=sk-local\n"
        repo = "TWELVE_DATA_API_KEY=repo-value\nANTHROPIC_API_KEY=\nFINNHUB_API_KEY=fh\n"
        m = envmerge.parse(envmerge.merge(local, repo))
        self.assertEqual(m["TWELVE_DATA_API_KEY"], "mine")          # NOT overwritten by the repo's different value
        self.assertEqual(m["ANTHROPIC_API_KEY"], "sk-local")        # NOT wiped by an empty repo value
        self.assertEqual(m["FINNHUB_API_KEY"], "fh")                # gaps are filled

    def test_empty_local_value_is_filled_from_the_repo(self):
        self.assertEqual(envmerge.parse(envmerge.merge("A=\nB=keep\n", "A=filled\nB=other\n")), {"A": "filled", "B": "keep"})

    def test_keys_only_present_locally_are_kept(self):
        self.assertEqual(envmerge.parse(envmerge.merge("GITHUB_MODELS_TOKEN=gh\n", "TWELVE_DATA_API_KEY=a\n")), {"GITHUB_MODELS_TOKEN": "gh", "TWELVE_DATA_API_KEY": "a"})

    def test_first_install_with_no_existing_file(self):
        self.assertEqual(envmerge.parse(envmerge.merge("", "A=1\nB=\n")), {"A": "1", "B": ""})

    def test_explicit_force_overrides_exactly_once(self):
        repo = "FINNHUB_API_KEY=correct\nOTHER=x\nFORCE_KEYS=FINNHUB_API_KEY\nFORCE_ID=r1\n"
        applied = set()
        m1 = envmerge.parse(envmerge.merge("FINNHUB_API_KEY=wrong\nOTHER=mine\n", repo, applied))
        self.assertEqual((m1["FINNHUB_API_KEY"], m1["OTHER"]), ("correct", "mine"))      # forced key changes; un-listed key still preserved
        self.assertEqual(applied, {"r1"})
        # you later fix it by hand; re-deploying the SAME release must not force again
        m2 = envmerge.parse(envmerge.merge("FINNHUB_API_KEY=my-later-fix\n", repo, applied))
        self.assertEqual(m2["FINNHUB_API_KEY"], "my-later-fix")
        # a NEW release id can force again
        m3 = envmerge.parse(envmerge.merge("FINNHUB_API_KEY=my-later-fix\n", repo.replace("r1", "r2"), applied))
        self.assertEqual(m3["FINNHUB_API_KEY"], "correct")

    def test_force_needs_both_keys_and_id_and_control_lines_are_not_written(self):
        out = envmerge.merge("A=local\n", "A=repo\nFORCE_KEYS=A\n", set())          # no FORCE_ID -> no force
        self.assertEqual(envmerge.parse(out)["A"], "local")
        out = envmerge.merge("A=local\n", "A=repo\nFORCE_ID=x\n", set())            # no FORCE_KEYS -> no force
        self.assertEqual(envmerge.parse(out)["A"], "local")
        out = envmerge.merge("", "A=1\nFORCE_KEYS=A\nFORCE_ID=z\n", set())
        self.assertNotIn("FORCE_", out)

    def test_cli_as_the_installer_runs_it_remembers_forced_ids_on_disk(self):
        d = tempfile.mkdtemp()
        ex, repo, out, ids = (os.path.join(d, n) for n in ("ex", "repo", "out", "ids"))
        with open(ex, "w") as f:
            f.write("K=wrong\n")
        with open(repo, "w") as f:
            f.write("K=right\nFORCE_KEYS=K\nFORCE_ID=once\n")
        script = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "paper_agent", "envmerge.py")
        subprocess.check_call([sys.executable, script, ex, repo, out, ids])
        with open(out) as f:
            self.assertEqual(envmerge.parse(f.read())["K"], "right")
        with open(ids) as f:
            self.assertEqual(f.read().strip(), "once")
        with open(ex, "w") as f:
            f.write("K=mine-again\n")
        subprocess.check_call([sys.executable, script, ex, repo, out, ids])
        with open(out) as f:
            self.assertEqual(envmerge.parse(f.read())["K"], "mine-again")           # second deploy: not forced again


if __name__ == "__main__":
    unittest.main()
