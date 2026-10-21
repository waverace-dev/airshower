"""Compile project-local owner/lock helpers with the existing system compiler."""
import os
import platform
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NATIVE = ROOT / "integrations/p2p/native"

def build():
    target = ["-mmacosx-version-min=12.0"] if platform.system() == "Darwin" else []
    subprocess.run(["cc", "-O2", "-Wall", "-Wextra", "-Werror", *target, str(NATIVE / "profile-lock.c"), "-o", str(NATIVE / "airshower-lock")], check=True)
    if platform.system() == "Darwin":
        if platform.machine() != "arm64":
            raise RuntimeError("The current Iroh macOS package requires arm64")
        app = NATIVE / "Airshower Approval.app"
        binary = app / "Contents/MacOS/AirshowerApproval"
        binary.parent.mkdir(parents=True, exist_ok=True)
        cache = ROOT / "build/swift-module-cache"
        cache.mkdir(parents=True, exist_ok=True)
        subprocess.run(["swiftc", "-O", "-target", "arm64-apple-macosx12.0", "-module-cache-path", str(cache), str(NATIVE / "OwnerPrompt.swift"), "-o", str(binary)], check=True)
        shutil.copyfile(NATIVE / "Info.plist", app / "Contents/Info.plist")
        subprocess.run(["codesign", "--force", "--sign", "-", str(app)], check=True)
        subprocess.run([str(binary), "--version"], check=True)
    print("Built local P2P helpers; no app installed or registered.")

if __name__ == "__main__":
    build()
