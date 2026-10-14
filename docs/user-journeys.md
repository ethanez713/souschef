# SousChef regular-usage user journeys

Steady-state paths — how the app is used week to week once it's set up. Not
first-run onboarding. Each story names the routes that serve it and whether the
app supports it today, so the gaps are concrete things to build.

Two users, each with their own kitchen (shared ingredient catalog only):
- **Jordan** — primary cook, not a chef. Wants to be told what to make and what to
  buy; will not maintain a full inventory or log chores.
- **Ethan** — second kitchen, same patterns.

The design promise these journeys protect: *the inventory is allowed to be wrong,
and the app knows how wrong.* A journey that forces Jordan to keep the count perfect
is a failed journey, not a missing feature.

## The weekly loop (the core rhythm)

1. **Pick this week's meals.** As Jordan, on Sunday, I open `/` (home) or `/plan` and
   browse **Cook now / Nearly there / Bigger shop**, tapping meals into the plan
   (`/plan/add`). Ranked by what's on hand, what's expiring, and what I haven't
   cooked lately. → *Supported* (`suggest.py`, dashboard carousels, plan chips).

2. **Check only the uncertain things before shopping.** Before I shop, I open
   `/audit`; it lists the handful of items whose uncertainty would change what I
   buy. I walk to the cupboard and confirm or correct each (`/audit/{id}`). → *Supported*
   (audit ranks by P(wrong) × cost; staples never appear).

3. **Shop the list.** I generate the list (`/shopping/generate`): grouped by aisle,
   rounded to real package sizes, each line saying which meal it's for. I tick items
   off as I go (`/shopping/item/{id}/check`). → *Supported*.

4. **Restock from the receipt.** Home from the store, I photograph the receipt
   (`/receipts/upload`), review the matched lines (`/receipts/{id}`), fix any bad
   match (`/receipts/{id}/line/{id}`, which teaches an alias for next time), and
   apply (`/receipts/{id}/apply`). Pantry goes up; nothing changes until I confirm. → *Supported*.

5. **Cook and let it debit.** When I cook a planned meal I open its cook preview
   (`/recipes/{id}/cook`), nudge quantities to what I actually used, and tap
   **I cooked this** (`/recipes/{id}/cooked`). Levels drop; confidence on estimated
   (bulk) items falls, staples are never debited or listed. → *Supported*.

## Pantry maintenance (between loops, ad hoc)

6. **"I know I'm out of that."** I open `/pantry`, live-search the item, set its
   level (`/pantry/{id}/level`). → *Supported* (search survives the edit).

7. **"I just looked — it's fine."** I physically see I have plenty; I tap confirm
   (`/pantry/{id}/confirm`) to restore confidence to full without changing the
   number. → *Supported*.

8. **Bought something with no receipt.** Farmstand produce, a corner-shop top-up:
   I add it verbatim (`/pantry/add`). → *Supported* (added as-is, not fuzzy-coerced).

9. **"Always keep me stocked on this."** I want chicken stock or rice to land on the
   list whenever it runs low, even if no planned recipe needs it. → **Gap.** The
   model and audit/shopping already honor `restock_threshold`, but there is no route
   to *set* one from the UI. See gaps below.

## Recipe curation (ongoing)

10. **Import a recipe I found.** From NYT Cooking I import via `/recipes/import`
    (using my stored cookie); from my phone browser I share the URL to the app via
    the share target (`/share`). → *Supported*.

11. **"What can I make right now?"** I have 20 minutes and want dinner from what's
    already here — I open `/` and read the **Cook now** row. → *Supported*.

## The messy middle (off-loop reality — where a non-chef most needs support)

12. **Cooked something off-plan.** I made a recipe that wasn't in the plan but still
    want the pantry debited: open `/recipes/{id}` → cook → cooked. → *Supported*
    (cooking doesn't require plan membership).

13. **Ate out / skipped a meal.** A planned meal didn't happen; I remove it
    (`/plan/{id}/remove`) so the plan and list stop assuming it. → *Supported*.

14. **Used more than the recipe said.** The pan took an extra glug; I edit actual
    quantities in the cook preview before debiting. → *Supported*.

15. **Something spoiled.** The spinach went off and I binned it. → **Gap.** The
    ledger has `EventReason.WASTE` but no UI writes it, so the level stays wrong
    until the next audit or confirm catches it. For a drift-aware app this is the
    most natural missing path. See gaps below.

## The two-kitchen reality

16. **Bring the other person in.** As Jordan I mint an invite (`/settings/invites`);
    Ethan joins (`/join`) and gets a separate kitchen. → *Supported*.

17. **Kitchens stay independent.** Each has its own pantry, plan, list, recipes and
    NYT cookie; only the ingredient catalog is shared. → *Supported* (user-scoped
    queries).

## Things the user should NOT have to do (the promise, stated as non-journeys)

- Maintain a complete, accurate inventory.
- Log staple restocks (salt, oil, vinegar) — Jordan buys those himself; they never hit
  a list and never need entering.
- Re-confirm items the audit didn't flag.
- Manually decrement anything after cooking — the cook button is the only debit.

If a proposed feature reintroduces one of these chores, it's working against the
design.

## Gaps to close (concrete paths not yet supported)

- **Set a restock floor (story 9).** Add a control on `/pantry` to set/clear
  `restock_threshold` per item. The downstream logic already consumes it.
- **Log waste/spoilage (story 15).** Add a "threw this out" action on `/pantry` (or
  the item row) writing a `WASTE` event — quantity down, and it counts as a physical
  look, so confidence should reset like a confirm.
- **Editable servings (stories 1, 5).** Plan and cook currently use the recipe's
  default servings (hidden field). Let the user scale servings when planning a meal
  or cooking it, so the debit and the shopping quantities match reality.
