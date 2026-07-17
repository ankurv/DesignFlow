"""Validated, versioned application prompt templates."""
from __future__ import annotations

import json
from pathlib import Path
from string import Template


class PromptCatalog:
    def __init__(self, root: Path | None = None):
        self.root = root or Path(__file__).with_name("prompts")
        self.manifest = json.loads((self.root / "manifest.json").read_text())
        self._templates: dict[str, str] = {}
        self.validate()

    def validate(self) -> None:
        prompts = self.manifest.get("prompts", {})
        if not prompts:
            raise ValueError("Prompt manifest contains no prompts")
        for name, spec in prompts.items():
            text = (self.root / spec["file"]).read_text()
            for marker in spec.get("required_markers", []):
                if marker not in text:
                    raise ValueError(f"Prompt {name} is missing required marker: {marker}")
            declared = set(spec.get("placeholders", []))
            found = {match[1] or match[2] for match in Template.pattern.findall(text) if match[1] or match[2]}
            if found != declared:
                raise ValueError(f"Prompt {name} placeholders differ: declared={sorted(declared)} found={sorted(found)}")
            self._templates[name] = text.strip()

    def render(self, name: str, **values) -> str:
        spec = self.manifest["prompts"].get(name)
        if not spec:
            raise KeyError(f"Unknown prompt: {name}")
        expected = set(spec.get("placeholders", []))
        supplied = set(values)
        if supplied != expected:
            raise ValueError(f"Prompt {name} values differ: expected={sorted(expected)} supplied={sorted(supplied)}")
        return Template(self._templates[name]).substitute({key: str(value) for key, value in values.items()})

    def text(self, name: str) -> str:
        return self.render(name)

    def version(self, name: str) -> str:
        return str(self.manifest["prompts"][name]["version"])

    def versions(self) -> dict[str, str]:
        """Return the immutable prompt-version snapshot used by a run."""
        return {name: str(spec["version"]) for name, spec in self.manifest["prompts"].items()}


prompt_catalog = PromptCatalog()
