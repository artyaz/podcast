"""The two research tools the model is given, and the dispatcher behind them.

Exa searches. Firecrawl reads. That is the whole toolbox, and it is deliberately
small: the Exa Research API was retired (it answers HTTP 410), so synthesis is
this agent's job rather than a vendor's.

Raw page text never leaves this module in the graph state. Pages get read, fed
to the model inside the current invocation, and dropped. Only distilled claims
plus their source URLs survive into the checkpoint, which is what keeps the
checkpoint small enough to travel in a request body across a resume.
"""

from typing import Any, Dict, List, Optional

from .keys import SecretVault
from .transport import (
    ProviderHttpError,
    bearer_header,
    get_json,
    plain_key,
    post_json,
    run_with_rotation,
)

EXA_SEARCH_URL = "https://api.exa.ai/search"
FIRECRAWL_SCRAPE_URL = "https://api.firecrawl.dev/v2/scrape"
FIRECRAWL_CREDIT_URL = "https://api.firecrawl.dev/v2/team/credit-usage"

# Measured against the live API, not read off a docs page. Cost is per call at
# small result counts; latency is wall clock for a two-result query.
#
#   instant / neural / magic     $0.007   0.1-0.3s
#   fast / hybrid / blue         $0.007   0.6s
#   auto                         $0.007   1.2s
#   deep / deep-lite             $0.012   3.0-4.5s
#   deep-reasoning               $0.015   5.7s
#
# Deep tiers cost roughly double and run several times longer. That is cheap
# enough that a contested question should always get the deep treatment.
EXA_SEARCH_MODES = (
    "instant",
    "fast",
    "auto",
    "neural",
    "keyword",
    "hybrid",
    "magic",
    "blue",
    "deep-lite",
    "deep",
    "deep-reasoning",
)

MAX_PAGE_CHARACTERS = 12000
MAX_SNIPPET_CHARACTERS = 1600


def _truncate(text: Optional[str], limit: int) -> str:
    if not text:
        return ""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit] + " …[truncated]"


def exa_search(
    vault: SecretVault,
    query: str,
    mode: str = "auto",
    result_count: int = 6,
    include_domains: Optional[List[str]] = None,
    start_published_date: Optional[str] = None,
    category: Optional[str] = None,
) -> Dict[str, Any]:
    """Run one Exa search and return trimmed results.

    Summaries and highlights come back inline, which usually answers the
    question without a follow-up scrape. Full text is deliberately not
    requested here — that is Firecrawl's job, and only for sources that earn it.
    """
    if mode not in EXA_SEARCH_MODES:
        mode = "auto"

    request_payload: Dict[str, Any] = {
        "query": query,
        "type": mode,
        "numResults": max(1, min(int(result_count or 6), 15)),
        "contents": {
            "summary": True,
            "highlights": True,
        },
    }
    if include_domains:
        request_payload["includeDomains"] = include_domains
    if start_published_date:
        request_payload["startPublishedDate"] = start_published_date
    if category:
        request_payload["category"] = category

    def attempt(api_key: str):
        response_body = post_json(
            EXA_SEARCH_URL,
            headers={"x-api-key": plain_key(api_key), "Content-Type": "application/json"},
            payload=request_payload,
        )
        reported_cost = float(
            (response_body.get("costDollars") or {}).get("total") or 0.0
        )
        return response_body, {"dollars": reported_cost}

    response_body = run_with_rotation(vault.pool("exa"), attempt)

    trimmed_results = []
    for raw_result in response_body.get("results") or []:
        highlight_text = " ".join(raw_result.get("highlights") or [])
        trimmed_results.append(
            {
                "url": raw_result.get("url"),
                "title": raw_result.get("title"),
                "published": raw_result.get("publishedDate"),
                "author": raw_result.get("author"),
                "summary": _truncate(raw_result.get("summary"), MAX_SNIPPET_CHARACTERS),
                "highlights": _truncate(highlight_text, MAX_SNIPPET_CHARACTERS),
            }
        )

    return {
        "query": query,
        "mode": mode,
        "result_count": len(trimmed_results),
        "results": trimmed_results,
    }


def firecrawl_scrape(vault: SecretVault, url: str) -> Dict[str, Any]:
    """Read one page as markdown.

    Used when a snippet is not enough — a statute, a ruling, a paper's methods
    section. Snippets strip qualifications, and qualifications are usually where
    the argument lives.
    """

    def attempt(api_key: str):
        response_body = post_json(
            FIRECRAWL_SCRAPE_URL,
            headers={
                "Authorization": bearer_header(api_key),
                "Content-Type": "application/json",
            },
            payload={"url": url, "formats": ["markdown"], "onlyMainContent": True},
        )
        page_data = response_body.get("data") or {}
        page_metadata = page_data.get("metadata") or {}
        credits_used = int(page_metadata.get("creditsUsed") or 1)

        remaining_credits = None
        usage_record = vault.pool("firecrawl").usage_for(api_key)
        if usage_record.get("remaining_credits") is None:
            # Learn the real balance once per key, then decrement locally so a
            # long research run does not spend an extra round trip per page.
            try:
                credit_body = get_json(
                    FIRECRAWL_CREDIT_URL,
                    headers={"Authorization": bearer_header(api_key)},
                )
                remaining_credits = (credit_body.get("data") or {}).get("remainingCredits")
            except ProviderHttpError:
                remaining_credits = None
        else:
            remaining_credits = float(usage_record["remaining_credits"]) - credits_used

        return (
            {
                "url": page_metadata.get("sourceURL") or url,
                "title": page_metadata.get("title"),
                "status_code": page_metadata.get("statusCode"),
                "markdown": _truncate(page_data.get("markdown"), MAX_PAGE_CHARACTERS),
            },
            {"dollars": 0.0, "remaining_credits": remaining_credits},
        )

    return run_with_rotation(vault.pool("firecrawl"), attempt)


TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "exa_search",
            "description": (
                "Search the web for evidence. Choose the mode deliberately, because "
                "they differ in cost and depth:\n"
                "  instant, fast, magic, neural — $0.007, under a second. Use for a "
                "known-item lookup: confirming a date, a name, a citation, a number "
                "you already half-remember.\n"
                "  auto — $0.007, ~1s. Sensible default when the query is a plain "
                "factual question.\n"
                "  deep, deep-lite — $0.012, 3-5s. Use when the query needs more than "
                "one hop, when you want the primary document rather than coverage of "
                "it, or when the first search returned only commentary.\n"
                "  deep-reasoning — $0.015, ~6s. Use for contested questions where the "
                "query itself needs decomposing, where sources are known to disagree, "
                "or where you are hunting for the strongest version of an opposing "
                "position. Do not flinch at the cost; being wrong is more expensive."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query. Be specific; name the statute, case, dataset, or author if you know it.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": list(EXA_SEARCH_MODES),
                        "description": "Search depth. See the cost and latency guidance above.",
                    },
                    "result_count": {
                        "type": "integer",
                        "description": "How many results to return, 1-15. Default 6.",
                    },
                    "include_domains": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Restrict to specific domains. Use this to go primary: "
                            "eur-lex.europa.eu, supremecourt.gov, oecd.org, imf.org, "
                            "ec.europa.eu/eurostat, arxiv.org, ssrn.com."
                        ),
                    },
                    "start_published_date": {
                        "type": "string",
                        "description": "ISO date. Only return sources published after this.",
                    },
                    "category": {
                        "type": "string",
                        "description": "Optional Exa category filter, e.g. research_paper, news, pdf, company.",
                    },
                },
                "required": ["query", "mode"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "firecrawl_scrape",
            "description": (
                "Read one web page in full, as markdown. Use it when a search snippet "
                "is not enough — the text of a statute or ruling, a paper's methods or "
                "limitations section, a table you need the actual numbers from. "
                "Snippets strip the qualifications, and the qualifications are usually "
                "where the argument lives. Prefer the primary document over any "
                "write-up of it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Absolute URL of the page to read."}
                },
                "required": ["url"],
            },
        },
    },
]


def dispatch_tool_call(
    vault: SecretVault, tool_name: str, tool_arguments: Dict[str, Any]
) -> Dict[str, Any]:
    """Run one model-requested tool call.

    Tool failures are returned to the model as data rather than raised. A model
    that is told "that key is out of credits" can adapt — try another query, or
    report the gap honestly. A model that gets an exception just dies mid-run.
    """
    try:
        if tool_name == "exa_search":
            return exa_search(
                vault,
                query=tool_arguments.get("query") or "",
                mode=tool_arguments.get("mode") or "auto",
                result_count=tool_arguments.get("result_count") or 6,
                include_domains=tool_arguments.get("include_domains"),
                start_published_date=tool_arguments.get("start_published_date"),
                category=tool_arguments.get("category"),
            )
        if tool_name == "firecrawl_scrape":
            return firecrawl_scrape(vault, url=tool_arguments.get("url") or "")
        return {"error": "unknown tool: {0}".format(tool_name)}
    except Exception as tool_error:  # noqa: BLE001 - reported to the model, not swallowed
        return {"error": "{0}: {1}".format(type(tool_error).__name__, str(tool_error))}
