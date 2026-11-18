"""The text of an HTML fragment from a job board's JSON (a description), for the LLM to read."""

from html.parser import HTMLParser


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.parts.append(" ")  # words of two tags never run together: "<li>Go</li><li>SQL</li>"

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(fragment: str) -> str:
    """Tags dropped, entities decoded (&amp;, &nbsp;), whitespace collapsed to single spaces."""
    parser = _Text()
    parser.feed(fragment)
    parser.close()
    return " ".join("".join(parser.parts).split())
