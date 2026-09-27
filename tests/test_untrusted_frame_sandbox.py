import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_every_static_srcdoc_iframe_disables_scripts():
    templates = ROOT / "src" / "landppt" / "web" / "templates"
    offenders = []
    pattern = re.compile(r"<iframe\b(?P<attrs>[^>]*)\bsrcdoc\s*=", re.IGNORECASE | re.DOTALL)

    for path in templates.rglob("*.html"):
        source = path.read_text(encoding="utf-8-sig")
        for match in pattern.finditer(source):
            attrs = match.group("attrs")
            sandbox_match = re.search(r'\bsandbox\s*=\s*["\']([^"\']*)["\']', attrs, re.IGNORECASE)
            if not sandbox_match or "allow-scripts" in sandbox_match.group(1).lower().split():
                offenders.append(str(path.relative_to(ROOT)))

    assert offenders == []


def test_slide_iframe_helpers_apply_scriptless_sandbox_before_srcdoc():
    files = [
        ROOT / "src/landppt/web/static/js/pages/project/slides_editor/projectSlidesEditor.core.js",
        ROOT / "src/landppt/web/static/js/pages/project/slides_editor/projectSlidesEditor.slideshow.js",
        ROOT / "src/landppt/web/static/js/pages/template/global_master/globalMasterTemplates.exportHelpers.js",
    ]
    for path in files:
        source = path.read_text(encoding="utf-8")
        sandbox_position = source.find("setAttribute('sandbox', 'allow-same-origin')")
        srcdoc_position = source.find(".srcdoc =", sandbox_position)
        assert sandbox_position >= 0, path
        assert srcdoc_position > sandbox_position, path
        assert "allow-scripts" not in source[sandbox_position:srcdoc_position], path
