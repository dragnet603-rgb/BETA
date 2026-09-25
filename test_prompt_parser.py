"""Unit tests for prompt parsing logic in static/sync.js."""
import re
import unittest
from pathlib import Path


def load_parser():
    js = (Path(__file__).parent / "static" / "sync.js").read_text(encoding="utf-8")
    m = re.search(r"const PROMPT_LINE_RE =\s*/(.*?)/;", js)
    re_obj = re.compile(m.group(1))

    def clean_text(text):
        s = str(text or "").strip()
        changed = True
        while changed and len(s) >= 2:
            changed = False
            first, last = s[0], s[-1]
            if (first == '"' and last == '"') or (first == "'" and last == "'"):
                s = s[1:-1].strip(); changed = True; continue
            if (s.startswith("***") and s.endswith("***")) or (s.startswith("___") and s.endswith("___")):
                if len(s) >= 6: s = s[3:-3].strip(); changed = True; continue
            if (s.startswith("**") and s.endswith("**")) or (s.startswith("__") and s.endswith("__")):
                if len(s) >= 4: s = s[2:-2].strip(); changed = True; continue
            if (first in "*_`") and (last == first):
                s = s[1:-1].strip(); changed = True; continue
        return s

    def parse_lines(raw):
        prompts, errors = [], []
        for i, raw_line in enumerate(str(raw or "").splitlines()):
            line = raw_line.strip()
            if not line:
                continue
            plain = re.sub(r"[*_`~]", "", line).strip()
            m = re_obj.match(line) or re_obj.match(plain)
            if not m:
                errors.append(f'Line {i+1}: expected "0:00 - description" (got "{line[:30]}").')
                continue
            speaker = m.group(1) or m.group(2) or m.group(3) or m.group(4) or ""
            hours = int(m.group(5)) if m.group(5) else 0
            start = hours * 3600 + int(m.group(6)) * 60 + float(m.group(7).replace(",", "."))
            prev = prompts[-1]["start"] if prompts else -float("inf")
            if start <= prev:
                errors.append(f"Line {i+1}: timestamps must go up - each line needs a later time than the one above.")
                continue
            text = clean_text(re.sub(r"^[-–—|/:\s]+", "", m.group(8) or "").strip())
            if speaker:
                prefix = speaker.strip() + ": "
                if not text.startswith(prefix):
                    text = prefix + text
            prompts.append({"start": start, "text": text})
        return {"prompts": prompts, "errors": errors}

    return parse_lines

class TestPromptParser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parser = staticmethod(load_parser())

    def test_standard_formats(self):
        script = "00:00 - First beat\n0:05 - Second beat\n1:02:03 - Third beat"
        res = self.parser(script)
        self.assertEqual(res["errors"], [])
        self.assertEqual(len(res["prompts"]), 3)
        self.assertEqual(res["prompts"][0], {"start": 0.0, "text": "First beat"})
        self.assertEqual(res["prompts"][1], {"start": 5.0, "text": "Second beat"})
        self.assertEqual(res["prompts"][2], {"start": 3723.0, "text": "Third beat"})

    def test_subsecond_timestamps(self):
        script = "00:01.500 - Dot ms\n00:03,750 - Comma ms"
        res = self.parser(script)
        self.assertEqual(res["errors"], [])
        self.assertAlmostEqual(res["prompts"][0]["start"], 1.5)
        self.assertEqual(res["prompts"][0]["text"], "Dot ms")
        self.assertAlmostEqual(res["prompts"][1]["start"], 3.75)
        self.assertEqual(res["prompts"][1]["text"], "Comma ms")

    def test_markdown_and_code_styles(self):
        script = "**00:00** — Bold\n***00:05***: Italic\n`00:10` - Code\n_00:15_ - Under"
        res = self.parser(script)
        self.assertEqual(res["errors"], [])
        self.assertEqual(res["prompts"][0], {"start": 0.0, "text": "Bold"})
        self.assertEqual(res["prompts"][1], {"start": 5.0, "text": "Italic"})
        self.assertEqual(res["prompts"][2], {"start": 10.0, "text": "Code"})
        self.assertEqual(res["prompts"][3], {"start": 15.0, "text": "Under"})

    def test_brackets_parentheses_and_ranges(self):
        script = "[00:00] In brackets\n(0:05) In parens\n00:10 - 00:20 Range\n[00:25 - 00:30] Bracket\n(00:35 -> 00:40) Arrow\n00:45 to 00:50 Word to"
        res = self.parser(script)
        self.assertEqual(res["errors"], [])
        self.assertEqual(res["prompts"][0], {"start": 0.0, "text": "In brackets"})
        self.assertEqual(res["prompts"][1], {"start": 5.0, "text": "In parens"})
        self.assertEqual(res["prompts"][2], {"start": 10.0, "text": "Range"})
        self.assertEqual(res["prompts"][3], {"start": 25.0, "text": "Bracket"})
        self.assertEqual(res["prompts"][4], {"start": 35.0, "text": "Arrow"})
        self.assertEqual(res["prompts"][5], {"start": 45.0, "text": "Word to"})

    def test_list_markers_and_speakers(self):
        script = "1. 00:00 - Dot\n2) [00:05] Paren\n- 00:10 Hyphen\nSpeaker 1: 00:15 - Hello\nNarrator [00:20]: Story\n[Host] (00:25) Welcome\nJohn Doe: [00:30 - 00:35] Glad"
        res = self.parser(script)
        self.assertEqual(res["errors"], [])
        self.assertEqual(res["prompts"][0], {"start": 0.0, "text": "Dot"})
        self.assertEqual(res["prompts"][1], {"start": 5.0, "text": "Paren"})
        self.assertEqual(res["prompts"][2], {"start": 10.0, "text": "Hyphen"})
        self.assertEqual(res["prompts"][3], {"start": 15.0, "text": "Speaker 1: Hello"})
        self.assertEqual(res["prompts"][4], {"start": 20.0, "text": "Narrator: Story"})
        self.assertEqual(res["prompts"][5], {"start": 25.0, "text": "Host: Welcome"})
        self.assertEqual(res["prompts"][6], {"start": 30.0, "text": "John Doe: Glad"})

    def test_timestamp_ordering_errors(self):
        script = "00:00 - First beat\n00:05 - Second beat\n00:03 - Out of order"
        res = self.parser(script)
        self.assertEqual(len(res["errors"]), 1)
        self.assertIn("timestamps must go up", res["errors"][0])
        self.assertIn("Line 3", res["errors"][0])

    def test_invalid_line_errors(self):
        script = "00:00 - Valid beat\nThis is not a timestamp beat"
        res = self.parser(script)
        self.assertEqual(len(res["errors"]), 1)
        self.assertIn('expected "0:00 - description"', res["errors"][0])
        self.assertIn("Line 2", res["errors"][0])


if __name__ == "__main__":
    unittest.main()
