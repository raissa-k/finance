"""AI-assisted transaction categorization.

Given a batch of parsed bank transactions plus the user's category/payee lists,
an LLM suggests a category and payee for each. Two providers are supported and
selected from whichever API key is configured (env / .env.local, never
hardcoded):

* ``anthropic`` -- Claude (default model: Haiku, the cheapest tier), via the
  official ``anthropic`` SDK.
* ``gemini``    -- Google Gemini (developer key), via a plain HTTPS call (no
  extra SDK dependency, matching the currency-rate fetch pattern).

The model returns a category *by name* (reusing an existing one when it fits, or
proposing a new one, optionally with a parent). The backend resolves names to
existing category ids; unmatched names come back so the client can create the
category/subcategory on import. The user always reviews before saving.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from sqlalchemy.orm import Session

from app.config import settings
from app.models import Category, Payee

logger = logging.getLogger("ai_categorize")

CHUNK_SIZE = 60

SYSTEM_PROMPT = (
    "You categorize personal bank transactions for a finance app. Bank "
    "descriptions are mostly Brazilian Portuguese (Nubank and Santander). "
    "You are given the existing categories and known payees. For each "
    "transaction choose the best category and extract the counterparty "
    "(payee).\n"
    "Rules:\n"
    "- Prefer an existing category, returned exactly as shown. If none fits, "
    "propose a concise new category name (and a parent when it is naturally a "
    "sub-category, e.g. parent 'Food', category 'Groceries').\n"
    "- amount is signed: negative = money leaving the account (expense), "
    "positive = money arriving (income). Use the sign to disambiguate (e.g. "
    "'PIX RECEBIDO' is income, 'PIX ENVIADO' is an expense).\n"
    "- For payee, reuse a known payee name verbatim when the transaction "
    "clearly involves them; otherwise return a cleaned human/company name "
    "(Title Case, no CPF/CNPJ/account numbers). Use null when there is no "
    "meaningful counterparty (bank fees, interest, internal yield like 'RDB').\n"
    "- confidence is 0.0-1.0.\n"
    "Respond with ONLY a JSON array, one object per transaction, in the same "
    "order, shaped exactly: "
    '{"index": <int>, "category": <string|null>, "parent": <string|null>, '
    '"payee": <string|null>, "confidence": <number>}. '
    "category is the (leaf) category name; parent is its parent category name "
    "or null. No prose, no code fences."
)


class AICategorizationError(RuntimeError):
    pass


def resolve_provider() -> str | None:
    choice = (settings.ai_provider or "").strip().lower()
    if choice == "anthropic" and settings.anthropic_api_key:
        return "anthropic"
    if choice == "gemini" and settings.gemini_api_key:
        return "gemini"
    if not choice:
        if settings.anthropic_api_key:
            return "anthropic"
        if settings.gemini_api_key:
            return "gemini"
    return None


def is_enabled() -> bool:
    return resolve_provider() is not None


def active_model() -> str | None:
    provider = resolve_provider()
    if provider == "anthropic":
        return settings.anthropic_model
    if provider == "gemini":
        return settings.gemini_model
    return None


def _fold(text: str) -> str:
    return " ".join(str(text).strip().lower().split())


def _category_context(db: Session):
    cats = db.query(Category).filter(Category.is_hidden == False).all()  # noqa: E712
    by_id = {c.category_id: c for c in cats}
    options = []
    lookup: dict[str, int] = {}  # normalized name -> id
    for c in cats:
        parent = by_id.get(c.parent_category_id) if c.parent_category_id else None
        full = f"{parent.name}: {c.name}" if parent else c.name
        options.append(full)
        lookup[_fold(full)] = c.category_id
        # also index by leaf name (first wins) so a bare leaf still resolves
        lookup.setdefault(_fold(c.name), c.category_id)
    return options, lookup


def _payee_names(db: Session) -> list[str]:
    return [p.name for p in db.query(Payee).order_by(Payee.name).all()]


def _extract_json_array(text: str) -> list:
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.split("```", 2)[1] if text.count("```") >= 2 else text
        if text.lstrip().lower().startswith("json"):
            text = text.lstrip()[4:]
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        raise AICategorizationError("Model did not return a JSON array")
    return json.loads(text[start : end + 1])


def _call_anthropic(context_text: str, txns_text: str, max_tokens: int) -> str:
    try:
        import anthropic  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise AICategorizationError(
            "The 'anthropic' package is required for the Anthropic provider."
        ) from exc

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    try:
        response = client.messages.create(
            model=settings.anthropic_model,
            max_tokens=max_tokens,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": context_text,
                            # stable across chunks/imports -> cache it
                            "cache_control": {"type": "ephemeral"},
                        },
                        {"type": "text", "text": txns_text},
                    ],
                }
            ],
        )
    except anthropic.APIError as exc:
        raise AICategorizationError(f"Anthropic API error: {exc}") from exc
    return "".join(b.text for b in response.content if b.type == "text")


def _call_gemini(context_text: str, txns_text: str) -> str:
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{settings.gemini_model}:generateContent?key={settings.gemini_api_key}"
    )
    body = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [
            {"role": "user", "parts": [{"text": context_text + "\n\n" + txns_text}]}
        ],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(  # noqa: S310 (fixed https host)
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise AICategorizationError(f"Gemini API error ({exc.code}): {detail}") from exc
    except urllib.error.URLError as exc:
        raise AICategorizationError(f"Gemini request failed: {exc}") from exc

    try:
        return payload["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as exc:
        raise AICategorizationError("Unexpected Gemini response shape") from exc


def _classify_chunk(provider, lookup, chunk, context_text) -> list[dict]:
    txns_text = "Transactions to categorize:\n" + json.dumps(chunk, ensure_ascii=False)
    if provider == "anthropic":
        raw = _call_anthropic(context_text, txns_text, min(16000, 400 + 80 * len(chunk)))
    else:
        raw = _call_gemini(context_text, txns_text)

    parsed = _extract_json_array(raw)
    results = []
    for item in parsed:
        if not isinstance(item, dict) or "index" not in item:
            continue
        category = item.get("category")
        parent = item.get("parent")
        category = category.strip() if isinstance(category, str) and category.strip() else None
        parent = parent.strip() if isinstance(parent, str) and parent.strip() else None

        category_id = None
        if category:
            full = f"{parent}: {category}" if parent else category
            category_id = lookup.get(_fold(full)) or lookup.get(_fold(category))
        if category_id:
            # Matched an existing category -> client just selects it.
            category = None
            parent = None

        payee = item.get("payee")
        payee = payee.strip() if isinstance(payee, str) and payee.strip() else None

        results.append(
            {
                "index": item["index"],
                "category_id": category_id,
                "category": category,
                "parent": parent,
                "payee": payee,
                "confidence": item.get("confidence"),
            }
        )
    return results


def suggest_categorization(db: Session, transactions: list[dict]) -> list[dict]:
    """Return ``{index, category_id, category, parent, payee, confidence}`` per txn.

    ``transactions`` items are ``{index, description, amount}``. ``category_id``
    is set when the suggestion matches an existing category; otherwise
    ``category``/``parent`` hold a proposed new (sub)category to create.
    """
    provider = resolve_provider()
    if provider is None:
        raise AICategorizationError(
            "AI categorization is not configured (set ANTHROPIC_API_KEY or GEMINI_API_KEY)."
        )
    if not transactions:
        return []

    options, lookup = _category_context(db)
    payees = _payee_names(db)
    context_text = "Existing categories and known payees:\n" + json.dumps(
        {"categories": options, "known_payees": payees[:400]}, ensure_ascii=False
    )

    suggestions: list[dict] = []
    for start in range(0, len(transactions), CHUNK_SIZE):
        chunk = transactions[start : start + CHUNK_SIZE]
        try:
            suggestions.extend(_classify_chunk(provider, lookup, chunk, context_text))
        except AICategorizationError:
            raise
        except (ValueError, KeyError, json.JSONDecodeError) as exc:
            logger.error("Failed to parse categorization response: %s", exc)
            raise AICategorizationError(
                "Could not parse the categorization response."
            ) from exc

    return suggestions
