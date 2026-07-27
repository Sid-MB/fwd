"""Tests for durable command/agent task state and unified ``fwd send`` routing."""

from __future__ import annotations

import json
import subprocess
from io import BytesIO
from types import SimpleNamespace

from typer.testing import CliRunner

from fwd import agents, cli_completion, remote_tasks, task_stream
from fwd.backends.base import TargetStatus
from fwd.cli import app
from fwd.ops import send as send_ops
from fwd.send_tasks import SendTask, SendTaskStore


class FakeEndpoint:
    """Small endpoint double that records remote shell commands."""

    def __init__(self, outputs: list[str] | None = None) -> None:
        self.commands: list[str] = []
        self.outputs = list(outputs or [])

    def run(self, command: str, **kwargs):
        del kwargs
        self.commands.append(command)
        stdout = self.outputs.pop(0) if self.outputs else ""
        return subprocess.CompletedProcess(["ssh"], 0, stdout=stdout, stderr="")

    def popen(self, command: str, **kwargs):
        self.commands.append(command)
        return SimpleNamespace(command=command, kwargs=kwargs)


def test_task_store_round_trip_and_active_semantics(tmp_path) -> None:
    store = SendTaskStore(tmp_path / "tasks.json")
    task = SendTask(id="cmd-a1", session="demo", kind="command", command=["pytest", "-q"], label="pytest -q")
    store.upsert(task)

    assert store.get("cmd-a1").active is True
    assert store.all()[0].command == ["pytest", "-q"]
    store.update("cmd-a1", status="completed", exit_code=0)
    assert store.get("cmd-a1").active is False
    assert json.loads((tmp_path / "tasks.json").read_text())["tasks"]["cmd-a1"]["exit_code"] == 0


def test_agent_send_commands_resume_existing_conversations() -> None:
    claude = agents.AGENTS["claude"].send_command("fix it", {"resume_id": "session-123"})
    codex = agents.AGENTS["codex"].send_command("fix it", {})

    assert claude == ("claude", "--print", "--verbose", "--output-format", "stream-json", "--resume", "session-123", "fix it")
    assert codex == ("codex", "exec", "--json", "resume", "--last", "fix it")


def test_remote_task_start_uses_manager_window_logs_and_queue_marker() -> None:
    endpoint = FakeEndpoint()
    task = SendTask(
        id="agt-a1",
        session="demo",
        kind="agent",
        agent="codex",
        command=["codex", "exec", "--json", "resume", "--last", "continue"],
        label="continue",
        status="queued",
        depends_on="agt-old",
    )

    remote_tasks.start(endpoint, "demo", "/workspace/project", task)

    assert len(endpoint.commands) == 2
    assert "tmux new-session" in endpoint.commands[0]
    assert "tmux new-window" in endpoint.commands[1]
    assert "agt-old/exit" in endpoint.commands[1]
    assert 'printf "queued\\n"' in endpoint.commands[1]
    assert 'export FWD_TASK_DIR="$task_dir"' in endpoint.commands[1]
    assert "/output" in endpoint.commands[1]


def test_remote_task_status_recognizes_completion_queue_and_running() -> None:
    task = SendTask(id="cmd-a1", session="demo", kind="command", command=["true"], label="true")
    completed = FakeEndpoint(["done 0\n"])
    queued = FakeEndpoint(["queued"])
    running = FakeEndpoint(["running"])

    assert remote_tasks.status(completed, task) == ("completed", 0)
    assert remote_tasks.status(queued, task) == ("queued", None)
    assert remote_tasks.status(running, task) == ("running", None)


def test_remote_task_manager_kill_is_best_effort() -> None:
    endpoint = FakeEndpoint()
    remote_tasks.kill_manager(endpoint, "demo")
    assert "tmux kill-session" in endpoint.commands[0]
    assert "fwd-tasks-demo" in endpoint.commands[0]
    assert endpoint.commands[0].endswith("|| true")


def test_remote_task_follower_fails_instead_of_hanging_when_window_disappears() -> None:
    endpoint = FakeEndpoint()
    task = SendTask(id="cmd-a1", session="demo", kind="command", command=["sleep", "10"], label="sleep 10")

    process = remote_tasks.follow_process(endpoint, task)

    assert "list-windows" in process.command
    assert "cmd-a1" in process.command
    assert 'printf "1\\n" > "$task_dir/exit"' in process.command


def _send_world(monkeypatch, tmp_path, *, initial_command=("codex",)):
    task_store = SendTaskStore(tmp_path / "tasks.json")
    endpoint = FakeEndpoint()
    session = SimpleNamespace(name="demo", remote_dir="/workspace/project", tmux_session="fwd-demo", flags={"initial_command": list(initial_command)})
    backend = SimpleNamespace(endpoint=lambda value: endpoint)
    monkeypatch.setattr(send_ops, "store", lambda: task_store)
    monkeypatch.setattr(send_ops.launch_ops, "resolve_session", lambda name: session)
    monkeypatch.setattr(send_ops.launch_ops, "backend_for", lambda value: backend)
    monkeypatch.setattr(send_ops.launch_ops, "status_of", lambda owner, value: TargetStatus.RUNNING)
    return task_store, endpoint, session


def test_detached_command_is_registered_and_started(monkeypatch, tmp_path) -> None:
    task_store, endpoint, _ = _send_world(monkeypatch, tmp_path)
    started: list[SendTask] = []
    monkeypatch.setattr(send_ops.remote_tasks, "start", lambda ep, session, directory, task: started.append(task))

    assert send_ops.dispatch(("pytest", "-q"), detach=True) == 0
    task = task_store.all()[0]
    assert task.kind == "command"
    assert task.label == "pytest -q"
    assert started == [task]
    assert endpoint is not None


def test_command_stop_after_is_armed_remotely_before_returning(monkeypatch, tmp_path) -> None:
    task_store, _, _ = _send_world(monkeypatch, tmp_path)
    started: list[SendTask] = []
    monkeypatch.setattr(send_ops, "_prepare_stop_after", lambda session, endpoint: "/tools/stop-after/demo/action")
    monkeypatch.setattr(send_ops.remote_tasks, "start", lambda endpoint, session, directory, task: started.append(task))
    monkeypatch.setattr(send_ops.remote_tasks, "status", lambda endpoint, task: (task.status, None))

    assert send_ops.dispatch(("pytest", "-q"), detach=True, stop_after=True) == 0

    stop, command = started
    assert command.kind == "command"
    assert stop.kind == "stopafter"
    assert stop.command == ["/tools/stop-after/demo/action", "--foreground"]
    assert stop.dependencies == [command.id]
    assert {task.id for task in task_store.all()} == {command.id, stop.id}


def test_stopafter_command_queues_after_every_active_task(monkeypatch, tmp_path) -> None:
    task_store, _, _ = _send_world(monkeypatch, tmp_path)
    task_store.upsert(SendTask(id="cmd-one", session="demo", kind="command", command=["one"], label="one"))
    task_store.upsert(SendTask(id="agt-two", session="demo", kind="agent", agent="codex", command=["two"], label="two", status="queued"))
    started: list[SendTask] = []
    monkeypatch.setattr(send_ops, "_prepare_stop_after", lambda session, endpoint: "/tools/stop-after/demo/action")
    monkeypatch.setattr(send_ops.remote_tasks, "start", lambda endpoint, session, directory, task: started.append(task))
    monkeypatch.setattr(send_ops.remote_tasks, "status", lambda endpoint, task: (task.status, None))

    assert send_ops.dispatch(("stopafter",)) == 0

    assert started[0].kind == "stopafter"
    assert set(started[0].dependencies) == {"cmd-one", "agt-two"}


def test_cancel_stopafter_cancels_queued_task_and_remote_timer(monkeypatch, tmp_path) -> None:
    task_store, _, session = _send_world(monkeypatch, tmp_path)
    task_store.upsert(SendTask(id="stp-one", session="demo", kind="stopafter", command=["stop"], label="stop", status="queued"))
    stopped: list[str] = []
    canceled: list[str] = []
    monkeypatch.setattr(send_ops.remote_tasks, "status", lambda endpoint, task: ("queued", None))
    monkeypatch.setattr(send_ops.remote_tasks, "stop", lambda endpoint, task: stopped.append(task.id))
    monkeypatch.setattr(send_ops.stop_after_ops, "cancel", lambda endpoint, value: canceled.append(value.name) or True)

    assert send_ops.dispatch(("cancel", "stopafter")) == 0

    assert stopped == ["stp-one"]
    assert canceled == [session.name]
    assert task_store.get("stp-one").status == "canceled"


def test_cancel_without_subject_cancels_every_queued_task_but_not_running_work(monkeypatch, tmp_path) -> None:
    task_store, _, _ = _send_world(monkeypatch, tmp_path)
    task_store.upsert(SendTask(id="cmd-queued", session="demo", kind="command", command=["queued"], label="queued", status="queued"))
    task_store.upsert(SendTask(id="cmd-running", session="demo", kind="command", command=["running"], label="running"))
    stopped: list[str] = []
    monkeypatch.setattr(send_ops.remote_tasks, "status", lambda endpoint, task: (task.status, None))
    monkeypatch.setattr(send_ops.remote_tasks, "stop", lambda endpoint, task: stopped.append(task.id))

    assert send_ops.dispatch(("cancel",)) == 0

    assert stopped == ["cmd-queued"]
    assert task_store.get("cmd-queued").status == "canceled"
    assert task_store.get("cmd-running").status == "running"


def test_agent_immediate_cancels_active_turn_then_starts_replacement(monkeypatch, tmp_path) -> None:
    task_store, _, _ = _send_world(monkeypatch, tmp_path)
    active = SendTask(id="agt-old", session="demo", kind="agent", agent="codex", command=["old"], label="old")
    task_store.upsert(active)
    stopped: list[str] = []
    started: list[SendTask] = []
    monkeypatch.setattr(send_ops.remote_tasks, "status", lambda endpoint, task: ("running", None))
    monkeypatch.setattr(send_ops.remote_tasks, "stop", lambda endpoint, task: stopped.append(task.id))
    monkeypatch.setattr(send_ops.remote_tasks, "start", lambda endpoint, session, directory, task: started.append(task))

    assert send_ops.dispatch(("agent", "use", "another", "approach"), immediate=True, detach=True) == 0
    assert stopped == ["agt-old"]
    assert started[0].agent == "codex"
    assert started[0].label == "use another approach"
    assert started[0].command[-1] == "use another approach"
    assert task_store.get("agt-old").status == "canceled"


def test_agent_follow_up_is_queued_behind_active_turn(monkeypatch, tmp_path) -> None:
    task_store, _, _ = _send_world(monkeypatch, tmp_path)
    task_store.upsert(SendTask(id="agt-old", session="demo", kind="agent", agent="codex", command=["old"], label="old"))
    started: list[SendTask] = []
    monkeypatch.setattr(send_ops.remote_tasks, "status", lambda endpoint, task: ("running", None))
    monkeypatch.setattr(send_ops.remote_tasks, "start", lambda endpoint, session, directory, task: started.append(task))

    assert send_ops.dispatch(("agent", "follow", "up"), detach=True) == 0
    assert started[0].status == "queued"
    assert started[0].depends_on == "agt-old"


def test_agent_stop_without_managed_task_interrupts_original_tmux_agent(monkeypatch, tmp_path) -> None:
    _send_world(monkeypatch, tmp_path)
    interrupted: list[str] = []
    monkeypatch.setattr(send_ops.remote, "tmux_interrupt", lambda endpoint, tmux: interrupted.append(tmux))

    assert send_ops.dispatch(("agent",), stop=True) == 0
    assert interrupted == ["fwd-demo"]


def test_send_ls_has_structured_json_and_does_not_require_a_session(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(send_ops, "store", lambda: SendTaskStore(tmp_path / "tasks.json"))
    result = CliRunner().invoke(app, ["send", "--ls", "--format", "json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["title"] == "fwd send tasks (0 active)"
    assert payload["rows"] == []


def test_send_ls_json_exposes_stop_after_dependencies(monkeypatch, tmp_path, capsys) -> None:
    task_store = SendTaskStore(tmp_path / "tasks.json")
    task_store.upsert(
        SendTask(
            id="stp-one",
            session="demo",
            kind="stopafter",
            command=["stop"],
            label="stop session demo",
            status="completed",
            dependencies=["cmd-one", "agt-two"],
        )
    )
    monkeypatch.setattr(send_ops, "store", lambda: task_store)

    send_ops.list_tasks(output_format="json", include_all=True)

    payload = json.loads(capsys.readouterr().out)
    assert payload["rows"][0]["kind"] == "stopafter"
    assert payload["rows"][0]["after"] == "cmd-one, agt-two"


def test_cli_parses_agent_stop_message_and_detach_flags(monkeypatch) -> None:
    captured: list[tuple[tuple[str, ...], dict[str, object]]] = []
    monkeypatch.setattr(send_ops, "dispatch", lambda arguments, **kwargs: captured.append((arguments, kwargs)) or 0)

    result = CliRunner().invoke(app, ["send", "agent", "--stop", "try", "again", "--detach"])

    assert result.exit_code == 0, result.output
    assert captured[0][0] == ("agent", "try", "again")
    assert captured[0][1]["stop"] is True
    assert captured[0][1]["detach"] is True


def test_cli_passes_stop_after_for_new_send_task(monkeypatch) -> None:
    captured: list[tuple[tuple[str, ...], dict[str, object]]] = []
    monkeypatch.setattr(send_ops, "dispatch", lambda arguments, **kwargs: captured.append((arguments, kwargs)) or 0)

    result = CliRunner().invoke(app, ["send", "--stop-after", "--", "pytest", "-q"])

    assert result.exit_code == 0, result.output
    assert captured[0][0] == ("pytest", "-q")
    assert captured[0][1]["stop_after"] is True
    assert captured[0][1]["literal_command"] is True


def test_human_agent_output_renders_codex_tool_and_message(monkeypatch) -> None:
    task = SendTask(id="agt-a1", session="demo", kind="agent", agent="codex", command=["codex"], label="fix")
    decoder = task_stream.AgentOutput(task)
    decoder.human = True
    destination = BytesIO()

    decoder.feed(b'{"type":"turn.started"}\n{"type":"item.started","item":{"type":"command_execution","command":"pytest -q"}}\n', destination)
    decoder.feed(b'{"type":"item.completed","item":{"type":"agent_message","text":"Fixed it."}}\n', destination)

    assert destination.getvalue().decode() == "Working…\n→ pytest -q\nFixed it.\n"


def test_send_completion_includes_active_tasks_and_agent_selectors(monkeypatch, tmp_path) -> None:
    task_store = SendTaskStore(tmp_path / "tasks.json")
    task_store.upsert(SendTask(id="cmd-a1", session="demo", kind="command", command=["pytest"], label="pytest"))
    monkeypatch.setattr(cli_completion, "SendTaskStore", lambda: task_store)

    candidates = dict(cli_completion.complete_send_subject(None, [], ""))
    assert candidates["agent"] == "agent running in this fwd session"
    assert "cmd-a1" in candidates
    assert "codex" in candidates
