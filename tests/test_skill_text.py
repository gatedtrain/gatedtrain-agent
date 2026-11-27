"""The packaged operator skill states the gatedtrain 0.7.1 and VLM pack 0.3.1 facts an operator needs.

Each test names the claim it pins; the claims were checked against the gatedtrain v0.7.1 docs and code and the
gatedtrain-pack-vlm-clevr v0.3.1 README and source (see the commit message).
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from gatedtrain_agent import cli


def _skill() -> str:
    return Path(cli.skill_path()).read_text(encoding="utf-8")


class SkillTextTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text = _skill()

    def test_names_the_versions_it_describes(self) -> None:
        self.assertIn("gatedtrain 0.7.1", self.text)
        self.assertIn("gatedtrain-pack-vlm-clevr 0.3.1", self.text)

    def test_registered_greedy_decoding(self) -> None:
        for needle in ("decoding: greedy", "temperature 0", "one evaluation seed", "K=1", "max_num_seqs=1", "AN-26"):
            self.assertIn(needle, self.text)
        # Greedy is an explicit registration, never a side effect of temperature 0 (REG-GREEDY).
        self.assertRegex(self.text, r"(?i)never.{0,60}temperature 0|temperature 0 alone")
        # A failed control check costs alpha and settles the attempt as not passed.
        self.assertRegex(self.text, r"(?is)AN-26.{0,300}alpha")

    def test_keeper_modes_and_verify_exit_codes(self) -> None:
        for needle in ("same-user", "separate-user", "gatedtrain setup keeper --mode separate-user",
                       "--accept-mode same-user", "ACCEPTED WEAKER MODE: same-user", "keeper mode:"):
            self.assertIn(needle, self.text)
        self.assertRegex(self.text, r"(?is)separate-user.{0,200}exit(s)? 0")
        self.assertRegex(self.text, r"(?is)same-user.{0,200}exit(s)? 1")
        # The weaker-mode opt-in is the person's decision, per call (not an agent's).
        self.assertRegex(self.text, r"(?i)never pass `?--accept-mode")

    def test_same_user_result_is_not_called_sealed(self) -> None:
        self.assertRegex(self.text, r"(?i)not protected from the user's account|NOT sealed")
        self.assertRegex(self.text, r"(?i)quote the mode line")

    def test_image_lock(self) -> None:
        for needle in ("data/image-lock.json", "images.json", "gatedtrain image add", "gatedtrain --json image list"):
            self.assertIn(needle, self.text)
        self.assertRegex(self.text, r"(?i)never (add|widen|edit).{0,80}(image|lock)")

    def test_owner_side_settings(self) -> None:
        for needle in ("host.toml", "gpu_cap_h", "gpu_mem_limit_gb", "[packs.<name>]", "runtime_dir", "--eval-python"):
            self.assertIn(needle, self.text)
        self.assertRegex(self.text, r"(?i)owner")

    def test_deprecated_variables_removed_in_0_9_0(self) -> None:
        self.assertIn("GATEDTRAIN_EXT_*", self.text)
        self.assertIn("0.9.0", self.text)
        self.assertRegex(self.text, r"(?i)deprecated")

    def test_stays_a_short_skill(self) -> None:
        self.assertLessEqual(len(self.text.splitlines()), 120)

    def test_keeps_no_machine_facts(self) -> None:
        # Engineering standards 1 and 12: no user names, absolute home paths or image digests in the text.
        self.assertIsNone(re.search(r"/home/|/Users/|sha256:[0-9a-f]{12,}", self.text))


if __name__ == "__main__":
    unittest.main()
