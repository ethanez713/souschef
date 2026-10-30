# UX journal 3 — mobile pass, open findings

Driven on a 412×891 phone viewport (portrait and landscape, light and dark) against
`scripts/demo_data.py`, plus an empty database. Fixed findings are removed as they land;
what remains is waiting on a decision or on a plan phase.

### 1. Recipe page says "have none" for stock the pantry lists — S2 — bug
Where: recipe page ingredient rows, for stock flagged probably gone (old heavy cream).
Steps: open Chicken tikka masala; open Pantry.
Expected / actual: one story about the cream. Recipe: "Missing · need ½ cup · have none". Pantry
and the cook screen: "1 cup" with a red `?`. `test_recipe_page_agrees_with_the_list_about_spoiled_stock`
pins "have none" and forbids the words "probably gone", so changing it reverses that decision.

### 2. Cook screen is a 1,900 px scroll of tall cards — S2 — friction
Where: `/recipes/{id}/cook`, 9 tracked ingredients.
Steps: Home → Cook.
Expected / actual: the debit check fits about a screen; each ingredient is a ~180 px card (name,
have, warning chip, label, full-width box), and "Subtract from pantry" is at the very bottom.
Phase 13 redesigns this screen.

### 3. Tapping a pantry item does nothing — S3 — missing
Where: Pantry rows.
Steps: tap "butter".
Expected / actual: a sheet with Threw it out, restock floor, history; the row highlights and
nothing opens. Phase 10.

### 4. Recipe rows say the gap twice, in two numbers — S3 — friction
Where: Recipes list. Steps: look at Mushroom risotto.
Expected / actual: one count. Sub-line "Needs 3 things · 1 line not identified" beside a
"Missing 4" chip. Phase 15 vocabulary sweep.

### 5. Shop sheet unit says "count" for a row that says "2 cans" — S3 — friction
Where: Shopping → tap black beans. Expected / actual: Unit "can"; shows "count".

### 6. Onions have no noun on the list — S4 — visual
Where: Shopping, Produce. Expected / actual: "1 onion"; shows "1×" and "2×".

### 7. Wine sold as "1 × 3⅛ cups" — S4 — visual
Where: Shopping, Beer & wine. Expected / actual: "1 bottle (750 ml)"; shows the recipe-sized
package.
