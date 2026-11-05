import json
import tempfile
import unittest
from pathlib import Path
from organization import load


class OrganizationTest(unittest.TestCase):
    def test_custom_offices_and_invalid_names(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "organization.json"
            for names, valid in [(["장관실", "상담실"], True), (["A", "a"], False), (["../실"], False), ([], False)]:
                path.write_text(json.dumps({"organization": "예시부", "offices": names}), encoding="utf-8-sig")
                with self.subTest(names=names):
                    if valid:
                        self.assertEqual(load(path), ("예시부", names))
                    else:
                        with self.assertRaises(ValueError):
                            load(path)
