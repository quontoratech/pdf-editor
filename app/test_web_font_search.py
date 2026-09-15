from __future__ import annotations

import sys

from app.online_font_resolver import OnlineFontResolver


def main():
    if len(sys.argv) < 2:
        print('Usage: python test_web_font_search.py "Family Name" [weight] [normal|italic] [text]')
        raise SystemExit(2)

    family = sys.argv[1]
    weight = int(sys.argv[2]) if len(sys.argv) > 2 else 400
    italic = (sys.argv[3].lower() == "italic") if len(sys.argv) > 3 else False
    text = sys.argv[4] if len(sys.argv) > 4 else "Hello World"

    resolver = OnlineFontResolver()
    result, note = resolver.find_exact(family, weight, italic, text)

    print("NOTE:")
    print(note)
    print()

    if result is None:
        print("NO VERIFIED FONT FOUND")
        raise SystemExit(1)

    print("VERIFIED FONT FOUND")
    print("path:", result.path)
    print("provider:", result.provider)
    print("family:", result.family)
    print("weight:", result.weight)
    print("italic:", result.italic)
    print("source_url:", result.source_url)
    print("sha256:", result.sha256)


if __name__ == "__main__":
    main()
