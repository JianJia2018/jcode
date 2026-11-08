"""Exercise the fork workflow's release decisions without contacting GitHub."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/fork-release.yml"
GIT = shutil.which("git")
PWSH = shutil.which("pwsh")


def find_bash():
    override = os.environ.get("FORK_RELEASE_BASH")
    if override:
        return override
    if os.name == "nt" and GIT:
        git_bash = Path(GIT).parent.parent / "bin" / "bash.exe"
        if git_bash.is_file():
            return str(git_bash)
    return shutil.which("bash")


BASH = find_bash()


def workflow_script(step_name):
    """Extract a named step's literal run block for execution, not inspection."""
    text = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(r"^      - name: " + re.escape(step_name) + r"\s*$", text, re.M)
    if not match:
        raise AssertionError(f"Workflow step not found: {step_name}")
    lines = text[match.end():].splitlines()
    for index, line in enumerate(lines):
        if line == "        run: |":
            break
        if line.startswith("      - ") or re.match(r"^  [\w-]+:", line):
            raise AssertionError(f"No literal run block in step: {step_name}")
    else:
        raise AssertionError(f"No literal run block in step: {step_name}")
    script = []
    for line in lines[index + 1:]:
        if line.startswith("          "):
            script.append(line[10:])
        elif not line:
            script.append("")
        else:
            break
    return "\n".join(script) + "\n"


def release(tag, published_at, *, draft=False, prerelease=False):
    return {"tag_name": tag, "published_at": published_at,
            "draft": draft, "prerelease": prerelease}


def windows_release(tag):
    published = release(tag, "2026-11-08T12:00:00Z")
    published["assets"] = [
        {"name": name, "state": "uploaded", "size": 123}
        for name in ("jcode-windows-x86_64.exe", "jcode-windows-x86_64.zip")
    ]
    return published


# gh returns an outer array of pages with --slurp; without --paginate only
# page one is visible. Selection remains entirely in the workflow's script.
# Git transport, rebase, commit and push are deliberately blocked: candidate
# tag lookups still execute real git against the isolated repository.
SYNC_FIXTURE = r'''
python3() { "$FIXTURE_PYTHON" "$@"; }
gh() {
    if [ "${1-}" != api ]; then
        echo "Unexpected gh operation" >&2
        return 97
    fi
    printf 'api\n' >> .fixture-gh-calls
    shift
    if [ "${1-}" = "repos/fixture/fork/releases" ]; then
        cat .fixture-fork.json
        return 0
    fi
    local paginate=0 slurp=0 page
    local -a pages=()
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --paginate) paginate=1 ;;
            --slurp) slurp=1 ;;
        esac
        shift
    done
    for page in .fixture-page-*.json; do
        [ -f "$page" ] || break
        pages+=("$page")
        [ "$paginate" -eq 1 ] || break
    done
    if [ "$slurp" -eq 1 ]; then
        "$FIXTURE_PYTHON" -c '
import json, sys
pages = []
for path in sys.argv[1:]:
    with open(path, encoding="utf-8") as page:
        pages.append(json.load(page))
print(json.dumps(pages))
' "${pages[@]}"
    else
        for page in "${pages[@]}"; do cat "$page"; done
    fi
}
git() {
    case "${1-}" in
        rev-parse)
            if [ "${2-}" = origin/master ]; then
                printf '%040d\n' 0
            else
                command git "$@"
            fi ;;
        merge-base) printf '%040d\n' 0 ;;
        config|fetch|rebase|add|commit|tag|push)
            printf '%s\n' "$*" >> .fixture-git-calls ;;
        *) echo "Unexpected git operation: $*" >&2; return 97 ;;
    esac
}
'''


@unittest.skipUnless(BASH and GIT, "requires bash and git")
class SyncWorkflowTests(unittest.TestCase):
    def run_sync(self, *, pages=(), current="v0.90.0", requested="", tags=(),
                 force=False, version="0.90.0", fork_releases=()):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            subprocess.run([GIT, "init", "--quiet", str(repo)], check=True,
                           capture_output=True)
            blob = subprocess.run([GIT, "hash-object", "-w", "--stdin"], cwd=repo,
                                  input="fixture", text=True, capture_output=True,
                                  check=True).stdout.strip()
            for tag in tags:
                subprocess.run([GIT, "update-ref", f"refs/tags/{tag}", blob], cwd=repo,
                               check=True, capture_output=True)
            (repo / ".github").mkdir()
            (repo / ".github/upstream-version").write_text(current + "\n", encoding="utf-8")
            (repo / "Cargo.toml").write_text(
                f'[package]\nname = "jcode"\nversion = "{version}"\n', encoding="utf-8")
            (repo / "Cargo.lock").write_text(
                f'[[package]]\nname = "jcode"\nversion = "{version}"\n', encoding="utf-8")
            for number, page in enumerate(pages, 1):
                (repo / f".fixture-page-{number}.json").write_text(
                    json.dumps(page), encoding="utf-8")
            (repo / ".fixture-fork.json").write_text(
                json.dumps([list(fork_releases)]), encoding="utf-8")
            env = dict(os.environ, REQUESTED_UPSTREAM_TAG=requested,
                       GITHUB_OUTPUT=".fixture-output", FIXTURE_PYTHON=sys.executable,
                       GH_TOKEN="fixture", GITHUB_REPOSITORY="fixture/fork",
                       FORCE_BUILD="true" if force else "false")
            script = repo / ".fixture-sync.sh"
            script.write_text(SYNC_FIXTURE + workflow_script(
                "Rebase custom commits onto latest upstream release"),
                encoding="utf-8", newline="\n")
            result = subprocess.run([BASH, ".fixture-sync.sh"], cwd=repo, env=env,
                                    text=True, capture_output=True, timeout=30)

            def read_optional(path):
                path = repo / path
                return path.read_text(encoding="utf-8") if path.exists() else ""

            return {"result": result, "output": read_optional(".fixture-output"),
                    "marker": read_optional(".github/upstream-version").strip(),
                    "cargo": read_optional("Cargo.toml"), "lock": read_optional("Cargo.lock"),
                    "gh": read_optional(".fixture-gh-calls").splitlines(),
                    "git": read_optional(".fixture-git-calls").splitlines()}

    def assert_selected(self, outcome, upstream, fork_tag):
        self.assertEqual(outcome["result"].returncode, 0, outcome["result"].stderr)
        self.assertEqual(outcome["marker"], upstream)
        self.assertEqual(outcome["output"].strip(), f"release_tag={fork_tag}")
        version = fork_tag.removeprefix("v")
        self.assertIn(f'version = "{version}"', outcome["cargo"])
        self.assertIn(f'version = "{version}"', outcome["lock"])

    def test_latest_published_release_can_be_preview_or_stable_across_pages(self):
        cases = (
            (
                [
                    [release("v0.99.1", "2026-10-18T12:00:00Z"),
                     release("v1.0.0-preview.135", "2026-10-21T12:00:00Z", prerelease=True)],
                    [release("v99.0.0", "2026-11-01T12:00:00Z", draft=True),
                     release("v1.0.0-preview.136", "2026-10-26T12:00:00Z", prerelease=True),
                     release("v0.98.0", "2026-09-01T12:00:00Z")],
                ],
                "v1.0.0-preview.136", "v1.0.0-preview.136.fork.1",
            ),
            (
                [
                    [release("v1.0.0-preview.136", "2026-10-26T12:00:00Z", prerelease=True),
                     release("v90.0.0", None)],
                    [release("v1.0.0", "2026-10-28T12:00:00Z"),
                     release("v91.0.0", "2026-11-01T12:00:00Z", draft=True),
                     release("v0.99.1", "2026-10-18T12:00:00Z")],
                ],
                "v1.0.0", "v1.0.1",
            ),
        )
        for pages, upstream, fork_tag in cases:
            with self.subTest(upstream=upstream):
                outcome = self.run_sync(pages=pages)
                self.assert_selected(outcome, upstream, fork_tag)
                self.assertEqual(outcome["gh"], ["api"])

    def test_no_published_non_draft_release_fails_before_git_or_output(self):
        outcome = self.run_sync(pages=[[release("v1.0.0", None),
                                        release("v2.0.0", "2026-11-01T00:00:00Z", draft=True)]])
        self.assertNotEqual(outcome["result"].returncode, 0)
        self.assertRegex(outcome["result"].stderr.lower(), r"(?:could not|cannot|no|unable).*release")
        self.assertEqual(outcome["gh"], ["api"])
        self.assertEqual(outcome["marker"], "v0.90.0")
        self.assertEqual(outcome["output"], "")
        self.assertEqual(outcome["git"], [])

    def test_requested_upstream_overrides_api_and_preview_collisions(self):
        tags = ("v1.0.0-preview.136.fork.1", "v1.0.0-preview.136.fork.2",
                "v1.0.0-preview.136.fork.4")
        outcome = self.run_sync(
            requested="v1.0.0-preview.136", tags=tags,
            pages=[[release("v2.0.0", "2026-11-01T00:00:00Z")]])
        self.assert_selected(outcome, "v1.0.0-preview.136", "v1.0.0-preview.136.fork.3")
        self.assertEqual(outcome["gh"], [])

    def test_stable_uses_first_free_next_patch(self):
        outcome = self.run_sync(requested="v0.99.1", tags=("v0.99.2", "v0.99.4"))
        self.assert_selected(outcome, "v0.99.1", "v0.99.3")
        self.assertEqual(outcome["gh"], [])

    def test_manual_force_builds_current_preview_with_new_tag(self):
        tag = "v1.0.0-preview.199.fork.1"
        outcome = self.run_sync(
            current="v1.0.0-preview.199", requested="v1.0.0-preview.199",
            force=True, version=tag.removeprefix("v"), tags=(tag,),
            fork_releases=[windows_release(tag)])
        self.assert_selected(outcome, "v1.0.0-preview.199", "v1.0.0-preview.199.fork.2")
        self.assertEqual(outcome["gh"], [])

    def test_scheduled_current_source_with_only_old_stable_release_builds_preview(self):
        outcome = self.run_sync(
            current="v1.0.0-preview.199", version="0.99.3",
            pages=[[release("v1.0.0-preview.199", "2026-11-06T15:01:12Z", prerelease=True)]],
            fork_releases=[windows_release("v0.99.3")])
        self.assert_selected(outcome, "v1.0.0-preview.199", "v1.0.0-preview.199.fork.1")

    def test_complete_current_publication_skips_git_and_output(self):
        for upstream, tag in (("v1.0.0-preview.199", "v1.0.0-preview.199.fork.1"),
                              ("v0.99.1", "v0.99.3")):
            with self.subTest(upstream=upstream):
                outcome = self.run_sync(
                    current=upstream, version=tag.removeprefix("v"),
                    pages=[[release(upstream, "2026-11-06T15:01:12Z")]],
                    fork_releases=[windows_release(tag)])
                self.assertEqual(outcome["result"].returncode, 0, outcome["result"].stderr)
                self.assertEqual(outcome["marker"], upstream)
                self.assertEqual(outcome["git"], [])
                self.assertEqual(outcome["output"], "")

    def test_missing_draft_or_incomplete_preview_is_rebuilt(self):
        tag = "v1.0.0-preview.199.fork.1"
        draft = windows_release(tag)
        draft["draft"] = True
        incomplete = windows_release(tag)
        incomplete["assets"] = incomplete["assets"][:1]
        empty_asset = windows_release(tag)
        empty_asset["assets"][0]["size"] = 0
        for published in ([], [draft], [incomplete], [empty_asset]):
            with self.subTest(releases=published):
                outcome = self.run_sync(
                    current="v1.0.0-preview.199", requested="v1.0.0-preview.199",
                    version=tag.removeprefix("v"), tags=(tag,), fork_releases=published)
                self.assert_selected(outcome, "v1.0.0-preview.199", "v1.0.0-preview.199.fork.2")


@unittest.skipUnless(BASH and GIT, "requires bash and git")
class RebaseWorkflowTests(unittest.TestCase):
    def test_shared_post_release_commits_are_not_replayed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            repo = root / "work"
            upstream = root / "upstream.git"
            origin = root / "origin.git"

            def git(*args, cwd=repo):
                return subprocess.run([GIT, *args], cwd=cwd, check=True,
                                      text=True, capture_output=True).stdout.strip()

            git("init", "--bare", str(upstream), cwd=root)
            git("init", "--bare", str(origin), cwd=root)
            git("init", "-b", "master", str(repo), cwd=root)
            git("config", "user.name", "Fixture")
            git("config", "user.email", "fixture@example.invalid")
            (repo / "Cargo.toml").write_text(
                '[package]\nname = "jcode"\nversion = "0.99.1"\n', encoding="utf-8")
            (repo / "Cargo.lock").write_text(
                '[[package]]\nname = "jcode"\nversion = "0.99.1"\n', encoding="utf-8")
            badge = repo / "downloads.json"
            badge.write_text('"old release"\n', encoding="utf-8")
            git("add", ".")
            git("commit", "-m", "old upstream release")
            git("tag", "v1.0.0-preview.136")
            badge.write_text('"shared post-release update"\n', encoding="utf-8")
            git("commit", "-am", "shared upstream badge update")
            git("branch", "upstream-base")

            (repo / ".github").mkdir()
            (repo / ".github/upstream-version").write_text(
                "v1.0.0-preview.136\n", encoding="utf-8")
            (repo / "custom.txt").write_text("keep fork customization\n", encoding="utf-8")
            git("add", ".")
            git("commit", "-m", "fork customization")
            git("remote", "add", "origin", str(origin))
            git("push", "-u", "origin", "master")

            git("checkout", "upstream-base")
            badge.write_text('"latest upstream update"\n', encoding="utf-8")
            git("commit", "-am", "latest upstream badge update")
            git("tag", "v1.0.0-preview.199")
            git("push", str(upstream), "--tags")
            git("checkout", "master")
            git("config", f"url.{upstream.as_uri()}.insteadOf",
                "https://github.com/1jehuang/jcode.git")

            script = root / "sync.sh"
            script.write_text('python3() { "$FIXTURE_PYTHON" "$@"; }\n' + workflow_script(
                "Rebase custom commits onto latest upstream release"),
                encoding="utf-8", newline="\n")
            output = root / "output"
            env = dict(os.environ, REQUESTED_UPSTREAM_TAG="v1.0.0-preview.199",
                       FIXTURE_PYTHON=sys.executable, GITHUB_OUTPUT=output.as_posix())
            result = subprocess.run([BASH, script.as_posix()], cwd=repo, env=env,
                                    text=True, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(git("show", "master:downloads.json"),
                             '"latest upstream update"')
            self.assertEqual(git("show", "master:custom.txt"), "keep fork customization")
            self.assertEqual(git("log", "--format=%s", "v1.0.0-preview.199..master"),
                             "fork customization")
            self.assertEqual(git("rev-parse", "master"),
                             git("rev-parse", "refs/heads/master", cwd=origin))
            self.assertEqual(git("show", "master:.github/upstream-version"),
                             "v1.0.0-preview.199")
            self.assertEqual(output.read_text(encoding="utf-8").strip(),
                             "release_tag=v1.0.0-preview.199.fork.1")


PUBLISH_FIXTURE = r'''
function gh {
    $call = @($args)
    $json = ConvertTo-Json -InputObject $call -Compress
    [System.IO.File]::AppendAllText((Join-Path (Get-Location) '.fixture-gh-calls'),
                                   $json + [Environment]::NewLine)
    if ($call[0] -eq 'release' -and $call[1] -eq 'view' -and
        $env:FIXTURE_RELEASE_EXISTS -eq '0') {
        $global:LASTEXITCODE = 1
    } else {
        $global:LASTEXITCODE = 0
    }
}
'''


def boolean_flag(arguments, name):
    """Read a gh boolean flag; None means that the flag was not specified."""
    result = None
    for index, argument in enumerate(arguments):
        if argument == name:
            if index + 1 < len(arguments) and arguments[index + 1].lower() in ("true", "false"):
                result = arguments[index + 1].lower() == "true"
            else:
                result = True
        elif argument.startswith(name + "="):
            value = argument.split("=", 1)[1].lower()
            if value not in ("true", "false"):
                raise AssertionError(f"Invalid boolean flag: {argument}")
            result = value == "true"
    return result


@unittest.skipUnless(PWSH, "requires PowerShell")
class PublishWorkflowTests(unittest.TestCase):
    def run_publish(self, tag, exists):
        with tempfile.TemporaryDirectory() as temp:
            repo = Path(temp)
            script = repo / "publish-fixture.ps1"
            script.write_text(PUBLISH_FIXTURE + workflow_script("Publish GitHub release"),
                              encoding="utf-8")
            env = dict(os.environ, RELEASE_TAG=tag,
                       FIXTURE_RELEASE_EXISTS="1" if exists else "0", GH_TOKEN="fixture",
                       GH_REPO="fixture/fixture")
            result = subprocess.run([PWSH, "-NoLogo", "-NoProfile", "-NonInteractive",
                                     "-File", str(script)], cwd=repo, env=env,
                                    text=True, capture_output=True, timeout=30)
            calls = repo / ".fixture-gh-calls"
            return result, ([json.loads(line) for line in calls.read_text(encoding="utf-8").splitlines()]
                            if calls.exists() else [])

    def test_preview_and_stable_new_and_existing_release_flags(self):
        for tag, prerelease in (("v1.0.0-preview.136.fork.1", True), ("v0.99.2", False)):
            for exists in (False, True):
                with self.subTest(tag=tag, exists=exists):
                    result, calls = self.run_publish(tag, exists)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    create_calls = [call for call in calls if len(call) > 1 and call[1] == "create"]
                    self.assertEqual(len(create_calls), 0 if exists else 1)
                    if not exists:
                        create = create_calls[0]
                        self.assertIs(boolean_flag(create, "--draft"), True)
                        self.assertIs(boolean_flag(create, "--prerelease"), prerelease)
                    edit_calls = [call for call in calls if len(call) > 1 and call[1] == "edit"]
                    self.assertEqual(len(edit_calls), 1)
                    edit = edit_calls[0]
                    self.assertIs(boolean_flag(edit, "--draft"), False)
                    self.assertIs(boolean_flag(edit, "--prerelease"), prerelease)
                    if prerelease:
                        for call in calls:
                            self.assertIsNot(boolean_flag(call, "--latest"), True)


if __name__ == "__main__":
    unittest.main()
