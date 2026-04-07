import json
import os
import signal
import subprocess
import time
import urllib.request

from config import get_server_ports

_FULLSTACK = "__fullstack__"


class DevServer:
    def __init__(
        self,
        comms_dir: str,
        output_dir: str,
        startup_wait: int = 5,
        health_timeout: int = 60,
        health_url: str = "http://localhost:5173",
    ):
        self.comms_dir = comms_dir
        self.output_dir = output_dir
        self.startup_wait = startup_wait
        self.health_timeout = health_timeout
        self.health_url = health_url
        self.start_cmd: str | None = None
        self.processes: list[subprocess.Popen] = []

    def detect(self):
        """Detect dev server command from comms JSON, output JSON, or project structure."""
        for base in (self.comms_dir, self.output_dir):
            try:
                with open(os.path.join(base, "dev_server.json")) as f:
                    self.start_cmd = json.load(f).get("start")
                    return
            except FileNotFoundError:
                continue

        pkg_root = os.path.join(self.output_dir, "package.json")
        if os.path.exists(pkg_root):
            self.start_cmd = "npm run dev"
            return

        pkg_frontend = os.path.join(self.output_dir, "frontend", "package.json")
        main_backend = os.path.join(self.output_dir, "backend", "main.py")

        if os.path.exists(pkg_frontend) and os.path.exists(main_backend):
            self.start_cmd = _FULLSTACK
        elif os.path.exists(pkg_frontend):
            self.start_cmd = "cd frontend && npm run dev"

    def _spawn(self, cmd: str, cwd: str):
        self.processes.append(subprocess.Popen(
            cmd,
            shell=True,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=os.setsid,
        ))

    def start(self):
        if not self.start_cmd:
            self.detect()
        if not self.start_cmd:
            raise RuntimeError("No dev server command found. Cannot start server.")

        _kill_all_ports()

        if self.start_cmd == _FULLSTACK:
            self._start_fullstack()
        else:
            self._spawn(self.start_cmd, self.output_dir)

        time.sleep(self.startup_wait)
        self._wait_until_ready()

    def _wait_until_ready(self):
        """Poll the health URL until the server responds or timeout."""
        deadline = time.time() + self.health_timeout
        last_err = None
        while time.time() < deadline:
            try:
                urllib.request.urlopen(self.health_url, timeout=2)
                return
            except Exception as exc:
                last_err = exc
                time.sleep(1)

        for proc in self.processes:
            if proc.poll() is not None:
                stderr = proc.stderr.read().decode() if proc.stderr else ""
                raise RuntimeError(
                    f"Dev server process exited with code {proc.returncode}. "
                    f"stderr: {stderr[:500]}"
                )

        raise RuntimeError(
            f"Dev server not ready after {self.health_timeout}s at {self.health_url}. "
            f"Last error: {last_err}"
        )

    def _start_fullstack(self):
        backend_dir = os.path.join(self.output_dir, "backend")
        frontend_dir = os.path.join(self.output_dir, "frontend")

        req = os.path.join(backend_dir, "requirements.txt")
        if os.path.exists(req):
            result = subprocess.run(
                "pip3 install -r requirements.txt",
                shell=True, cwd=backend_dir,
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"pip install failed in backend/ (exit {result.returncode}): "
                    f"{result.stderr[:500]}"
                )
        if not os.path.isdir(os.path.join(frontend_dir, "node_modules")):
            result = subprocess.run(
                "npm install",
                shell=True, cwd=frontend_dir,
                capture_output=True, text=True,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"npm install failed in frontend/ (exit {result.returncode}): "
                    f"{result.stderr[:500]}"
                )

        self._spawn("python3 -m uvicorn main:app --reload --port 8000", backend_dir)
        self._spawn("npm run dev", frontend_dir)

    def stop(self):
        for proc in self.processes:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        self.processes = []
        _kill_all_ports()


def _kill_all_ports():
    for port in get_server_ports():
        _kill_port(port)


def _kill_port(port: int):
    try:
        result = subprocess.run(
            f"lsof -ti:{port}",
            shell=True, capture_output=True, text=True,
        )
        pids = result.stdout.strip()
        if pids:
            subprocess.run(f"kill -9 {pids}", shell=True, capture_output=True)
    except Exception:
        pass
