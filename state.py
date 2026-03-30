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
            "current_sprint": 0,
            "total_sprints": 0,
            "timings": {"plan": 0, "sprints": []},
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

    def set_sprint_info(self, current: int, total: int):
        data = self.load()
        data["current_sprint"] = current
        data["total_sprints"] = total
        self._save(data)

    def record_plan_time(self, seconds: int):
        data = self.load()
        data["timings"]["plan"] = seconds
        self._save(data)

    def add_sprint_timing(self, sprint_num: int, phase: str, seconds: int):
        data = self.load()
        sprints = data["timings"]["sprints"]

        entry = None
        for s in sprints:
            if s["sprint"] == sprint_num:
                entry = s
                break
        if entry is None:
            entry = {"sprint": sprint_num}
            sprints.append(entry)

        if phase == "negotiate":
            entry["negotiate"] = seconds
        else:
            if phase not in entry:
                entry[phase] = []
            entry[phase].append(seconds)

        self._save(data)
