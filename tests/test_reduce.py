from scrape.reduce import reduce_html


def test_strips_tags_to_newlines():
    html = "<div><p>Hello</p><p>World</p></div>"
    assert reduce_html(html) == "Hello\nWorld"


def test_removes_script_style_svg_noscript_blocks_entirely():
    html = (
        "<p>Keep</p>"
        "<script>alert('drop me');</script>"
        "<style>.a { color: red; }</style>"
        "<svg><path d='M0 0'/></svg>"
        "<noscript>Enable JS</noscript>"
        "<p>Also keep</p>"
    )
    result = reduce_html(html)
    assert "drop me" not in result
    assert "color: red" not in result
    assert "Enable JS" not in result
    assert "Keep" in result
    assert "Also keep" in result


def test_removes_html_comments():
    html = "<p>Visible</p><!-- this is a secret comment --><p>Also visible</p>"
    result = reduce_html(html)
    assert "secret comment" not in result
    assert "Visible" in result


def test_collapses_repeated_spaces_and_tabs():
    html = "<p>Too    many\t\tspaces</p>"
    assert reduce_html(html) == "Too many spaces"


def test_collapses_repeated_blank_lines():
    html = "<p>First</p>\n\n\n\n<p>Second</p>"
    result = reduce_html(html)
    assert result == "First\nSecond"


def test_strips_leading_and_trailing_whitespace():
    html = "   \n <p>content</p> \n   "
    assert reduce_html(html) == "content"


def test_empty_input_returns_empty_string():
    assert reduce_html("") == ""


def test_script_regex_is_case_insensitive_and_multiline():
    html = "<SCRIPT type='text/javascript'>\nvar x = 1;\n</SCRIPT><p>ok</p>"
    result = reduce_html(html)
    assert "var x" not in result
    assert "ok" in result
