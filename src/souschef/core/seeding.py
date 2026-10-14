"""Load the canonical ingredient catalog into the database.

Idempotent: re-running updates the physical facts on existing rows and adds any
new aliases without duplicating or clobbering aliases the user has learned.
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from souschef.config import REPO_ROOT
from souschef.core.matching import normalize
from souschef.models import CanonicalIngredient, IngredientAlias, IngredientCategory, TrackingMode
from souschef.models.ingredient import AliasSource

SEED_PATH = REPO_ROOT / "data" / "seeds" / "ingredients.json"


def load_seed_file(path: Path | None = None) -> list[dict]:
    path = path or SEED_PATH
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload["ingredients"]


def seed_ingredients(session: Session, path: Path | None = None) -> tuple[int, int, int]:
    """Insert or update canonical ingredients and their seed aliases.

    Returns ``(created, updated, aliases_added)``.
    """
    records = load_seed_file(path)
    created = updated = aliases_added = 0

    existing = {
        normalize(ing.name): ing
        for ing in session.scalars(select(CanonicalIngredient))
    }

    for rec in records:
        key = normalize(rec["name"])
        ing = existing.get(key)

        if ing is None:
            ing = CanonicalIngredient(name=rec["name"])
            session.add(ing)
            existing[key] = ing
            created += 1
        else:
            updated += 1

        ing.category = IngredientCategory(rec.get("category", "other"))
        ing.aisle = rec.get("aisle")
        ing.tracking_mode = TrackingMode(rec.get("tracking_mode", "bulk"))
        ing.base_unit = rec.get("base_unit", "g")
        ing.density_g_per_ml = rec.get("density_g_per_ml")
        ing.grams_per_count = rec.get("grams_per_count")
        ing.package_qty = rec.get("package_qty")
        ing.package_unit = rec.get("package_unit")
        ing.package_noun = rec.get("package_noun")
        ing.package_source = rec.get("package_source")
        ing.count_noun = rec.get("count_noun")
        ing.shelf_life_days = rec.get("shelf_life_days")
        ing.is_staple = bool(rec.get("is_staple", False))

        session.flush()  # assign ing.id for the alias rows

        # The canonical name is itself an alias, so exact lookups hit the index.
        alias_texts = {normalize(rec["name"])}
        alias_texts.update(normalize(a) for a in rec.get("aliases", []))
        alias_texts.discard("")

        aliases = session.scalars(
            select(IngredientAlias).where(IngredientAlias.ingredient_id == ing.id)
        ).all()
        # A seed alias the seed file dropped would keep resolving to the old food
        # ("smoked paprika" stayed paprika after it became its own ingredient).
        # Aliases a user taught are theirs and are never pruned.
        for alias in aliases:
            if alias.source == AliasSource.SEED and alias.normalized_text not in alias_texts:
                session.delete(alias)
        known = {a.normalized_text for a in aliases}
        for text in alias_texts - known:
            session.add(
                IngredientAlias(
                    ingredient_id=ing.id,
                    normalized_text=text,
                    raw_text=text,
                    source=AliasSource.SEED,
                    confidence=1.0,
                )
            )
            aliases_added += 1

    session.flush()
    return created, updated, aliases_added
