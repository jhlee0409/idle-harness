import json
import os
import signal
import subprocess
import time


class DevServer:
    def __init__(self, comms_dir: str, output_dir: str, startup_wait: int = 5):
        self.comms_dir = comms_dir
        self.output_dir = output_dir
        self.startup_wait = startup_wait
        self.start_cmd: str | None = None
        self.processes: list[subprocess.Popen] = []

    def detect_from_comms(self) -> dict | None:
        path = os.path.join(self.comms_dir, "dev_server.json")
        try:
            with open(path) as f:
                data = json.load(f)
            self.start_cmd = data.get("start")
            return data
        except FileNotFoundError:
            return None

    def detect_from_structure(self) -> dict | None:
        pkg_root = os.path.join(self.output_dir, "package.json")
        if os.path.exists(pkg_root):
            self.start_cmd = "npm run dev"
            return {"start": "npm run dev"}

        pkg_frontend = os.path.join(self.output_dir, "frontend", "package.json")
        main_backend = os.path.join(self.output_dir, "backend", "main.py")

        if os.path.exists(pkg_frontend) and os.path.exists(main_backend):
            self.start_cmd = "__fullstack__"
            return {"start": "__fullstack__"}

        if os.path.exists(pkg_frontend):
            self.start_cmd = "cd frontend && npm run dev"
            return {"start": "cd frontend && npm run dev"}

        return None

    def detect(self) -> str | None:
        result = self.detect_from_comms()
        if result:
            return result["start"]
        result = self.detect_from_structure()
        if result:
            return result["start"]
        return None

    def start(self):
        if not self.start_cmd:
            self.detect()
        if not self.start_cmd:
            raise RuntimeError("No dev server command found. Cannot start server.")

        # Kill any leftover processes on our ports
        _kill_port(5173)
        _kill_port(8000)

        if self.start_cmd == "__fullstack__":
            self._start_fullstack()
        else:
            self.processes.append(subprocess.Popen(
                self.start_cmd,
                shell=True,
                cwd=self.output_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                preexec_fn=os.setsid,
            ))

        time.sleep(self.startup_wait)

    def _start_fullstack(self):
        backend_dir = os.path.join(self.output_dir, "backend")
        frontend_dir = os.path.join(self.output_dir, "frontend")

        req = os.path.join(backend_dir, "requirements.txt")
        if os.path.exists(req):
            subprocess.run(
                "pip3 install -r requirements.txt",
                shell=True, cwd=backend_dir,
                capture_output=True,
            )
        if not os.path.isdir(os.path.join(frontend_dir, "node_modules")):
            subprocess.run(
                "npm install",
                shell=True, cwd=frontend_dir,
                capture_output=True,
            )

        self.processes.append(subprocess.Popen(
            "python3 -m uvicorn main:app --reload --port 8000",
            shell=True,
            cwd=backend_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=os.setsid,
        ))

        self.processes.append(subprocess.Popen(
            "npm run dev",
            shell=True,
            cwd=frontend_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=os.setsid,
        ))

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
        _kill_port(5173)
        _kill_port(8000)


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
