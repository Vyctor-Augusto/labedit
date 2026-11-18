"""Instala o ambiente local e adiciona o LabEdit ao menu do Resolve."""

import os
from pathlib import Path
import subprocess
import sys
import venv


ROOT = Path(__file__).resolve().parent
LOCAL = ROOT / ".local"
UTILITY = Path("Blackmagic Design/DaVinci Resolve/Support/Fusion/Scripts/Utility")


def lua_literal(path):
    levels = "="
    while f"]{levels}]" in str(path):
        levels += "="
    return f"[{levels}[{path}]{levels}]"


def build_launcher(root, python_gui):
    paths = {
        "snapshot_folder": root / ".local" / "resolve-timeline",
        "python_gui": python_gui,
        "editor_script": root / "LabEdit.py",
    }
    prefix = "".join(f"local {key} = {lua_literal(path)}\n" for key, path in paths.items())
    return prefix + (root / "LabEdit Resolve.lua").read_text(encoding="utf-8")


def install():
    if os.name != "nt" or sys.version_info[:2] != (3, 14):
        raise ValueError("Este instalador precisa do Python 3.14 de 64 bits no Windows.")
    environment = LOCAL / "venv"
    executable = environment / "Scripts" / "python.exe"
    python_gui = executable.with_name("pythonw.exe")
    if not executable.is_file() or not python_gui.is_file():
        venv.EnvBuilder(with_pip=True).create(environment)
    subprocess.run([str(executable), "-m", "pip", "install", "-r",
                    str(ROOT / "requirements.txt")], check=True)
    (LOCAL / "resolve-timeline").mkdir(parents=True, exist_ok=True)
    scripts = Path(os.environ["APPDATA"]) / UTILITY
    scripts.mkdir(parents=True, exist_ok=True)
    destination = scripts / "LabEdit.lua"
    temporary = scripts / "LabEdit.lua.installing"
    temporary.write_text(build_launcher(ROOT, python_gui), encoding="utf-8")
    temporary.replace(destination)
    print("LabEdit instalado. Abra Workspace > Scripts > LabEdit.")
    print("Se a entrada não aparecer, reabra o Resolve.")


if __name__ == "__main__":
    try:
        install()
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"Falha na instalação: {error}") from error
