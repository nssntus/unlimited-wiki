from __future__ import annotations

import ast
import json
import re
import struct
import sys
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"
GITHUB = "https://github.com/nssntus/unlimited-wiki"
PAGES = "https://nssntus.github.io/unlimited-wiki/"
ACTION_PINS = {
    "actions/checkout": "11d5960a326750d5838078e36cf38b85af677262",
    "actions/configure-pages": "983d7736d9b0ae728b81ab479565c72886d7745b",
    "actions/upload-pages-artifact": "56afc609e74202658d3ffba0e8f6dda462b719fa",
    "actions/deploy-pages": "d6db90164ac5ed86f2b6aed7e0febac5b3c0c03e",
}


def test_validator_uses_only_the_python_standard_library() -> None:
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    imports = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        node.module.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )
    assert imports <= sys.stdlib_module_names


def test_pages_reuse_valid_brand_assets() -> None:
    public = ROOT / "viewer" / "public"
    for name in ("favicon.svg", "favicon.ico", "apple-touch-icon.png"):
        assert (SITE / name).read_bytes() == (public / name).read_bytes()

    svg_path = SITE / "favicon.svg"
    svg = ET.fromstring(svg_path.read_text(encoding="utf-8"))
    assert svg.tag == "{http://www.w3.org/2000/svg}svg"
    assert svg.attrib["viewBox"] == "0 0 64 64"
    assert {node.tag.rsplit("}", 1)[-1] for node in svg.iter()} <= {"svg", "rect", "path"}

    with (SITE / "favicon.ico").open("rb") as handle:
        reserved, image_type, count = struct.unpack("<HHH", handle.read(6))
        entries = [struct.unpack("<BBBBHHII", handle.read(16)) for _ in range(count)]
    assert (reserved, image_type) == (0, 1)
    assert {(entry[0] or 256, entry[1] or 256) for entry in entries} == {
        (16, 16),
        (32, 32),
        (48, 48),
    }

    png = (SITE / "apple-touch-icon.png").read_bytes()
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    assert png[12:16] == b"IHDR"
    assert struct.unpack(">II", png[16:24]) == (180, 180)
    assert png[25] == 2

    index = (SITE / "index.html").read_text(encoding="utf-8")
    for name in ("favicon.svg", "favicon.ico", "apple-touch-icon.png"):
        assert f'href="./{name}"' in index


def test_pages_scripts_use_safe_static_dom_projection() -> None:
    source = "\n".join(
        (SITE / name).read_text(encoding="utf-8")
        for name in ("index.html", "language-init.js", "app.js")
    )
    for unsafe in (
        "innerHTML",
        "outerHTML",
        "insertAdjacentHTML",
        "document.write",
        "eval(",
        "new Function",
        "fetch(",
        "XMLHttpRequest",
        "WebSocket",
    ):
        assert unsafe not in source
    assert "textContent" in source
    assert "localStorage.getItem" in source
    assert "localStorage.setItem" in source
    assert "document.documentElement.lang" in source


class DocumentParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[tuple[str, str, str]] = []
        self.ids: set[str] = set()
        self.meta: dict[tuple[str, str], str] = {}
        self.title = ""
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if values.get("id"):
            self.ids.add(values["id"])
        for name in ("href", "src"):
            if values.get(name):
                self.links.append((tag, name, values[name]))
        if tag == "meta":
            key = values.get("name") or values.get("property") or values.get("http-equiv")
            if key:
                self.meta[("meta", key)] = values.get("content", "")
        if tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data


def parse_document(name: str = "index.html") -> DocumentParser:
    parser = DocumentParser()
    parser.feed((SITE / name).read_text(encoding="utf-8"))
    return parser


def test_pages_content_matches_product_and_readme_contract() -> None:
    index = (SITE / "index.html").read_text(encoding="utf-8")
    script = (SITE / "app.js").read_text(encoding="utf-8")
    combined = index + script

    for phrase in (
        "Unlimited Wiki",
        "Raw",
        "Markdown",
        "AI 预审",
        "人工审核",
        "Wiki 广场",
        "Private Workspace",
        "投稿快照",
        "公开修订",
        "默认私有",
        "强租户隔离",
        "来源可追溯",
        "失败可见且可恢复",
    ):
        assert phrase in combined

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    commands = (
        "git clone https://github.com/nssntus/unlimited-wiki.git",
        "cd unlimited-wiki",
        "python3 -m venv .venv",
        "source .venv/bin/activate",
        "python3 -m pip install -r requirements.txt",
        "npm --prefix viewer ci",
        "npm --prefix viewer run build",
        "python3 serve.py",
    )
    for command in commands:
        assert command in readme

    assert combined.count(GITHUB) >= 3
    assert "online demo" not in combined.lower()
    assert "not a hosted SaaS, multi-node cluster, or shared network filesystem" in combined
    assert "single-node deployment" in combined
    assert f'href="{GITHUB}#readme"' in index


def test_editorial_information_architecture_and_boundary_facts() -> None:
    index = (SITE / "index.html").read_text(encoding="utf-8")
    app = (SITE / "app.js").read_text(encoding="utf-8")

    for section_id, number, label_key in (
        ("capabilities", "01", "capabilities.meta"),
        ("boundaries", "02", "boundaries.meta"),
        ("principles", "03", "principles.meta"),
    ):
        section = index.split(f'id="{section_id}"', 1)[1].split("</section>", 1)[0]
        assert f"<span>{number}</span>" in section
        assert f'data-i18n="{label_key}"' in section

    facts = index.split('<section class="fact-band"', 1)[1].split("</section>", 1)[0]
    assert facts.count("<article>") == 4
    for fact_key in ("facts.markdownMeta", "facts.workspaceMeta", "facts.reviewMeta", "facts.licenseMeta"):
        assert f'data-i18n="{fact_key}"' in facts
    assert "MIT License" in facts

    capabilities = index.split('class="capability-list"', 1)[1].split("</div>", 1)[0]
    assert capabilities.count("<article>") == 3
    mobile_boundaries = index.split('class="boundary-mobile"', 1)[1].split('class="deployment-note"', 1)[0]
    assert mobile_boundaries.count("<article") == 3
    assert index.count('<li><strong data-i18n="principles.') == 6

    workflow_keys = ("workflow.raw", "workflow.markdown", "workflow.ai", "workflow.human", "workflow.public")
    positions = [index.index(f'data-i18n="{key}"') for key in workflow_keys]
    assert positions == sorted(positions)

    for fact in (
        "Raw sources and canonical Markdown remain private.",
        "AI preflight checks quality and policy. It is not publication.",
        "Public revisions never write back to private sources.",
        "local or same-host HTTPS reverse-proxy single-node deployment",
    ):
        assert fact in app


def test_all_visible_copy_has_complete_chinese_and_english_keys() -> None:
    app = (SITE / "app.js").read_text(encoding="utf-8")
    zh_block, en_tail = app.split("    en: {", 1)
    en_block = en_tail.split("\n    }\n  };", 1)[0]
    key_pattern = re.compile(r'^\s+"([A-Za-z][A-Za-z0-9.]+)":', re.MULTILINE)
    zh_keys = set(key_pattern.findall(zh_block))
    en_keys = set(key_pattern.findall(en_block))
    assert zh_keys == en_keys

    references: set[str] = set()
    for name in ("index.html", "404.html"):
        source = (SITE / name).read_text(encoding="utf-8")
        references.update(re.findall(r'data-i18n(?:-aria|-content)?="([A-Za-z][A-Za-z0-9.]+)"', source))
    assert references <= zh_keys


def test_pages_navigation_assets_and_seo_are_subpath_safe() -> None:
    parser = parse_document()
    index = (SITE / "index.html").read_text(encoding="utf-8")

    assert parser.title.strip() == "Unlimited Wiki"
    assert parser.meta[("meta", "description")]
    assert parser.meta[("meta", "og:title")] == "Unlimited Wiki"
    assert parser.meta[("meta", "og:description")]
    assert parser.meta[("meta", "og:type")] == "website"
    assert parser.meta[("meta", "og:url")] == PAGES
    assert parser.meta[("meta", "og:image")].startswith(PAGES)
    assert parser.meta[("meta", "twitter:card")] == "summary"
    assert f'<link rel="canonical" href="{PAGES}"' in index

    for tag, attribute, value in parser.links:
        parsed = urlparse(value)
        if parsed.scheme:
            assert value.startswith((GITHUB, PAGES))
            continue
        if value.startswith("#"):
            assert value[1:] in parser.ids
            continue
        assert value.startswith("./"), (tag, attribute, value)
        assert "../" not in value


def test_language_switcher_is_complete_and_accessible() -> None:
    index = (SITE / "index.html").read_text(encoding="utf-8")
    init = (SITE / "language-init.js").read_text(encoding="utf-8")
    app = (SITE / "app.js").read_text(encoding="utf-8")

    assert 'data-locale-choice="zh-CN" aria-pressed="true"' in index
    assert 'data-locale-choice="en" aria-pressed="false"' in index
    assert 'aria-controls="primary-nav"' in index
    assert 'aria-expanded="false"' in index
    assert "navigator.language" in init
    assert "unlimited-wiki-locale" in init and "unlimited-wiki-locale" in app
    assert '"zh-CN": {' in app and "en: {" in app
    assert "document.documentElement.lang = locale" in app
    assert 'event.key === "Escape"' in app
    assert 'menuToggle.getAttribute("aria-expanded") === "true"' in app
    assert 'element.setAttribute("aria-label", dictionary[key])' in app
    assert 'element.setAttribute("content", dictionary[key])' in app


def test_manifest_and_404_are_static_pages_compatible() -> None:
    manifest = json.loads((SITE / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert manifest["start_url"] == "./"
    assert manifest["scope"] == "./"
    assert {icon["src"] for icon in manifest["icons"]} == {
        "./apple-touch-icon.png",
        "./favicon.svg",
    }
    assert all(icon["src"].startswith("./") for icon in manifest["icons"])

    missing = (SITE / "404.html").read_text(encoding="utf-8")
    assert '<html lang="en" data-locale="en">' in missing
    assert '<base href="/unlimited-wiki/" />' in missing
    assert 'href="./"' in missing
    assert 'src="./favicon.svg"' in missing
    assert 'src="./language-init.js"' in missing
    assert 'src="./app.js"' in missing
    assert 'data-locale-choice="zh-CN"' in missing
    assert 'data-locale-choice="en"' in missing
    assert 'data-i18n="notFound.title"' in missing


def test_pages_apply_a_strict_meta_csp_before_loading_resources() -> None:
    expected = {
        "index.html": {
            "default-src": "'none'",
            "script-src": "'self'",
            "style-src": "'self'",
            "img-src": "'self'",
            "manifest-src": "'self'",
            "base-uri": "'none'",
            "object-src": "'none'",
            "form-action": "'none'",
        },
        "404.html": {
            "default-src": "'none'",
            "script-src": "'self'",
            "style-src": "'self'",
            "img-src": "'self'",
            "base-uri": "'self'",
            "object-src": "'none'",
            "form-action": "'none'",
        },
    }
    for name in ("index.html", "404.html"):
        source = (SITE / name).read_text(encoding="utf-8")
        parser = parse_document(name)
        policy = parser.meta[("meta", "Content-Security-Policy")]
        directives = {
            part.split(maxsplit=1)[0]: part.split(maxsplit=1)[1]
            for part in (item.strip() for item in policy.split(";"))
            if part and " " in part
        }
        assert directives == expected[name]
        assert parser.meta[("meta", "referrer")] == "no-referrer"
        csp_position = source.index('http-equiv="Content-Security-Policy"')
        first_resource = min(
            position
            for token in ("<base ", '<link rel="icon"', '<link rel="stylesheet"', "<script")
            if (position := source.find(token)) >= 0
        )
        assert csp_position < first_resource

    scripts = (SITE / "app.js").read_text(encoding="utf-8")
    init = (SITE / "language-init.js").read_text(encoding="utf-8")
    for source in (scripts, init):
        assert ".style." not in source
        assert 'setAttribute("style"' not in source
    for name in ("index.html", "404.html"):
        source = (SITE / name).read_text(encoding="utf-8")
        assert " style=" not in source
        assert "<style" not in source


def test_styles_include_focus_responsive_and_reduced_motion_contracts() -> None:
    styles = (SITE / "styles.css").read_text(encoding="utf-8")
    assert ":focus-visible" in styles
    assert "@media (prefers-reduced-motion: reduce)" in styles
    assert "@media (max-width: 520px)" in styles
    assert "overflow-x: auto" in styles
    assert "letter-spacing: 0" in styles
    assert "gradient(" not in styles
    assert 'Georgia, "Times New Roman", serif' in styles
    assert "ui-monospace" in styles
    assert "fonts.googleapis.com" not in styles


def _workflow_step_block(workflow: str, name: str) -> list[str]:
    marker = f"      - name: {name}"
    lines = workflow.splitlines()
    positions = [index for index, line in enumerate(lines) if line == marker]
    assert len(positions) == 1
    step = [marker]
    for line in lines[positions[0] + 1 :]:
        if line.startswith("      - name: ") or re.fullmatch(r"  [A-Za-z0-9_-]+:", line):
            break
        step.append(line)
    while step and not step[-1].strip():
        step.pop()
    return step


def _workflow_job_block(workflow: str, name: str) -> list[str]:
    marker = f"  {name}:"
    lines = workflow.splitlines()
    positions = [index for index, line in enumerate(lines) if line == marker]
    assert len(positions) == 1
    job = [marker]
    for line in lines[positions[0] + 1 :]:
        if re.fullmatch(r"  [A-Za-z0-9_-]+:", line):
            break
        job.append(line)
    while job and not job[-1].strip():
        job.pop()
    return job


def _workflow_step_run(workflow: str, name: str) -> list[str]:
    step = _workflow_step_block(workflow, name)

    run_positions = [index for index, line in enumerate(step) if line == "        run: |"]
    assert len(run_positions) == 1
    commands: list[str] = []
    for line in step[run_positions[0] + 1 :]:
        if not line.strip():
            continue
        assert line.startswith("          ")
        command = line.strip()
        assert not command.startswith("#")
        commands.append(command)
    return commands


def _validate_active_workflow_commands(workflow: str) -> None:
    lines = workflow.splitlines()
    jobs_position = lines.index("jobs:")
    job_names = [
        match.group(1)
        for line in lines[jobs_position + 1 :]
        if (match := re.fullmatch(r"  ([A-Za-z0-9_-]+):", line))
    ]
    assert job_names == ["validate_and_upload", "deploy"]
    validate_job = _workflow_job_block(workflow, "validate_and_upload")
    deploy_job = _workflow_job_block(workflow, "deploy")
    assert [
        match.group(1)
        for line in validate_job
        if (match := re.fullmatch(r"    ([A-Za-z0-9_-]+):(?:\s.*)?", line))
    ] == ["permissions", "runs-on", "timeout-minutes", "steps"]
    assert [
        match.group(1)
        for line in deploy_job
        if (match := re.fullmatch(r"    ([A-Za-z0-9_-]+):(?:\s.*)?", line))
    ] == ["needs", "if", "permissions", "environment", "runs-on", "timeout-minutes", "steps"]
    assert [line.removeprefix("      - name: ") for line in validate_job if line.startswith("      - name: ")] == [
        "Checkout",
        "Validate static scripts",
        "Validate Pages contract",
        "Upload static site",
    ]
    assert [line.removeprefix("      - name: ") for line in deploy_job if line.startswith("      - name: ")] == [
        "Verify deployment is still the branch tip",
        "Configure Pages",
        "Deploy",
    ]
    assert all(
        line.startswith("      - name: ")
        for line in validate_job + deploy_job
        if line.startswith("      - ")
    )
    forbidden_control = re.compile(r"(?:|    )(?:defaults|env|container|services):(?:\s.*)?$")
    assert not any(forbidden_control.fullmatch(line) for line in lines)
    assert "continue-on-error:" not in workflow
    assert "if: always()" not in workflow
    assert _workflow_step_block(workflow, "Validate static scripts") == [
        "      - name: Validate static scripts",
        "        run: |",
        "          node --check site/language-init.js",
        "          node --check site/app.js",
    ]
    assert _workflow_step_block(workflow, "Validate Pages contract") == [
        "      - name: Validate Pages contract",
        "        run: |",
        "          python3 -c 'import sys; assert sys.version_info[:2] == (3, 12)'",
        "          python3 tests/test_github_pages.py",
    ]
    assert _workflow_step_block(workflow, "Verify deployment is still the branch tip") == [
        "      - name: Verify deployment is still the branch tip",
        "        env:",
        "          EXPECTED_SHA: ${{ github.sha }}",
        "          GH_TOKEN: ${{ github.token }}",
        "        run: |",
        "          latest_sha=\"$(gh api -H 'X-GitHub-Api-Version: 2022-11-28' \\",
        '            "repos/${GITHUB_REPOSITORY}/git/ref/heads/codex/github-pages-site" \\',
        "            --jq .object.sha)\"",
        '          test "$latest_sha" = "$EXPECTED_SHA"',
    ]
    assert _workflow_step_block(workflow, "Checkout") == [
        "      - name: Checkout",
        "        uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4",
        "        with:",
        "          persist-credentials: false",
    ]
    assert _workflow_step_block(workflow, "Upload static site") == [
        "      - name: Upload static site",
        "        uses: actions/upload-pages-artifact@56afc609e74202658d3ffba0e8f6dda462b719fa # v3",
        "        with:",
        "          path: ./site",
    ]
    assert _workflow_step_block(workflow, "Configure Pages") == [
        "      - name: Configure Pages",
        "        uses: actions/configure-pages@983d7736d9b0ae728b81ab479565c72886d7745b # v5",
    ]
    assert _workflow_step_block(workflow, "Deploy") == [
        "      - name: Deploy",
        "        id: deployment",
        "        uses: actions/deploy-pages@d6db90164ac5ed86f2b6aed7e0febac5b3c0c03e # v4",
    ]
    assert _workflow_step_run(workflow, "Validate Pages contract") == [
        "python3 -c 'import sys; assert sys.version_info[:2] == (3, 12)'",
        "python3 tests/test_github_pages.py",
    ]
    assert _workflow_step_run(workflow, "Verify deployment is still the branch tip") == [
        "latest_sha=\"$(gh api -H 'X-GitHub-Api-Version: 2022-11-28' \\",
        '"repos/${GITHUB_REPOSITORY}/git/ref/heads/codex/github-pages-site" \\',
        "--jq .object.sha)\"",
        'test "$latest_sha" = "$EXPECTED_SHA"',
    ]


def test_pages_workflow_only_deploys_the_static_site() -> None:
    workflow = (ROOT / ".github" / "workflows" / "pages.yml").read_text(encoding="utf-8")
    _validate_active_workflow_commands(workflow)
    assert "workflow_dispatch:" not in workflow
    assert "pull_request_target:" not in workflow
    assert "secrets." not in workflow
    assert "permissions: {}" in workflow
    assert "branches:\n      - codex/github-pages-site" in workflow
    assert "needs: validate_and_upload" in workflow
    assert "if: github.event_name == 'push' && github.ref == 'refs/heads/codex/github-pages-site'" in workflow
    assert "cancel-in-progress: true" in workflow
    assert 'node --check site/language-init.js' in workflow
    assert 'node --check site/app.js' in workflow
    for path in (
        '"tests/test_github_pages.py"',
        '"README.md"',
        '"viewer/index.html"',
        '"viewer/public/**"',
        '"viewer/src/**"',
        '"viewer/package-lock.json"',
    ):
        assert path in workflow
    assert workflow.count("runs-on: ubuntu-24.04") == 2
    assert "ubuntu-latest" not in workflow
    assert "pip install" not in workflow
    assert "python3 -m pytest" not in workflow
    contract = "python3 tests/test_github_pages.py"
    tip_lookup = '"repos/${GITHUB_REPOSITORY}/git/ref/heads/codex/github-pages-site"'
    tip_fence = 'test "$latest_sha" = "$EXPECTED_SHA"'
    for command in (contract, tip_lookup, tip_fence):
        assert command in workflow
    validate_job = workflow.split("  validate_and_upload:", 1)[1].split("\n  deploy:", 1)[0]
    deploy_job = workflow.split("\n  deploy:", 1)[1]
    assert "contents: read" in validate_job
    assert "pages: write" not in validate_job
    assert "id-token: write" not in validate_job
    assert "contents: read" in deploy_job
    assert "pages: write" in deploy_job
    assert "id-token: write" in deploy_job
    assert "GH_TOKEN: ${{ github.token }}" in deploy_job
    uses = re.findall(r"^\s*uses:\s*([^\s#]+)", workflow, flags=re.MULTILINE)
    assert len(uses) == len(ACTION_PINS)
    assert set(uses) == {f"{owner}@{sha}" for owner, sha in ACTION_PINS.items()}
    assert all(owner.startswith("actions/") for owner in ACTION_PINS)
    assert all(re.fullmatch(r"[0-9a-f]{40}", sha) for sha in ACTION_PINS.values())
    assert "persist-credentials: false" in workflow
    assert "path: ./site" in workflow
    assert "path: ./viewer" not in workflow
    assert "npm " not in workflow
    assert workflow.index(contract) < workflow.index("path: ./site")
    assert workflow.index("path: ./site") < workflow.index(tip_lookup)
    assert workflow.index(tip_fence) < workflow.index("actions/configure-pages@")
    assert workflow.index(tip_fence) < workflow.index("actions/deploy-pages@")


def test_workflow_commands_reject_comment_and_success_bypasses() -> None:
    workflow = (ROOT / ".github" / "workflows" / "pages.yml").read_text(encoding="utf-8")
    mutations = (
        workflow.replace(
            "          python3 tests/test_github_pages.py",
            "          # python3 tests/test_github_pages.py",
            1,
        ),
        workflow.replace(
            "          python3 tests/test_github_pages.py",
            "          true # python3 tests/test_github_pages.py",
            1,
        ),
        workflow.replace(
            '          test "$latest_sha" = "$EXPECTED_SHA"',
            '          true # test "$latest_sha" = "$EXPECTED_SHA"',
            1,
        ),
        workflow.replace(
            '          test "$latest_sha" = "$EXPECTED_SHA"',
            '          test "$latest_sha" = "$EXPECTED_SHA" || true',
            1,
        ),
        workflow.replace(
            "          python3 tests/test_github_pages.py",
            "          python3 tests/test_github_pages.py\n          exit 0",
            1,
        ),
        workflow.replace(
            "      - name: Validate Pages contract",
            "      - name: Validate Pages contract\n        continue-on-error: true",
            1,
        ),
        workflow.replace(
            "      - name: Upload static site",
            "      - name: Upload static site\n        if: always()",
            1,
        ),
        workflow.replace(
            "      - name: Validate Pages contract",
            "      - name: Validate Pages contract\n        if: false",
            1,
        ),
        workflow.replace(
            "      - name: Verify deployment is still the branch tip",
            "      - name: Verify deployment is still the branch tip\n        if: false",
            1,
        ),
        workflow.replace(
            "          node --check site/language-init.js",
            "          printf 'pass' > tests/test_github_pages.py\n          node --check site/language-init.js",
            1,
        ),
        workflow.replace(
            "  validate_and_upload:\n",
            "  validate_and_upload:\n    defaults:\n      run:\n        shell: true {0}\n",
            1,
        ),
        workflow.replace(
            "  deploy:\n",
            "  deploy:\n    defaults:\n      run:\n        shell: true {0}\n",
            1,
        ),
        workflow.replace(
            "permissions: {}\n",
            "permissions: {}\nenv:\n  BASH_ENV: /tmp/bypass\n",
            1,
        ),
        workflow.replace(
            "  validate_and_upload:\n",
            "  validate_and_upload:\n    container: attacker/image\n",
            1,
        ),
        workflow.replace(
            "  deploy:\n",
            "  deploy:\n    services:\n      bypass:\n        image: attacker/image\n",
            1,
        ),
    )
    for mutated in mutations:
        try:
            _validate_active_workflow_commands(mutated)
        except AssertionError:
            continue
        raise AssertionError("workflow command bypass was accepted")


if __name__ == "__main__":
    checks = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for check in checks:
        check()
    print(f"GitHub Pages contract: {len(checks)} checks passed")
