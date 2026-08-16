"""One-click launcher and login-autostart helpers for the widget.

macOS: compiles a small AppleScript ``.app`` into ``~/Applications/`` via the
stock ``osacompile`` and writes a LaunchAgent plist that re-launches the widget
at login. Windows: writes ``.lnk`` shortcuts through the stock PowerShell
``WScript.Shell`` COM object -- Start Menu + Desktop for an on-demand launcher,
and the Startup folder for login autostart. Neither path adds a package
dependency; both shell out to tools that ship with the OS.

The widget command is pinned to the absolute interpreter of the env that runs
the install command, so a re-install from a freshly switched env re-pins
cleanly. On Windows the shortcut runs ``pythonw -m vibesignal widget`` so there
is no console window. Linux has no single conventional autostart target, so the
helpers refuse there; ``vibesignal widget &`` plus a ``~/.config/autostart/``
``.desktop`` entry is the documented path.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import xml.sax.saxutils
from pathlib import Path

from . import lock

LAUNCH_AGENT_LABEL = "io.github.yzhao062.vibesignal"
APP_NAME = "VibeSignal.app"
SHORTCUT_NAME = "VibeSignal.lnk"

# Console-script filenames pip can produce. POSIX wheels create a bare
# `vibesignal`; Windows wheels add a `.exe` launcher. Listing both keeps the
# resolver correct across platforms.
_SCRIPT_NAMES = ("vibesignal", "vibesignal.exe")


def _check_darwin() -> None:
    if sys.platform != "darwin":
        raise SystemExit(
            "vibesignal installer: only macOS is supported here; "
            f"current platform is {sys.platform!r}."
        )


def _check_supported() -> None:
    """Guard for non-macOS/non-Windows platforms (Linux, etc.)."""
    if sys.platform not in ("darwin", "win32"):
        raise SystemExit(
            "vibesignal installer: install-launcher / install-autostart support "
            f"macOS and Windows; current platform is {sys.platform!r}. On Linux, "
            "run `vibesignal widget &` and add a ~/.config/autostart/ .desktop entry."
        )


def vibesignal_args() -> list[str]:
    """Resolve the widget invocation as an absolute argv list.

    Prefers the actual invocation in this process so a manual
    ``python -m vibesignal install-autostart`` from a freshly switched
    env never pins back to a stale ``vibesignal`` from a prior env
    still on ``PATH``. ``shutil.which`` is deliberately not used.

    Order:

    1. ``sys.argv[0]`` when it is an existing executable file named
       ``vibesignal`` (POSIX) or ``vibesignal.exe`` (Windows pip wheel
       launcher) -- this is how an installed console script invokes itself;
       the path is absolute and matches the env it lives in.
    2. ``<sys.executable parent>/vibesignal`` or ``vibesignal.exe`` when
       present -- handles ``python -m vibesignal ...``: the sibling script of
       the running interpreter is the one pinned to this env.
    3. Module form ``[sys.executable, "-m", "vibesignal"]`` as a last
       resort, for editable installs that have not exposed the console
       script yet.
    """
    argv0_str = sys.argv[0] if sys.argv else ""
    if argv0_str:
        argv0 = Path(argv0_str).resolve()
        if (
            argv0.name in _SCRIPT_NAMES
            and argv0.is_file()
            and os.access(argv0, os.X_OK)
        ):
            return [str(argv0)]
    bin_dir = Path(sys.executable).resolve().parent
    for name in _SCRIPT_NAMES:
        sibling = bin_dir / name
        if sibling.is_file() and os.access(sibling, os.X_OK):
            return [str(sibling)]
    return [str(Path(sys.executable).resolve()), "-m", "vibesignal"]


# --------------------------------------------------------------------------- #
# macOS: AppleScript .app launcher + LaunchAgent autostart
# --------------------------------------------------------------------------- #

def _user_applications_dir() -> Path:
    return Path.home() / "Applications"


def _launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _plist_path() -> Path:
    return _launch_agents_dir() / f"{LAUNCH_AGENT_LABEL}.plist"


def _launchd_target() -> str:
    return f"gui/{os.getuid()}"


def applescript_source(args: list[str]) -> str:
    """Render the AppleScript that launches the widget headlessly.

    Backgrounded with ``&`` so the shell call returns at once; the widget
    process detaches and stays alive in the Aqua session. AppleScript string
    literals only need ``\\`` and ``"`` escaped, which is what the body does.
    """
    cmd = " ".join(shlex.quote(a) for a in [*args, "widget"])
    escaped = cmd.replace("\\", "\\\\").replace('"', '\\"')
    return f'do shell script "{escaped} > /dev/null 2>&1 &"\n'


def plist_content(args: list[str]) -> str:
    """Render the LaunchAgent plist as a UTF-8 XML string."""
    parts = [*args, "widget"]
    args_xml = "\n".join(
        f"        <string>{xml.sax.saxutils.escape(p)}</string>" for p in parts
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "    <key>Label</key>\n"
        f"    <string>{LAUNCH_AGENT_LABEL}</string>\n"
        "    <key>ProgramArguments</key>\n"
        "    <array>\n"
        f"{args_xml}\n"
        "    </array>\n"
        "    <key>RunAtLoad</key>\n"
        "    <true/>\n"
        "    <key>KeepAlive</key>\n"
        "    <false/>\n"
        "    <key>ProcessType</key>\n"
        "    <string>Interactive</string>\n"
        "    <key>StandardOutPath</key>\n"
        f"    <string>/tmp/{LAUNCH_AGENT_LABEL}.log</string>\n"
        "    <key>StandardErrorPath</key>\n"
        f"    <string>/tmp/{LAUNCH_AGENT_LABEL}.err</string>\n"
        "</dict>\n"
        "</plist>\n"
    )


def _macos_install_launcher() -> Path:
    args = vibesignal_args()
    src = applescript_source(args)

    dest_dir = _user_applications_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / APP_NAME

    if dest.exists():
        shutil.rmtree(dest)

    # Write AppleScript to a temp file so osacompile reads from disk; the
    # alternative `-e <source>` would inline a large string into argv, which
    # is fine for short scripts but loses on robustness around quoting.
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".applescript", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(src)
        src_path = fh.name
    try:
        subprocess.run(["osacompile", "-o", str(dest), src_path], check=True)
    finally:
        Path(src_path).unlink(missing_ok=True)
    return dest


def _macos_uninstall_launcher() -> bool:
    dest = _user_applications_dir() / APP_NAME
    if not dest.exists():
        return False
    shutil.rmtree(dest)
    return True


def _macos_install_autostart(launch_now: bool = True) -> Path:
    args = vibesignal_args()
    content = plist_content(args)

    agents = _launch_agents_dir()
    agents.mkdir(parents=True, exist_ok=True)
    plist = _plist_path()
    target = _launchd_target()

    if plist.exists():
        # `bootout` is idempotent: an already-unloaded label produces a
        # non-zero exit that we deliberately swallow.
        subprocess.run(
            ["launchctl", "bootout", target, str(plist)],
            check=False,
            capture_output=True,
        )

    plist.write_text(content, encoding="utf-8")
    if launch_now:
        # `bootstrap` loads the agent into the running GUI session, and RunAtLoad
        # starts the widget immediately. Skipped when launch_now is False: the
        # plist in ~/Library/LaunchAgents is still loaded by launchd at the next
        # login, so login autostart works without spawning a widget right now.
        subprocess.run(
            ["launchctl", "bootstrap", target, str(plist)],
            check=True,
        )
    return plist


def _macos_uninstall_autostart() -> bool:
    plist = _plist_path()
    if not plist.exists():
        return False
    target = _launchd_target()
    subprocess.run(
        ["launchctl", "bootout", target, str(plist)],
        check=False,
        capture_output=True,
    )
    plist.unlink()
    return True


# --------------------------------------------------------------------------- #
# Windows: .lnk shortcuts via the stock PowerShell WScript.Shell COM object.
# The widget runs `pythonw -m vibesignal widget` so there is no console window.
# [Environment]::GetFolderPath resolves Startup / Programs / Desktop correctly
# even when the Desktop is redirected into OneDrive. Resolving the folder is
# not the whole story: the COM object cannot *save* into one whose name is not
# ANSI-encodable, so shortcuts are staged on an ASCII path and moved into
# place. See _windows_shortcut_ps1.
# --------------------------------------------------------------------------- #

def _windows_pythonw() -> str:
    """pythonw.exe (no console window) for the widget shortcut.

    pythonw is the sibling of the running interpreter; falls back to the plain
    executable if pythonw is missing (the widget still runs, with a brief
    console flash).
    """
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        return str(exe)
    pyw = exe.with_name("pythonw.exe")
    return str(pyw if pyw.is_file() else exe)


def _ps_squote(value: str) -> str:
    """Quote a string as a PowerShell single-quoted literal (doubling any quote)."""
    return "'" + value.replace("'", "''") + "'"


def _windows_shortcut_ps1(folder_id: str, target: str, arguments: str, workdir: str) -> str:
    """PowerShell that writes (overwriting) VibeSignal.lnk into a known folder.

    ``folder_id`` is a ``System.Environment.SpecialFolder`` name -- ``Startup``,
    ``Programs``, or ``Desktop``. The script prints the resolved .lnk path.

    The shortcut is built at an ASCII staging path and then moved into place.
    ``WScript.Shell``'s ``Save()`` converts its destination through the ANSI
    codepage, so saving straight into a directory whose name is not encodable
    there loses the characters and throws ``FileNotFoundException``:

        Unable to save shortcut "C:\\Users\\me\\OneDrive\\??\\VibeSignal.lnk".

    That covers any localized Desktop (``桌面``, ``デスクトップ``, ``Рабочий стол``)
    and OneDrive-redirected profiles, which is the common case on non-English
    Windows rather than an exotic one. ``Move-Item`` is plain .NET and handles
    Unicode, so staging first and moving second is safe for every folder.
    """
    return (
        "$ErrorActionPreference = 'Stop'\n"
        # Emit the resolved path as UTF-8 so a non-ASCII folder survives the
        # trip back to Python, which decodes this stdout as UTF-8.
        "try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }\n"
        f"$dir = [Environment]::GetFolderPath({_ps_squote(folder_id)})\n"
        f"$lnk = Join-Path $dir {_ps_squote(SHORTCUT_NAME)}\n"
        # The staging directory has to be ANSI-encodable for the same reason the
        # destination cannot be. $env:TEMP sits under the user profile, so it is
        # non-ASCII exactly when the account name is; fall back to its 8.3 short
        # name when that happens. The fallback is not guaranteed: Microsoft
        # documents that a short name may not exist (8.3 creation is disabled
        # per-volume or system-wide) and that the API can succeed by returning
        # the long path unchanged. Silently continuing there would hand
        # WScript.Shell the very path this function exists to avoid, so the
        # result is re-checked and a failure is reported instead of retried.
        "$stageDir = $env:TEMP\n"
        "if ($stageDir -match '[^\\x00-\\x7F]') {\n"
        "  try {\n"
        "    $stageDir = (New-Object -ComObject Scripting.FileSystemObject)"
        ".GetFolder($stageDir).ShortPath\n"
        "  } catch {\n"
        "    throw \"VibeSignal could not resolve an ASCII staging path from "
        "TEMP ($stageDir): $($_.Exception.Message)\"\n"
        "  }\n"
        "}\n"
        "if ($stageDir -match '[^\\x00-\\x7F]') {\n"
        "  throw 'VibeSignal could not find an ASCII staging path; set TEMP to "
        "an ASCII-writable directory and retry.'\n"
        "}\n"
        "$stage = Join-Path $stageDir "
        "('VibeSignal.' + [guid]::NewGuid().ToString('N') + '.lnk')\n"
        # Creation and Save() belong inside the cleanup boundary: a Save() that
        # fails after partially writing the stage would otherwise leak the file.
        "try {\n"
        "  $sh = New-Object -ComObject WScript.Shell\n"
        "  $s = $sh.CreateShortcut($stage)\n"
        f"  $s.TargetPath = {_ps_squote(target)}\n"
        f"  $s.Arguments = {_ps_squote(arguments)}\n"
        f"  $s.WorkingDirectory = {_ps_squote(workdir)}\n"
        "  $s.Description = 'VibeSignal status widget'\n"
        "  $s.Save()\n"
        "  New-Item -ItemType Directory -Force -Path $dir | Out-Null\n"
        "  Move-Item -LiteralPath $stage -Destination $lnk -Force\n"
        "} finally {\n"
        "  if (Test-Path -LiteralPath $stage) { "
        "Remove-Item -LiteralPath $stage -Force -ErrorAction SilentlyContinue }\n"
        "}\n"
        "Write-Output $lnk\n"
    )


def _windows_remove_ps1(folder_id: str) -> str:
    """PowerShell that removes VibeSignal.lnk from a known folder.

    Prints ``removed`` if a shortcut was deleted, ``absent`` otherwise.
    """
    return (
        "$ErrorActionPreference = 'Stop'\n"
        f"$dir = [Environment]::GetFolderPath({_ps_squote(folder_id)})\n"
        f"$lnk = Join-Path $dir {_ps_squote(SHORTCUT_NAME)}\n"
        "if (Test-Path -LiteralPath $lnk) { Remove-Item -LiteralPath $lnk -Force; "
        "Write-Output 'removed' } else { Write-Output 'absent' }\n"
    )


def _run_powershell(script: str) -> str:
    """Run a PowerShell script from a temp file; return its trimmed stdout.

    Written with a UTF-8 BOM on purpose: Windows PowerShell 5.1 decodes a
    ``-File`` script that has no BOM using the ANSI codepage, which corrupts any
    non-ASCII path embedded in the script (a profile under a non-ASCII account
    name). Stdout is decoded as UTF-8 to match the console encoding the script
    sets for itself.

    A non-zero exit raises with the child's stderr attached. The bare
    ``CalledProcessError`` this used to raise printed only the temp-file path,
    which said nothing about what actually went wrong.
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".ps1", delete=False, encoding="utf-8-sig"
    ) as fh:
        fh.write(script)
        path = fh.name
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(
                f"PowerShell helper failed (exit {result.returncode}): "
                f"{detail or 'no output'}"
            )
        return result.stdout.strip()
    finally:
        Path(path).unlink(missing_ok=True)


def _windows_launch_widget() -> None:
    """Start the widget now, detached and console-less, mirroring macOS RunAtLoad."""
    creationflags = getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen(
        [_windows_pythonw(), "-m", "vibesignal", "widget"],
        creationflags=creationflags,
        close_fds=True,
    )


def _windows_install_launcher() -> Path:
    """Create on-demand VibeSignal shortcuts in the Start Menu and on the Desktop.

    The Start Menu copy is searchable (type ``VibeSignal`` in the Start menu);
    the Desktop copy is one double-click away. Returns the Start Menu .lnk path.
    """
    target, args, workdir = _windows_pythonw(), "-m vibesignal widget", str(Path.home())
    programs = Path(
        _run_powershell(_windows_shortcut_ps1("Programs", target, args, workdir))
    )
    _run_powershell(_windows_shortcut_ps1("Desktop", target, args, workdir))
    return programs


def _windows_uninstall_launcher() -> bool:
    """Remove the Start Menu and Desktop shortcuts. True iff one was removed."""
    results = (
        _run_powershell(_windows_remove_ps1("Programs")),
        _run_powershell(_windows_remove_ps1("Desktop")),
    )
    return "removed" in results


def _windows_install_autostart(launch_now: bool = True) -> Path:
    """Write a Startup-folder shortcut so the widget launches at every login.
    Also starts the widget now unless ``launch_now`` is False. Returns the .lnk path."""
    target, args, workdir = _windows_pythonw(), "-m vibesignal widget", str(Path.home())
    lnk = Path(_run_powershell(_windows_shortcut_ps1("Startup", target, args, workdir)))
    if launch_now:
        _windows_launch_widget()
    return lnk


def _windows_uninstall_autostart() -> bool:
    """Remove the Startup-folder shortcut. True iff it was removed."""
    return _run_powershell(_windows_remove_ps1("Startup")) == "removed"


# --------------------------------------------------------------------------- #
# Public API: dispatch by platform.
# --------------------------------------------------------------------------- #

def install_launcher() -> Path:
    """Install a one-click launcher (macOS .app, or Windows Start Menu + Desktop
    shortcuts). Returns the launcher path. Re-install overwrites the prior one."""
    if sys.platform == "win32":
        return _windows_install_launcher()
    _check_supported()
    return _macos_install_launcher()


def uninstall_launcher() -> bool:
    """Remove the one-click launcher. Returns True iff something was removed."""
    if sys.platform == "win32":
        return _windows_uninstall_launcher()
    _check_supported()
    return _macos_uninstall_launcher()


def install_autostart(launch_now: bool = True) -> Path:
    """Install login autostart (macOS LaunchAgent, or a Windows Startup shortcut).
    Also starts the widget now unless ``launch_now`` is False. Returns the
    autostart file path."""
    if sys.platform == "win32":
        return _windows_install_autostart(launch_now=launch_now)
    _check_supported()
    return _macos_install_autostart(launch_now=launch_now)


def uninstall_autostart() -> bool:
    """Remove login autostart. Returns True iff something was removed."""
    if sys.platform == "win32":
        return _windows_uninstall_autostart()
    _check_supported()
    return _macos_uninstall_autostart()


# -- Agent hook installers (cross-platform) --------------------------------
#
# The launcher/autostart helpers above pin the absolute vibesignal path so a
# LaunchAgent's empty PATH cannot break them. Hooks have the same problem:
# Claude Code and Codex run each hook command in a short-lived shell whose PATH
# need not contain the env vibesignal was installed into (a conda env on macOS
# is the common trap), yet the shipped snippet uses a bare ``vibesignal`` and
# leaves the pinning to a hand-merge the user may skip -- so a hook silently
# fails with "command not found" and nothing ever records. These helpers close
# that gap: they merge the hook block and pin ``vibesignal_args()`` the same
# way, so ``pip install`` + ``install-hooks`` needs no PATH surgery.

def claude_settings_path() -> Path:
    return Path.home() / ".claude" / "settings.json"


def codex_hooks_path() -> Path:
    return Path.home() / ".codex" / "hooks.json"


def _agent_settings_path(agent: str) -> Path:
    if agent == "claude":
        return claude_settings_path()
    if agent == "codex":
        return codex_hooks_path()
    raise SystemExit(
        f"vibesignal: unknown agent {agent!r}; expected 'claude' or 'codex'."
    )


def _cmd_quote(token: str) -> str:
    """Quote one token for a ``cmd.exe`` command line.

    A bare token is left alone only when it carries nothing that either the
    shell or a later reader could misinterpret. Quotes are added for
    whitespace, for cmd's own metacharacters (``&``, ``|``, ``<``, ``>``,
    ``^``, parentheses), for the empty string, and -- importantly -- for a
    backslash.

    The backslash rule is not about cmd, which treats a backslash as an
    ordinary character. It is about the round trip: this string is stored in
    the agent's settings file and later re-parsed by ``_hook_is_vibesignal``
    with POSIX ``shlex.split`` to decide whether a handler is ours. Outside
    quotes ``shlex`` consumes each backslash as an escape, so a bare
    ``C:\\env\\Scripts\\vibesignal.exe`` comes back as
    ``C:envScriptsvibesignal.exe``, the handler stops being recognized, and
    reinstall duplicates it while uninstall cannot find it. Quoting keeps the
    separators intact on the way back.

    Any run of backslashes immediately before the closing quote is doubled per
    the CRT argument rules, so a path ending in a separator does not swallow
    the quote that terminates it.

    Two characters are rejected rather than encoded, because neither can be
    represented faithfully and a silently mangled hook is worse than a loud
    install failure:

    * ``%`` -- cmd expands ``%NAME%`` even inside double quotes, and the ``%%``
      escape works only inside a batch file.
    * ``"`` -- the CRT layer can be escaped correctly, but cmd does not treat
      the preceding backslash as an escape, so a later metacharacter can break
      out of the quoted region (``a"b&c`` launches ``c"`` as a second command).

    Neither can occur in a Windows filesystem path, and the fixed hook tail
    contains neither, so this rejects only inputs the installer cannot produce.
    """
    for bad, why in (
        ("%", "cmd expands a percent sign as an environment variable even "
              "inside quotes, and offers no escape for it on a command line"),
        ('"', "cmd does not treat the preceding backslash as an escape, so a "
              "later metacharacter can break out of the quoted region"),
    ):
        if bad in token:
            raise ValueError(
                f"cannot place {token!r} in a cmd.exe hook command: {why}"
            )
    if token and not any(c in token for c in " \t\\&|<>^()"):
        return token
    trailing = len(token) - len(token.rstrip("\\"))
    return '"' + token + "\\" * trailing + '"'


def _hook_command(args: list[str], tail: list[str], agent: str = "claude") -> str:
    """Join the pinned argv with a hook tail into one shell command string.

    Each token is quoted so a vibesignal path with a space survives the
    round-trip through the settings JSON and the hook shell.

    The quoting is per-agent on Windows, because the two agents hand the string
    to different shells and those shells have different grammars. Claude Code
    runs hooks through a POSIX shell, so POSIX quoting is correct there. Codex
    runs them through ``cmd.exe``, where a single quote is an ordinary
    character, so a ``shlex.quote`` path (single-quoted, because a Windows path
    contains backslashes) reaches cmd verbatim and the hook dies with::

        The filename, directory name, or volume label syntax is incorrect.

    which Codex reports as ``hook exited with code 1``.

    A single shared format cannot serve both. Double quotes look portable but
    are not: a POSIX shell still expands ``$`` and backticks inside them, and
    still treats ``\\\\`` as an escaped backslash, so a UNC path loses a
    separator and a ``$`` path component is eaten. Each shell therefore gets
    its own routine.
    """
    tokens = [*args, *tail]
    if sys.platform == "win32" and agent == "codex":
        return " ".join(_cmd_quote(t) for t in tokens)
    return " ".join(shlex.quote(a) for a in tokens)


def agent_hooks_spec(args: list[str], agent: str) -> dict:
    """The vibesignal hook block for one agent, ready to merge.

    Claude Code and Codex use DIFFERENT hook vocabularies, so the spec is
    per-agent (verified against the official Codex hooks docs, developers.
    openai.com/codex/hooks): Codex's approval/input event is ``PermissionRequest``
    (Claude uses ``Notification`` with ``permission_prompt`` / ``idle_prompt``
    matchers), and Codex has no ``StopFailure`` or ``SessionEnd``. A closed Codex
    session therefore has no session-close hook and ages out by its per-state TTL
    instead of clearing at once; ``Stop`` still carries the "your move" (done)
    signal. Both agents share ``UserPromptSubmit`` / ``PostToolUse`` / ``Stop``.
    """
    def cmd(*tail: str) -> dict:
        return {"type": "command", "command": _hook_command(args, list(tail), agent)}

    if agent == "codex":
        # Codex parses a hook's stdout as JSON, so every Codex command passes
        # --quiet to keep stdout empty (the state is still recorded); without it
        # Codex reports "hook returned invalid post-tool-use JSON output".
        return {
            "UserPromptSubmit": [
                {"hooks": [cmd("event", "--agent", agent, "--state", "working", "--quiet")]},
            ],
            "PostToolUse": [
                {"matcher": "*", "hooks": [cmd("event", "--agent", agent, "--state", "working", "--quiet")]},
            ],
            "PermissionRequest": [
                {"matcher": "*", "hooks": [cmd("event", "--agent", agent, "--state", "blocked", "--quiet")]},
            ],
            "Stop": [
                {"hooks": [cmd("event", "--agent", agent, "--state", "done", "--quiet")]},
            ],
        }

    return {
        "UserPromptSubmit": [
            {"hooks": [cmd("event", "--agent", agent, "--state", "working")]},
        ],
        "PostToolUse": [
            {"matcher": "*", "hooks": [cmd("event", "--agent", agent, "--state", "working")]},
        ],
        "Notification": [
            {"matcher": "permission_prompt",
             "hooks": [cmd("event", "--agent", agent, "--state", "blocked")]},
            {"matcher": "idle_prompt",
             "hooks": [cmd("event", "--agent", agent, "--state", "done")]},
        ],
        "Stop": [
            {"hooks": [cmd("event", "--agent", agent, "--state", "done")]},
        ],
        "StopFailure": [
            {"hooks": [cmd("event", "--agent", agent, "--state", "done")]},
        ],
        "SessionEnd": [
            {"hooks": [cmd("end", "--agent", agent)]},
        ],
    }


def _argv_is_vibesignal(argv: list[str]) -> bool:
    """True if argv is a command THIS installer generates: the vibesignal
    console script (or ``<python> -m vibesignal``) invoking ``event``/``end`` with
    an ``--agent`` tag. Matching this shape -- not a bare "vibesignal" substring --
    avoids deleting a user's unrelated hook whose command merely contains the word
    (e.g. /opt/vibesignal-notify/run.sh)."""
    if not argv:
        return False
    if Path(argv[0]).name.lower() in {"vibesignal", "vibesignal.exe"}:
        tail = argv[1:]
    elif argv[1:3] == ["-m", "vibesignal"]:
        # Module form: key on the `-m vibesignal` shape, NOT a python-basename
        # allow-list, so a versioned interpreter (python3.12, pypy3, ...) still
        # round-trips through uninstall instead of leaving a stale hook behind.
        tail = argv[3:]
    else:
        return False
    return len(tail) >= 2 and tail[0] in {"event", "end"} and "--agent" in tail


def _hook_is_vibesignal(h: object) -> bool:
    """True if a single hook handler's command is one this installer wrote."""
    if not isinstance(h, dict):
        return False
    command = h.get("command")
    if not isinstance(command, str):
        return False
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    return _argv_is_vibesignal(argv)


def _entry_is_vibesignal(entry: object) -> bool:
    """True if a hook entry contains any command this installer wrote."""
    if not isinstance(entry, dict):
        return False
    hooks = entry.get("hooks")
    if not isinstance(hooks, list):  # tolerate malformed entry, do not crash
        return False
    return any(_hook_is_vibesignal(h) for h in hooks)


def _strip_vibesignal_from_entry(entry: object) -> tuple:
    """Filter one hook entry at the HANDLER level: drop our commands, keep the
    user's. Returns (kept_entry_or_None, removed_any); None when no handler is
    left. Handler-level (not whole-entry) filtering is what preserves a foreign
    command a user placed in the same matcher group's hooks list as one of ours.
    """
    if not isinstance(entry, dict):
        return entry, False
    hooks = entry.get("hooks")
    if not isinstance(hooks, list):
        return entry, False
    kept = [h for h in hooks if not _hook_is_vibesignal(h)]
    removed = len(kept) != len(hooks)
    if not kept:
        return None, removed
    if removed:
        entry = {**entry, "hooks": kept}
    return entry, removed


def _load_settings_obj(path: Path) -> dict:
    """Load a settings JSON object, or {} when the file is absent.

    Reads as ``utf-8-sig`` so a leading BOM (some editors add one) is tolerated
    rather than rejected. A present file that is unparseable, or whose JSON root
    is not an object, is a hard error rather than a silent {}: overwriting a real
    settings file we could not read or that has an unexpected shape would destroy
    the user's other config.
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (json.JSONDecodeError, OSError) as exc:
        raise SystemExit(
            f"vibesignal: cannot read {path} ({exc}); fix or move it, then retry."
        )
    if not isinstance(data, dict):
        raise SystemExit(
            f"vibesignal: {path} is not a JSON object; refusing to overwrite it."
        )
    return data


def _strip_vibesignal_hooks(hooks: dict) -> bool:
    """Remove vibesignal handlers across EVERY event in a hooks dict, at the
    handler level. Foreign siblings are kept; an emptied entry is dropped and an
    emptied event key is removed. Returns True iff anything was removed."""
    removed = False
    for event in list(hooks.keys()):
        entries = hooks[event]
        if not isinstance(entries, list):
            continue
        new_entries = []
        for e in entries:
            kept_e, rem = _strip_vibesignal_from_entry(e)
            removed = removed or rem
            if kept_e is not None:
                new_entries.append(kept_e)
        if new_entries:
            hooks[event] = new_entries
        else:
            del hooks[event]
    return removed


def _merge_hooks(settings: dict, spec: dict) -> None:
    """Merge spec into settings['hooks'] in place, idempotently and convergently.

    First strip ALL prior vibesignal handlers across every event -- not just the
    events in the new spec -- so a re-install after the agent's event set changed
    (e.g. old Codex Notification / SessionEnd entries from before the schema fix)
    leaves no orphans. Foreign hooks, including a command that shares a matcher
    group with one of ours, are preserved. Then append the fresh block.
    """
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    _strip_vibesignal_hooks(hooks)
    for event, entries in spec.items():
        existing = hooks.get(event)
        existing = existing if isinstance(existing, list) else []
        hooks[event] = existing + entries
    settings["hooks"] = hooks


def _strip_hooks(settings: dict) -> bool:
    """Remove only vibesignal commands from settings['hooks'] in place, at the
    handler level so a foreign command sharing a matcher group survives. Returns
    True iff something was removed; an emptied hooks object is dropped so
    uninstall leaves no residue."""
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return False
    removed = _strip_vibesignal_hooks(hooks)
    if hooks:
        settings["hooks"] = hooks
    else:
        settings.pop("hooks", None)
    return removed


def _backup_once(path: Path) -> None:
    """Copy path to ``<name>.bak-vibesignal`` once, preserving the pristine
    pre-vibesignal file across repeated installs."""
    backup = path.parent / (path.name + ".bak-vibesignal")
    if path.exists() and not backup.exists():
        shutil.copy2(path, backup)


def _settings_lock_path(path: Path) -> Path:
    return path.parent / f".{path.name}.vibesignal.lock"


def _write_settings(path: Path, settings: dict) -> None:
    """Atomically write settings, preserving a symlink target and file mode.

    store._atomic_write is for private state files; a user-managed settings file
    needs two extra guarantees. (1) If it is a dotfiles symlink, write THROUGH to
    the real target instead of replacing the link with a standalone regular file
    (os.replace over the link name would sever it and silently diverge from the
    dotfiles repo). (2) Preserve the existing mode rather than narrowing to
    mkstemp's 0600, so a deliberately group-readable settings file keeps its bits.
    """
    target = path.resolve() if path.is_symlink() else path
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = None
    if target.exists():
        with contextlib.suppress(OSError):
            mode = stat.S_IMODE(target.stat().st_mode)
    text = json.dumps(settings, indent=2) + "\n"
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def install_hooks(agent: str = "claude") -> Path:
    """Merge the vibesignal hook block for `agent` and pin the absolute path.

    Cross-platform. Idempotent: re-running re-pins ``vibesignal_args()`` and
    never double-adds. Returns the settings file that was written.
    """
    args = vibesignal_args()
    path = _agent_settings_path(agent)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Serialize the read-modify-write so a concurrent settings edit (the agent UI
    # toggling a setting, or a second install-hooks) cannot clobber this one.
    with lock.file_lock(_settings_lock_path(path)):
        settings = _load_settings_obj(path)
        _backup_once(path)
        _merge_hooks(settings, agent_hooks_spec(args, agent))
        _write_settings(path, settings)
    return path


def uninstall_hooks(agent: str = "claude") -> bool:
    """Remove vibesignal hooks for `agent`, leaving the user's other hooks
    untouched. Returns True iff something was removed."""
    path = _agent_settings_path(agent)
    if not path.exists():
        return False
    with lock.file_lock(_settings_lock_path(path)):
        settings = _load_settings_obj(path)
        if not _strip_hooks(settings):
            return False
        _backup_once(path)
        _write_settings(path, settings)
    return True
