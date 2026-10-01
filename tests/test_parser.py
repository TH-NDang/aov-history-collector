import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from collector import parse_person, parse_team, slugify


TEAM_HTML = """
<div class="infobox">
  <div class="infobox-header">Box Gaming</div>
  <img alt="Box Gaming" src="//liquipedia.net/commons/images/box.png">
  <div class="infobox-cell-2"><div class="infobox-description">Location</div><div class="infobox-description-value">Vietnam</div></div>
</div>
<h3>Former Squad</h3>
<table><tr><th>ID</th><th>Name</th><th>Position</th><th>Join Date</th><th>Leave Date</th></tr>
<tr><td><a href="/honorofkings/Adonis">Adonis</a></td><td>Nguyễn Thái Sơn</td><td>Jungler</td><td>2024-07-25</td><td>2025-12-25</td></tr></table>
<h3>Former Organization</h3>
<table><tr><th>ID</th><th>Name</th><th>Position</th><th>Join Date</th><th>Leave Date</th></tr>
<tr><td><a href="/honorofkings/Toản_Bee">Toản Bee</a></td><td>Đỗ Văn Toản</td><td>Coach</td><td>2018-08-22</td><td>2019-07-27</td></tr></table>
"""

PERSON_HTML = """
<div class="infobox">
  <div class="infobox-header">Adonis</div>
  <img alt="Adonis" src="//liquipedia.net/commons/images/adonis.png">
  <div class="infobox-cell-2"><div class="infobox-description">Name</div><div class="infobox-description-value">Nguyễn Thái Sơn</div></div>
  <div class="infobox-cell-2"><div class="infobox-description">Nationality</div><div class="infobox-description-value">Vietnam</div></div>
</div>
"""


class ParserTests(unittest.TestCase):
    def test_slugify_vietnamese(self):
        self.assertEqual(slugify("Đội Tuyển Sài Gòn"), "doi-tuyen-sai-gon")

    def test_team_memberships_and_staff(self):
        parsed = {"text": TEAM_HTML, "revid": 123}
        team, memberships, _ = parse_team("Box Gaming", parsed, "https://liquipedia.net/honorofkings/")
        self.assertEqual(team["name"], "Box Gaming")
        self.assertEqual(team["location"], "Vietnam")
        self.assertEqual(len(memberships), 2)
        self.assertEqual(memberships[0]["member_type"], "player")
        self.assertEqual(memberships[1]["member_type"], "staff")
        self.assertEqual(memberships[1]["role"], "Coach")

    def test_person(self):
        person, _ = parse_person("Adonis", {"text": PERSON_HTML, "revid": 5}, "https://liquipedia.net/honorofkings/")
        self.assertEqual(person["real_name"], "Nguyễn Thái Sơn")
        self.assertEqual(person["nationality"], "Vietnam")


if __name__ == "__main__":
    unittest.main()
