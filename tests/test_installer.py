"""A half-patched mixture of two builds was once installed. The installer must refuse that, and must be able to prove a folder is exactly one release."""
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(script, *args, cwd=None):
    p = subprocess.run(["bash", os.path.join(ROOT, "scripts", script)] + list(args), capture_output=True, text=True, cwd=cwd)
    return p.returncode, p.stdout + p.stderr


class TestPackageVerification(unittest.TestCase):
    def setUp(self):
        self.d = os.path.join(tempfile.mkdtemp(), "pkg")
        shutil.copytree(ROOT, self.d, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git", "*.rej", "*.orig"))
        self.assertEqual(subprocess.run(["bash", os.path.join(self.d, "scripts", "make-manifest.sh")], capture_output=True).returncode, 0)

    def verify(self):
        return run("verify-package.sh", self.d)

    def test_a_pristine_release_is_accepted(self):
        rc, out = self.verify()
        self.assertEqual(rc, 0, out)
        self.assertIn("integrity verified", out)

    def test_leftovers_of_a_failed_patch_are_refused(self):
        for junk in ("paper_agent/funnel.py.rej", "BUILD.orig"):
            with open(os.path.join(self.d, junk), "w") as f:
                f.write("x")
        rc, out = self.verify()
        self.assertEqual(rc, 1)
        self.assertIn("FAILED patch", out)
        self.assertIn("funnel.py.rej", out)

    def test_a_modified_file_is_refused(self):
        """What a half-applied patch looks like: files that are neither the old build nor the new one."""
        with open(os.path.join(self.d, "paper_agent", "trader.py"), "a") as f:
            f.write("\n# patched by hand\n")
        rc, out = self.verify()
        self.assertEqual(rc, 1)
        self.assertIn("trader.py", out)
        self.assertIn("do not match the packaged release", out)

    def test_a_missing_or_extra_file_is_detected(self):
        os.remove(os.path.join(self.d, "paper_agent", "funnel.py"))
        rc, out = self.verify()
        self.assertEqual(rc, 1)
        self.assertIn("funnel.py", out)

    def test_the_manifest_covers_every_file(self):
        with open(os.path.join(self.d, "MANIFEST.txt")) as f:
            listed = set(f.read().split("\n")) - {""}
        with open(os.path.join(self.d, "MANIFEST.sha256")) as f:
            hashed = {line.split(None, 1)[1].strip() for line in f if line.strip()}
        self.assertEqual(listed, hashed)
        self.assertIn("scripts/install.sh", listed)
        self.assertIn("paper_agent/trader.py", listed)

    def test_installer_calls_the_verification_and_announces_its_build(self):
        with open(os.path.join(ROOT, "scripts", "install.sh")) as f:
            text = f.read()
        self.assertIn('verify-package.sh', text)
        self.assertIn("Installing Paper Agent BUILD", text)
        self.assertLess(text.index("verify-package.sh"), text.index("mkdir -p \"$B\""), "the check must run BEFORE anything on the Pi is touched")

    def test_publishing_refuses_a_folder_with_failed_patch_leftovers(self):
        with open(os.path.join(self.d, "x.rej"), "w") as f:
            f.write("x")
        env = dict(os.environ, ALLOW_ROOT="1", REPO_DIR=os.path.join(tempfile.mkdtemp(), "r"), REPO_URL="/nonexistent")
        p = subprocess.run(["bash", os.path.join(self.d, "scripts", "publish.sh")], capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 1)
        self.assertIn("Refusing to publish", p.stdout + p.stderr)


if __name__ == "__main__":
    unittest.main()
