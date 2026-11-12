"""Public source metadata shared by rating, freshness and the proposal agent."""
import logging
from typing import Any
from app.core.excel_ingestion import load_profiles

logger = logging.getLogger(__name__)
# Words in the equipment description that mean a protective design (coated or corrosion-
# resistant tubing, inhibitor containers). Profiles may override them ("protection_keywords").
_DEFAULT_PROTECTION_WORDS = ["покрыт", "коррозионностойк", "нержав", "стеклопластик", "футеров",
                             "протектор", "контейнер ингибитора", "ингибиторный контейнер"]
_DEFAULT_WORK_TYPES = {
    "install": ["спуск оборудования", "спуск", "монтаж оборудования"],
    "demount": ["демонтаж", "акт демонтажа", "подъем оборудования"],
    "base_inspection": ["разбор на базе", "акт разбора", "разбор", "дефектовка на базе"],
}


def normalize_label(value: Any) -> str:
    return str(value or "").strip().casefold().replace("ё", "е")


class SourceCatalog:
    """Source titles, laboratory sources and ERA work-type keywords from the profiles."""

    def __init__(self) -> None:
        try:
            profiles = load_profiles()
        except Exception:  # a broken profile file must not stop the rating
            logger.exception("Source profiles could not be loaded for the rating")
            profiles = {}
        self.titles: dict[str, str] = {}
        self.lab_ids: set[str] = set()
        work_types: dict[str, list[str]] = {}
        protection_words: list[str] = []
        for profile_id, profile in profiles.items():
            source_id = str(profile.get("source_id", profile_id)).upper()
            self.titles[source_id] = str(profile.get("short_title") or profile.get("title") or source_id)
            if profile.get("lab_samples"):
                self.lab_ids.add(source_id)
            for key, words in (profile.get("rating_work_types") or {}).items():
                work_types.setdefault(key, []).extend(normalize_label(word) for word in words)
            protection_words.extend(normalize_label(word) for word in profile.get("protection_keywords") or [])
        # Data taken from documents (acts, lab protocols, passports) is stored under the
        # document type's source_id; lab protocols count as laboratory samples.
        from app.core.documents.types import lab_source_ids, source_titles

        self.titles.update(source_titles())
        self.lab_ids |= lab_source_ids()
        self.protection_words = protection_words or [normalize_label(word) for word in _DEFAULT_PROTECTION_WORDS]
        self.work_types = {key: work_types.get(key) or _DEFAULT_WORK_TYPES[key] for key in _DEFAULT_WORK_TYPES}

    def title(self, source_id: str | None) -> str:
        parts = [part for part in str(source_id or "").split("+") if part]
        return " + ".join(self.titles.get(part, part) for part in parts) or "источник не указан"

    def classify(self, work_type: Any) -> str | None:
        text = normalize_label(work_type)
        if not text:
            return None
        # Base inspection first: «разбор на базе после демонтажа» is a base inspection.
        for key in ("base_inspection", "demount", "install"):
            if any(word and word in text for word in self.work_types[key]):
                return key
        return None

    def protective_equipment(self, equipment: Any) -> bool:
        text = normalize_label(equipment)
        return bool(text) and any(word in text for word in self.protection_words)

