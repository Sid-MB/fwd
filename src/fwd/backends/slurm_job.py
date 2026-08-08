"""Slurm ``job.sh`` generation — pure, network-free string rendering.

Design intent
-------------
The single hardest thing about the Slurm backend is *quoting*, so it lives here alone, with no ssh and no state, and is
tested exhaustively. The generated script is what tmux runs on the login node, and it has to survive four nested
levels of interpretation:

1. ``endpoint.run()`` hands a command line to the remote login shell (which writes this file via a quoted heredoc, so
   the file content itself is never re-interpreted — that is deliberate).
2. ``bash job.sh`` interprets the file.
3. ``salloc`` receives argv and passes the tail (``srun --pty bash -lc <inner>``) through to the allocation.
4. ``bash -lc <inner>`` interprets the payload on the compute node.

Every hop is handled with :func:`shlex.quote`/:func:`shlex.join` rather than hand-written escaping. In particular the
payload (env setup + ``cd`` + the ``claude`` command) is quoted exactly once, as one argv element for ``bash -lc``, so a
``claude_cmd`` containing double quotes, single quotes or ``$`` reaches the compute node byte-for-byte.

Why the allocation is not submitted by ``provision``
----------------------------------------------------
``salloc ... srun --pty`` is an *interactive* allocation: whoever runs it owns the job's lifetime. We want that owner to
be the detached tmux session on the login node, so a dropped laptop connection cannot kill the job. Therefore
``provision`` only prepares the login node, and ``ops/launch.py`` starts the job by handing
:func:`render_tmux_command` to ``remote.tmux_new``.

Shape of the rendered script::

    #!/bin/bash
    set -euo pipefail
    if [ -f <tool_prefix>/fwd-env.sh ]; then . <tool_prefix>/fwd-env.sh; fi
    exec salloc <alloc> -J fwd-<session> [--partition=..] [--account=..] \\
        srun --pty bash -lc '<env_setup joined with &&>; cd <remote_dir>; <claude_cmd>'

``fwd-env.sh`` is written by ``bootstrap.sh`` (owned by the bootstrap teammate) and exports the cache redirections that
keep inode-hungry caches off ``$HOME``; see ``dev-docs/slurm-notes.md`` for the exact variable contract. It is sourced on
the login node *before* ``salloc`` because Slurm propagates the submitting environment into the allocation, and it is
sourced again inside the payload for clusters configured with ``--export=NONE``.
"""

from __future__ import annotations

import shlex

from fwd import ui
from fwd.config import SlurmTargetConfig

# Job- and tmux-session names share this prefix so `squeue -u $USER` output is self-explaining and `scancel -n` can
# find a job even when the recorded job id was lost.
JOB_NAME_PREFIX = "fwd-"

# Relative to remote_dir. Kept inside the project tree (not /tmp) so it lives on the shared filesystem every login and
# compute node can read, and so `fwd rm` removes it along with everything else.
JOB_SCRIPT_RELPATH = ".fwd/job.sh"

# Written by bootstrap.sh under tool_prefix; see dev-docs/slurm-notes.md "fwd-env.sh contract".
ENV_FILE_NAME = "fwd-env.sh"


def job_name(session_name: str) -> str:
    """Return the Slurm job name for a session (``fwd-<session_name>``).

    Slurm truncates ``%j`` output at 24 characters in some ``squeue`` formats, so matching is always done on the exact
    string we asked for rather than a prefix test.
    """
    return f"{JOB_NAME_PREFIX}{session_name}"


def job_script_path(remote_dir: str) -> str:
    """Return the absolute remote path of the generated ``job.sh`` for a project directory."""
    return f"{remote_dir.rstrip('/')}/{JOB_SCRIPT_RELPATH}"


def env_file_path(tool_prefix: str) -> str:
    """Return the absolute remote path of ``fwd-env.sh`` under a tool prefix."""
    return f"{tool_prefix.rstrip('/')}/{ENV_FILE_NAME}"


def effective_alloc(alloc: str, gpu: str | None = None) -> list[str]:
    """Split the configured ``alloc`` template into argv, appending a ``--gres`` request for ``gpu`` if needed.

    ``--gpu`` on the command line is a convenience over editing config, so it must not fight a template that already
    asks for GPUs: when ``alloc`` mentions ``--gres``/``--gpus`` we leave it alone. A bare count (``--gpu 2``) becomes
    ``--gres=gpu:2``; anything else is treated as a type (``--gpu a100`` → ``--gres=gpu:a100:1``), matching how users
    talk about cluster GPUs.

    Args:
        alloc: Raw ``alloc`` string from config, parsed with shell rules so quoted values survive.
        gpu: Optional ``--gpu`` override.

    Returns:
        argv tokens for the ``salloc`` line.
    """
    argv = shlex.split(alloc)
    if not gpu:
        return argv
    if any(tok.startswith(("--gres", "--gpus", "-G")) for tok in argv):
        return argv
    spec = f"gpu:{gpu}" if gpu.isdigit() else f"gpu:{gpu}:1"
    return [*argv, f"--gres={spec}"]


def render_payload(cfg: SlurmTargetConfig, remote_dir: str, tool_prefix: str, claude_cmd: str) -> str:
    """Render the compute-node payload handed to ``bash -lc`` as a single (unquoted) shell string.

    Order matters: ``env_setup`` lines (``module load ...``) are joined with ``&&`` so a failed module load aborts
    before ``claude`` starts with a broken toolchain, while the top-level statements are joined with ``;`` because the
    ``fwd-env.sh`` re-source is best-effort and must not abort the launch.
    """
    source_env = f"if [ -f {shlex.quote(env_file_path(tool_prefix))} ]; then . {shlex.quote(env_file_path(tool_prefix))}; fi"
    parts = [source_env]
    if cfg.env_setup:
        parts.append(" && ".join(line.strip() for line in cfg.env_setup if line.strip()))
    parts.append(f"cd {shlex.quote(remote_dir)}")
    parts.append(claude_cmd)
    return "; ".join(part for part in parts if part)


def render_salloc_argv(
    cfg: SlurmTargetConfig,
    session_name: str,
    remote_dir: str,
    tool_prefix: str,
    claude_cmd: str,
    *,
    gpu: str | None = None,
) -> list[str]:
    """Render the full ``salloc`` argv, including the ``srun --pty bash -lc <payload>`` tail.

    Exposed separately from :func:`render_job_script` because argv is far easier to assert on in tests than a rendered
    script, and because the quoting bug class we care about is entirely inside this list.
    """
    argv = ["salloc", *effective_alloc(cfg.alloc, gpu), "-J", job_name(session_name)]
    if cfg.partition:
        argv += [f"--partition={cfg.partition}"]
    if cfg.account:
        argv += [f"--account={cfg.account}"]
    argv += ["srun", "--pty", "bash", "-lc", render_payload(cfg, remote_dir, tool_prefix, claude_cmd)]
    return argv


def render_job_script(
    cfg: SlurmTargetConfig,
    session_name: str,
    remote_dir: str,
    tool_prefix: str,
    claude_cmd: str,
    *,
    gpu: str | None = None,
) -> str:
    """Render the complete ``job.sh`` that tmux runs on the login node.

    ``exec`` replaces the shell with ``salloc`` so the tmux pane's single process *is* the allocation: killing the
    tmux session cancels the job, and ``scancel`` collapses the pane, with no orphaned wrapper shell in between.

    Args:
        cfg: Target config supplying ``alloc``, ``env_setup``, ``partition``, ``account``.
        session_name: fwd session name; becomes the ``-J fwd-<name>`` job name used by ``squeue``/``scancel``.
        remote_dir: Absolute project directory on the shared filesystem; the payload ``cd``s here.
        tool_prefix: Where bootstrap installed tooling; ``fwd-env.sh`` is sourced from it.
        claude_cmd: The command to run inside the allocation, e.g. ``claude --resume abc123``. Passed through
            verbatim (quoted as one argv element), so callers may include their own quoting.
        gpu: Optional ``--gpu`` override, see :func:`effective_alloc`.

    Returns:
        Script text ending in a newline, safe to write with a quoted heredoc.
    """
    salloc = shlex.join(render_salloc_argv(cfg, session_name, remote_dir, tool_prefix, claude_cmd, gpu=gpu))
    env_file = shlex.quote(env_file_path(tool_prefix))
    lines = [
        "#!/bin/bash",
        f"# Generated by {ui.command()} -- do not edit; regenerated on every launch.",
        "set -euo pipefail",
        "",
        "# Cache/venv redirections written by bootstrap.sh; Slurm propagates them into the allocation.",
        f"if [ -f {env_file} ]; then",
        f"  . {env_file}",
        "fi",
        "",
        f"exec {salloc}",
        "",
    ]
    return "\n".join(lines)


def render_tmux_command(remote_dir: str) -> str:
    """Return the command ``ops/launch.py`` should hand to :func:`fwd.remote.tmux_new`.

    Slurm is the one backend where tmux does *not* run ``claude`` directly: it runs this script, which allocates a
    compute node and starts ``claude`` inside it. Keeping the indirection in a file (rather than a giant inline command)
    means the user can read ``.fwd/job.sh``, tweak it, and relaunch by hand when a cluster does something exotic.
    """
    return f"bash {shlex.quote(job_script_path(remote_dir))}"
