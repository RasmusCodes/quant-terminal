"""Optional answer synthesis with Claude. Used only when credentials exist; the extractive answer is always produced first.

The model sees only the numbered evidence and must cite it as [n]; citations that do not exist are rejected and the
extractive answer is used instead. Server-side refusal fallbacks are enabled (fallbacks="default").
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from .util import log

SYSTEM = (
    "You answer questions about one person's own investment research, using ONLY the numbered evidence provided. "
    "Cite every factual statement with its evidence number in square brackets, like [3]. If the evidence does not answer "
    "the question, say so plainly; never add facts, numbers or companies that are not in the evidence. Keep each item's "
    "status: research claims are arguments, model estimates are model outputs, observed facts are observations, verified "
    "results were checked independently. Write 120-300 words in plain prose with short paragraphs; no headings."
)


def credentials_available() -> bool:
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    return (Path.home() / ".config" / "anthropic").exists()


def enabled(settings) -> bool:
    mode = str(settings.get("llm.enabled", "auto")).lower()
    if mode in ("false", "off", "no", "0"):
        return False
    return credentials_available()


def synthesize(settings, question: str, evidence: list[dict], draft: str) -> dict | None:
    if not evidence:
        return None
    try:
        import anthropic
    except ImportError:
        return None
    lines = []
    for i, e in enumerate(evidence[:30], 1):
        tag = e.get("claim_type") or e.get("kind") or "evidence"
        where = f"{e.get('project') or ''} / {e.get('title') or ''}".strip(" /")
        lines.append(f"[{i}] ({tag}; {where}) {str(e.get('text') or '')[:700]}")
    prompt = (f"Question: {question}\n\nEvidence:\n" + "\n".join(lines) +
              f"\n\nA draft built mechanically from the same evidence (for reference only):\n{draft[:3000]}")
    try:
        client = anthropic.Anthropic()
        response = client.beta.messages.create(
            model=settings.get("llm.model", "claude-opus-5-5"),
            max_tokens=4000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": settings.get("llm.effort", "medium")},
            system=SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
    except anthropic.AuthenticationError as exc:
        log.info("Claude synthesis skipped (no valid credentials): %s", exc)
        return None
    except anthropic.RateLimitError as exc:
        log.warning("Claude synthesis rate-limited: %s", exc)
        return None
    except anthropic.APIStatusError as exc:
        log.warning("Claude synthesis failed: %s", exc)
        return None
    except anthropic.APIConnectionError as exc:
        log.warning("Claude synthesis connection error: %s", exc)
        return None
    if response.stop_reason == "refusal":
        return None
    text = "".join(b.text for b in response.content if getattr(b, "type", "") == "text").strip()
    cited = {int(n) for n in re.findall(r"\[(\d{1,2})\]", text)}
    if not text or not cited or max(cited) > min(len(evidence), 30):
        return None
    return {"text": text, "model": getattr(response, "model", None), "cited": sorted(cited)}
