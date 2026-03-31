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
            "current_sprint": 0,
            "total_sprints": 0,
            "sprint_attempts": {},
            "cost": {"input_tokens": 0, "output_tokens": 0},
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

    # --- Per-sprint attempt tracking ---

    def _sprint_key(self, sprint_num: int) -> str:
        return str(sprint_num)

    def _ensure_sprint_entry(self, data: dict, sprint_num: int) -> dict:
        key = self._sprint_key(sprint_num)
        attempts = data.setdefault("sprint_attempts", {})
        if key not in attempts:
            attempts[key] = {"build": 0, "eval": 0}
        return attempts[key]

    def increment_build(self, sprint_num: int):
        data = self.load()
        entry = self._ensure_sprint_entry(data, sprint_num)
        entry["build"] += 1
        self._save(data)

    def increment_eval(self, sprint_num: int):
        data = self.load()
        entry = self._ensure_sprint_entry(data, sprint_num)
        entry["eval"] += 1
        self._save(data)

    def get_sprint_attempt(self, sprint_num: int, phase: str) -> int:
        data = self.load()
        entry = self._ensure_sprint_entry(data, sprint_num)
        return entry.get(phase, 0)

    def total_builds(self) -> int:
        data = self.load()
        return sum(
            e["build"]
            for e in data.get("sprint_attempts", {}).values()
        )

    def total_evals(self) -> int:
        data = self.load()
        return sum(
            e["eval"]
            for e in data.get("sprint_attempts", {}).values()
        )

    # --- Cost tracking ---

    def add_cost(self, input_tokens: int, output_tokens: int):
        data = self.load()
        cost = data.setdefault("cost", {"input_tokens": 0, "output_tokens": 0})
        cost["input_tokens"] += input_tokens
        cost["output_tokens"] += output_tokens
        self._save(data)

    # --- Sprint info ---

    def set_sprint_info(self, current: int, total: int):
        data = self.load()
        data["current_sprint"] = current
        data["total_sprints"] = total
        self._save(data)

    # --- Timing ---

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
