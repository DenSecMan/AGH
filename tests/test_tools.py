"""The starter tools for code work."""
import asyncio
import shutil
import subprocess
import sys

import pytest
from langchain_core.messages import HumanMessage

from agh.engine import Engine
from agh.llm import ScriptedChatModel
from agh.tooling import load_workspace_tools


def call(name, **args):
    return {"tool_calls": [{"name": name, "args": args}]}


@pytest.fixture
async def kit(ws):
    engine = Engine(ws, "assistant", model=ScriptedChatModel(responses=["ok"]))
    await engine.refresh()
    tools = load_workspace_tools(ws)

    async def use(name, **args):
        return await tools[name].invoke(args, engine.ctx())

    yield use, engine
    engine.close()


@pytest.mark.parametrize("ripgrep", [True, False])
async def test_find_and_search_skip_ignored_dirs(ws, kit, monkeypatch, ripgrep):
    use, _ = kit
    if not ripgrep:
        monkeypatch.setattr(shutil, "which", lambda _: None)
    elif not shutil.which("rg"):
        pytest.skip("ripgrep not installed")
    (ws.root / "src" / "pkg").mkdir(parents=True)
    (ws.root / "src" / "pkg" / "mod.py").write_text("def needle():\n    pass\n")
    (ws.root / "node_modules").mkdir()
    (ws.root / "node_modules" / "junk.py").write_text("needle = 1\n")
    found = await use("find_files", pattern="*.py", path="src")
    assert "src/pkg/mod.py" in found and "junk" not in found
    hits = await use("search_files", pattern="needle")
    assert "src/pkg/mod.py:1:" in hits and "junk" not in hits
    assert "mod.py" in await use("search_files", pattern="NEEDLE", ignore_case=True, path="src")
    assert "src/pkg/mod.py:1:" in await use("search_files", pattern="needle", path="src/pkg/mod.py")  # a file path


async def test_search_files_leaves_git_ignored_files_alone(ws, kit):
    use, _ = kit
    if not (shutil.which("rg") and shutil.which("git")):
        pytest.skip("needs ripgrep and git")
    subprocess.run(["git", "init", "-q"], cwd=ws.root, check=True)
    (ws.root / ".gitignore").write_text("dist/\n*.log\n")
    (ws.root / "dist").mkdir()
    for name in ("dist/bundle.js", "app.log", "kept.js", "kept.py"):
        (ws.root / name).write_text("needle\n")
    assert sorted((await use("search_files", pattern="needle")).splitlines()) == ["kept.js:1:needle", "kept.py:1:needle"]
    assert await use("search_files", pattern="needle", glob="*.js") == "kept.js:1:needle"
    assert await use("search_files", pattern="needle", glob="*.{js,log}") == "kept.js:1:needle"
    assert "app.log:1:needle" in await use("search_files", pattern="needle", path="app.log")  # named outright


async def test_edits_keep_a_files_line_endings(ws, kit):
    use, _ = kit
    unix, windows, mixed = ws.root / "unix.txt", ws.root / "windows.txt", ws.root / "mixed.txt"
    unix.write_bytes(b"one\ntwo\nthree\n")
    windows.write_bytes(b"one\r\ntwo\r\nthree\r\n")
    await use("edit_file", path="unix.txt", old="two", new="2")
    assert unix.read_bytes() == b"one\n2\nthree\n"
    await use("edit_file", path="windows.txt", old="one\ntwo", new="1\n2")  # the agent sees and writes "\n"
    assert windows.read_bytes() == b"1\r\n2\r\nthree\r\n"
    await use("write_file", path="windows.txt", content="a\nb\n")
    assert windows.read_bytes() == b"a\r\nb\r\n"
    await use("write_file", path="unix.txt", content="a\nb\n")
    assert unix.read_bytes() == b"a\nb\n"
    await use("write_file", path="fresh.txt", content="a\nb\n")  # a new file is written as given
    assert (ws.root / "fresh.txt").read_bytes() == b"a\nb\n"
    mixed.write_bytes(b"a\r\nb\r\nc\nd\r\n")  # mostly CRLF, so that is what it gets
    await use("edit_file", path="mixed.txt", old="c", new="C")
    assert mixed.read_bytes() == b"a\r\nb\r\nC\r\nd\r\n"


async def test_edit_file_replace_all_multi_edit_and_staleness(ws, kit):
    use, _ = kit
    f = ws.root / "a.txt"
    f.write_text("x x y\n")
    with pytest.raises(ValueError, match="found 2"):
        await use("edit_file", path="a.txt", old="x", new="z")
    assert "2 replacements" in await use("edit_file", path="a.txt", old="x", new="z", replace_all=True)
    await use("edit_file", path="a.txt", edits=[{"old": "z z", "new": "1"}, {"old": "y", "new": "2"}])
    assert f.read_text() == "1 2\n"
    with pytest.raises(ValueError, match="edit 2"):  # all or nothing
        await use("edit_file", path="a.txt", edits=[{"old": "1", "new": "9"}, {"old": "missing", "new": "_"}])
    assert f.read_text() == "1 2\n"
    await use("read_file", path="a.txt")
    import os
    import time
    os.utime(f, ns=(time.time_ns(), time.time_ns() + 10_000_000_000))  # someone else changes it
    with pytest.raises(ValueError, match="changed since you last read it"):
        await use("edit_file", path="a.txt", old="1", new="3")
    await use("read_file", path="a.txt")
    await use("edit_file", path="a.txt", old="1", new="3")
    assert f.read_text() == "3 2\n"


async def test_read_file_pages_a_long_file_by_lines(ws, kit):
    use, _ = kit
    (ws.root / "long.txt").write_text("\n".join(f"line {i} " + "x" * 90 for i in range(1, 501)))
    first = await use("read_file", path="long.txt")
    assert len(first) < 17_000 and first.startswith("1\tline 1 ")
    last_shown = int(first.splitlines()[-2].split("\t")[0])
    assert first.endswith(f"[{500 - last_shown} more lines; continue with start_line={last_shown + 1}]")
    rest = await use("read_file", path="long.txt", start_line=last_shown + 1)
    assert rest.startswith(f"{last_shown + 1}\tline {last_shown + 1} ")
    assert "more lines" not in await use("read_file", path="long.txt", start_line=450)


async def test_background_process_output_and_stop(ws, kit):
    use, engine = kit
    script = ws.root / "loop.py"
    script.write_text("import time\nfor i in range(600):\n    print('tick', i, flush=True)\n    time.sleep(0.1)\n")
    started = await use("shell", command=f'"{sys.executable}" loop.py', background=True)
    assert "p1" in started
    for _ in range(50):
        if "tick 2" in await use("process", action="output", id="p1"):
            break
        await asyncio.sleep(0.1)
    assert "tick" in await use("process", action="output", id="p1")
    assert "running" in await use("process", action="list")
    await use("process", action="stop", id="p1")
    assert "exited" in await use("process", action="list")


async def test_closing_the_session_stops_background_processes(ws, kit):
    use, engine = kit
    script = ws.root / "sleep.py"
    script.write_text("import time\ntime.sleep(60)\n")
    await use("shell", command=f'"{sys.executable}" sleep.py', background=True)
    proc = engine.processes["p1"].proc
    engine.close()
    await asyncio.wait_for(proc.wait(), 10)
    assert proc.returncode is not None


async def test_git_tool_reads_but_refuses_changes(ws, kit):
    use, _ = kit
    if not shutil.which("git"):
        pytest.skip("git not installed")
    subprocess.run(["git", "init", "-q"], cwd=ws.root, check=True)
    assert "agh.yaml" in await use("git", subcommand="status", args=["--short"])
    for bad in [("commit", []), ("branch", ["-D", "main"]), ("diff", ["--output=x.txt"]),
                ("branch", ["--edit-description"])]:
        with pytest.raises(ValueError):
            await use("git", subcommand=bad[0], args=bad[1])


async def test_git_tool_cannot_read_files_outside_the_sandbox(ws, kit, tmp_path):
    use, _ = kit
    if not shutil.which("git"):
        pytest.skip("git not installed")
    subprocess.run(["git", "init", "-q"], cwd=ws.root, check=True)
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET\n")
    # `git diff` compares any two files when one is outside the repository, with or without --no-index.
    for args in (["--no-index", "--", "agh.yaml", str(secret)], ["--", "agh.yaml", str(secret)],
                 ["--", "agh.yaml", "../secret.txt"], ["--contents", str(secret), "agh.yaml"]):
        with pytest.raises(ValueError):
            await use("git", subcommand="diff" if "--contents" not in args else "blame", args=args)
    assert "TOP-SECRET" not in await use("git", subcommand="log", args=["--grep=/x", "main..HEAD"])  # not paths


async def test_view_image_reaches_the_model_as_a_user_image(ws):
    import base64
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
    (ws.root / "dot.png").write_bytes(png)
    engine = Engine(ws, "assistant", model=ScriptedChatModel(responses=[call("view_image", path="dot.png"), "seen"]))
    await engine.run("look")
    tool_msg = next(m for m in engine.session.messages if m.type == "tool")
    assert "image(s) attached" in tool_msg.content
    image_msg = next(m for m in engine.session.messages if isinstance(m, HumanMessage) and isinstance(m.content, list))
    assert any(b.get("type") == "image" for b in image_msg.content)
    engine.close()


async def test_shell_can_run_in_docker_when_configured(ws):
    from agh.sdk import shell_argv

    cmd = shell_argv("ls", {"sandbox": {"shell": "docker", "image": "alpine"}}, ws.root)
    assert cmd.startswith("docker run --rm") and "--network none" in cmd and "alpine" in cmd
    assert shell_argv("ls", {}, ws.root) == "ls"


def _language(ws, **checker):
    ws.config["language"] = {"checkers": {"toy": {"extensions": [".toy"], **checker}}}


async def test_writes_report_diagnostics_for_the_changed_file(ws, kit):
    use, _ = kit
    (ws.root / "check.py").write_text(
        "import sys\ntext = open(sys.argv[1]).read()\n"
        "if 'bad' in text:\n    print(sys.argv[1] + ':1: bad token')\n    sys.exit(1)\n")
    _language(ws, diagnostics=f'"{sys.executable}" check.py {{file}}')
    clean = await use("write_file", path="a.toy", content="good\n")
    assert "diagnostics" not in clean
    dirty = await use("edit_file", path="a.toy", old="good", new="bad")
    assert "diagnostics (toy) reported problems" in dirty and "a.toy:1: bad token" in dirty
    assert "bad token" in await use("diagnostics", paths=["a.toy"])


async def test_format_on_write_is_opt_in_and_keeps_edits_fresh(ws, kit):
    use, _ = kit
    (ws.root / "fmt.py").write_text(
        "import sys\np = sys.argv[1]\ns = open(p).read()\nopen(p, 'w').write(s.upper())\n")
    _language(ws, format=f'"{sys.executable}" fmt.py {{file}}')
    await use("write_file", path="b.toy", content="hi\n")
    assert (ws.root / "b.toy").read_text() == "hi\n"
    ws.config["language"]["format_on_write"] = True
    out = await use("write_file", path="b.toy", content="hi\n")
    assert (ws.root / "b.toy").read_text() == "HI\n" and "formatter rewrote" in out
    await use("edit_file", path="b.toy", old="HI", new="yo")  # no staleness error after the rewrite


def test_default_checkers_run_on_current_tools(ws):
    from agh.language import DEFAULT_CHECKERS, _render

    assert "--format" not in DEFAULT_CHECKERS["typescript"]["diagnostics"]  # ESLint 9 has no unix formatter
    nested = ws.root / "internal" / "pkg" / "a.go"
    assert _render("go vet {dir}", nested, ws.root) == "go vet ./internal/pkg"  # a folder, not a package name
    assert _render("go vet {dir}", ws.root / "main.go", ws.root) == "go vet ."


async def test_project_tools_are_found_only_when_switched_on(ws, kit):
    use, _ = kit
    bin_dir = ws.root / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    if sys.platform == "win32":
        (bin_dir / "toylint.cmd").write_text("@echo local linter ran on %1\n@exit /b 1\n")
    else:
        (bin_dir / "toylint").write_text("#!/bin/sh\necho \"local linter ran on $1\"\nexit 1\n")
        (bin_dir / "toylint").chmod(0o755)
    _language(ws, diagnostics="toylint {file}")
    assert await use("write_file", path="a.toy", content="x") == "wrote 1 characters to a.toy"
    assert "not checked: toylint is not installed" in await use("diagnostics", paths=["a.toy"])
    ws.config["language"]["project_tools"] = True
    assert "local linter ran" in await use("write_file", path="a.toy", content="y")


async def test_missing_checker_program_and_disabled_feedback_are_silent(ws, kit):
    use, _ = kit
    _language(ws, diagnostics="definitely-not-a-real-linter {file}")
    assert await use("write_file", path="c.toy", content="x") == "wrote 1 characters to c.toy"
    ws.config["language"] = False
    assert await use("write_file", path="d.py", content="x = (") == "wrote 5 characters to d.py"
