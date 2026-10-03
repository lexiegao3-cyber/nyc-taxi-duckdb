"""Keep the shared locale catalog complete as static UI labels evolve."""
from html.parser import HTMLParser
from pathlib import Path
import re

from taxi.localization import EN

STATIC = Path(__file__).parents[1] / 'taxi' / 'static'


class Labels(HTMLParser):
    def __init__(self):
        super().__init__()
        self.labels = []
        self.tag = ''

    def handle_starttag(self, tag, attrs):
        self.tag = tag
        for key, value in attrs:
            if key == 'placeholder':
                self.labels.append(value.strip())

    def handle_data(self, text):
        if self.tag not in ('script', 'style', 'option'):
            self.labels.append(text.strip())


def test_static_labels_and_js_translation_keys_have_english():
    parser = Labels()
    parser.feed((STATIC / 'index.html').read_text())
    labels = parser.labels
    for name in ('app.js','agent.js'):
        labels += [m.strip() for m in re.findall(r'\b(?:t|tr)\("([^"\n]+)"', (STATIC/name).read_text())]
    missing = {s for s in labels if re.search(r'[\u4e00-\u9fff]', s) and s not in EN}
    assert not missing, missing
