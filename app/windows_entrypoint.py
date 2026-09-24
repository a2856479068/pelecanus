"""Windows executable entry point for Pelican Watch."""
import os
from pathlib import Path
import sys
import threading
import webbrowser


def configure_frozen_runtime():
    if not getattr(sys, "frozen", False):
        return

    executable_dir = Path(sys.executable).resolve().parent
    project_data = next((candidate for candidate in (
        executable_dir / "app" / "data",
        executable_dir.parent / "app" / "data",
    ) if (candidate / "monitor.sqlite3").is_file()), None)
    if project_data is None:
        local_app_data = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        project_data = local_app_data / "PelicanWatch" / "data"

    os.environ.setdefault("DATA_DIR", str(project_data))
    host = os.environ.get("HOST", "127.0.0.1")
    port = os.environ.get("PORT", "8765")
    if host in ("127.0.0.1", "localhost") and os.environ.get("PELICAN_OPEN_BROWSER", "1") != "0":
        url = f"http://127.0.0.1:{port}/"
        print(f"Pelican Watch data: {project_data}", flush=True)
        threading.Timer(1.5, webbrowser.open, args=(url,)).start()


configure_frozen_runtime()

from server import main  # noqa: E402


if __name__ == "__main__":
    main()
