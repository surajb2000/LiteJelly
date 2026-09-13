"""Checks on the browser code, which no other test touches.

There is no build step and no runtime here, so nothing catches a renamed
element or a method the target browser does not have until it fails on the
television. A call to a function that does not exist once reached production
this way, and so did a lookup for an id that had been renamed.

These are deliberately blunt textual checks. They cannot prove the code
works; they only catch the mistakes that have actually happened, and the
ones the browser floor makes easy.

Run with:  python -m unittest discover -s tests
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

STATIC = Path(__file__).resolve().parent.parent / "static"


def strip_js(source: str) -> str:
    """Remove comments, strings and regex literals.

    Everything here scans for code, and a word inside a message or a comment
    is not code. Regex literals are skipped by treating a slash after an
    operator as the start of one.
    """
    out = []
    i = 0
    n = len(source)
    while i < n:
        char = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        if char == "/" and nxt == "/":
            while i < n and source[i] != "\n":
                i += 1
        elif char == "/" and nxt == "*":
            i += 2
            while i + 1 < n and not (source[i] == "*" and source[i + 1] == "/"):
                i += 1
            i += 2
        elif char in "\"'`":
            quote = char
            i += 1
            while i < n and source[i] != quote:
                if source[i] == "\\":
                    i += 1
                i += 1
            i += 1
            out.append('""')
        else:
            out.append(char)
            i += 1
    return "".join(out)


def read(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


# Names the browser provides. Anything called but not defined and not here is
# either a typo or a function that was deleted from under its callers.
BROWSER_GLOBALS = {
    "Array", "Boolean", "Date", "Error", "Infinity", "Intl", "JSON", "Map",
    "Math", "NaN", "Number", "Object", "Promise", "RegExp", "Set", "String",
    "Symbol", "WeakMap", "clearInterval", "clearTimeout", "confirm", "decodeURI",
    "decodeURIComponent", "encodeURI", "encodeURIComponent", "fetch",
    "isFinite", "isNaN", "parseFloat", "parseInt", "requestAnimationFrame",
    "cancelAnimationFrame", "setInterval", "setTimeout", "alert",
    "IntersectionObserver", "MutationObserver", "AbortController", "Image",
    "URLSearchParams", "FormData", "Headers", "Request", "Response", "Blob",
    "FileReader", "TextDecoder", "TextEncoder", "CustomEvent", "Event",
    "document", "window", "navigator", "location", "history", "localStorage",
    "console", "performance", "screen", "matchMedia", "getComputedStyle",
    "if", "for", "while", "switch", "catch", "return", "typeof", "function",
    "new", "else", "do", "await", "in", "of", "case", "delete", "void", "yield",
    "super", "this", "instanceof", "async",
}


class ScriptSanityTests(unittest.TestCase):
    """Every function that is called has to exist."""

    def _check(self, filename):
        code = strip_js(read(filename))

        defined = set(re.findall(r"function\s+([A-Za-z_$][\w$]*)", code))
        defined |= set(re.findall(
            r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=", code))
        # Destructured and parameter names, which are also callable.
        defined |= set(re.findall(r"(?:const|let|var)\s*{([^}]*)}", code and
                                  " ".join(re.findall(
                                      r"(?:const|let|var)\s*{([^}]*)}", code))))
        for group in re.findall(r"(?:const|let|var)\s*{([^}]*)}", code):
            defined |= {part.strip().split(":")[-1].strip()
                        for part in group.split(",") if part.strip()}

        missing = set()
        for name in re.findall(r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(", code):
            if name in defined or name in BROWSER_GLOBALS:
                continue
            missing.add(name)

        self.assertEqual(missing, set(),
                         f"{filename} calls names that are never defined")

    def test_app_js_calls_resolve(self):
        self._check("app.js")

    def test_admin_js_calls_resolve(self):
        self._check("admin.js")


class ElementIdTests(unittest.TestCase):
    """Ids looked up in script have to exist in the markup.

    Renaming a control and missing one lookup leaves a null that only fails
    when that feature is used.
    """

    def _ids_in(self, html_name):
        html = read(html_name)
        return set(re.findall(r'\bid="([^"]+)"', html))

    def _check(self, js_name, html_name):
        code = strip_js_keep_strings(read(js_name))
        wanted = set(re.findall(r"""\$\(\s*['"]#([A-Za-z][\w-]*)['"]""", code))
        wanted |= set(re.findall(
            r"""getElementById\(\s*['"]([A-Za-z][\w-]*)['"]""", code))
        present = self._ids_in(html_name)
        self.assertEqual(sorted(wanted - present), [],
                         f"{js_name} looks up ids that {html_name} does not define")

    def test_app_ids_exist(self):
        self._check("app.js", "index.html")

    def test_admin_ids_exist(self):
        self._check("admin.js", "admin.html")


def strip_js_keep_strings(source: str) -> str:
    """Drop comments only. Selectors live inside strings."""
    out = []
    i = 0
    n = len(source)
    while i < n:
        char = source[i]
        nxt = source[i + 1] if i + 1 < n else ""
        if char == "/" and nxt == "/":
            while i < n and source[i] != "\n":
                i += 1
        elif char == "/" and nxt == "*":
            i += 2
            while i + 1 < n and not (source[i] == "*" and source[i + 1] == "/"):
                i += 1
            i += 2
        else:
            out.append(char)
            i += 1
    return "".join(out)


def _supports_ranges(css: str):
    """Character spans covered by an @supports block, by matching braces.

    Splitting on the first block only checked the top of the file, which let
    a rule appended to the end through unnoticed.
    """
    spans = []
    for match in re.finditer(r"@supports[^{]*\{", css):
        depth = 1
        i = match.end()
        while i < len(css) and depth:
            if css[i] == "{":
                depth += 1
            elif css[i] == "}":
                depth -= 1
            i += 1
        spans.append((match.start(), i))
    return spans


class BrowserFloorTests(unittest.TestCase):
    """The television runs a browser from 2016.

    Anything newer has to be behind a feature query or a fallback, because it
    fails silently there and works everywhere we develop.
    """

    def test_no_modern_javascript_syntax(self):
        code = strip_js(read("app.js")) + strip_js(read("admin.js"))
        self.assertNotIn("?.", code, "optional chaining needs Chrome 80")
        self.assertNotIn("??", code, "nullish coalescing needs Chrome 80")
        self.assertNotRegex(code, r"\.flatMap\(", "flatMap needs Chrome 69")
        self.assertNotRegex(code, r"Object\.fromEntries",
                            "Object.fromEntries needs Chrome 73")
        self.assertNotRegex(code, r"\.at\(", "Array.at needs Chrome 92")

    def test_replace_children_has_a_fallback(self):
        """replaceChildren is Chrome 86 and used throughout, so the polyfill
        has to be present or the whole interface renders nothing."""
        code = read("app.js")
        self.assertIn("replaceChildren", code)
        self.assertRegex(
            code, r"replaceChildren\s*(?:=|\]\s*=)|prototype\.replaceChildren",
            "replaceChildren is used but never polyfilled")

    def test_modern_css_is_behind_a_feature_query(self):
        """Each of these silently does nothing on the floor browser."""
        css = read("style.css")
        guarded = _supports_ranges(css)
        for prop in ("aspect-ratio:", "content-visibility:", "inset:"):
            for match in re.finditer(re.escape(prop), css):
                where = match.start()
                self.assertTrue(
                    any(start <= where < end for start, end in guarded),
                    f"{prop} at offset {where} is not inside an @supports block")

    def test_flex_gap_has_a_margin_fallback(self):
        """gap in flexbox is Chrome 84. Where it is used there must also be a
        sibling-margin rule, or rows collapse together."""
        css = read("style.css")
        if re.search(r"^\s*gap:", css, re.MULTILINE):
            self.assertRegex(css, r"\>\s*\*\s*\+\s*\*",
                             "flex gap is used with no margin fallback")


class NoRemoteAssetTests(unittest.TestCase):
    """Nothing may be fetched from the internet.

    The server is meant to work on a phone with no connection, and the policy
    blocks outside script anyway.
    """

    def test_markup_has_no_external_references(self):
        for name in ("index.html", "admin.html"):
            html = read(name)
            for attr in re.findall(r'(?:src|href)="([^"]+)"', html):
                self.assertFalse(
                    attr.startswith(("http://", "https://", "//")),
                    f"{name} loads {attr} from the network")

    def test_stylesheets_have_no_external_references(self):
        for name in ("style.css", "admin.css"):
            css = read(name)
            for url in re.findall(r"url\(\s*['\"]?([^)'\"]+)", css):
                self.assertFalse(
                    url.startswith(("http://", "https://", "//")),
                    f"{name} loads {url} from the network")

    def test_fonts_are_bundled(self):
        css = read("style.css")
        for url in re.findall(r"url\(\s*['\"]?([^)'\"]+)", css):
            if not url.startswith("data:"):
                target = (STATIC / url.lstrip("/").replace("static/", "")).resolve()
                self.assertTrue(target.exists(), f"missing font file {url}")


if __name__ == "__main__":
    unittest.main()
