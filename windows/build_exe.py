"""Build LocalChat.exe with PyInstaller.

Usage:
    python build_exe.py

Output:
    dist/LocalChat.exe   (single-file, windowed, no console)

Notes:
    - The paperclip SVG icon asset is bundled via --add-data and resolved at
      runtime from sys._MEIPASS (see localchat/ui/chat_page.py).
    - PyQt6.QtSvg is imported by main.py so the SVG image-format plugin is
      bundled; without it the file-send button icon would render blank.
    - The app stores its database in a "data" folder next to the exe
      (see main.base_dir), so chat history survives updates of the exe.
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))


def _ffmpeg_binary_args() -> list:
    """Bundle the imageio-ffmpeg package (incl. its binaries dir) so the frozen
    exe can capture the camera via DirectShow even when ffmpeg is not on PATH
    (used when OpenCV's Windows camera backend cannot enumerate devices — see
    localchat/call.py). The binaries subpackage is loaded dynamically via
    importlib.resources, so a plain --hidden-import would miss it."""
    try:
        import imageio_ffmpeg  # noqa: F401

        return ["--collect-all", "imageio_ffmpeg"]
    except Exception:
        return []


def main() -> int:
    os.chdir(ROOT)

    icon_ico = os.path.join(ROOT, "build", "app_icon.ico")
    if not os.path.exists(icon_ico):
        make_icon = os.path.join(ROOT, "build", "make_icon.py")
        if not os.path.exists(make_icon):
            print("icon generator missing:", make_icon)
            return 1
        subprocess.check_call([sys.executable, make_icon])

    asset = os.path.join("localchat", "ui", "assets", "paperclip.svg")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--windowed", "--onefile",
        "--name", "LocalChat",
        "--icon", icon_ico,
        "--add-data", asset + os.pathsep + os.path.join("localchat", "ui", "assets"),
        os.path.join(ROOT, "main.py"),
    ]
    cmd += _ffmpeg_binary_args()
    # sounddevice (PortAudio) audio capture/playback fallback — bundle the
    # package including its portaudio DLL data files
    try:
        import sounddevice  # noqa: F401

        cmd += ["--collect-all", "sounddevice"]
    except Exception:
        pass
    return subprocess.call(cmd)


if __name__ == "__main__":
    sys.exit(main())
