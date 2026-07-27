"""Pure parsing tests for the RunPod backend, pinned against real ``runpodctl`` output.

Design intent
-------------
Every fixture in ``tests/fixtures/runpod/`` is verbatim output from ``runpodctl 2.6.0`` captured during the Phase 0
S2 spike (see ``docs/runpod-notes.md``), with ssh public keys redacted. The point is regression detection: when
RunPod changes a field name, these tests fail loudly and locally instead of the failure surfacing as a mysterious
hang during a live provision.

Nothing here touches the network or the ``runpodctl`` binary — only the module-level parse functions are exercised,
which is exactly why they were factored out of :class:`~fwd.backends.runpod.RunpodBackend`.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from fwd.backends.base import TargetStatus
from fwd.backends.runpod import (
    CONTAINER_DISK_BASE,
    RunpodBackend,
    RunpodError,
    create_pod_args,
    create_summary,
    error_message,
    find_pod_by_name,
    is_missing_pod_error,
    parse_pod,
    parse_pod_list,
    parse_proxy_target,
    parse_ssh_info,
    pod_name_for,
    pod_status,
    port_is_open,
    resolve_paths,
)
from fwd.config import Config, ConfigError, RunpodTargetConfig, parse_target
from fwd.state import SessionState

FIXTURES = Path(__file__).parent / "fixtures" / "runpod"


def fixture(name: str) -> str:
    """Return a captured runpodctl output verbatim."""
    return (FIXTURES / name).read_text(encoding="utf-8")


class TestParsePod:
    """``pod get``/``create``/``start``/``stop`` all return the same single-pod document."""

    def test_running_pod(self) -> None:
        pod = parse_pod(fixture("pod-get-running.json"))
        assert pod["id"] == "nlom3h0kpps2y8"
        assert pod["name"] == "fwd-test-spike"
        assert pod["desiredStatus"] == "RUNNING"
        assert pod["volumeMountPath"] == "/workspace"

    def test_create_output_is_a_pod_document(self) -> None:
        pod = parse_pod(fixture("pod-create.json"))
        assert pod["id"] == "nlom3h0kpps2y8"
        assert pod["ports"] == ["22/tcp"]

    def test_start_output_is_a_pod_document(self) -> None:
        pod = parse_pod(fixture("pod-start.json"))
        assert pod["desiredStatus"] == "RUNNING"

    def test_missing_pod_raises_with_the_provider_message(self) -> None:
        # runpodctl prints an error object, then cobra usage text, then the error again; the first JSON value wins.
        with pytest.raises(RunpodError, match="pod not found"):
            parse_pod(fixture("pod-get-missing.json"))

    def test_non_pod_output_raises(self) -> None:
        with pytest.raises(RunpodError, match="unexpected"):
            parse_pod("not json at all")


class TestParsePodList:
    def test_empty_account(self) -> None:
        assert parse_pod_list(fixture("pod-list-empty.json")) == []

    def test_lists_running_and_exited(self) -> None:
        pods = parse_pod_list(fixture("pod-list.json"))
        assert [p["name"] for p in pods] == ["fwd-test-vol", "fwd-test-spike"]
        assert pods[1]["desiredStatus"] == "EXITED"

    def test_error_document_raises(self) -> None:
        with pytest.raises(RunpodError, match="unauthorized"):
            parse_pod_list('{"error":"unauthorized"}')


class TestFindPodByName:
    def test_exact_match_only(self) -> None:
        pods = parse_pod_list(fixture("pod-list.json"))
        assert find_pod_by_name(pods, "fwd-test-spike")["id"] == "nlom3h0kpps2y8"
        assert find_pod_by_name(pods, "fwd-test") is None
        assert find_pod_by_name(pods, "fwd-test-spike-2") is None

    def test_prefers_a_running_duplicate(self) -> None:
        # RunPod does not enforce unique names; a crashed launch can leave a stale stopped twin behind.
        pods = [
            {"id": "stale", "name": "fwd-dup", "desiredStatus": "EXITED"},
            {"id": "live", "name": "fwd-dup", "desiredStatus": "RUNNING"},
        ]
        assert find_pod_by_name(pods, "fwd-dup")["id"] == "live"

    def test_falls_back_to_the_first_when_none_running(self) -> None:
        pods = [{"id": "a", "name": "fwd-dup", "desiredStatus": "EXITED"}, {"id": "b", "name": "fwd-dup", "desiredStatus": "EXITED"}]
        assert find_pod_by_name(pods, "fwd-dup")["id"] == "a"


class TestParseSshInfo:
    def test_from_pod_get_nested_block(self) -> None:
        assert parse_ssh_info(parse_pod(fixture("pod-get-running.json"))) == ("216.243.220.199", 18876)

    def test_from_bare_ssh_info_document(self) -> None:
        assert parse_ssh_info(parse_pod(fixture("ssh-info.json"))) == ("216.243.220.199", 18876)

    def test_verbose_ssh_info_is_the_same_shape(self) -> None:
        assert parse_ssh_info(parse_pod(fixture("ssh-info-verbose.json"))) == ("216.243.220.199", 18876)

    def test_stopped_pod_has_no_address(self) -> None:
        # A stopped pod still carries an ssh block, but it holds {"error": "pod not ready"} instead of ip/port.
        assert parse_ssh_info(parse_pod(fixture("pod-get-stopped.json"))) is None

    def test_freshly_created_pod_has_no_ssh_block_yet(self) -> None:
        assert parse_ssh_info(parse_pod(fixture("pod-create.json"))) is None

    def test_proxy_only_document_has_no_direct_address(self) -> None:
        assert parse_ssh_info(parse_pod(fixture("ssh-info-proxy.json"))) is None


class TestParseProxyTarget:
    def test_extracts_the_proxy_login(self) -> None:
        assert parse_proxy_target(parse_pod(fixture("ssh-info-proxy.json"))) == "nlom3h0kpps2y8-644117d0@ssh.runpod.io"

    def test_direct_ssh_command_is_not_a_proxy(self) -> None:
        assert parse_proxy_target(parse_pod(fixture("pod-get-running.json"))) is None

    def test_missing_ssh_command(self) -> None:
        assert parse_proxy_target({"id": "x"}) is None


class TestPodStatus:
    def test_running_with_address(self) -> None:
        assert pod_status(parse_pod(fixture("pod-get-running.json"))) is TargetStatus.RUNNING

    def test_running_without_address_is_pending(self) -> None:
        # desiredStatus flips to RUNNING the instant the pod is provisioned, minutes before sshd answers.
        assert pod_status(parse_pod(fixture("pod-create.json"))) is TargetStatus.PENDING

    def test_exited_is_stopped(self) -> None:
        assert pod_status(parse_pod(fixture("pod-get-stopped.json"))) is TargetStatus.STOPPED

    @pytest.mark.parametrize(
        ("desired", "expected"),
        [
            ("TERMINATED", TargetStatus.GONE),
            ("DEAD", TargetStatus.GONE),
            ("PAUSED", TargetStatus.STOPPED),
            ("CREATED", TargetStatus.PENDING),
            ("", TargetStatus.PENDING),
        ],
    )
    def test_other_states(self, desired: str, expected: TargetStatus) -> None:
        assert pod_status({"id": "x", "desiredStatus": desired}) is expected


class TestErrorHelpers:
    def test_error_message_only_matches_top_level_errors(self) -> None:
        assert error_message({"error": "boom"}) == "boom"
        assert error_message({"ssh": {"error": "pod not ready"}}) is None
        assert error_message([{"error": "boom"}]) is None

    def test_missing_pod_detection(self) -> None:
        assert is_missing_pod_error('api error: {"error":"pod not found","status":404}')
        assert is_missing_pod_error("Pod Not Found")
        assert not is_missing_pod_error("connection refused")


def test_pod_name_is_prefixed() -> None:
    assert pod_name_for("myproj") == "fwd-myproj"


def runpod_target(**overrides: object) -> RunpodTargetConfig:
    """Build a RunpodTargetConfig with the fields these tests care about, defaults elsewhere."""
    return RunpodTargetConfig(name="pod", **overrides)  # type: ignore[arg-type]


class TestCreatePodArgs:
    """The ``pod create`` flag matrix, checked against the flags in the captured ``pod-create-help.txt`` fixture."""

    def test_every_flag_emitted_is_real(self) -> None:
        # Guards against inventing a flag: each --flag we emit must appear in runpodctl's own help output.
        help_text = fixture("pod-create-help.txt")
        emitted = {a for a in create_pod_args(runpod_target(), "fwd-x") if a.startswith("--")}
        assert emitted, "no flags emitted"
        for flag in emitted:
            assert flag in help_text, f"{flag} is not a real 'pod create' flag"

    def test_cpu_secure_defaults(self) -> None:
        args = create_pod_args(runpod_target(), "fwd-x")
        assert args[:2] == ["pod", "create"]
        assert args[args.index("--compute-type") + 1] == "CPU"
        assert args[args.index("--cloud-type") + 1] == "SECURE"
        assert "--gpu-id" not in args
        assert args[args.index("--image") + 1] == "runpod/base:0.6.2-cpu"
        assert args[args.index("--ports") + 1] == "22/tcp"
        assert args[args.index("--name") + 1] == "fwd-x"

    def test_community_cloud_is_upper_cased(self) -> None:
        args = create_pod_args(runpod_target(cloud_type="community"), "fwd-x")
        assert args[args.index("--cloud-type") + 1] == "COMMUNITY"

    def test_cpu_pod_omits_gpu_flags_entirely(self) -> None:
        # --gpu-id is meaningless on a CPU pod and passing it yields an opaque scheduling failure.
        args = create_pod_args(runpod_target(compute_type="cpu"), "fwd-x")
        assert args[args.index("--compute-type") + 1] == "CPU"
        assert "--gpu-id" not in args
        assert not any("RTX" in a for a in args)

    def test_cpu_pod_ignores_an_explicit_gpu_override(self) -> None:
        args = create_pod_args(runpod_target(compute_type="cpu"), "fwd-x", gpu="NVIDIA A40")
        assert "--gpu-id" not in args

    def test_gpu_override_wins_over_config(self) -> None:
        args = create_pod_args(runpod_target(compute_type="gpu", gpu="NVIDIA RTX A4000"), "fwd-x", gpu="NVIDIA A40")
        assert args[args.index("--gpu-id") + 1] == "NVIDIA A40"

    def test_volume_flags_always_present(self) -> None:
        args = create_pod_args(runpod_target(volume_gb=20, volume_mount_path="/data"), "fwd-x")
        assert args[args.index("--volume-in-gb") + 1] == "20"
        assert args[args.index("--volume-mount-path") + 1] == "/data"


class TestResolvePaths:
    def test_with_a_volume_everything_stays_put(self) -> None:
        remote_dir, tool_prefix, scratch, notes = resolve_paths(runpod_target(), "myproj", has_volume=True)
        assert remote_dir == "/workspace/myproj"
        assert tool_prefix == "/workspace/.fwd-tools"
        assert scratch == "/workspace/.fwd-cache"
        assert notes == []

    def test_without_a_volume_paths_move_to_the_container_disk_and_warn(self) -> None:
        remote_dir, tool_prefix, scratch, notes = resolve_paths(runpod_target(), "myproj", has_volume=False)
        assert remote_dir == f"{CONTAINER_DISK_BASE}/workspace/myproj"
        assert tool_prefix == f"{CONTAINER_DISK_BASE}/workspace/.fwd-tools"
        assert scratch == f"{CONTAINER_DISK_BASE}/workspace/.fwd-cache"
        assert len(notes) == 1
        assert "WIPED" in notes[0] and "no persistent volume" in notes[0]
        # R2-3: the mount usually *does* exist as a writable container-disk directory, so the note must say the
        # volume is missing rather than the path. A user who checks and finds the directory there would otherwise
        # reasonably conclude fwd is confused.
        assert "does not exist" not in notes[0]
        assert "not backed by one" in notes[0]

    def test_paths_already_off_the_volume_are_left_alone(self) -> None:
        # The user never relied on the volume here, so relocating them would be the surprising behaviour.
        cfg = runpod_target(remote_base="/opt/work", tool_prefix="/opt/work/tools")
        remote_dir, tool_prefix, _, notes = resolve_paths(cfg, "myproj", has_volume=False)
        assert remote_dir == "/opt/work/myproj"
        assert tool_prefix == "/opt/work/tools"
        assert notes == []

    def test_trailing_slashes_do_not_double_up(self) -> None:
        cfg = runpod_target(remote_base="/workspace/", volume_mount_path="/workspace/")
        remote_dir, _, scratch, _ = resolve_paths(cfg, "myproj", has_volume=True)
        assert remote_dir == "/workspace/myproj"
        assert scratch == "/workspace/.fwd-cache"


class TestRunpodConfigFields:
    """``compute_type``/``cloud_type`` parsing and validation, so a typo fails at load rather than mid-launch."""

    def test_defaults(self) -> None:
        cfg = parse_target("pod", {"backend": "runpod"})
        assert (cfg.compute_type, cfg.cloud_type) == ("cpu", "secure")
        assert cfg.image == "runpod/base:0.6.2-cpu"

    def test_parsed_from_config_table(self) -> None:
        cfg = parse_target("pod", {"backend": "runpod", "compute_type": "cpu", "cloud_type": "community"})
        assert (cfg.compute_type, cfg.cloud_type) == ("cpu", "community")

    def test_case_and_whitespace_are_normalized(self) -> None:
        cfg = parse_target("pod", {"backend": "runpod", "compute_type": " CPU ", "cloud_type": "Community"})
        assert (cfg.compute_type, cfg.cloud_type) == ("cpu", "community")

    def test_invalid_compute_type_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="compute_type"):
            parse_target("pod", {"backend": "runpod", "compute_type": "tpu"})

    def test_invalid_cloud_type_is_rejected(self) -> None:
        with pytest.raises(ConfigError, match="cloud_type"):
            parse_target("pod", {"backend": "runpod", "cloud_type": "hybrid"})

    def test_backend_metadata_closes_enums_but_keeps_gpu_and_image_extensible(self) -> None:
        parameters = {parameter.name: parameter for parameter in RunpodBackend.config_parameters()}
        assert [choice.value for choice in parameters["compute_type"].choices] == ["cpu", "gpu"]
        assert parameters["compute_type"].allow_free_text is False
        assert [choice.value for choice in parameters["cloud_type"].choices] == ["secure", "community"]
        assert parameters["cloud_type"].allow_free_text is False
        assert parameters["gpu"].allow_free_text is True
        assert parameters["image"].allow_free_text is True


class TestCreateSummary:
    """The progress label must describe only what is actually sent (docs/live-e2e-report.md, R2-4)."""

    def test_gpu_pod_names_the_gpu_and_volume(self) -> None:
        summary = create_summary(runpod_target(compute_type="gpu", gpu="NVIDIA RTX A4000", volume_gb=20))
        assert "NVIDIA RTX A4000" in summary
        assert "20 GB volume" in summary
        assert "secure cloud" in summary

    def test_cpu_pod_mentions_neither_a_gpu_nor_a_volume(self) -> None:
        summary = create_summary(runpod_target(compute_type="cpu", volume_gb=20))
        assert "CPU" in summary
        assert "RTX" not in summary and "NVIDIA" not in summary
        assert "GB volume" not in summary
        assert "container disk only" in summary

    def test_gpu_override_is_reflected(self) -> None:
        assert "NVIDIA A40" in create_summary(runpod_target(compute_type="gpu", gpu="NVIDIA RTX A4000"), "NVIDIA A40")

    def test_community_cloud_is_reflected(self) -> None:
        assert "community cloud" in create_summary(runpod_target(cloud_type="community"))


class TestStatusNeverConfusesUnreachableWithGone:
    """R2-1: only a confirmed 404 may become ``GONE``, because ``GONE`` unlocks deleting the user's session entry.

    A transient provider failure reported as ``GONE`` invites the user to prune the state of a pod that is still
    running and still billing — the exact hazard seen live right after a ``pod stop``.
    """

    def make_backend(self, stdout: str, *, returncode: int = 0) -> RunpodBackend:
        """Build a backend whose ``runpodctl`` calls always return the given canned output."""
        cfg = runpod_target()
        backend = RunpodBackend(cfg, Config(targets={"pod": cfg}))

        def fake_run_ctl(args: list[str], *, check: bool = True, timeout: float = 120.0) -> str:
            if returncode != 0 and check:
                raise RunpodError(f"runpodctl {' '.join(args)} failed: {stdout}")
            return stdout

        backend._run_ctl = fake_run_ctl  # type: ignore[method-assign]
        return backend

    def session(self) -> SessionState:
        return SessionState(
            name="s",
            backend="runpod",
            local_cwd="/tmp/p",
            remote_dir="/workspace/p",
            tmux_session="fwd-s",
            endpoint={},
            backend_ids={"pod_id": "nlom3h0kpps2y8"},
        )

    def test_confirmed_404_is_gone(self) -> None:
        # The real "pod was deleted" response, verbatim from the CLI.
        backend = self.make_backend(fixture("pod-get-missing.json"))
        assert backend.status(self.session()) is TargetStatus.GONE

    def test_transient_api_error_is_unknown_not_gone(self) -> None:
        backend = self.make_backend('{"error":"api error: 502 Bad Gateway (status 502)"}', returncode=1)
        assert backend.status(self.session()) is TargetStatus.UNKNOWN

    def test_network_failure_is_unknown_not_gone(self) -> None:
        backend = self.make_backend('{"error":"dial tcp: lookup api.runpod.io: no such host"}', returncode=1)
        assert backend.status(self.session()) is TargetStatus.UNKNOWN

    def test_unauthorized_is_unknown_not_gone(self) -> None:
        # A revoked key must not read as "your pod is gone" — the pod is very much alive and billing.
        backend = self.make_backend('{"error":"unauthorized"}', returncode=1)
        assert backend.status(self.session()) is TargetStatus.UNKNOWN

    def test_garbage_output_is_unknown_not_gone(self) -> None:
        backend = self.make_backend("<html>502 Bad Gateway</html>")
        assert backend.status(self.session()) is TargetStatus.UNKNOWN

    def test_missing_pod_id_in_state_is_unknown_not_gone(self) -> None:
        backend = self.make_backend(fixture("pod-get-running.json"))
        session = self.session()
        session.backend_ids = {}
        assert backend.status(session) is TargetStatus.UNKNOWN

    def test_healthy_pod_still_reports_its_real_state(self) -> None:
        running = self.make_backend(fixture("pod-get-running.json"))
        assert running.status(self.session()) is TargetStatus.RUNNING
        stopped = self.make_backend(fixture("pod-get-stopped.json"))
        assert stopped.status(self.session()) is TargetStatus.STOPPED


class TestPortProbe:
    """The probe that distinguishes a live published port from the stale one ``pod get`` replays after a restart."""

    def test_open_port_is_detected(self) -> None:
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            assert port_is_open(*server.getsockname(), timeout=2.0)

    def test_closed_port_is_detected(self) -> None:
        # Bind then close to obtain a port number that is almost certainly unused, without hitting the network.
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            _, port = probe.getsockname()
        assert not port_is_open("127.0.0.1", port, timeout=2.0)
