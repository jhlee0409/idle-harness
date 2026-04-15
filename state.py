import json
import os
import tempfile
import threading


class HarnessState:
    def __init__(self, comms_dir: str):
        self.path = os.path.join(comms_dir, "status.json")
        self.comms_dir = comms_dir
        self._lock = threading.Lock()

    def _default_data(self) -> dict:
        return {
            "run_meta": {},
            "phase": "planning",
            "current_sprint": 0,
            "total_sprints": 0,
            "sprint_attempts": {},
            "cost": {"total_usd": 0.0, "by_phase": {}},
            "timings": {"plan": 0, "sprints": []},
        }

    def init(self):
        os.makedirs(self.comms_dir, exist_ok=True)
        self._save(self._default_data())

    def load(self) -> dict:
        try:
            with open(self.path, "r") as f:
                return json.load(f)
        except json.JSONDecodeError:
            # Corrupted — backup and re-initialize
            import shutil
            backup = self.path + ".corrupted"
            try:
                shutil.copy2(self.path, backup)
            except OSError:
                pass
            import sys
            print(f"⚠ WARNING: {self.path} corrupted, backed up to {backup}, re-initializing",
                  file=sys.stderr)
            data = self._default_data()
            self._save(data)
            return data
        except FileNotFoundError:
            data = self._default_data()
            self._save(data)
            return data

    def _save(self, data: dict):
        # Atomic write: write to temp file then rename (prevents corruption on crash)
        dir_name = os.path.dirname(self.path)
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp_path, self.path)  # atomic on POSIX
        except Exception:
            # Clean up temp file if rename failed
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def set_phase(self, phase: str):
        with self._lock:
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
        with self._lock:
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

    def add_cost_usd(self, amount: float, label: str = ""):
        with self._lock:
            data = self.load()
            cost = data.setdefault("cost", {"total_usd": 0.0, "by_phase": {}})
            cost["total_usd"] = cost.get("total_usd", 0.0) + amount
            if label:
                by_phase = cost.setdefault("by_phase", {})
                by_phase[label] = by_phase.get(label, 0.0) + amount
            self._save(data)

    def set_sprint_result(self, sprint_num: int, passed: bool):
        with self._lock:
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
        with self._lock:
            data = self.load()
            data["current_sprint"] = current
            data["total_sprints"] = total
            self._save(data)

    def record_plan_time(self, seconds: int):
        with self._lock:
            data = self.load()
            data["timings"]["plan"] = seconds
            self._save(data)

    def add_eval_score(self, sprint_num: int, attempt: int, passed: int, total: int):
        """Record eval score for regression detection."""
        with self._lock:
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
        with self._lock:
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

    def set_run_meta(self, meta: dict):
        """Record run metadata (config, models, user prompt) at startup."""
        with self._lock:
            data = self.load()
            data["run_meta"] = meta
            self._save(data)

    def get_cna(self, sprint_num: int | None = None) -> float:
        """Cost-Normalized Accuracy: score / cost * 100. Higher = better.

        If sprint_num is given, uses the last eval score for that sprint
        and the total cost. Otherwise uses the overall last eval score.
        """
        data = self.load()
        scores = data.get("eval_scores", [])
        if sprint_num is not None:
            scores = [s for s in scores if s["sprint"] == sprint_num]
        if not scores:
            return 0.0
        last_score = scores[-1].get("pct", 0)
        cost = data.get("cost", {}).get("total_usd", 0.0)
        if cost <= 0:
            return 0.0
        return last_score / cost * 100

    def get_run_efficiency(self) -> dict:
        """Compute run efficiency metrics for the final report.

        Returns dict with: score_pct, cost_usd, cna, build_attempts, eval_attempts.
        """
        data = self.load()
        scores = data.get("eval_scores", [])
        last_score = scores[-1].get("pct", 0) if scores else 0
        cost = data.get("cost", {}).get("total_usd", 0.0)
        cna = last_score / cost * 100 if cost > 0 else 0.0
        return {
            "score_pct": last_score,
            "cost_usd": cost,
            "cna": round(cna, 1),
            "build_attempts": self.total("build", data),
            "eval_attempts": self.total("eval", data),
        }

    def add_eval_details(self, sprint_num: int, attempt: int,
                         failed: list[str], persistent: list[str] | None = None):
        """Record failed criteria for cross-attempt analysis."""
        with self._lock:
            data = self.load()
            details = data.setdefault("eval_details", [])
            details.append({
                "sprint": sprint_num,
                "attempt": attempt,
                "failed_criteria": failed,
                "persistent_failures": persistent or [],
            })
            self._save(data)
