"""Extraction du texte du CV (pypdf) + sauvegarde des artefacts par candidature."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

from pypdf import PdfReader

from .logging_setup import get_logger

log = get_logger("cv_adapter")


def extract_cv_text(pdf_path: Path, cache_dir: Path | None = None) -> str:
    """Lit un PDF et renvoie son texte concaténé. Cache disque optionnel."""
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise FileNotFoundError(f"CV introuvable : {pdf_path}")

    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        digest = _file_digest(pdf_path)
        cache_file = cache_dir / f"cv-{digest}.txt"
        if cache_file.exists():
            return cache_file.read_text(encoding="utf-8")

    reader = PdfReader(str(pdf_path))
    parts: list[str] = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception as exc:  # noqa: BLE001
            log.debug("pypdf page extract failed: %s", exc)
    text = "\n".join(p.strip() for p in parts if p and p.strip())

    if cache_dir is not None:
        cache_file.write_text(text, encoding="utf-8")  # type: ignore[possibly-undefined]
    return text


def save_artifacts(
    *,
    generated_dir: Path,
    application_id: int,
    cover_letter_text: str,
    cv_path: Path,
) -> Path:
    """Crée data/generated/app_<id>/ et y dépose la lettre + une copie du CV de base.

    Le CV de base reste intact (copie, pas déplacement).
    """
    folder = Path(generated_dir) / f"app_{application_id}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "cover_letter.txt").write_text(cover_letter_text, encoding="utf-8")
    dest_cv = folder / "cv.pdf"
    if Path(cv_path).resolve() != dest_cv.resolve():
        shutil.copyfile(cv_path, dest_cv)
    log.info("Artifacts saved for app=%d at %s", application_id, folder)
    return folder


def _file_digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()[:16]
