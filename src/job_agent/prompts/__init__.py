"""Templates de prompts (fichiers .txt) injectés via str.replace."""

from __future__ import annotations

from pathlib import Path

_PROMPTS_DIR = Path(__file__).parent


def load(name: str) -> str:
    """Lit un fichier prompt et le renvoie en str."""
    return (_PROMPTS_DIR / f"{name}.txt").read_text(encoding="utf-8")


def render(name: str, **variables: object) -> str:
    """Charge un prompt et remplace les placeholders {{var}} par leur valeur."""
    text = load(name)
    for key, value in variables.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text
