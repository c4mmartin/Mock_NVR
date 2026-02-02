from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from shutil import which
from typing import Iterable, Sequence


@dataclass(frozen=True)
class FirewallRule:
    name: str
    port: int
    protocol: str = "TCP"


@dataclass(frozen=True)
class FirewallCommand:
    argv: list[str]
    display: str
    needs_admin: bool = True
    interactive_yes: bool = False


def _is_windows() -> bool:
    return os.name == "nt"


def _is_macos() -> bool:
    return sys.platform == "darwin"


def detect_backend() -> str:
    if _is_windows():
        return "windows"
    if _is_macos():
        return "macos_appfirewall"
    if which("firewall-cmd"):
        return "firewalld"
    if which("ufw"):
        return "ufw"
    return "unknown"


def build_open_commands(*, rules: Sequence[FirewallRule], python_executable: str) -> list[FirewallCommand]:
    backend = detect_backend()

    if backend == "windows":
        cmds: list[FirewallCommand] = []
        for rule in rules:
            name = f"mock_nvr {rule.name} {rule.port}"
            cmds.append(
                FirewallCommand(
                    argv=[
                        "netsh",
                        "advfirewall",
                        "firewall",
                        "add",
                        "rule",
                        f"name={name}",
                        "dir=in",
                        "action=allow",
                        "protocol=TCP",
                        f"localport={rule.port}",
                    ],
                    display=f"netsh advfirewall firewall add rule name=\"{name}\" dir=in action=allow protocol=TCP localport={rule.port}",
                    needs_admin=True,
                )
            )
        return cmds

    if backend == "macos_appfirewall":
        # macOS Application Firewall is app-based, not port-based.
        sockfw = "/usr/libexec/ApplicationFirewall/socketfilterfw"
        py = python_executable
        return [
            FirewallCommand(
                argv=[sockfw, "--add", py],
                display=f"sudo {sockfw} --add {py}",
                needs_admin=True,
            ),
            FirewallCommand(
                argv=[sockfw, "--unblockapp", py],
                display=f"sudo {sockfw} --unblockapp {py}",
                needs_admin=True,
            ),
        ]

    if backend == "firewalld":
        cmds = [
            FirewallCommand(
                argv=["firewall-cmd", "--add-service=http"],
                display="sudo firewall-cmd --add-service=http",
                needs_admin=True,
            )
        ]
        # Don't assume http service maps to our port; explicitly add ports.
        cmds = []
        for rule in rules:
            cmds.append(
                FirewallCommand(
                    argv=["firewall-cmd", "--add-port", f"{rule.port}/tcp", "--permanent"],
                    display=f"sudo firewall-cmd --add-port={rule.port}/tcp --permanent",
                    needs_admin=True,
                )
            )
        cmds.append(
            FirewallCommand(
                argv=["firewall-cmd", "--reload"],
                display="sudo firewall-cmd --reload",
                needs_admin=True,
            )
        )
        return cmds

    if backend == "ufw":
        cmds = []
        for rule in rules:
            # Use a short comment; ufw supports: ufw allow <port>/tcp comment <text>
            cmds.append(
                FirewallCommand(
                    argv=["ufw", "allow", f"{rule.port}/tcp", "comment", f"mock_nvr {rule.name}"],
                    display=f"sudo ufw allow {rule.port}/tcp comment 'mock_nvr {rule.name}'",
                    needs_admin=True,
                )
            )
        return cmds

    # Unknown system: just provide generic guidance.
    return []


def build_close_commands(*, rules: Sequence[FirewallRule], python_executable: str) -> list[FirewallCommand]:
    backend = detect_backend()

    if backend == "windows":
        cmds: list[FirewallCommand] = []
        for rule in rules:
            name = f"mock_nvr {rule.name} {rule.port}"
            cmds.append(
                FirewallCommand(
                    argv=[
                        "netsh",
                        "advfirewall",
                        "firewall",
                        "delete",
                        "rule",
                        f"name={name}",
                    ],
                    display=f"netsh advfirewall firewall delete rule name=\"{name}\"",
                    needs_admin=True,
                )
            )
        return cmds

    if backend == "macos_appfirewall":
        sockfw = "/usr/libexec/ApplicationFirewall/socketfilterfw"
        py = python_executable
        return [
            FirewallCommand(
                argv=[sockfw, "--blockapp", py],
                display=f"sudo {sockfw} --blockapp {py}",
                needs_admin=True,
            )
            ,
            FirewallCommand(
                argv=[sockfw, "--remove", py],
                display=f"sudo {sockfw} --remove {py}",
                needs_admin=True,
            ),
        ]

    if backend == "firewalld":
        cmds: list[FirewallCommand] = []
        for rule in rules:
            cmds.append(
                FirewallCommand(
                    argv=["firewall-cmd", "--remove-port", f"{rule.port}/tcp", "--permanent"],
                    display=f"sudo firewall-cmd --remove-port={rule.port}/tcp --permanent",
                    needs_admin=True,
                )
            )
        cmds.append(
            FirewallCommand(
                argv=["firewall-cmd", "--reload"],
                display="sudo firewall-cmd --reload",
                needs_admin=True,
            )
        )
        return cmds

    if backend == "ufw":
        cmds = []
        for rule in rules:
            # ufw delete prompts for confirmation.
            cmds.append(
                FirewallCommand(
                    argv=["ufw", "delete", "allow", f"{rule.port}/tcp"],
                    display=f"sudo ufw delete allow {rule.port}/tcp",
                    needs_admin=True,
                    interactive_yes=True,
                )
            )
        return cmds

    return []


def format_commands(commands: Iterable[FirewallCommand]) -> str:
    return "\n".join(f"  - {c.display}" for c in commands)


def apply_commands(commands: Sequence[FirewallCommand], *, assume_yes: bool) -> tuple[bool, list[str]]:
    """Run firewall commands. Returns (ok, messages)."""

    messages: list[str] = []
    ok = True

    for cmd in commands:
        try:
            proc = subprocess.run(
                cmd.argv,
                input=(b"y\n" if (assume_yes and cmd.interactive_yes) else None),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            out = (proc.stdout or b"").decode("utf-8", errors="replace").strip()
            if proc.returncode != 0:
                ok = False
                messages.append(f"FAILED: {cmd.display}")
                if out:
                    messages.append(out)
            else:
                messages.append(f"OK: {cmd.display}")
        except FileNotFoundError:
            ok = False
            messages.append(f"FAILED: command not found: {cmd.argv[0]}")
        except Exception as e:
            ok = False
            messages.append(f"FAILED: {cmd.display}: {e}")

    return ok, messages
