import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from paper_agent import envmerge  # noqa: E402


class TestEnvMerge(unittest.TestCase):
    def test_local_llm_key_survives_an_update_that_has_it_empty(self):
        local = "TWELVE_DATA_API_KEY=old\nANTHROPIC_API_KEY=sk-local\n"
        repo = "TWELVE_DATA_API_KEY=new\nANTHROPIC_API_KEY=\nFINNHUB_API_KEY=fh\n"
        m = envmerge.parse(envmerge.merge(local, repo))
        self.assertEqual(m["ANTHROPIC_API_KEY"], "sk-local")       # not wiped
        self.assertEqual(m["TWELVE_DATA_API_KEY"], "new")          # repo non-empty value wins
        self.assertEqual(m["FINNHUB_API_KEY"], "fh")               # new keys are added

    def test_keys_only_present_locally_are_kept(self):
        m = envmerge.parse(envmerge.merge("GITHUB_MODELS_TOKEN=gh\n", "TWELVE_DATA_API_KEY=a\n"))
        self.assertEqual(m, {"GITHUB_MODELS_TOKEN": "gh", "TWELVE_DATA_API_KEY": "a"})

    def test_first_install_with_no_existing_file(self):
        self.assertEqual(envmerge.parse(envmerge.merge("", "A=1\nB=\n")), {"A": "1", "B": ""})


if __name__ == "__main__":
    unittest.main()
