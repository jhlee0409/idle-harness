import json
import os


class HarnessState:
    def __init__(self, comms_dir: str):
        self.path = os.path.join(comms_dir, "status.json")
        self.comms_dir = comms_dir

    def init(self):
        os.makedirs(self.comms_dir, exist_ok=True)
        os.makedirs(os.path.join(self.comms_dir, "screenshots"), exist_ok=True)
        data = {
            "phase": "planning",
            "build_attempts": 0,
            "eval_attempts": 0,
        }
        self._save(data)

    def load(self) -> dict:
        with open(self.path, "r") as f:
            return json.load(f)

    def _save(self, data: dict):
        with open(self.path, "w") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def set_phase(self, phase: str):
        data = self.load()
        data["phase"] = phase
        self._save(data)

    def increment_build(self):
        data = self.load()
        data["build_attempts"] += 1
        self._save(data)

    def increment_eval(self):
        data = self.load()
        data["eval_attempts"] += 1
        self._save(data)
