import json
import os
import tempfile
from unittest.mock import patch, MagicMock
from server import DevServer


def test_detect_server_cmd_from_json():
    with tempfile.TemporaryDirectory() as tmpdir:
        dev_server_json = os.path.join(tmpdir, "dev_server.json")
        with open(dev_server_json, "w") as f:
            json.dump({"start": "npm run dev", "stop": "kill"}, f)
        server = DevServer(comms_dir=tmpdir, output_dir="/tmp/out")
        cmd = server.detect_from_comms()
        assert cmd["start"] == "npm run dev"


def test_detect_root_package_json():
    with tempfile.TemporaryDirectory() as tmpdir:
        pkg = {"scripts": {"dev": "vite"}}
        with open(os.path.join(tmpdir, "package.json"), "w") as f:
            json.dump(pkg, f)

        server = DevServer(comms_dir=tmpdir, output_dir=tmpdir)
        cmd = server.detect_from_structure()
        assert cmd["start"] == "npm run dev"


def test_detect_fullstack_structure():
    with tempfile.TemporaryDirectory() as tmpdir:
        os.makedirs(os.path.join(tmpdir, "frontend"))
        os.makedirs(os.path.join(tmpdir, "backend"))
        with open(os.path.join(tmpdir, "frontend", "package.json"), "w") as f:
            json.dump({"scripts": {"dev": "vite"}}, f)
        with open(os.path.join(tmpdir, "backend", "main.py"), "w") as f:
            f.write("app = None")

        server = DevServer(comms_dir=tmpdir, output_dir=tmpdir)
        cmd = server.detect_from_structure()
        assert cmd["start"] == "__fullstack__"


def test_start_stop_lifecycle():
    with tempfile.TemporaryDirectory() as tmpdir:
        server = DevServer(comms_dir=tmpdir, output_dir=tmpdir, startup_wait=0)
        server.start_cmd = "echo 'server started'"

        with patch("subprocess.Popen") as mock_popen, \
             patch("server._kill_port"):
            mock_proc = MagicMock()
            mock_proc.pid = 12345
            mock_popen.return_value = mock_proc

            with patch("os.killpg"):
                server.start()
                assert len(server.processes) == 1
                server.stop()
