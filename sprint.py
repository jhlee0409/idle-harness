import re
from dataclasses import dataclass, field


@dataclass
class Sprint:
    number: int
    name: str
    features: list[str] = field(default_factory=list)
    goal: str = ""


def parse_sprints(spec: str) -> list[Sprint]:
    """Parse ## Sprints section from spec. Falls back to single sprint if absent."""
    sprints_match = re.search(r"^## Sprints\s*$", spec, re.MULTILINE)
    if not sprints_match:
        return [Sprint(number=1, name="Full Build")]

    sprints_text = spec[sprints_match.end():]
    sprint_blocks = re.split(r"^### Sprint (\d+):\s*(.+)$", sprints_text, flags=re.MULTILINE)

    # split gives: [preamble, num1, name1, body1, num2, name2, body2, ...]
    results = []
    i = 1
    while i < len(sprint_blocks) - 2:
        number = int(sprint_blocks[i])
        name = sprint_blocks[i + 1].strip()
        body = sprint_blocks[i + 2]

        features = []
        features_match = re.search(r"^Features:\s*(.+)$", body, re.MULTILINE)
        if features_match:
            features = [f.strip() for f in features_match.group(1).split(",")]

        goal = ""
        goal_match = re.search(r"^Goal:\s*(.+)$", body, re.MULTILINE)
        if goal_match:
            goal = goal_match.group(1).strip()

        results.append(Sprint(number=number, name=name, features=features, goal=goal))
        i += 3

    return results if results else [Sprint(number=1, name="Full Build")]
