"""Integration checks driven by ``run_integration.sh`` against the docker-sshd container.

Design intent
-------------
These are the assertions that a unit test structurally cannot make: that our ssh argv actually authenticates, that
rsync's filter syntax is accepted by a real rsync, that a piped ``bash -s`` bootstrap produces a usable environment
file, and that tmux sessions survive the ssh connection that created them.

Run indirectly — ``bash tests/harness/docker-sshd/run_integration.sh`` sets ``HOME`` to a throwaway directory first,
because ``fwd.config``/``fwd.state``/``fwd.sshexec`` all resolve their paths from ``Path.home()`` at import time.
"""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path

from fwd.backends.ssh import SshHostBackend
from fwd.config import load_config
from fwd.remote import BOOTSTRAP_PATH, detect_dep_commands, tmux_attach_argv, tmux_exists, tmux_kill, tmux_new
from fwd.sshexec import SSHError
from fwd.sync import sync_down, sync_up, tar_down, tar_up

PROJECT_NAME = "harness-proj"
SESSION = "fwd-harness"

_failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    """Record one assertion, printing immediately so a hang is attributable to the last line printed."""
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {name}{(' — ' + detail) if detail else ''}", flush=True)
    if not condition:
        _failures.append(name)


def main() -> int:
    workdir = Path(os.environ["FWD_HARNESS_WORKDIR"])
    local_dir = workdir / "project"
    pull_dir = workdir / "pulled"

    config = load_config(local_dir)
    target = config.target("docker")
    backend = SshHostBackend(target, config)

    # -- provision -------------------------------------------------------------------------------------------
    info = backend.provision(SESSION, PROJECT_NAME)
    endpoint = info.endpoint
    check("provision returns absolute remote_dir", info.remote_dir.startswith("/home/dev/"), info.remote_dir)
    check("tool_prefix expanded remotely", bool(info.tool_prefix) and info.tool_prefix.startswith("/home/dev/"), str(info.tool_prefix))
    check("remote_dir created", endpoint.run(f"test -d {info.remote_dir}", check=False).returncode == 0)

    # -- sshexec ---------------------------------------------------------------------------------------------
    check("run captures stdout", endpoint.run("echo hello-fwd").stdout.strip() == "hello-fwd")
    check("run exports env", endpoint.run("printf %s \"$FWD_TEST\"", env={"FWD_TEST": "v a l"}).stdout == "v a l")
    check("run check=False tolerates failure", endpoint.run("exit 3", check=False).returncode == 3)
    try:
        endpoint.run("echo to-stderr >&2; exit 4")
        check("run raises SSHError on failure", False)
    except SSHError as exc:
        check("run raises SSHError with stderr", "to-stderr" in str(exc), str(exc))

    script = endpoint.run_script("echo script:$1:$FWD_SCRIPT_VAR", args=["arg1"], env={"FWD_SCRIPT_VAR": "ok"}, stream=False)
    check("run_script pipes over stdin", script.stdout.strip() == "script:arg1:ok", script.stdout.strip())

    endpoint.open_control_master()
    check("control master socket opened", endpoint.control_path().exists(), str(endpoint.control_path()))

    # -- sync ------------------------------------------------------------------------------------------------
    sync_up(endpoint, local_dir, info.remote_dir, config.sync)
    listing = endpoint.run(f"cd {info.remote_dir} && ls -A").stdout.split()
    check("sync_up transfers tracked files", "main.py" in listing, " ".join(listing))
    check("sync_up keeps .git", ".git" in listing, " ".join(listing))
    check("sync_up honours config excludes", "node_modules" not in listing, " ".join(listing))
    check("sync_up honours .gitignore", "ignored.log" not in listing, " ".join(listing))
    check("sync_up honours .fwdignore", "secret-fixture.bin" not in listing, " ".join(listing))

    # --delete makes the remote a mirror: a remote-only file must not survive a second push.
    endpoint.run(f"touch {info.remote_dir}/stale.txt")
    sync_up(endpoint, local_dir, info.remote_dir, config.sync)
    check("sync_up --delete removes remote-only files", endpoint.run(f"test -e {info.remote_dir}/stale.txt", check=False).returncode != 0)

    endpoint.run(f"echo remote-edit > {info.remote_dir}/from-remote.txt")
    pull_dir.mkdir(parents=True, exist_ok=True)
    sync_down(endpoint, info.remote_dir, pull_dir, sync_cfg=config.sync)
    check("sync_down roundtrips a remote edit", (pull_dir / "from-remote.txt").read_text().strip() == "remote-edit")
    check("sync_down brings back the original file", (pull_dir / "main.py").is_file())

    scoped = workdir / "pulled-scoped"
    sync_down(endpoint, info.remote_dir, scoped, paths=["from-remote.txt"])
    check("sync_down path-scoped pull", sorted(p.name for p in scoped.iterdir()) == ["from-remote.txt"])

    # -- tar fallback (the RunPod-proxy transport path) ------------------------------------------------------
    tar_endpoint = replace(endpoint, supports_rsync=False)
    tar_remote = f"{info.remote_dir}-tar"
    tar_up(tar_endpoint, local_dir, tar_remote, config.sync)
    tar_listing = endpoint.run(f"cd {tar_remote} && ls -A").stdout.split()
    check("tar_up transfers files", "main.py" in tar_listing, " ".join(tar_listing))
    check("tar_up honours excludes", "node_modules" not in tar_listing, " ".join(tar_listing))
    tar_pull = workdir / "pulled-tar"
    tar_down(tar_endpoint, tar_remote, tar_pull)
    check("tar_down roundtrips", (tar_pull / "main.py").is_file())

    # -- bootstrap -------------------------------------------------------------------------------------------
    minimal = os.environ.get("FWD_BOOTSTRAP_MINIMAL", "1")
    endpoint.run_script(
        BOOTSTRAP_PATH,
        env={
            "FWD_TOOL_PREFIX": info.tool_prefix or "",
            "FWD_REMOTE_DIR": info.remote_dir,
            "FWD_SCRATCH": info.scratch or "",
            "FWD_BOOTSTRAP_MINIMAL": minimal,
        },
        stream=True,
    )
    env_file = f"{info.tool_prefix}/fwd-env.sh"
    check("bootstrap wrote fwd-env.sh", endpoint.run(f"test -f {env_file}", check=False).returncode == 0, env_file)
    check("bootstrap wrote a version marker", endpoint.run(f"ls {info.tool_prefix}/.fwd-bootstrap-*", check=False).returncode == 0)
    check(
        "bootstrap hooked the login shell",
        endpoint.run("grep -l '# fwd environment' ~/.bashrc ~/.profile", check=False).returncode == 0,
    )
    check(
        "fwd-env.sh puts the tool bin dir on PATH",
        info.tool_prefix in endpoint.run(f". {env_file}; printf %s \"$PATH\"").stdout,
    )
    if minimal != "1":
        check("bootstrap retained tmux", endpoint.run(f". {env_file}; tmux -V", check=False).returncode == 0)

    # -- dependency detection (local inspection, asserted against the fixture) -------------------------------
    check("detect_dep_commands sees the fixture lockfile", detect_dep_commands(local_dir) == ["uv sync", "bash .fwd/setup.sh"], str(detect_dep_commands(local_dir)))

    # -- tmux ------------------------------------------------------------------------------------------------
    tmux_kill(endpoint, SESSION)
    check("tmux_exists false before creation", tmux_exists(endpoint, SESSION) is False)
    tmux_new(endpoint, SESSION, info.remote_dir, "sleep 300", env={"FWD_MARKER": "present"})
    check("tmux_exists true after tmux_new", tmux_exists(endpoint, SESSION) is True)
    check("tmux session survives its creating ssh connection", endpoint.run("tmux list-sessions", check=False).stdout.count(SESSION) == 1)
    argv = tmux_attach_argv(endpoint, SESSION)
    check("tmux_attach_argv is an ssh -t invocation", argv[0] == "ssh" and "-t" in argv)
    tmux_kill(endpoint, SESSION)
    check("tmux_exists false after tmux_kill", tmux_exists(endpoint, SESSION) is False)
    tmux_kill(endpoint, SESSION)
    check("tmux_kill is idempotent", True)

    # -- backend lifecycle -------------------------------------------------------------------------------------
    checks = backend.doctor()
    check("doctor reports all checks passing", all(c.ok for c in checks), "; ".join(f"{c.name}={c.ok}:{c.detail}" for c in checks))

    endpoint.close_control_master()
    check("control master closed", not endpoint.control_path().exists())

    print()
    if _failures:
        print(f"{len(_failures)} check(s) FAILED: {', '.join(_failures)}", file=sys.stderr)
        return 1
    print("all integration checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
