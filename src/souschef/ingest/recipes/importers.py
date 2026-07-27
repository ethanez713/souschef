"""Fetch recipes from the web and turn them into library entries.

Design constraints worth stating, because they shaped the code:

**NYT Cooking has no public API**, and its terms prohibit automated scraping. So
this is built as an *on-demand, one-recipe-at-a-time* importer: you hand it a URL
you are looking at, it fetches that single page with your own session, and it
stops. There is no crawler, no search backend, and no bulk job. That keeps the
behaviour equivalent to your browser loading a page you pay for.

**Only the session cookie is stored, never the password.** NYT gates login
behind bot detection, so automating it is fragile and risks locking the account,
and a stored password would be a far bigger prize sitting on an internet-facing
box.

Every fetch is gated on ``settings.no_external`` so tests and dev runs cannot
quietly reach a real service.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from souschef.config import get_settings
from souschef.core.crypto import CredentialError, decrypt
from souschef.ingest.recipes.jsonld import RawRecipe, extract_recipe
from souschef.logconfig import get_logger
from souschef.models import RecipeSource, StoredCredential
from souschef.models.settings import CredentialKind

log = get_logger("souschef.import")

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0 Safari/537.36"
)
REQUEST_TIMEOUT = 20.0

_NYT_HOST = re.compile(r"(^|\.)cooking\.nytimes\.com$", re.IGNORECASE)
_NYT_RECIPE_ID = re.compile(r"/recipes/(\d+)")


class ImportError_(RuntimeError):
    """Import failed for a reason worth showing the user."""


class ExternalCallBlocked(ImportError_):
    """A network call was attempted while external access is disabled."""


@dataclass
class ImportResult:
    recipe: RawRecipe
    source: RecipeSource
    external_id: str | None = None


def is_nyt_url(url: str) -> bool:
    try:
        host = httpx.URL(url).host or ""
    except Exception:  # noqa: BLE001 - malformed URL is simply not NYT
        return False
    return bool(_NYT_HOST.search(host))


def _guard_external(what: str) -> None:
    if get_settings().no_external:
        raise ExternalCallBlocked(
            f"Refusing to {what}: external calls are disabled. "
            "Set SOUSCHEF_NO_EXTERNAL=false to allow live fetches."
        )


def get_nyt_cookie(session: Session, user_id: int) -> str | None:
    """Decrypt this user's stored NYT session cookie, if they configured one.

    Strictly per-user: it is someone's paid subscription, and lending it to
    another account would both leak the credential and risk NYT revoking it for
    concurrent use.
    """
    credential = session.scalar(
        select(StoredCredential).where(
            StoredCredential.user_id == user_id,
            StoredCredential.kind == CredentialKind.NYT_COOKING_COOKIE,
        )
    )
    if credential is None:
        return None
    try:
        return decrypt(credential.ciphertext)
    except CredentialError:
        # A key rotation shouldn't take the whole importer down; the caller
        # falls back to an unauthenticated fetch and the UI flags re-entry.
        log.error(
            "Stored NYT cookie could not be decrypted -- SOUSCHEF_SECRET_KEY may have "
            "changed. Re-enter the cookie in Settings."
        )
        credential.is_working = False
        return None


def fetch_html(url: str, *, cookie: str | None = None) -> str:
    """Fetch one page. The only place this module touches the network."""
    _guard_external(f"fetch {url}")

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if cookie:
        headers["Cookie"] = cookie

    log.info("Fetching %s (authenticated=%s)", url, bool(cookie))
    try:
        response = httpx.get(
            url, headers=headers, timeout=REQUEST_TIMEOUT, follow_redirects=True
        )
    except httpx.HTTPError as exc:
        log.warning("Fetch failed for %s: %s", url, exc)
        raise ImportError_(f"Could not reach {url}: {exc}") from exc

    if response.status_code == 403:
        log.warning("403 from %s (cookie present=%s)", url, bool(cookie))
        raise ImportError_(
            "The site refused the request (403). If this is a subscriber recipe, "
            "your saved session cookie may have expired -- re-enter it in Settings."
        )
    if response.status_code >= 400:
        log.warning("HTTP %d from %s", response.status_code, url)
        raise ImportError_(f"{url} returned HTTP {response.status_code}")

    log.info("Fetched %s (%d bytes)", url, len(response.content))
    return response.text


def import_from_url(session: Session, user_id: int, url: str) -> ImportResult:
    """Import a single recipe from any URL, on behalf of one user.

    Tries JSON-LD first because it is the most stable target, then falls back to
    ``recipe-scrapers``, which carries site-specific rules for pages whose markup
    is incomplete.
    """
    url = url.strip()
    if not url.lower().startswith(("http://", "https://")):
        raise ImportError_("Enter a full URL starting with http:// or https://")

    nyt = is_nyt_url(url)
    cookie = get_nyt_cookie(session, user_id) if nyt else None

    if nyt and not cookie:
        raise ImportError_(
            "NYT Cooking recipes need your session cookie. Add it in Settings -- "
            "SousChef stores the cookie only, never your password."
        )

    html = fetch_html(url, cookie=cookie)

    recipe = extract_recipe(html, source_url=url)
    if recipe is None:
        # Worth a warning: JSON-LD is the stable path, so falling through to
        # site-specific scraping is the first sign a source changed its markup.
        log.warning("No JSON-LD recipe found at %s; trying recipe-scrapers", url)
        recipe = _scrape_fallback(html, url)

    if recipe is None or not recipe.is_usable:
        log.warning("No usable recipe extracted from %s", url)
        raise ImportError_(
            "No recipe found on that page. If it is behind a paywall, check that "
            "your session cookie is current."
        )

    log.info(
        "Imported %r: %d ingredients, %d servings",
        recipe.title, len(recipe.ingredients), recipe.servings or 0,
    )

    if nyt:
        match = _NYT_RECIPE_ID.search(url)
        return ImportResult(
            recipe=recipe,
            source=RecipeSource.NYT,
            external_id=match.group(1) if match else None,
        )
    return ImportResult(recipe=recipe, source=RecipeSource.URL, external_id=None)


def _scrape_fallback(html: str, url: str) -> RawRecipe | None:
    """Second attempt via recipe-scrapers' site-specific rules."""
    try:
        from recipe_scrapers import scrape_html
    except ImportError:
        return None

    try:
        scraper = scrape_html(html, org_url=url)
    except Exception:  # noqa: BLE001 - unsupported site is an expected outcome
        return None

    def safe(getter, default=None):  # noqa: ANN001, ANN202
        try:
            return getter()
        except Exception:  # noqa: BLE001 - scrapers raise for absent fields
            return default

    ingredients = safe(scraper.ingredients, []) or []
    if not ingredients:
        return None

    return RawRecipe(
        title=safe(scraper.title, "Untitled recipe") or "Untitled recipe",
        ingredients=list(ingredients),
        instructions=safe(scraper.instructions),
        servings=_first_int(safe(scraper.yields)),
        total_minutes=safe(scraper.total_time),
        image_url=safe(scraper.image),
        author=safe(scraper.author),
        description=safe(scraper.description),
        source_url=url,
    )


def _first_int(value: object) -> int | None:
    if value is None:
        return None
    match = re.search(r"\d+", str(value))
    return int(match.group()) if match else None
