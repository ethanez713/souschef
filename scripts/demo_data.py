"""Fill a throwaway SousChef database with invented demo data for screenshots.

Refuses to run unless SOUSCHEF_DATA_DIR points under the system temp dir, so it can
never write into a real kitchen. Run `souschef init` against the same environment
first, then this, then `souschef serve`:

    export SOUSCHEF_DATA_DIR=$(mktemp -d) SOUSCHEF_CONFIG_DIR=$SOUSCHEF_DATA_DIR/config \
           SOUSCHEF_NO_EXTERNAL=true
    souschef init && python scripts/demo_data.py && souschef serve
"""

from __future__ import annotations

import tempfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from souschef.config import get_settings
from souschef.core.inventory import record_event
from souschef.core.recipes import save_imported_recipe
from souschef.db import session_scope
from souschef.ingest.recipes.jsonld import RawRecipe
from souschef.models import EventReason, MealPlanEntry, MealSlot, Recipe, RecipeSource
from souschef.planning.shopping import build_shopping_list
from souschef.web.auth import sole_user

RECIPES = [
    RawRecipe(
        title="Chicken tikka masala",
        servings=4,
        total_minutes=50,
        ingredients=[
            "1 1/2 lb boneless chicken thighs",
            "1 cup plain yogurt",
            "2 tbsp garam masala",
            "1 yellow onion",
            "4 cloves garlic",
            "1 tbsp grated ginger",
            "1 (14 oz) can crushed tomatoes",
            "1/2 cup heavy cream",
            "1 1/2 cups basmati rice",
        ],
        instructions="Marinate the chicken in yogurt and spices. Brown, then simmer in the sauce.",
    ),
    RawRecipe(
        title="Lemon garlic spaghetti",
        servings=2,
        total_minutes=20,
        ingredients=[
            "8 oz spaghetti",
            "3 tbsp olive oil",
            "4 cloves garlic",
            "1 lemon",
            "1/2 cup grated parmesan",
            "1 cup baby spinach",
        ],
        instructions="Cook the pasta. Toss with garlic oil, lemon and parmesan.",
    ),
    RawRecipe(
        title="Black bean tacos",
        servings=4,
        total_minutes=25,
        ingredients=[
            "2 (15 oz) cans black beans",
            "8 corn tortillas",
            "1 red onion",
            "1 avocado",
            "1 lime",
            "1/2 cup sour cream",
            "1 tsp ground cumin",
            "1 cup shredded cheddar",
        ],
        instructions="Warm the beans with cumin. Fill the tortillas.",
    ),
    RawRecipe(
        title="Shakshuka",
        servings=2,
        total_minutes=30,
        ingredients=[
            "6 eggs",
            "1 (28 oz) can whole tomatoes",
            "1 red bell pepper",
            "1 yellow onion",
            "2 cloves garlic",
            "1 tsp smoked paprika",
            "2 tbsp olive oil",
            "1/4 cup feta",
        ],
        instructions="Soften the pepper and onion, add tomatoes, then poach the eggs.",
    ),
    RawRecipe(
        title="Mushroom risotto",
        servings=4,
        total_minutes=45,
        ingredients=[
            "1 1/2 cups arborio rice",
            "1 lb cremini mushrooms",
            "6 cups chicken broth",
            "1 shallot",
            "1/2 cup dry white wine",
            "3 tbsp butter",
            "1/2 cup grated parmesan",
        ],
        instructions="Toast the rice, add broth a ladle at a time, finish with parmesan.",
    ),
    RawRecipe(
        title="Thai green curry",
        servings=4,
        total_minutes=35,
        ingredients=[
            "1 lb chicken breast",
            "1 (13.5 oz) can coconut milk",
            "3 tbsp green curry paste",
            "1 red bell pepper",
            "1 cup green beans",
            "1 tbsp fish sauce",
            "1 1/2 cups jasmine rice",
        ],
        instructions="Fry the paste, add coconut milk and chicken, then the vegetables.",
    ),
]

# (recipe line prefix, multiple of the recipe quantity bought, days ago, confidence).
# Old or low-confidence stock is what the pre-shop check asks about.
STOCK = [
    ("1 1/2 cups basmati", 4, 40, 0.9),
    ("1 cup plain yogurt", 1, 9, 1.0),
    ("4 cloves garlic", 3, 12, 1.0),
    ("1/2 cup heavy cream", 2, 21, 0.6),
    ("8 oz spaghetti", 2, 30, 1.0),
    ("3 tbsp olive oil", 10, 60, 0.7),
    ("1/2 cup grated parmesan", 1, 18, 1.0),
    ("6 eggs", 2, 6, 1.0),
    ("1 tsp smoked paprika", 8, 200, 0.8),
    ("3 tbsp butter", 3, 14, 1.0),
    ("6 cups chicken broth", 1, 50, 0.6),
    ("1 tsp ground cumin", 10, 150, 0.8),
]
PLAN = [
    "Chicken tikka masala",
    "Lemon garlic spaghetti",
    "Shakshuka",
    "Black bean tacos",
    "Mushroom risotto",
]


def _assert_throwaway() -> None:
    data = get_settings().data_dir.resolve()
    tmp = Path(tempfile.gettempdir()).resolve()
    if tmp not in data.parents:
        raise SystemExit(f"refusing to run: SOUSCHEF_DATA_DIR ({data}) is not under {tmp}")


def main() -> None:
    _assert_throwaway()
    now = datetime.now(UTC)
    today = date.today()
    with session_scope() as db:
        user = sole_user(db)
        recipes = {
            r.title: save_imported_recipe(db, user.id, r, source=RecipeSource.MANUAL)
            for r in RECIPES
        }
        lines = [line for recipe in recipes.values() for line in recipe.ingredients]
        for prefix, multiple, days_ago, confidence in STOCK:
            line = next(x for x in lines if x.raw_text.startswith(prefix))
            if line.ingredient is None or line.quantity is None:
                raise SystemExit(f"demo recipe line did not resolve: {prefix!r}")
            record_event(
                db,
                user.id,
                line.ingredient,
                reason=EventReason.PURCHASE,
                delta=line.quantity * multiple,
                unit=line.unit,
                event_confidence=confidence,
                occurred_at=now - timedelta(days=days_ago),
            )
        for offset, title in enumerate(PLAN):
            recipe: Recipe = recipes[title]
            db.add(
                MealPlanEntry(
                    user_id=user.id,
                    plan_date=today + timedelta(days=offset),
                    slot=MealSlot.DINNER,
                    recipe_id=recipe.id,
                    servings=recipe.servings or 4,
                )
            )
        db.flush()
        build_shopping_list(db, user.id, covers_from=today, covers_to=today + timedelta(days=6))
    print(
        f"demo data: {len(RECIPES)} recipes, {len(STOCK)} pantry purchases, "
        f"{len(PLAN)} planned dinners"
    )


if __name__ == "__main__":
    main()
