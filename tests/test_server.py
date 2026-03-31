import json
import os
import tempfile
from unittest.mock import patch, MagicMock
from server import DevServer


def test_detect_server_cmd_from_comms_json():
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "dev_server.json"), "w") as f:
            json.dump({"start": "npm run dev", "stop": "kill"}, f)
        server = DevServer(comms_dir=tmpdir, output_dir="/tmp/out")
        server.detect()
        assert server.start_cmd == "npm run dev"


def test_detect_server_cmd_from_output_json():
    with tempfile.TemporaryDirectory() as comms_dir, \
         tempfile.TemporaryDirectory() as output_dir:
        with open(os.path.join(output_dir, "dev_server.json"), "w") as f:
            json.dump({"start": "npm run dev"}, f)
        server = DevServer(comms_dir=comms_dir, output_dir=output_dir)
        server.detect()
        assert server.start_cmd == "npm run dev"


def test_detect_root_package_json():
    with tempfile.TemporaryDirectory() as tmpdir:
        with open(os.path.join(tmpdir, "package.json"), "w") as f:
            json.dump({"scripts": {"dev": "vite"}}, f)
        server = DevServer(comms_dir=tmpdir, output_dir=tmpdir)
        server.detect()
        assert server.start_cmd == "npm run dev"


def test_detect_fullstack_structure():
    with tempfile.TemporaryDirectory() as tmpdir:
        os.makedirs(os.path.join(tmpdir, "frontend"))
        os.makedirs(os.path.join(tmpdir, "backend"))
        with open(os.path.join(tmpdir, "frontend", "package.json"), "w") as f:
            json.dump({"scripts": {"dev": "vite"}}, f)
        with open(os.path.join(tmpdir, "backend", "main.py"), "w") as f:
            f.write("app = None")
        server = DevServer(comms_dir=tmpdir, output_dir=tmpdir)
        server.detect()
        assert server.start_cmd == "__fullstack__"


def test_start_stop_lifecycle():
    with tempfile.TemporaryDirectory() as tmpdir:
        server = DevServer(
            comms_dir=tmpdir, output_dir=tmpdir,
            startup_wait=0, health_timeout=0,
        )
        server.start_cmd = "echo 'server started'"

        with patch("subprocess.Popen") as mock_popen, \
             patch("server._kill_port"), \
             patch("server._kill_all_ports"), \
             patch.object(server, "_wait_until_ready"):
            mock_proc = MagicMock()
            mock_proc.pid = 12345
            mock_popen.return_value = mock_proc

            with patch("os.killpg"):
                server.start()
                assert len(server.processes) == 1
                server.stop()


def test_health_check_raises_on_timeout():
    with tempfile.TemporaryDirectory() as tmpdir:
        server = DevServer(
            comms_dir=tmpdir, output_dir=tmpdir,
            startup_wait=0, health_timeout=1,
            health_url="http://localhost:59999",
        )
        import pytest
        with pytest.raises(RuntimeError, match="not ready"):
            server._wait_until_ready()
