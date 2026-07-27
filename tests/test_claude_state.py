"""Unit tests for the Claude state layer.

These pin the two things that silently corrupt a transfer when wrong: the ``~/.claude/projects`` path encoding, and
the transcript path rewrite. Both are reverse-engineered from a real Claude Code install (see the S1 spike writeup in
``docs/session-transfer-notes.md``), so the encoding cases below are copied verbatim from observed directory names on
the spike machine — they are regression evidence, not invented examples.

Everything filesystem-touching runs against a synthetic ``~/.claude`` in ``tmp_path`` via the ``FWD_CLAUDE_HOME``
env override, so the suite never reads or writes the developer's real Claude state. Nothing here shells out to ssh:
remote paths are exercised with a duck-typed fake endpoint.
"""

from __future__ import annotations

import json
import tarfile
from pathlib import Path

import pytest

from fwd import claude_state
from fwd.claude_state import (
    CONFIG_EXCLUDE,
    build_config_bundle,
    encode_project_path,
    export_session_bundle,
    rewrite_jsonl,
)

# (cwd, encoded dir) pairs read off a real ~/.claude/projects on claude 2.1.220. Each covers a distinct character
# class: plain, dot-prefixed + underscore, dot in a filename-ish component, spaces, and pre-existing hyphens/digits.
REAL_ENCODING_PAIRS = [
    ("/Users/sid/Coding/Python/fwd", "-Users-sid-Coding-Python-fwd"),
    ("/Users/sid", "-Users-sid"),
    ("/Users/sid/.shell/shared_scripts", "-Users-sid--shell-shared-scripts"),
    (
        "/Users/sid/Coding/Swift/Pocket-Congress/pocketcongress.org",
        "-Users-sid-Coding-Swift-Pocket-Congress-pocketcongress-org",
    ),
    ("/Users/sid/Downloads/Believe It or Not Data", "-Users-sid-Downloads-Believe-It-or-Not-Data"),
    (
        "/private/tmp/claude-501/-Users-sid-Coding-Python-prov/fb030e53-7ad7-4260-b346-d4f24c3a29f1/scratchpad/m2-gate",
        "-private-tmp-claude-501--Users-sid-Coding-Python-prov-fb030e53-7ad7-4260-b346-d4f24c3a29f1-scratchpad-m2-gate",
    ),
]


@pytest.fixture
def claude_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the module at a synthetic ``~/.claude`` under ``tmp_path``."""
    home = tmp_path / "claude-home"
    (home / "projects").mkdir(parents=True)
    monkeypatch.setenv("FWD_CLAUDE_HOME", str(home))
    return home


class FakeEndpoint:
    """Duck-typed stand-in for ``SSHEndpoint`` — records commands instead of running ssh."""

    def __init__(self) -> None:
        self.commands: list[str] = []

    def ssh_argv(self, *, tty: bool = False, control: bool = True) -> list[str]:
        return ["ssh", "fake"]

    def run(self, cmd: str, **kwargs: object) -> None:
        self.commands.append(cmd)


# --------------------------------------------------------------------------------------------- encode_project_path


@pytest.mark.parametrize(("cwd", "expected"), REAL_ENCODING_PAIRS)
def test_encode_matches_real_directory_names(cwd: str, expected: str) -> None:
    assert encode_project_path(cwd) == expected


def test_encode_accepts_path_objects_and_strips_trailing_slash() -> None:
    assert encode_project_path(Path("/Users/sid/Coding")) == "-Users-sid-Coding"
    assert encode_project_path("/Users/sid/Coding/") == encode_project_path("/Users/sid/Coding")


def test_encode_collapses_every_non_alphanumeric() -> None:
    assert encode_project_path("/a b/c.d/e_f/g-h/i+j") == "-a-b-c-d-e-f-g-h-i-j"


def test_encode_preserves_digits_and_case() -> None:
    assert encode_project_path("/opt/App2/RunPod9") == "-opt-App2-RunPod9"


# ------------------------------------------------------------------------------------------------------ rewrite_jsonl


def test_rewrite_jsonl_replaces_cwd_and_home_and_counts_lines(tmp_path: Path) -> None:
    src = tmp_path / "s.jsonl"
    src.write_text(
        json.dumps({"cwd": "/Users/sid/proj", "text": "see /Users/sid/.claude/CLAUDE.md"})
        + "\n"
        + json.dumps({"cwd": "/Users/sid/proj", "text": "nothing to change here"})
        + "\n",
        encoding="utf-8",
    )
    dst = tmp_path / "d.jsonl"

    count = rewrite_jsonl(src, dst, {"/Users/sid/proj": "/root/proj", "/Users/sid": "/root"})

    assert count == 2
    lines = [json.loads(line) for line in dst.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["cwd"] == "/root/proj"
    assert lines[0]["text"] == "see /root/.claude/CLAUDE.md"
    assert lines[1]["cwd"] == "/root/proj"


def test_rewrite_jsonl_cwd_wins_over_overlapping_home_prefix(tmp_path: Path) -> None:
    """The cwd lives *under* home, so home-first ordering would mangle it. Dict order is the contract."""
    src = tmp_path / "s.jsonl"
    src.write_text(json.dumps({"cwd": "/Users/sid/proj"}) + "\n", encoding="utf-8")
    dst = tmp_path / "d.jsonl"

    rewrite_jsonl(src, dst, {"/Users/sid/proj": "/workspace/proj", "/Users/sid": "/root"})

    assert json.loads(dst.read_text(encoding="utf-8"))["cwd"] == "/workspace/proj"


def test_rewrite_jsonl_counts_all_lines_including_untouched(tmp_path: Path) -> None:
    src = tmp_path / "s.jsonl"
    src.write_text("a\nb\nc\n", encoding="utf-8")
    dst = tmp_path / "d.jsonl"
    assert rewrite_jsonl(src, dst, {"/nope": "/nah"}) == 3
    assert dst.read_text(encoding="utf-8") == "a\nb\nc\n"


def test_rewrite_jsonl_survives_malformed_lines(tmp_path: Path) -> None:
    """A truncated final line (common on a crashed session) must not abort the rest of the copy."""
    src = tmp_path / "s.jsonl"
    src.write_text(json.dumps({"cwd": "/old"}) + '\n{"cwd": "/old", trunc', encoding="utf-8")
    dst = tmp_path / "d.jsonl"
    assert rewrite_jsonl(src, dst, {"/old": "/new"}) == 2
    assert "/old" not in dst.read_text(encoding="utf-8")


def test_rewrite_jsonl_creates_missing_parent(tmp_path: Path) -> None:
    src = tmp_path / "s.jsonl"
    src.write_text("x\n", encoding="utf-8")
    dst = tmp_path / "deep" / "nested" / "d.jsonl"
    assert rewrite_jsonl(src, dst, {}) == 1
    assert dst.exists()


# ----------------------------------------------------------------------------------------------- export_session_bundle


def _seed_session(home: Path, cwd: Path, session_id: str, *, mtime: float | None = None) -> Path:
    """Create a transcript for ``cwd`` inside the synthetic claude home and return its path."""
    project = home / "projects" / encode_project_path(cwd.resolve())
    project.mkdir(parents=True, exist_ok=True)
    transcript = project / f"{session_id}.jsonl"
    transcript.write_text(json.dumps({"cwd": str(cwd), "sessionId": session_id}) + "\n", encoding="utf-8")
    if mtime is not None:
        import os

        os.utime(transcript, (mtime, mtime))
    return transcript


def test_export_bundle_structure(claude_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_state, "_claude_version", lambda: "2.1.220 (Claude Code)")
    cwd = tmp_path / "proj"
    cwd.mkdir()
    transcript = _seed_session(claude_home, cwd, "aaaa-1111")
    (transcript.with_suffix("") / "subagents").mkdir(parents=True)
    (transcript.with_suffix("") / "subagents" / "a.jsonl").write_text("{}\n", encoding="utf-8")
    (claude_home / "tasks" / "aaaa-1111").mkdir(parents=True)
    (claude_home / "tasks" / "aaaa-1111" / "1.json").write_text("{}", encoding="utf-8")

    bundle = export_session_bundle(cwd, tmp_path / "out")

    assert bundle is not None and bundle.exists()
    with tarfile.open(bundle) as tar:
        names = set(tar.getnames())
        meta = json.loads(tar.extractfile("meta.json").read().decode())
    assert "transcript/aaaa-1111.jsonl" in names
    assert "transcript/aaaa-1111/subagents/a.jsonl" in names
    assert "todos/aaaa-1111/1.json" in names
    assert meta["session_id"] == "aaaa-1111"
    assert meta["local_cwd"] == str(cwd.resolve())
    assert meta["claude_version"] == "2.1.220 (Claude Code)"
    assert meta["encoded_local"] == encode_project_path(cwd.resolve())


def test_export_bundle_picks_latest_by_mtime(claude_home: Path, tmp_path: Path) -> None:
    cwd = tmp_path / "proj"
    cwd.mkdir()
    _seed_session(claude_home, cwd, "old", mtime=1_000_000)
    _seed_session(claude_home, cwd, "new", mtime=2_000_000)

    bundle = export_session_bundle(cwd, tmp_path / "out")

    assert bundle is not None
    with tarfile.open(bundle) as tar:
        assert json.loads(tar.extractfile("meta.json").read().decode())["session_id"] == "new"


def test_export_bundle_honours_explicit_session_id(claude_home: Path, tmp_path: Path) -> None:
    cwd = tmp_path / "proj"
    cwd.mkdir()
    _seed_session(claude_home, cwd, "old", mtime=1_000_000)
    _seed_session(claude_home, cwd, "new", mtime=2_000_000)

    bundle = export_session_bundle(cwd, tmp_path / "out", session_id="old")

    assert bundle is not None
    with tarfile.open(bundle) as tar:
        assert json.loads(tar.extractfile("meta.json").read().decode())["session_id"] == "old"


def test_export_bundle_returns_none_when_no_transcript(claude_home: Path, tmp_path: Path) -> None:
    cwd = tmp_path / "empty"
    cwd.mkdir()
    assert export_session_bundle(cwd, tmp_path / "out") is None


def test_export_bundle_returns_none_for_unknown_session_id(claude_home: Path, tmp_path: Path) -> None:
    cwd = tmp_path / "proj"
    cwd.mkdir()
    _seed_session(claude_home, cwd, "aaaa")
    assert export_session_bundle(cwd, tmp_path / "out", session_id="zzzz") is None


# --------------------------------------------------------------------------------------------- import_session_bundle


def test_import_session_bundle_returns_id_and_rewrites_remotely(
    claude_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cwd = tmp_path / "proj"
    cwd.mkdir()
    _seed_session(claude_home, cwd, "sid-1")
    bundle = export_session_bundle(cwd, tmp_path / "out")
    assert bundle is not None

    monkeypatch.setattr(claude_state, "_upload_file", lambda *a, **k: None)
    endpoint = FakeEndpoint()

    session_id = claude_state.import_session_bundle(endpoint, bundle, "/workspace/proj", "/root")

    assert session_id == "sid-1"
    script = endpoint.commands[0]
    assert f"/root/.claude/projects/{encode_project_path('/workspace/proj')}" in script
    # Rewrite args are passed positionally to the remote python3: local cwd, remote cwd, local home, remote home.
    assert f"{cwd.resolve()} /workspace/proj {Path.home()} /root" in script


def test_import_session_bundle_returns_none_on_remote_failure(
    claude_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cwd = tmp_path / "proj"
    cwd.mkdir()
    _seed_session(claude_home, cwd, "sid-1")
    bundle = export_session_bundle(cwd, tmp_path / "out")
    assert bundle is not None

    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("connection reset")

    monkeypatch.setattr(claude_state, "_upload_file", boom)
    assert claude_state.import_session_bundle(FakeEndpoint(), bundle, "/workspace/proj", "/root") is None


def test_import_session_bundle_returns_none_on_garbage_bundle(tmp_path: Path) -> None:
    junk = tmp_path / "junk.tar.gz"
    junk.write_bytes(b"not a tarball")
    assert claude_state.import_session_bundle(FakeEndpoint(), junk, "/workspace/proj", "/root") is None


# ------------------------------------------------------------------------------------------------------- user config


def test_config_bundle_includes_allowlist_only(claude_home: Path, tmp_path: Path) -> None:
    (claude_home / "CLAUDE.md").write_text("rules", encoding="utf-8")
    (claude_home / "settings.json").write_text("{}", encoding="utf-8")
    (claude_home / "settings.local.json").write_text("{}", encoding="utf-8")
    (claude_home / "skills" / "demo").mkdir(parents=True)
    (claude_home / "skills" / "demo" / "SKILL.md").write_text("skill", encoding="utf-8")
    (claude_home / "history.jsonl").write_text("{}", encoding="utf-8")

    bundle, count = build_config_bundle(tmp_path / "cfg.tar.gz")

    with tarfile.open(bundle) as tar:
        names = set(tar.getnames())
    assert names == {"CLAUDE.md", "settings.json", "skills/demo/SKILL.md"}
    assert count == 3


def test_config_bundle_drops_excluded_names_even_when_requested(claude_home: Path, tmp_path: Path) -> None:
    (claude_home / ".credentials.json").write_text("{}", encoding="utf-8")
    (claude_home / "settings.local.json").write_text("{}", encoding="utf-8")
    (claude_home / "projects").mkdir(exist_ok=True)
    (claude_home / "CLAUDE.md").write_text("rules", encoding="utf-8")

    bundle, count = build_config_bundle(
        tmp_path / "cfg.tar.gz",
        include=[".credentials.json", "settings.local.json", "projects", "CLAUDE.md"],
    )

    with tarfile.open(bundle) as tar:
        assert set(tar.getnames()) == {"CLAUDE.md"}
    assert count == 1


def test_config_bundle_pattern_excludes_secrets_nested_in_skills(claude_home: Path, tmp_path: Path) -> None:
    skill = claude_home / "skills" / "deploy"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("ok", encoding="utf-8")
    (skill / ".env.local").write_text("TOKEN=1", encoding="utf-8")
    (skill / "server.pem").write_text("key", encoding="utf-8")
    (skill / "id_rsa").write_text("key", encoding="utf-8")

    bundle, _ = build_config_bundle(tmp_path / "cfg.tar.gz", include=["skills"])

    with tarfile.open(bundle) as tar:
        assert set(tar.getnames()) == {"skills/deploy/SKILL.md"}


def test_config_exclude_covers_the_dangerous_names() -> None:
    assert {"settings.local.json", ".credentials.json", "projects", "history.jsonl"} <= CONFIG_EXCLUDE


def test_upload_user_config_noop_when_nothing_to_send(claude_home: Path) -> None:
    endpoint = FakeEndpoint()
    claude_state.upload_user_config(endpoint)
    assert endpoint.commands == []


# ------------------------------------------------------------------------------------------------------- credentials


def test_read_keychain_creds_falls_back_to_file(claude_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_state.platform, "system", lambda: "Linux")
    (claude_home / ".credentials.json").write_text('{"token": "x"}', encoding="utf-8")
    assert claude_state.read_keychain_creds() == '{"token": "x"}'


def test_read_keychain_creds_returns_none_when_absent(claude_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_state.platform, "system", lambda: "Linux")
    assert claude_state.read_keychain_creds() is None


def test_upload_creds_pipes_token_over_stdin_never_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    """The token must never reach argv, where remote ``ps`` would expose it."""
    captured: dict[str, object] = {}

    class Result:
        returncode = 0
        stderr = b""

    def fake_run(argv: list[str], **kwargs: object) -> Result:
        captured["argv"] = argv
        captured["input"] = kwargs.get("input")
        return Result()

    monkeypatch.setattr(claude_state.subprocess, "run", fake_run)
    claude_state.upload_creds(FakeEndpoint(), '{"token": "secret"}')

    assert captured["input"] == b'{"token": "secret"}'
    assert not any("secret" in arg for arg in captured["argv"])
    assert "chmod 600" in captured["argv"][-1] and "umask 077" in captured["argv"][-1]


# ----------------------------------------------------------------------------------------------------------- handoff


def test_make_handoff_returns_path_when_claude_writes_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Result:
        returncode = 0
        stdout = "done"
        stderr = ""

    def fake_run(argv: list[str], **kwargs: object) -> Result:
        Path(kwargs["cwd"], "HANDOFF.md").write_text("# HANDOFF\nreal content\n", encoding="utf-8")
        return Result()

    monkeypatch.setattr(claude_state.subprocess, "run", fake_run)
    path = claude_state.make_handoff(tmp_path)

    assert path == (tmp_path.resolve() / "HANDOFF.md")
    assert "real content" in path.read_text(encoding="utf-8")


def test_make_handoff_falls_back_to_template_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise OSError("claude: command not found")

    monkeypatch.setattr(claude_state.subprocess, "run", boom)
    path = claude_state.make_handoff(tmp_path)

    assert path.exists()
    assert "TODO" in path.read_text(encoding="utf-8")


def test_make_handoff_falls_back_when_claude_exits_ok_without_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Result:
        returncode = 0
        stdout = "Here is a summary instead of writing the file."
        stderr = ""

    monkeypatch.setattr(claude_state.subprocess, "run", lambda *a, **k: Result())
    assert "TODO" in claude_state.make_handoff(tmp_path).read_text(encoding="utf-8")
