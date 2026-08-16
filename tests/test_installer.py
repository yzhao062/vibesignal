"""Unit tests for the installer helpers (macOS and Windows).

Covers the pure-string helpers (AppleScript and plist generation on macOS;
PowerShell shortcut-script generation on Windows), the platform guards, and
the platform dispatch of the public install/uninstall functions. The
subprocess wrappers (`osacompile`, `launchctl bootstrap`, and the PowerShell
`WScript.Shell` .lnk writes) are integration paths that depend on OS system
tools; those are exercised manually via the CLI rather than in unit tests.
"""

import json
import shlex
import sys

import pytest

from vibesignal import installer


def test_check_darwin_refuses_non_darwin(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    with pytest.raises(SystemExit) as exc:
        installer._check_darwin()
    assert "linux" in str(exc.value)


def test_check_darwin_passes_on_darwin(monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    installer._check_darwin()  # must not raise


def test_applescript_source_quotes_paths_with_spaces():
    src = installer.applescript_source(["/Users/jane doe/bin/vibesignal"])
    # shlex.quote wraps the path in single quotes, which AppleScript embeds
    # verbatim inside its own double-quoted string literal. The `widget` arg
    # is appended without quoting since it has no special characters.
    assert "'/Users/jane doe/bin/vibesignal'" in src
    assert "widget" in src
    assert src.endswith(' > /dev/null 2>&1 &"\n')


def test_applescript_source_escapes_double_quotes():
    # A path with a literal double quote (rare, but possible) must be escaped
    # so it does not terminate the AppleScript string early.
    src = installer.applescript_source(['/tmp/odd"name/vibesignal'])
    assert '\\"' in src


def test_applescript_source_uses_module_form_when_no_script():
    src = installer.applescript_source(["/abs/python", "-m", "vibesignal"])
    assert "/abs/python -m vibesignal widget" in src


def test_plist_content_has_required_keys():
    plist = installer.plist_content(["/usr/local/bin/vibesignal"])
    for key in ("Label", "ProgramArguments", "RunAtLoad", "KeepAlive",
                "ProcessType", "StandardOutPath", "StandardErrorPath"):
        assert f"<key>{key}</key>" in plist
    assert "<string>io.github.yzhao062.vibesignal</string>" in plist
    assert "<true/>" in plist   # RunAtLoad
    assert "<false/>" in plist  # KeepAlive


def test_plist_content_args_expand_correctly():
    # Console-script form: single ProgramArguments string + "widget".
    plist = installer.plist_content(["/usr/local/bin/vibesignal"])
    assert "<string>/usr/local/bin/vibesignal</string>" in plist
    assert "<string>widget</string>" in plist
    # Module form: three strings + "widget".
    plist2 = installer.plist_content(["/abs/python", "-m", "vibesignal"])
    assert "<string>/abs/python</string>" in plist2
    assert "<string>-m</string>" in plist2
    assert "<string>vibesignal</string>" in plist2
    assert "<string>widget</string>" in plist2


def test_plist_content_escapes_xml_special_chars():
    # An ampersand in a path would otherwise break the plist parser; the
    # rendering must XML-escape it.
    plist = installer.plist_content(["/tmp/a&b/vibesignal"])
    assert "<string>/tmp/a&amp;b/vibesignal</string>" in plist
    assert "/tmp/a&b/" not in plist  # raw ampersand must not appear


def test_vibesignal_args_falls_back_to_module_form(monkeypatch, tmp_path):
    # No argv0 hint and no sibling script -> module form. Point sys.executable
    # at an isolated tmp dir so we know there is no sibling vibesignal.
    fake_python = tmp_path / "python"
    fake_python.write_text("")
    monkeypatch.setattr("sys.argv", ["/some/path/__main__.py"])
    monkeypatch.setattr("sys.executable", str(fake_python))
    args = installer.vibesignal_args()
    assert args == [str(fake_python), "-m", "vibesignal"]


def test_vibesignal_args_uses_argv0_when_console_script(
    monkeypatch, tmp_path
):
    # When sys.argv[0] points at a real executable named vibesignal,
    # use it -- this is the installed-console-script invocation case.
    script = tmp_path / "vibesignal"
    script.write_text("#!/usr/bin/env python\n")
    script.chmod(0o755)
    monkeypatch.setattr("sys.argv", [str(script), "install-autostart"])
    args = installer.vibesignal_args()
    assert args == [str(script)]


def test_vibesignal_args_uses_sibling_in_python_module_form(
    monkeypatch, tmp_path
):
    # `python -m vibesignal install-autostart` from a specific env:
    # sys.argv[0] is __main__.py; sys.executable's sibling is the right
    # script for the env. Pinning to the sibling avoids a stale PATH match.
    env_bin = tmp_path / "env" / "bin"
    env_bin.mkdir(parents=True)
    fake_python = env_bin / "python"
    fake_python.write_text("")
    sibling = env_bin / "vibesignal"
    sibling.write_text("#!/usr/bin/env python\n")
    sibling.chmod(0o755)
    monkeypatch.setattr("sys.argv", ["/some/path/__main__.py"])
    monkeypatch.setattr("sys.executable", str(fake_python))
    args = installer.vibesignal_args()
    assert args == [str(sibling)]


def test_vibesignal_args_handles_windows_exe_argv0(monkeypatch, tmp_path):
    # On Windows, pip ships the console script as `vibesignal.exe`; sys.argv[0]
    # therefore carries the .exe suffix. The resolver must accept both forms so
    # a future relaxation of `_check_darwin()` does not regress Windows.
    script = tmp_path / "vibesignal.exe"
    script.write_text("")  # mock exe; content is irrelevant to path-based resolution
    script.chmod(0o755)
    monkeypatch.setattr("sys.argv", [str(script), "install-launcher"])
    args = installer.vibesignal_args()
    assert args == [str(script)]


def test_vibesignal_args_finds_exe_sibling(monkeypatch, tmp_path):
    # `python -m vibesignal ...` from a Windows env: sys.executable is
    # python.exe and the sibling launcher is vibesignal.exe (NOT bare
    # vibesignal). The resolver must prefer the .exe sibling over module form.
    env_bin = tmp_path / "Scripts"
    env_bin.mkdir(parents=True)
    fake_python = env_bin / "python.exe"
    fake_python.write_text("")
    sibling = env_bin / "vibesignal.exe"
    sibling.write_text("")
    sibling.chmod(0o755)
    monkeypatch.setattr("sys.argv", ["/some/path/__main__.py"])
    monkeypatch.setattr("sys.executable", str(fake_python))
    args = installer.vibesignal_args()
    assert args == [str(sibling)]


def test_vibesignal_args_ignores_stale_path_lookup(
    monkeypatch, tmp_path
):
    # The previous implementation called shutil.which, which would find an
    # older env's console script when run via `python -m vibesignal ...`.
    # The new resolver must NOT consult PATH; monkeypatching shutil.which to
    # a stale entry must not affect the result.
    env_bin = tmp_path / "current-env" / "bin"
    env_bin.mkdir(parents=True)
    fake_python = env_bin / "python"
    fake_python.write_text("")
    stale = tmp_path / "stale-env" / "bin" / "vibesignal"
    stale.parent.mkdir(parents=True)
    stale.write_text("#!/usr/bin/env python\n")
    stale.chmod(0o755)
    monkeypatch.setattr("sys.argv", ["/some/path/__main__.py"])
    monkeypatch.setattr("sys.executable", str(fake_python))
    monkeypatch.setattr("shutil.which", lambda name: str(stale))
    args = installer.vibesignal_args()
    # Must NOT pick the stale PATH script; should fall through to module form
    # because the sibling next to fake_python does not exist.
    assert args == [str(fake_python), "-m", "vibesignal"]
    assert str(stale) not in args


# ----- Windows shortcut helpers (pure string + dispatch; .lnk creation is
# an integration path exercised via the CLI, like the macOS subprocess wrappers) -----

def test_ps_squote_wraps_and_doubles_quotes():
    assert installer._ps_squote("plain") == "'plain'"
    assert installer._ps_squote("a'b") == "'a''b'"


def test_windows_shortcut_ps1_has_folder_target_and_args():
    ps1 = installer._windows_shortcut_ps1(
        "Startup", r"C:\Py\pythonw.exe", "-m vibesignal widget", r"C:\Users\jane"
    )
    assert "GetFolderPath('Startup')" in ps1
    assert "CreateShortcut" in ps1
    assert r"$s.TargetPath = 'C:\Py\pythonw.exe'" in ps1
    assert "$s.Arguments = '-m vibesignal widget'" in ps1
    assert r"$s.WorkingDirectory = 'C:\Users\jane'" in ps1
    assert "VibeSignal.lnk" in ps1
    assert ps1.strip().endswith("Write-Output $lnk")


def test_windows_shortcut_ps1_escapes_single_quote_in_path():
    # A path with a single quote must be doubled so it cannot break the PS literal.
    ps1 = installer._windows_shortcut_ps1(
        "Desktop", r"C:\o'brien\pythonw.exe", "-m vibesignal widget", r"C:\o'brien"
    )
    assert "'C:\\o''brien\\pythonw.exe'" in ps1


def test_windows_shortcut_ps1_stages_on_ascii_path_then_moves():
    # WScript.Shell.Save() converts its destination through the ANSI codepage,
    # so it must never be pointed straight at the final folder: a localized or
    # OneDrive-redirected Desktop is not ANSI-encodable and the save throws.
    ps1 = installer._windows_shortcut_ps1(
        "Desktop", r"C:\Py\pythonw.exe", "-m vibesignal widget", r"C:\Users\jane"
    )
    assert "$s = $sh.CreateShortcut($stage)" in ps1
    assert "$sh.CreateShortcut($lnk)" not in ps1
    assert "Move-Item -LiteralPath $stage -Destination $lnk -Force" in ps1
    # The staging directory itself must be ANSI-encodable, which $env:TEMP is
    # not when the account name is non-ASCII; 8.3 short names always are.
    assert "ShortPath" in ps1
    # The resolved path travels back as UTF-8, matching _run_powershell.
    assert "[Console]::OutputEncoding = [Text.Encoding]::UTF8" in ps1


def test_windows_shortcut_ps1_cleans_up_stage_on_failure():
    ps1 = installer._windows_shortcut_ps1(
        "Startup", r"C:\Py\pythonw.exe", "-m vibesignal widget", r"C:\Users\jane"
    )
    assert "} finally {" in ps1
    assert "Remove-Item -LiteralPath $stage" in ps1
    # Creation and Save() must sit inside the cleanup boundary: a Save() that
    # fails after partially writing the stage would otherwise leak the file.
    assert ps1.index("try {") < ps1.index("$sh.CreateShortcut($stage)")
    assert ps1.index("$s.Save()") < ps1.index("} finally {")


def test_windows_shortcut_ps1_refuses_non_ascii_staging_path():
    # The 8.3 fallback is not guaranteed: Microsoft documents that a short name
    # may not exist and that the API can succeed by returning the long path
    # unchanged. Silently continuing would hand WScript.Shell exactly the path
    # this function exists to avoid, so the result is re-checked and the script
    # fails with an actionable message instead.
    ps1 = installer._windows_shortcut_ps1(
        "Desktop", r"C:\Py\pythonw.exe", "-m vibesignal widget", r"C:\Users\jane"
    )
    # Two guards: one after the catch, one on the resolved value.
    assert ps1.count("$stageDir -match '[^\\x00-\\x7F]'") == 2
    assert "could not find an ASCII staging path" in ps1
    # The ShortPath catch must report, not swallow. Exactly one silent catch
    # remains, the deliberate no-op on the OutputEncoding line.
    assert ps1.count("} catch { }") == 1
    assert "OutputEncoding = [Text.Encoding]::UTF8 } catch { }" in ps1
    assert "could not resolve an ASCII staging path" in ps1


def test_run_powershell_reports_stderr_on_failure(monkeypatch):
    # A bare CalledProcessError names only the temp script, which tells the
    # user nothing about what failed.
    class _Result:
        returncode = 1
        stdout = ""
        stderr = 'Unable to save shortcut "C:\\Users\\me\\OneDrive\\??\\x.lnk".'

    monkeypatch.setattr(installer.subprocess, "run", lambda *a, **k: _Result())
    with pytest.raises(RuntimeError) as exc:
        installer._run_powershell("Write-Output 'x'")
    assert "exit 1" in str(exc.value)
    assert "Unable to save shortcut" in str(exc.value)


def test_run_powershell_returns_trimmed_stdout(monkeypatch):
    class _Result:
        returncode = 0
        stdout = "  C:\\Users\\jane\\Desktop\\VibeSignal.lnk  \n"
        stderr = ""

    monkeypatch.setattr(installer.subprocess, "run", lambda *a, **k: _Result())
    assert (
        installer._run_powershell("Write-Output 'x'")
        == r"C:\Users\jane\Desktop\VibeSignal.lnk"
    )


@pytest.mark.skipif("sys.platform != 'win32'", reason="exercises real PowerShell")
def test_windows_shortcut_write_survives_non_ascii_destination(tmp_path):
    # End-to-end regression guard: write a real .lnk into a directory whose
    # name is not ANSI-encodable. Before staging was introduced this raised
    # FileNotFoundException with the name mangled to "?".
    dest = tmp_path / "桌面"
    dest.mkdir()
    script = _ps1_for_dir(dest, r"C:\Windows\System32\notepad.exe", "", str(tmp_path))
    out = installer._run_powershell(script)
    assert out == str(dest / "VibeSignal.lnk")
    assert (dest / "VibeSignal.lnk").is_file()


def _ps1_for_dir(directory, target, arguments, workdir):
    """The real shortcut script with a literal directory in place of a known folder.

    The marker is asserted before substitution on purpose. An unchecked replace
    turns into a silent no-op the moment production reformats that line, and the
    test would then run the real Desktop branch and overwrite a developer's
    actual VibeSignal.lnk before failing on its output assertion.
    """
    real = installer._windows_shortcut_ps1("Desktop", target, arguments, workdir)
    marker = "$dir = [Environment]::GetFolderPath('Desktop')"
    assert real.count(marker) == 1, (
        "shortcut script no longer contains exactly one Desktop folder lookup; "
        "update _ps1_for_dir rather than letting it silently target the real Desktop"
    )
    return real.replace(marker, f"$dir = {installer._ps_squote(str(directory))}", 1)


def test_windows_remove_ps1_targets_named_shortcut():
    ps1 = installer._windows_remove_ps1("Programs")
    assert "GetFolderPath('Programs')" in ps1
    assert "VibeSignal.lnk" in ps1
    assert "Remove-Item" in ps1
    assert "'removed'" in ps1 and "'absent'" in ps1


def test_windows_pythonw_prefers_sibling_pythonw(monkeypatch, tmp_path):
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    py = scripts / "python.exe"
    py.write_text("")
    pyw = scripts / "pythonw.exe"
    pyw.write_text("")
    monkeypatch.setattr("sys.executable", str(py))
    assert installer._windows_pythonw() == str(pyw)


def test_windows_pythonw_falls_back_to_executable(monkeypatch, tmp_path):
    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    py = scripts / "python.exe"
    py.write_text("")  # no pythonw.exe sibling
    monkeypatch.setattr("sys.executable", str(py))
    assert installer._windows_pythonw() == str(py)


def test_check_supported_refuses_linux(monkeypatch):
    monkeypatch.setattr("sys.platform", "linux")
    with pytest.raises(SystemExit) as exc:
        installer._check_supported()
    assert "linux" in str(exc.value)


def test_check_supported_passes_on_win32_and_darwin(monkeypatch):
    for plat in ("win32", "darwin"):
        monkeypatch.setattr("sys.platform", plat)
        installer._check_supported()  # must not raise


def test_install_launcher_dispatches_to_windows(monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    sentinel = object()
    monkeypatch.setattr(installer, "_windows_install_launcher", lambda: sentinel)
    assert installer.install_launcher() is sentinel


def test_install_autostart_dispatches_to_windows(monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    sentinel = object()
    monkeypatch.setattr(installer, "_windows_install_autostart", lambda launch_now=True: sentinel)
    assert installer.install_autostart() is sentinel


def test_uninstall_dispatches_to_windows(monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr(installer, "_windows_uninstall_launcher", lambda: True)
    monkeypatch.setattr(installer, "_windows_uninstall_autostart", lambda: False)
    assert installer.uninstall_launcher() is True
    assert installer.uninstall_autostart() is False


# ----- install-autostart launch_now (so CI can install the entry headlessly,
# without spawning the GUI widget on a runner with no usable display) -----

def test_windows_autostart_launch_now_controls_widget_start(monkeypatch):
    # The Startup .lnk is always written (PowerShell mocked here); the widget is
    # only started when launch_now is True.
    monkeypatch.setattr("sys.platform", "win32")
    monkeypatch.setattr(installer, "_run_powershell", lambda script: r"C:\Startup\VibeSignal.lnk")
    started = []
    monkeypatch.setattr(installer, "_windows_launch_widget", lambda: started.append(1))
    installer.install_autostart(launch_now=False)
    assert started == []        # --no-launch: widget not started
    installer.install_autostart(launch_now=True)
    assert started == [1]       # default: widget started now


def test_macos_autostart_launch_now_controls_bootstrap(monkeypatch, tmp_path):
    # The plist is always written; `launchctl bootstrap` (start now) only runs
    # when launch_now is True. Without it, login autostart still works because
    # launchd loads ~/Library/LaunchAgents at the next login.
    monkeypatch.setattr("sys.platform", "darwin")
    plist = tmp_path / "io.github.yzhao062.vibesignal.plist"
    monkeypatch.setattr(installer, "_launch_agents_dir", lambda: tmp_path)
    monkeypatch.setattr(installer, "_plist_path", lambda: plist)
    monkeypatch.setattr(installer, "_launchd_target", lambda: "gui/501")
    runs = []
    monkeypatch.setattr(installer.subprocess, "run", lambda cmd, **kw: runs.append(cmd))
    installer.install_autostart(launch_now=False)
    assert plist.exists()                                  # plist written
    assert not any("bootstrap" in cmd for cmd in runs)     # no start-now
    runs.clear()
    installer.install_autostart(launch_now=True)
    assert any("bootstrap" in cmd for cmd in runs)         # start-now


# -- Hook installers -------------------------------------------------------


def _all_commands(spec: dict) -> list[str]:
    return [h["command"]
            for entries in spec.values() for e in entries for h in e["hooks"]]


def test_agent_hooks_spec_claude_shape():
    spec = installer.agent_hooks_spec(["/env/bin/vibesignal"], "claude")
    assert set(spec) == {"UserPromptSubmit", "PostToolUse", "Notification",
                         "Stop", "StopFailure", "SessionEnd"}
    # PostToolUse fires on every tool (matcher "*").
    assert spec["PostToolUse"][0]["matcher"] == "*"
    # Notification is split into the two matchers, blocked vs done.
    by_matcher = {e["matcher"]: e["hooks"][0]["command"] for e in spec["Notification"]}
    assert set(by_matcher) == {"permission_prompt", "idle_prompt"}
    assert "--state blocked" in by_matcher["permission_prompt"]
    assert "--state done" in by_matcher["idle_prompt"]
    # Every command is absolute-path-pinned and agent-tagged.
    cmds = _all_commands(spec)
    assert all(c.startswith("/env/bin/vibesignal ") for c in cmds)
    assert all("--agent claude" in c for c in cmds)
    # SessionEnd uses the `end` subcommand, not `event`.
    assert "end --agent claude" in spec["SessionEnd"][0]["hooks"][0]["command"]


def test_agent_hooks_spec_codex_tag():
    spec = installer.agent_hooks_spec(["/env/bin/vibesignal"], "codex")
    assert all("--agent codex" in c for c in _all_commands(spec))


def test_hook_command_quotes_spaces(monkeypatch):
    monkeypatch.setattr("sys.platform", "darwin")
    cmd = installer._hook_command(
        ["/Users/jane doe/vibesignal"], ["event", "--state", "working"])
    assert "'/Users/jane doe/vibesignal'" in cmd
    assert cmd.endswith("event --state working")


def test_hook_command_codex_on_windows_uses_cmd_quoting(monkeypatch):
    # Codex runs hook commands through cmd.exe, where a single quote is an
    # ordinary character. A shlex-quoted Windows path therefore reaches cmd
    # verbatim and the hook dies with "The filename, directory name, or volume
    # label syntax is incorrect." -> exit 1.
    monkeypatch.setattr("sys.platform", "win32")
    cmd = installer._hook_command(
        [r"C:\Users\jane\envs\py312\python.exe", "-m", "vibesignal"],
        ["event", "--agent", "codex", "--state", "working", "--quiet"],
        "codex",
    )
    assert r'"C:\Users\jane\envs\py312\python.exe"' in cmd
    assert "'" not in cmd


def test_hook_command_claude_on_windows_stays_posix(monkeypatch):
    # Claude Code runs hooks through a POSIX shell, so POSIX quoting is the
    # correct grammar there. Sharing cmd's format would break a $ component.
    monkeypatch.setattr("sys.platform", "win32")
    cmd = installer._hook_command(
        [r"C:\Users\jane\python.exe"], ["event"], "claude")
    assert cmd.startswith("'")


def test_cmd_quote_leaves_plain_flag_bare():
    assert installer._cmd_quote("--quiet") == "--quiet"
    assert installer._cmd_quote("event") == "event"


def test_cmd_quote_quotes_backslash_for_the_stored_round_trip():
    # cmd itself would accept the bare path. The quoting is for the way back:
    # the stored command is re-parsed with POSIX shlex.split by
    # _hook_is_vibesignal, which would consume the separators.
    assert installer._cmd_quote(r"C:\dir\python.exe") == r'"C:\dir\python.exe"'
    assert shlex.split(installer._cmd_quote(r"C:\dir\python.exe")) == [
        r"C:\dir\python.exe"]


def test_cmd_quote_rejects_embedded_quote():
    # The CRT layer can be escaped, but cmd does not treat the preceding
    # backslash as an escape, so a later metacharacter escapes the quoted
    # region. Reject rather than claim support that does not hold.
    with pytest.raises(ValueError, match="break out of the quoted region"):
        installer._cmd_quote('a"b&c')


def test_cmd_quote_doubles_trailing_backslash_when_quoting():
    # Once quotes are required, the CRT reads \" as an escaped quote, so a path
    # ending in a separator would otherwise swallow its own closing quote.
    assert installer._cmd_quote("C:\\my dir\\") == '"C:\\my dir\\\\"'


def test_cmd_quote_quotes_empty_and_metacharacters():
    assert installer._cmd_quote("") == '""'
    assert installer._cmd_quote("a&b") == '"a&b"'


@pytest.mark.skipif("sys.platform != 'win32'", reason="Windows command persistence")
def test_codex_console_script_hooks_remain_idempotent_and_removable():
    """The stored Codex command must still be recognizable as ours.

    vibesignal_args() prefers the direct console-script form, so the pinned
    token is normally a bare `...\\Scripts\\vibesignal.exe`. If the stored
    command does not survive the POSIX shlex round trip in _hook_is_vibesignal,
    a reinstall silently duplicates every handler and uninstall cannot find any
    of them. Asserting the quote character alone does not catch that; this
    drives the real install/reinstall/remove lifecycle.
    """
    spec = installer.agent_hooks_spec([r"C:\env\Scripts\vibesignal.exe"], "codex")
    handler = spec["UserPromptSubmit"][0]["hooks"][0]
    assert installer._hook_is_vibesignal(handler)

    settings: dict = {}
    installer._merge_hooks(settings, spec)
    installer._merge_hooks(settings, spec)
    assert len(settings["hooks"]["UserPromptSubmit"]) == 1  # idempotent
    assert installer._strip_hooks(settings) is True          # removable
    assert "hooks" not in settings


def test_cmd_quote_rejects_percent():
    # cmd expands %NAME% even inside double quotes and %% works only in a batch
    # file, so this cannot be represented and must not be emitted silently.
    with pytest.raises(ValueError, match="percent"):
        installer._cmd_quote(r"C:\foo\%PATH%\python.exe")


@pytest.mark.skipif("sys.platform != 'win32'", reason="parses via real shells")
@pytest.mark.parametrize("path", [
    r"C:\Users\jane doe\envs\py312\python.exe",   # space
    r"\\server\share\python.exe",                  # UNC
    r"C:\foo\$bar\python.exe",                     # $ component
    r"C:\foo\bar\\",                               # trailing backslash
])
def test_hook_command_round_trips_through_each_real_shell(monkeypatch, path):
    """The generated string must survive the shell that will actually parse it.

    Asserting which quote character was chosen proves nothing: the original bug
    passed that kind of check while cmd still failed to launch. These run the
    token through both real parsers -- cmd.exe plus the CRT argv rules for the
    Codex path, and the POSIX shell for the Claude path -- and compare what the
    program actually received against what was asked for.
    """
    import subprocess as sp
    monkeypatch.setattr("sys.platform", "win32")
    echo_argv = "import sys;sys.stdout.write(sys.argv[1])"

    # Codex -> cmd.exe, then the CRT argv parser inside the launched program.
    line = " ".join([
        installer._cmd_quote(sys.executable),
        "-c", installer._cmd_quote(echo_argv),
        installer._cmd_quote(path),
    ])
    out = sp.run(line, shell=True, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert out.stdout == path

    # Claude -> POSIX shell. Parsed with shlex rather than executed through Git
    # Bash on purpose: MSYS rewrites Windows-looking arguments before the
    # program sees them, so a UNC or trailing-separator path comes back altered
    # and the test would be measuring the emulator's path translation instead of
    # the quoting. shlex.split is the exact inverse of shlex.quote, so this
    # checks the POSIX grammar itself.
    posix_line = installer._hook_command([path], [], "claude")
    assert shlex.split(posix_line) == [path]


def test_merge_hooks_preserves_and_is_idempotent():
    settings = {"hooks": {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "guard.py"}]}]}}
    spec = installer.agent_hooks_spec(["/env/bin/vibesignal"], "claude")
    installer._merge_hooks(settings, spec)
    # Existing foreign hook survived, our block landed.
    assert settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "guard.py"
    assert len(settings["hooks"]["Stop"]) == 1
    # Idempotent: merging again does not duplicate our entries.
    installer._merge_hooks(settings, spec)
    assert len(settings["hooks"]["Stop"]) == 1
    assert len(settings["hooks"]["Notification"]) == 2


def test_merge_hooks_keeps_foreign_entry_in_shared_event():
    # A user's own PostToolUse entry must coexist with ours.
    settings = {"hooks": {"PostToolUse": [
        {"matcher": "Write", "hooks": [{"type": "command", "command": "fmt.sh"}]}]}}
    installer._merge_hooks(
        settings, installer.agent_hooks_spec(["/env/bin/vibesignal"], "claude"))
    cmds = [h["command"] for e in settings["hooks"]["PostToolUse"] for h in e["hooks"]]
    assert "fmt.sh" in cmds
    assert any("vibesignal" in c for c in cmds)


def test_strip_hooks_removes_only_vibesignal():
    original = {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "guard.py"}]}]}
    settings = {"hooks": json.loads(json.dumps(original))}  # deep copy
    installer._merge_hooks(
        settings, installer.agent_hooks_spec(["/env/bin/vibesignal"], "claude"))
    assert installer._strip_hooks(settings) is True
    # Foreign hook intact; our keys gone entirely.
    assert settings["hooks"] == original
    # Nothing to remove the second time.
    assert installer._strip_hooks(settings) is False


def test_strip_hooks_drops_empty_hooks_object():
    settings = {"hooks": {}}
    installer._merge_hooks(
        settings, installer.agent_hooks_spec(["/env/bin/vibesignal"], "claude"))
    installer._strip_hooks(settings)
    assert "hooks" not in settings  # emptied object removed, no residue


def test_install_and_uninstall_hooks_roundtrip(monkeypatch, tmp_path):
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(json.dumps({"theme": "light", "hooks": {
        "SessionStart": [{"hooks": [{"type": "command", "command": "boot.py"}]}]}}))
    monkeypatch.setattr(installer, "claude_settings_path", lambda: settings_file)
    monkeypatch.setattr(installer, "vibesignal_args", lambda: ["/env/bin/vibesignal"])

    path = installer.install_hooks("claude")
    assert path == settings_file
    data = json.loads(settings_file.read_text())
    # Unrelated keys and the foreign hook are preserved.
    assert data["theme"] == "light"
    assert data["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "boot.py"
    # Our hooks are pinned to the absolute path.
    assert data["hooks"]["Stop"][0]["hooks"][0]["command"].startswith("/env/bin/vibesignal ")
    # Backup captured the pristine file.
    assert (tmp_path / "settings.json.bak-vibesignal").exists()

    assert installer.uninstall_hooks("claude") is True
    data2 = json.loads(settings_file.read_text())
    assert "Stop" not in data2["hooks"]
    assert data2["hooks"]["SessionStart"][0]["hooks"][0]["command"] == "boot.py"
    assert data2["theme"] == "light"


def test_install_hooks_creates_missing_file(monkeypatch, tmp_path):
    settings_file = tmp_path / "nested" / "settings.json"
    monkeypatch.setattr(installer, "claude_settings_path", lambda: settings_file)
    monkeypatch.setattr(installer, "vibesignal_args", lambda: ["/v/vibesignal"])
    installer.install_hooks("claude")
    data = json.loads(settings_file.read_text())
    assert set(data["hooks"]) >= {"UserPromptSubmit", "Stop", "SessionEnd"}


def test_install_hooks_codex_targets_codex_file(monkeypatch, tmp_path):
    codex_file = tmp_path / "hooks.json"
    monkeypatch.setattr(installer, "codex_hooks_path", lambda: codex_file)
    monkeypatch.setattr(installer, "vibesignal_args", lambda: ["/v/vibesignal"])
    installer.install_hooks("codex")
    data = json.loads(codex_file.read_text())
    assert "--agent codex" in data["hooks"]["Stop"][0]["hooks"][0]["command"]


def test_uninstall_hooks_missing_file_is_false(monkeypatch, tmp_path):
    monkeypatch.setattr(installer, "claude_settings_path", lambda: tmp_path / "nope.json")
    assert installer.uninstall_hooks("claude") is False


def test_load_settings_obj_raises_on_bad_json(tmp_path):
    bad = tmp_path / "settings.json"
    bad.write_text("{not json")
    with pytest.raises(SystemExit):
        installer._load_settings_obj(bad)


def test_unknown_agent_rejected():
    with pytest.raises(SystemExit):
        installer._agent_settings_path("gemini")


# -- Round 2 fixes: Codex schema, precise marker, settings-file safety --------


def test_agent_hooks_spec_codex_uses_permissionrequest():
    # Codex's approval/input event is PermissionRequest, NOT Claude's
    # Notification/permission_prompt, and Codex has no StopFailure/SessionEnd.
    spec = installer.agent_hooks_spec(["/env/bin/vibesignal"], "codex")
    assert "PermissionRequest" in spec
    assert "Notification" not in spec
    assert "--state blocked" in spec["PermissionRequest"][0]["hooks"][0]["command"]
    assert "StopFailure" not in spec and "SessionEnd" not in spec
    # Claude keeps the full Claude vocabulary and does NOT use PermissionRequest.
    claude = installer.agent_hooks_spec(["/env/bin/vibesignal"], "claude")
    assert "Notification" in claude and "SessionEnd" in claude
    assert "PermissionRequest" not in claude


def test_agent_hooks_spec_codex_is_quiet_claude_is_not():
    # Codex parses hook stdout as JSON, so every Codex command passes --quiet.
    codex = installer.agent_hooks_spec(["/env/bin/vibesignal"], "codex")
    assert all(c.endswith("--quiet") for c in _all_commands(codex))
    # Claude tolerates hook stdout, so it stays verbose (no --quiet).
    claude = installer.agent_hooks_spec(["/env/bin/vibesignal"], "claude")
    assert not any("--quiet" in c for c in _all_commands(claude))


def test_argv_is_vibesignal_shapes():
    assert installer._argv_is_vibesignal(["/env/bin/vibesignal", "event", "--agent", "claude"]) is True
    assert installer._argv_is_vibesignal(["/env/bin/vibesignal.exe", "end", "--agent", "codex"]) is True
    assert installer._argv_is_vibesignal(["/py/python", "-m", "vibesignal", "event", "--agent", "x"]) is True
    # Unrelated command that merely contains the word must NOT match.
    assert installer._argv_is_vibesignal(["/opt/vibesignal-notify/run.sh"]) is False
    # vibesignal invoked without event/end or without --agent is not one of ours.
    assert installer._argv_is_vibesignal(["/env/bin/vibesignal", "off"]) is False
    assert installer._argv_is_vibesignal(["/env/bin/vibesignal", "event", "--state", "working"]) is False


def test_entry_is_vibesignal_ignores_unrelated_substring():
    unrelated = {"hooks": [{"type": "command", "command": "/opt/vibesignal-notify/run.sh"}]}
    assert installer._entry_is_vibesignal(unrelated) is False
    ours = {"hooks": [{"type": "command",
                       "command": "/env/bin/vibesignal event --agent claude --state working"}]}
    assert installer._entry_is_vibesignal(ours) is True


def test_entry_is_vibesignal_tolerates_malformed_hooks():
    # Must not raise on null/int/str/None-in-list hook shapes (partial hand-edit).
    assert installer._entry_is_vibesignal({"hooks": None}) is False
    assert installer._entry_is_vibesignal({"hooks": 5}) is False
    assert installer._entry_is_vibesignal({"hooks": [None, 3, "x"]}) is False
    assert installer._entry_is_vibesignal("notadict") is False


def test_uninstall_keeps_unrelated_vibesignal_substring_hook():
    settings = {"hooks": {"PostToolUse": [
        {"matcher": "*", "hooks": [{"type": "command", "command": "/opt/vibesignal-notify/run.sh"}]}]}}
    installer._merge_hooks(settings, installer.agent_hooks_spec(["/env/bin/vibesignal"], "claude"))
    installer._strip_hooks(settings)
    cmds = [h["command"] for e in settings["hooks"].get("PostToolUse", []) for h in e["hooks"]]
    assert "/opt/vibesignal-notify/run.sh" in cmds  # user's hook survives uninstall


def test_write_settings_preserves_symlink_and_mode(tmp_path):
    import os
    import stat as stat_mod
    real = tmp_path / "real.json"
    real.write_text('{"a": 1}\n')
    os.chmod(real, 0o644)
    link = tmp_path / "link.json"
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this platform")
    installer._write_settings(link, {"b": 2})
    # Symlink intact (not replaced by a standalone regular file); target rewritten.
    assert link.is_symlink()
    assert os.path.realpath(link) == str(real)
    assert json.loads(real.read_text()) == {"b": 2}
    # 0644 mode preserved, not narrowed to mkstemp's 0600 -- POSIX only; Windows
    # has no Unix mode bits (files report 0o666 and chmod only toggles read-only).
    if os.name == "posix":
        assert stat_mod.S_IMODE(os.stat(real).st_mode) == 0o644


def test_load_settings_obj_accepts_bom(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text('﻿{"theme": "light"}', encoding="utf-8")
    assert installer._load_settings_obj(p) == {"theme": "light"}


def test_load_settings_obj_rejects_non_object_root(tmp_path):
    # A JSON array root must not be silently replaced with {} and overwritten.
    p = tmp_path / "settings.json"
    p.write_text("[1, 2, 3]")
    with pytest.raises(SystemExit):
        installer._load_settings_obj(p)


def test_argv_is_vibesignal_versioned_python_module_form():
    # A versioned interpreter basename running `-m vibesignal` must still be
    # recognized as ours, or uninstall leaves a stale hook / reinstall duplicates.
    assert installer._argv_is_vibesignal(
        ["/opt/homebrew/bin/python3.12", "-m", "vibesignal", "event", "--agent", "codex"]) is True
    assert installer._argv_is_vibesignal(
        ["/usr/bin/pypy3", "-m", "vibesignal", "end", "--agent", "claude"]) is True


def test_strip_hooks_keeps_foreign_sibling_in_same_entry():
    # One matcher-group entry holding BOTH a vibesignal command and a foreign one:
    # uninstall must drop only ours and keep the foreign handler.
    settings = {"hooks": {"Stop": [
        {"hooks": [
            {"type": "command", "command": "/env/bin/vibesignal event --agent claude --state done"},
            {"type": "command", "command": "/opt/foreign.sh"},
        ]}]}}
    assert installer._strip_hooks(settings) is True
    remaining = [h["command"] for e in settings["hooks"]["Stop"] for h in e["hooks"]]
    assert remaining == ["/opt/foreign.sh"]  # foreign sibling survived


def test_merge_hooks_keeps_foreign_sibling_in_same_entry():
    # Re-pin must not discard a foreign sibling sharing a matcher group with ours.
    settings = {"hooks": {"Stop": [
        {"hooks": [
            {"type": "command", "command": "/old/vibesignal event --agent claude --state done"},
            {"type": "command", "command": "/opt/foreign.sh"},
        ]}]}}
    installer._merge_hooks(settings, installer.agent_hooks_spec(["/new/vibesignal"], "claude"))
    stop_cmds = [h["command"] for e in settings["hooks"]["Stop"] for h in e["hooks"]]
    assert "/opt/foreign.sh" in stop_cmds                     # foreign kept
    assert any("/new/vibesignal" in c for c in stop_cmds)     # ours re-pinned
    assert not any("/old/vibesignal" in c for c in stop_cmds)  # old ours removed


def test_merge_hooks_converges_events_dropped_from_spec():
    # A prior install left codex hooks under events the CURRENT spec no longer
    # emits (Notification/SessionEnd, from before the schema fix). Re-install must
    # drop those stale vibesignal handlers, not just re-pin the current event set.
    settings = {"hooks": {
        "Notification": [{"matcher": "permission_prompt", "hooks": [
            {"type": "command", "command": "/old/vibesignal event --agent codex --state blocked"}]}],
        "SessionEnd": [{"hooks": [
            {"type": "command", "command": "/old/vibesignal end --agent codex"}]}],
        "PostToolUse": [{"matcher": "*", "hooks": [
            {"type": "command", "command": "/opt/foreign.sh"}]}],
    }}
    installer._merge_hooks(settings, installer.agent_hooks_spec(["/new/vibesignal"], "codex"))
    hooks = settings["hooks"]
    assert "Notification" not in hooks   # stale vibesignal-only event dropped
    assert "SessionEnd" not in hooks     # stale vibesignal-only event dropped
    posttool = [h["command"] for e in hooks["PostToolUse"] for h in e["hooks"]]
    assert "/opt/foreign.sh" in posttool  # foreign sibling preserved
    assert any(c.endswith("--quiet") and "/new/vibesignal" in c for c in posttool)
    assert "PermissionRequest" in hooks and "Stop" in hooks  # current spec present
