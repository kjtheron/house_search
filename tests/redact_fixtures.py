"""Strip third-party keys and session tokens from saved HTML fixtures.

Site pages embed the site's own public Google Maps key and per-visit tokens. They are not ours
and don't matter to the parsers, but GitHub's secret scanning flags them. Run after saving or
refreshing a fixture:  uv run python tests/redact_fixtures.py
"""

import re
from pathlib import Path

PATTERNS = [
    (re.compile(r"AIza[0-9A-Za-z_-]{35}"), "REDACTED_GOOGLE_KEY"),
    (re.compile(r'("tokenString"\s*:\s*")[^"]+'), r"\1REDACTED"),
]

if __name__ == "__main__":
    for f in sorted((Path(__file__).parent / "fixtures").rglob("*.html")):
        text = f.read_text()
        new = text
        for pat, repl in PATTERNS:
            new = pat.sub(repl, new)
        if new != text:
            f.write_text(new)
            print("redacted", f.relative_to(Path(__file__).parent))
