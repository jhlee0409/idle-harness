import json
import os


class HarnessState:
    def __init__(self, comms_dir: str):
        self.path = os.path.join(comms_dir, "status.json")
        self.comms_dir = comms_dir

    def init(self):
        os.makedirs(self.comms_dir, exist_ok=True)
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

    def _ensure_sprint_entry(self, data: dict, sprint_num: int) -> dict:
        key = str(sprint_num)
        attempts = data.setdefault("sprint_attempts", {})
        if key not in attempts:
            attempts[key] = {"build": 0, "eval": 0}
        return attempts[key]

    def increment(self, sprint_num: int, phase: str):
        data = self.load()
        entry = self._ensure_sprint_entry(data, sprint_num)
        entry[phase] += 1
        self._save(data)

    def get_sprint_attempt(self, sprint_num: int, phase: str) -> int:
        data = self.load()
        entry = self._ensure_sprint_entry(data, sprint_num)
        return entry.get(phase, 0)

    def total(self, phase: str, data: dict | None = None) -> int:
        if data is None:
            data = self.load()
        return sum(
            e.get(phase, 0)
            for e in data.get("sprint_attempts", {}).values()
        )

    def add_cost_usd(self, amount: float):
        data = self.load()
        cost = data.setdefault("cost", {})
        cost["total_usd"] = cost.get("total_usd", 0.0) + amount
        self._save(data)

    def add_cost(self, input_tokens: int, output_tokens: int, label: str = ""):
        data = self.load()
        cost = data.setdefault("cost", {"input_tokens": 0, "output_tokens": 0, "by_phase": {}})
        cost["input_tokens"] += input_tokens
        cost["output_tokens"] += output_tokens
        if label:
            by_phase = cost.setdefault("by_phase", {})
            phase_cost = by_phase.setdefault(label, {"input_tokens": 0, "output_tokens": 0})
            phase_cost["input_tokens"] += input_tokens
            phase_cost["output_tokens"] += output_tokens
        self._save(data)

    def set_sprint_result(self, sprint_num: int, passed: bool):
        data = self.load()
        results = data.setdefault("sprint_results", {})
        results[str(sprint_num)] = passed
        self._save(data)

    def get_sprint_results(self, data: dict | None = None) -> dict[int, bool]:
        if data is None:
            data = self.load()
        return {
            int(k): v
            for k, v in data.get("sprint_results", {}).items()
        }

    def set_sprint_info(self, current: int, total: int):
        data = self.load()
        data["current_sprint"] = current
        data["total_sprints"] = total
        self._save(data)

    def record_plan_time(self, seconds: int):
        data = self.load()
        data["timings"]["plan"] = seconds
        self._save(data)

    def add_eval_score(self, sprint_num: int, attempt: int, passed: int, total: int):
        """Record eval score for regression detection."""
        data = self.load()
        scores = data.setdefault("eval_scores", [])
        pct = int(passed / total * 100) if total > 0 else 0
        scores.append({
            "sprint": sprint_num,
            "attempt": attempt,
            "passed": passed,
            "total": total,
            "pct": pct,
        })
        self._save(data)

    def get_eval_scores(self, sprint_num: int | None = None) -> list[dict]:
        """Get eval score history, optionally filtered by sprint."""
        data = self.load()
        scores = data.get("eval_scores", [])
        if sprint_num is not None:
            scores = [s for s in scores if s["sprint"] == sprint_num]
        return scores

    def get_last_eval_score(self, sprint_num: int) -> dict | None:
        """Get the most recent eval score for a sprint."""
        scores = self.get_eval_scores(sprint_num)
        return scores[-1] if scores else None

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
