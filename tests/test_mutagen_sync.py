"""Continuous-sync unit tests — the pure, subprocess-free half of the Mutagen integration.

Everything that can be decided without a daemon is tested here: ignore translation, config precedence, argv
construction, SSH shim generation, the tip throttle, and CLI registration. The parts that genuinely need Mutagen and a
remote host (agent installation, propagation latency, conflict detection) are covered by live validation instead,
because faking them would only assert that the fake behaves like the fake.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from fwd import mutagen_sync, tips
from fwd.config import Config, SshTargetConfig, SyncConfig
from fwd.sshexec import SSHEndpoint

# --------------------------------------------------------------------------------------------- ignore translation


def test_root_gitignore_patterns_pass_through_unchanged() -> None:
    """Root rules are already root-relative, so translation must not rewrite them into something subtly different."""
    assert mutagen_sync.flatten_gitignore([("", "build/\n*.log\n/only-root\n")]) == ["build/", "*.log", "/only-root"]


def test_nested_unanchored_pattern_matches_at_any_depth_below_its_directory() -> None:
    """``foo`` in ``a/b/.gitignore`` means "any foo under a/b", which is ``a/b/**/foo`` in Mutagen's doublestar syntax."""
    assert mutagen_sync.flatten_gitignore([("a/b", "foo\n")]) == ["a/b/**/foo"]


def test_nested_anchored_pattern_binds_to_its_own_directory() -> None:
    """A leading slash anchors to the rule file's directory, not to the worktree root."""
    assert mutagen_sync.flatten_gitignore([("a/b", "/foo\n")]) == ["a/b/foo"]


def test_nested_pattern_with_interior_slash_is_anchored_by_gits_own_rule() -> None:
    """Git anchors any pattern containing a non-trailing slash, so ``src/gen`` must not become a ``**`` match."""
    assert mutagen_sync.flatten_gitignore([("pkg", "src/gen\n")]) == ["pkg/src/gen"]


def test_directory_only_marker_survives_relocation() -> None:
    """A trailing slash means "directories only" in both syntaxes and must not be lost when the pattern is rewritten."""
    assert mutagen_sync.flatten_gitignore([("web", "dist/\n")]) == ["web/**/dist/"]


def test_negations_keep_their_prefix_and_their_position() -> None:
    """Gitignore is last-match-wins, so a re-include must stay after the exclusion it reverses."""
    assert mutagen_sync.flatten_gitignore([("a", "*.bin\n!keep.bin\n")]) == ["a/**/*.bin", "!a/**/keep.bin"]


def test_comments_blank_lines_and_escaped_hashes_are_handled() -> None:
    """Only real rules become ignore arguments, and an escaped ``#`` is a literal filename rather than a comment."""
    assert mutagen_sync.flatten_gitignore([("", "# a comment\n\n  \n\\#hash\n")]) == ["#hash"]


def test_duplicate_patterns_collapse_while_preserving_last_occurrence() -> None:
    """Repeating the same rule from two files adds nothing but noise to the Mutagen command line."""
    assert mutagen_sync.flatten_gitignore([("", "*.log\n"), ("", "*.log\nbuild/\n")]) == ["*.log", "build/"]


def test_dedup_keeps_a_repeated_pattern_in_its_last_position() -> None:
    """Last-match-wins: collapsing a repeat onto its first position would move it ahead of a negation that reverses it."""
    assert mutagen_sync.flatten_gitignore([("", "dist\n"), ("", "!dist\n"), ("", "dist\n")]) == ["!dist", "dist"]


def test_a_negation_still_wins_when_the_pattern_it_reverses_repeats_later_in_the_layering(tmp_path: Path) -> None:
    """The realistic trigger: .gitignore excludes dist, .fwdignore re-includes it, and DEFAULT_EXCLUDES repeats dist."""
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    (tmp_path / ".gitignore").write_text("dist\n", encoding="utf-8")
    (tmp_path / ".fwdignore").write_text("!dist\n", encoding="utf-8")

    patterns = mutagen_sync.ignore_patterns(tmp_path, SyncConfig(exclude=["dist"]))

    # ``dist`` is excluded again by configured exclusions, which are deliberately layered after .fwdignore, so the
    # exclusion must be the final word — exactly as it is for a push, rather than being hoisted above the negation.
    assert patterns.index("!dist") < patterns.index("dist")


def test_fwdignore_comments_and_blank_lines_never_become_ignore_patterns(tmp_path: Path) -> None:
    """Mutagen accepts '--ignore "# comment"' happily and then ignores a file literally called that."""
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    (tmp_path / ".fwdignore").write_text("# a comment\n\n   \n  build/  \n\\#hash\n!keep\n", encoding="utf-8")

    patterns = mutagen_sync.ignore_patterns(tmp_path, SyncConfig(exclude=["# configured comment", "cache/"]))

    assert "# a comment" not in patterns
    assert "# configured comment" not in patterns
    assert not any(pattern.strip() == "" for pattern in patterns)
    assert "build/" in patterns and "cache/" in patterns
    assert "#hash" in patterns and "!keep" in patterns


def test_ignore_patterns_layers_git_fwdignore_config_and_always_excludes(tmp_path: Path) -> None:
    """The continuous domain must match the one-shot domain, plus the .git rule continuous sync adds on its own."""
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    (tmp_path / ".gitignore").write_text("*.log\n", encoding="utf-8")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / ".gitignore").write_text("secret\n", encoding="utf-8")
    (tmp_path / ".fwdignore").write_text("scratch/\n", encoding="utf-8")

    patterns = mutagen_sync.ignore_patterns(tmp_path, SyncConfig(exclude=["node_modules"]))

    assert "*.log" in patterns
    assert "nested/**/secret" in patterns
    assert "scratch/" in patterns
    assert "node_modules" in patterns
    assert ".DS_Store" in patterns
    # Continuous two-way sync of a live repository database can corrupt it, so this exclusion is not optional.
    assert "/.git" in patterns


def test_a_nested_gitignore_that_ignores_itself_still_contributes_its_rules(tmp_path: Path) -> None:
    """git ls-files --exclude-standard hides a self-ignored rule file; the upload path re-adds it, so this must too."""
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    (tmp_path / "local-state").mkdir()
    (tmp_path / "local-state" / ".gitignore").write_text(".gitignore\ncache\n", encoding="utf-8")

    patterns = mutagen_sync.ignore_patterns(tmp_path, SyncConfig())

    assert "local-state/**/cache" in patterns


def test_ignore_patterns_skips_gitignore_when_the_setting_is_off(tmp_path: Path) -> None:
    """``use_gitignore = false`` already means "do not interpret Git rules" for every other transfer."""
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    (tmp_path / ".gitignore").write_text("*.log\n", encoding="utf-8")

    patterns = mutagen_sync.ignore_patterns(tmp_path, SyncConfig(use_gitignore=False))

    assert "*.log" not in patterns
    assert "/.git" in patterns


# ---------------------------------------------------------------------------------------------- config resolution


def _config(*, global_default: bool, override: bool | None) -> Config:
    """Build a config with one target so precedence can be exercised without touching the filesystem."""
    return Config(sync=SyncConfig(continuous=global_default), targets={"box": SshTargetConfig(name="box", host="h", continuous_sync=override)})


@pytest.mark.parametrize(
    ("global_default", "override", "expected"),
    [
        (False, None, False),
        (True, None, True),
        (False, True, True),
        # The case that forces continuous_sync to be tri-state: an explicit per-target false must beat a global true.
        (True, False, False),
    ],
)
def test_target_override_beats_the_global_default(global_default: bool, override: bool | None, expected: bool) -> None:
    assert _config(global_default=global_default, override=override).continuous_sync_for("box") is expected


def test_unknown_or_absent_target_falls_back_to_the_global_default() -> None:
    """Implicit targets (``runpod``, ``user@host``, ssh aliases) have no table, so they inherit rather than error."""
    config = _config(global_default=True, override=False)
    assert config.continuous_sync_for("sid@gpu.example.com") is True
    assert config.continuous_sync_for(None) is True


def test_sync_continuous_is_read_from_config_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The loader must actually consume the key; a dataclass field nothing reads would silently do nothing."""
    from fwd import config as config_mod

    global_path = tmp_path / "config.toml"
    global_path.write_text('[sync]\ncontinuous = true\n[targets.box]\nbackend = "ssh"\nhost = "h"\ncontinuous_sync = false\n', encoding="utf-8")
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_PATH", global_path)

    config = config_mod.load_config(tmp_path)

    assert config.sync.continuous is True
    assert config.targets["box"].continuous_sync is False
    assert config.continuous_sync_for("box") is False


def test_default_config_leaves_continuous_sync_off() -> None:
    """Continuous sync runs a long-lived daemon, so nothing may turn it on without being asked."""
    assert Config().continuous_sync_for("anything") is False


# ------------------------------------------------------------------------------------------- argv and shim building


def test_create_arguments_use_two_way_safe_and_exclude_version_control() -> None:
    """Two-way-safe never discards a side, and ``--ignore-vcs`` is the documented guard for a repository database."""
    arguments = mutagen_sync.create_arguments("fwd-demo", Path("/local/project"), "host:/remote/project", ["*.log", "!keep.log"])

    assert arguments[:2] == ["sync", "create"]
    assert "--mode" in arguments and arguments[arguments.index("--mode") + 1] == "two-way-safe"
    assert "--ignore-vcs" in arguments
    assert arguments[arguments.index("--label") + 1] == "fwd-session=fwd-demo"
    assert arguments[-2:] == ["/local/project", "host:/remote/project"]
    # Order matters: gitignore semantics are last-match-wins, so a negation must follow what it re-includes.
    assert arguments[arguments.index("*.log") + 1 : arguments.index("*.log") + 3] == ["--ignore", "!keep.log"]


def test_endpoint_options_carry_full_ssh_fidelity_without_control_sockets() -> None:
    """A Mutagen URL cannot express these, and riding fwd's multiplex master would let ``fwd stop`` kill the sync."""
    endpoint = SSHEndpoint(host="gpu.example", user="sid", port=2222, key_path="/keys/id", proxy_jump="bastion", extra_opts=["-o", "ServerAliveInterval=30"])

    options = mutagen_sync.endpoint_options(endpoint)

    assert "-i" in options and "/keys/id" in " ".join(options)
    assert options[options.index("-p") + 1] == "2222"
    assert options[options.index("-J") + 1] == "bastion"
    assert "ServerAliveInterval=30" in options
    assert "ControlMaster=auto" not in options
    assert "sid@gpu.example" not in options


def test_scp_options_translate_the_port_flag() -> None:
    """scp spells the port ``-P``; passing ssh's ``-p`` through would silently drop it and mean "preserve times"."""
    assert mutagen_sync._scp_options(["-o", "BatchMode=yes", "-p", "2222", "-i", "/k"]) == ["-o", "BatchMode=yes", "-P", "2222", "-i", "/k"]


def test_remote_url_carries_the_port_so_the_shim_can_tell_two_pods_on_one_ip_apart() -> None:
    """Mutagen's grammar is ``[user@]host[:port]:path``; without the port the daemon calls ssh with no ``-p`` at all."""
    endpoint = SSHEndpoint(host="gpu.example", user="sid", port=2222)
    assert mutagen_sync.remote_url(endpoint, "/work/proj") == "sid@gpu.example:2222:/work/proj"


def test_write_shims_records_per_host_options_and_emits_executable_wrappers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One daemon means one shared MUTAGEN_SSH_PATH, so the wrappers must dispatch on the destination host."""
    monkeypatch.setattr(mutagen_sync, "DATA_DIR", tmp_path)
    monkeypatch.setattr(mutagen_sync, "SHIM_DIR", tmp_path / "shims")
    monkeypatch.setattr(mutagen_sync, "ENDPOINTS_PATH", tmp_path / "endpoints.json")

    mutagen_sync.write_shims([SSHEndpoint(host="a.example", user="one", port=2200)])
    mutagen_sync.write_shims([SSHEndpoint(host="b.example", user="two")])

    import json

    recorded = json.loads((tmp_path / "endpoints.json").read_text(encoding="utf-8"))
    # Both endpoints survive, because a second session must not evict the first session's connection options.
    assert {"one@a.example:2200", "two@b.example:22"} <= set(recorded)
    assert recorded["one@a.example:2200"]["scp"][recorded["one@a.example:2200"]["scp"].index("-P") + 1] == "2200"
    for tool in ("ssh", "scp"):
        shim = tmp_path / "shims" / tool
        assert shim.stat().st_mode & 0o111
        assert str(tmp_path / "endpoints.json") in shim.read_text(encoding="utf-8")


def test_two_pods_sharing_one_public_ip_keep_separate_options(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """RunPod direct-IP pods commonly differ only by port; a user@host key would silently cross-wire their sessions."""
    monkeypatch.setattr(mutagen_sync, "DATA_DIR", tmp_path)
    monkeypatch.setattr(mutagen_sync, "SHIM_DIR", tmp_path / "shims")
    monkeypatch.setattr(mutagen_sync, "ENDPOINTS_PATH", tmp_path / "endpoints.json")

    mutagen_sync.write_shims([SSHEndpoint(host="1.2.3.4", user="root", port=40001, key_path="/keys/a")])
    mutagen_sync.write_shims([SSHEndpoint(host="1.2.3.4", user="root", port=40002, key_path="/keys/b")])

    import json

    recorded = json.loads((tmp_path / "endpoints.json").read_text(encoding="utf-8"))
    assert "/keys/a" in " ".join(recorded["root@1.2.3.4:40001"]["ssh"])
    assert "/keys/b" in " ".join(recorded["root@1.2.3.4:40002"]["ssh"])


def _run_shim(tmp_path: Path, tool: str, arguments: list[str]) -> list[str]:
    """Run one generated wrapper with a stub 'real' binary and return the argv it would have exec'd."""
    from unittest import mock

    fake_real = tmp_path / f"real-{tool}"
    fake_real.write_text('#!/bin/sh\nfor argument in "$@"; do echo "$argument"; done\n', encoding="utf-8")
    fake_real.chmod(0o755)
    with mock.patch.object(mutagen_sync.shutil, "which", lambda name: str(fake_real)):
        source = mutagen_sync._shim_source(tool)
    shim = tmp_path / f"shim-{tool}"
    shim.write_text(source, encoding="utf-8")
    shim.chmod(0o755)
    return subprocess.run([str(shim), *arguments], check=True, capture_output=True, text=True).stdout.split()


def test_the_shim_selects_the_endpoint_matching_the_port_mutagen_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point of putting the port in the URL: it is the only thing that distinguishes these two endpoints."""
    monkeypatch.setattr(mutagen_sync, "DATA_DIR", tmp_path)
    monkeypatch.setattr(mutagen_sync, "SHIM_DIR", tmp_path / "shims")
    monkeypatch.setattr(mutagen_sync, "ENDPOINTS_PATH", tmp_path / "endpoints.json")
    mutagen_sync.write_shims(
        [SSHEndpoint(host="1.2.3.4", user="root", port=40001, key_path="/keys/a"), SSHEndpoint(host="1.2.3.4", user="root", port=40002, key_path="/keys/b")]
    )

    ssh_argv = _run_shim(tmp_path, "ssh", ["-p", "40001", "root@1.2.3.4", "echo", "hi"])
    scp_argv = _run_shim(tmp_path, "scp", ["-P", "40002", "/tmp/agent", "root@1.2.3.4:/tmp/agent"])

    assert "/keys/a" in ssh_argv and "/keys/b" not in ssh_argv
    assert "/keys/b" in scp_argv and "/keys/a" not in scp_argv


def test_the_shim_falls_back_to_the_bare_host_entry_when_no_port_flag_is_given(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A session created before fwd put the port in its URLs still calls ssh with no ``-p`` and must keep working."""
    monkeypatch.setattr(mutagen_sync, "DATA_DIR", tmp_path)
    monkeypatch.setattr(mutagen_sync, "SHIM_DIR", tmp_path / "shims")
    monkeypatch.setattr(mutagen_sync, "ENDPOINTS_PATH", tmp_path / "endpoints.json")
    mutagen_sync.write_shims([SSHEndpoint(host="gpu.example", user="sid", key_path="/keys/only")])

    assert "/keys/only" in _run_shim(tmp_path, "ssh", ["sid@gpu.example", "echo", "hi"])


def test_the_shim_leaves_a_destination_fwd_does_not_own_untouched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A Mutagen session the user created themselves must not acquire fwd's keys or ports."""
    monkeypatch.setattr(mutagen_sync, "DATA_DIR", tmp_path)
    monkeypatch.setattr(mutagen_sync, "SHIM_DIR", tmp_path / "shims")
    monkeypatch.setattr(mutagen_sync, "ENDPOINTS_PATH", tmp_path / "endpoints.json")
    mutagen_sync.write_shims([SSHEndpoint(host="gpu.example", user="sid", key_path="/keys/only")])

    assert _run_shim(tmp_path, "ssh", ["someone@elsewhere.example", "echo", "hi"]) == ["someone@elsewhere.example", "echo", "hi"]


def test_environment_isolates_fwds_daemon_from_a_user_owned_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """fwd's daemon carries fwd's ssh shims; adopting a user's daemon would use the wrong ssh options entirely."""
    monkeypatch.setattr(mutagen_sync, "DATA_DIR", Path("/fake/.fwd/mutagen"))
    monkeypatch.setattr(mutagen_sync, "SHIM_DIR", Path("/fake/.fwd/mutagen/shims"))

    environment = mutagen_sync.environment()

    assert environment["MUTAGEN_DATA_DIRECTORY"] == "/fake/.fwd/mutagen"
    assert environment["MUTAGEN_SSH_PATH"] == "/fake/.fwd/mutagen/shims"


@pytest.mark.parametrize(("given", "expected"), [("proj-a1b2c3", "proj-a1b2c3"), ("My_Project", "my-project"), ("_weird_", "weird"), ("9lives", "9lives"), ("", "fwd-session")])
def test_session_names_are_sanitized_for_mutagen(given: str, expected: str) -> None:
    """Mutagen rejects names outside ``[a-z0-9-]``; degrading beats surfacing a provider validation error."""
    assert mutagen_sync.session_name(given) == expected


def test_proxy_transports_are_reported_as_unsupported() -> None:
    """Mutagen installs its agent over scp, which the RunPod SSH proxy cannot run — the same limit as rsync."""
    assert mutagen_sync.supports_continuous(SSHEndpoint(host="h", user="u")) is True
    assert mutagen_sync.supports_continuous(SSHEndpoint(host="h", user="u", supports_rsync=False)) is False


def test_status_payload_surfaces_conflicts_and_problems() -> None:
    """Two-way-safe stops at conflicts rather than resolving them, so status is the only place a user learns of one."""
    entry = mutagen_sync._status_from_payload(
        {
            "name": "fwd-demo",
            "identifier": "sync_abc",
            "status": "watching",
            "alpha": {"path": "/local", "connected": True},
            "beta": {"protocol": "ssh", "host": "gpu", "path": "/remote", "connected": False},
            "conflicts": [{"root": "a.txt"}, {"root": "b.txt"}],
        }
    )

    assert entry.conflicts == 2
    assert entry.beta == "gpu:/remote"
    assert "beta disconnected" in entry.problems
    assert entry.healthy is False


def test_healthy_session_reports_healthy_and_exposes_its_remote_side() -> None:
    entry = mutagen_sync._status_from_payload({"name": "fwd-demo", "status": "watching", "alpha": {"path": "/l", "connected": True}, "beta": {"host": "g", "path": "/r", "connected": True}})
    assert entry.healthy is True
    assert entry.beta_endpoint == mutagen_sync.BetaEndpoint(user="", host="g", port=0, path="/r")


# ------------------------------------------------------------------------------------------------ session lifecycle


def _payload(*, host: str, path: str, user: str = "sid", port: int = 2222, paused: bool = False) -> dict:
    """Build one Mutagen session JSON object with a given remote side, as ``sync list --template`` would report it."""
    return {
        "name": "fwd-demo",
        "identifier": "sync_abc",
        "status": "watching",
        "paused": paused,
        "alpha": {"path": "/local", "connected": True},
        "beta": {"protocol": "ssh", "user": user, "host": host, "port": port, "path": path, "connected": True},
    }


@pytest.fixture()
def recorded_mutagen(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Neutralize every side effect of :func:`ensure_session` except the ``mutagen`` argv it decides to run."""
    calls: list[list[str]] = []
    monkeypatch.setattr(mutagen_sync, "write_shims", lambda endpoints=(): None)
    monkeypatch.setattr(mutagen_sync, "ignore_patterns", lambda source, sync_cfg: ["*.log"])
    monkeypatch.setattr(mutagen_sync, "_run", lambda arguments, **kwargs: calls.append(list(arguments)) or subprocess.CompletedProcess([], 0, "", ""))
    return calls


def test_ensure_session_recreates_a_session_whose_target_has_moved(recorded_mutagen: list[list[str]], monkeypatch: pytest.MonkeyPatch) -> None:
    """A re-provisioned pod keeps its fwd session name but not its address; resuming by name would sync to a dead host."""
    monkeypatch.setattr(mutagen_sync, "status_all", lambda: [mutagen_sync._status_from_payload(_payload(host="1.2.3.4", path="/work/proj"))])
    endpoint = SSHEndpoint(host="5.6.7.8", user="sid", port=2222)

    mutagen_sync.ensure_session(endpoint, "/local", "/work/proj", "fwd-demo", SyncConfig())

    assert recorded_mutagen[0] == ["sync", "terminate", "fwd-demo"]
    assert recorded_mutagen[1][:2] == ["sync", "create"]
    assert recorded_mutagen[1][-1] == "sid@5.6.7.8:2222:/work/proj"


@pytest.mark.parametrize(
    ("payload_kwargs", "endpoint"),
    [
        ({"host": "1.2.3.4", "path": "/work/proj", "port": 40001}, SSHEndpoint(host="1.2.3.4", user="sid", port=40002)),
        ({"host": "1.2.3.4", "path": "/work/proj", "user": "root"}, SSHEndpoint(host="1.2.3.4", user="sid", port=2222)),
        ({"host": "1.2.3.4", "path": "/work/old"}, SSHEndpoint(host="1.2.3.4", user="sid", port=2222)),
    ],
    ids=["port moved", "user changed", "remote directory changed"],
)
def test_every_reported_difference_in_the_remote_side_forces_a_recreate(
    payload_kwargs: dict, endpoint: SSHEndpoint, recorded_mutagen: list[list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mutagen_sync, "status_all", lambda: [mutagen_sync._status_from_payload(_payload(**payload_kwargs))])

    mutagen_sync.ensure_session(endpoint, "/local", "/work/proj", "fwd-demo", SyncConfig())

    assert [call[:2] for call in recorded_mutagen] == [["sync", "terminate"], ["sync", "create"]]


def test_ensure_session_reuses_a_matching_session_without_a_second_status_probe(recorded_mutagen: list[list[str]], monkeypatch: pytest.MonkeyPatch) -> None:
    """The session is already where it should be, so neither Mutagen nor the daemon needs to be touched again."""
    probes = []
    payload = _payload(host="1.2.3.4", path="/work/proj")
    monkeypatch.setattr(mutagen_sync, "status_all", lambda: probes.append(1) or [mutagen_sync._status_from_payload(payload)])

    state = mutagen_sync.ensure_session(SSHEndpoint(host="1.2.3.4", user="sid", port=2222), "/local", "/work/proj", "fwd-demo", SyncConfig())

    assert recorded_mutagen == []
    assert len(probes) == 1
    assert state is not None and state.status == "watching"


def test_ensure_session_resumes_a_paused_session_that_still_points_at_the_target(recorded_mutagen: list[list[str]], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mutagen_sync, "status_all", lambda: [mutagen_sync._status_from_payload(_payload(host="1.2.3.4", path="/work/proj", paused=True))])

    mutagen_sync.ensure_session(SSHEndpoint(host="1.2.3.4", user="sid", port=2222), "/local", "/work/proj", "fwd-demo", SyncConfig())

    assert recorded_mutagen == [["sync", "resume", "fwd-demo"]]


def test_terminate_reports_an_absent_session_without_a_status_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every probe is a daemon round-trip during ``fwd stop``; Mutagen already distinguishes "no such session" itself."""
    calls: list[list[str]] = []

    def fake_run(arguments, **kwargs):
        calls.append(list(arguments))
        return subprocess.CompletedProcess([], 1, "", 'Error: unable to locate requested sessions: specification "fwd-demo" did not match any sessions')

    monkeypatch.setattr(mutagen_sync, "binary_path", lambda: "/usr/local/bin/mutagen")
    monkeypatch.setattr(mutagen_sync, "_run", fake_run)

    assert mutagen_sync.terminate("fwd-demo") is False
    assert calls == [["sync", "terminate", "fwd-demo"]]


def test_terminate_surfaces_a_real_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A daemon that is broken rather than empty must not be reported as "nothing was running"."""
    monkeypatch.setattr(mutagen_sync, "binary_path", lambda: "/usr/local/bin/mutagen")
    monkeypatch.setattr(mutagen_sync, "_run", lambda arguments, **kwargs: subprocess.CompletedProcess([], 1, "", "Error: unable to connect to daemon"))

    with pytest.raises(mutagen_sync.MutagenError):
        mutagen_sync.terminate("fwd-demo")


# ------------------------------------------------------------------------------------ preference persistence and stop


def _session(**flags) -> object:
    """Build the smallest session-shaped object ``synccmd.stop_session`` reads."""
    from fwd.state import SessionState

    return SessionState(name="proj", backend="ssh", local_cwd="/local/proj", remote_dir="/work/proj", tmux_session="fwd-proj", endpoint={}, flags=dict(flags))


def test_stop_never_talks_to_mutagen_for_a_session_that_never_used_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """``mutagen sync list`` starts a persistent daemon, so a stop/rm must not probe on behalf of a non-user."""
    from fwd.ops import synccmd

    monkeypatch.setattr(mutagen_sync, "terminate", lambda name: pytest.fail("teardown must not reach Mutagen without a recorded session"))

    assert synccmd.stop_session(_session(target="box")) is False


def test_sync_off_still_terminates_by_explicit_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """``fwd sync off`` is a direct instruction to stop syncing, so it cleans up even if the flag was lost."""
    from fwd.ops import synccmd

    terminated: list[str] = []
    monkeypatch.setattr(mutagen_sync, "terminate", lambda name: bool(terminated.append(name)) or True)

    assert synccmd.stop_session(_session(target="box"), force=True) is True
    assert terminated == ["proj"]


def _write_config(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_toggling_a_project_declared_target_never_writes_a_backendless_table_into_the_global_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The reproduced failure: a ``[targets.box]`` with no backend in ~/.fwd/config.toml breaks fwd in every project."""
    from fwd import config as config_mod
    from fwd.ops import synccmd

    global_path = tmp_path / "global" / "config.toml"
    project_dir = tmp_path / "proj"
    _write_config(global_path, 'default_target = "box"\n')
    _write_config(project_dir / ".fwd" / "config.toml", '[targets.box]\nbackend = "ssh"\nhost = "gpu.example"\n')
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_PATH", global_path)

    synccmd._persist("box", True, project_dir)

    assert "targets" not in global_path.read_text(encoding="utf-8")
    assert "continuous_sync = true" in (project_dir / ".fwd" / "config.toml").read_text(encoding="utf-8")
    # The whole severity of the bug: an unrelated directory must still be able to load config at all.
    assert config_mod.load_config(tmp_path / "elsewhere").sync.continuous is False


def test_toggling_a_globally_declared_target_writes_the_override_beside_its_declaration(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The override has to live where the target does, or it would be a second, backend-less declaration of it."""
    from fwd import config as config_mod
    from fwd.ops import synccmd

    global_path = tmp_path / "global" / "config.toml"
    project_dir = tmp_path / "proj"
    _write_config(global_path, '[targets.box]\nbackend = "ssh"\nhost = "gpu.example"\n')
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_PATH", global_path)

    synccmd._persist("box", True, project_dir)

    assert "continuous_sync = true" in global_path.read_text(encoding="utf-8")
    assert not (project_dir / ".fwd" / "config.toml").exists()
    assert config_mod.load_config(project_dir).continuous_sync_for("box") is True


def test_an_undeclared_target_falls_back_to_the_project_scoped_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``runpod``, ``user@host``, and ssh aliases have no table to extend; materializing one would half-declare them."""
    from fwd import config as config_mod
    from fwd.ops import synccmd

    global_path = tmp_path / "global" / "config.toml"
    project_dir = tmp_path / "proj"
    _write_config(global_path, "[sync]\ncontinuous = false\n")
    monkeypatch.setattr(config_mod, "GLOBAL_CONFIG_PATH", global_path)

    synccmd._persist("sid@gpu.example.com", True, project_dir)

    assert "targets" not in global_path.read_text(encoding="utf-8")
    assert config_mod.load_config(project_dir).sync.continuous is True


# ---------------------------------------------------------------------------------------------------- doctor wiring


def test_doctor_requires_mutagen_only_when_some_launch_path_would_use_it() -> None:
    """Doctor must ask ``continuous_sync_for`` rather than re-deriving precedence, or the two answers can disagree."""
    from fwd import doctor
    from fwd.config import SshTargetConfig

    def required(cfg: Config) -> bool:
        return any(result.name == "mutagen" for result in doctor._local_checks(cfg))

    assert required(Config()) is False
    assert required(Config(sync=SyncConfig(continuous=True))) is True
    assert required(Config(targets={"box": SshTargetConfig(name="box", host="h", continuous_sync=True)})) is True
    # A global default every declared target opts out of still applies to implicit targets, so it still needs Mutagen.
    assert required(Config(sync=SyncConfig(continuous=True), targets={"box": SshTargetConfig(name="box", host="h", continuous_sync=False)})) is True


# ------------------------------------------------------------------------------------------------------ tip throttle


def test_tip_shows_once_then_throttles_for_a_day(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A hint printed on every pull stops being a hint, so the interval is the whole point of the record."""
    monkeypatch.setattr(tips, "TIPS_PATH", tmp_path / "tips.json")
    now = datetime(2026, 1, 1, tzinfo=UTC)

    assert tips.should_show(tips.CONTINUOUS_SYNC, now=now) is True
    tips.mark_shown(tips.CONTINUOUS_SYNC, now=now)
    assert tips.should_show(tips.CONTINUOUS_SYNC, now=now + timedelta(hours=1)) is False
    assert tips.should_show(tips.CONTINUOUS_SYNC, now=now + timedelta(hours=25)) is True


def test_unreadable_tip_state_degrades_to_showing_the_tip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Losing throttle state costs one extra hint; raising from a hint would break the command the user ran."""
    path = tmp_path / "tips.json"
    path.write_text("{ not json", encoding="utf-8")
    monkeypatch.setattr(tips, "TIPS_PATH", path)

    assert tips.should_show(tips.CONTINUOUS_SYNC) is True


# ---------------------------------------------------------------------------------------------- CLI registration


@pytest.mark.parametrize("subcommand", ["on", "off", "status"])
def test_sync_subcommands_are_registered_with_help(subcommand: str) -> None:
    from fwd.cli import app

    result = CliRunner().invoke(app, ["sync", subcommand, "--help"])
    assert result.exit_code == 0, result.output
    assert "session" in result.output.lower()


def test_sync_help_documents_the_git_exclusion() -> None:
    """The one behaviour that differs from ``fwd push`` must be discoverable from ``--help``, not only from docs."""
    from fwd.cli import app

    result = CliRunner().invoke(app, ["sync", "--help"])
    assert result.exit_code == 0
    assert ".git" in result.output


def test_setup_exposes_the_continuous_sync_flag() -> None:
    """Every backend declares ``continuous_sync``, so the non-interactive setup surface must accept it."""
    from fwd.cli import app

    result = CliRunner().invoke(app, ["setup", "--help"])
    assert result.exit_code == 0
    assert "--continuous-sync" in result.output


def test_missing_mutagen_never_installs_a_package_without_a_human(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ui.confirm`` answers with its default off-TTY, so the interactive gate is what stops an unasked install."""
    import typer

    monkeypatch.setattr(mutagen_sync, "binary_path", lambda: None)
    monkeypatch.setattr(mutagen_sync.shutil, "which", lambda name: "/opt/homebrew/bin/brew" if name == "brew" else None)
    monkeypatch.setattr(mutagen_sync.ui, "interactive_terminal", lambda: False)
    monkeypatch.setattr(mutagen_sync.ui, "confirm", lambda *args, **kwargs: pytest.fail("a non-interactive run must never be asked to confirm"))
    installs: list[object] = []
    monkeypatch.setattr(mutagen_sync.subprocess, "run", lambda *args, **kwargs: installs.append(args))

    with pytest.raises(typer.Exit):
        mutagen_sync.ensure_installed()
    assert installs == []
