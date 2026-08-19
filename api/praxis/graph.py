"""The research graph, built to survive a 300-second ceiling.

The Hobby plan terminates any invocation at 300 seconds, and a deep research run
on a contested question does not reliably fit in 300 seconds. So the graph is
resumable: every node checks the clock before it starts, and if there is not
enough time left to finish the work it is about to begin, it suspends without
starting. The browser gets the state back, immediately posts it to the same
endpoint again, and the graph re-enters at the phase it stopped on.

That design forces a discipline which turns out to be good for its own sake:
whatever crosses an invocation boundary has to fit in a request body, capped at
4.5 MB. Raw scraped pages cannot travel. So pages are read, distilled into
claims with sources and a status, and dropped inside the invocation that fetched
them. The checkpoint carries conclusions, never corpora — which is what the
persona demands anyway.

Phase order:

    scope -> brainstorm -> plan -> research -> distill -> revise_plan
          -> gap_check -> research (again) or write_section -> done

Writing is one plan section per node, so a killed invocation keeps every
finished chapter and the listener sees blocks as they land.

gap_check is the loop's conscience. It can send the graph back for another round,
and it is not permitted to declare readiness before the minimum number of rounds
has actually happened.
"""

import time
from typing import Any, Dict, List, Optional

from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer
from langgraph.graph import END, START, StateGraph

try:  # Python 3.9 has TypedDict in typing_extensions only for total=False niceties
    from typing import TypedDict
except ImportError:  # pragma: no cover
    from typing_extensions import TypedDict  # type: ignore

from .keys import SecretVault
from .llm import LlmProfile, chat_completion, chat_json, run_tool_loop
from .plan import (
    apply_plan_patches,
    mark_written,
    next_unwritten,
    plan_from_model,
    plan_from_subtopics,
    written_count,
)
from .prompts import (
    brainstorm_prompt,
    distill_prompt,
    gap_check_prompt,
    inline_question_prompt,
    lesson_research_system_prompt,
    outline_prompt,
    plan_prompt,
    research_round_prompt,
    revise_plan_prompt,
    scoping_prompt,
    section_writing_prompt,
)
from .transport import ProviderHttpError

# How long each node needs, worst case, to be worth starting. These are wall
# clock seconds and they are deliberately generous: suspending early is free,
# whereas being killed mid-node loses the whole node's work and its spend.
NODE_TIME_REQUIREMENTS = {
    "scope": 45,
    "brainstorm": 35,
    "plan": 40,
    "research": 100,
    "distill": 45,
    "revise_plan": 30,
    "gap_check": 35,
    # One section, not the whole episode. A measured whole-episode write started
    # with time that looked sufficient and still crossed 300s. Per-section writes
    # fit in a fresh invocation and stream to the browser as they finish.
    "write_section": 90,
}

DEFAULT_MINIMUM_ROUNDS = 2
DEFAULT_MAXIMUM_ROUNDS = 5
DEFAULT_TARGET_BLOCK_COUNT = 8


class ResearchDeadline:
    """Wall-clock budget for one invocation."""

    def __init__(self, budget_seconds: float):
        self.budget_seconds = float(budget_seconds)
        self.started_at = time.monotonic()

    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def remaining(self) -> float:
        return self.budget_seconds - self.elapsed()

    def has_room_for(self, required_seconds: float) -> bool:
        return self.remaining() >= required_seconds

    def may_start_node(self, required_seconds: float) -> bool:
        """Whether a node should begin work now.

        The obvious rule — start only if the estimated cost fits in the time
        left — livelocks when the whole budget is smaller than one node's
        estimate. The graph suspends, the browser posts the identical state
        back, the graph suspends again at the same phase, forever, burning an
        invocation each time and making no progress.

        So there is a second condition: if nothing has run yet in this
        invocation, start anyway. Being cut off mid-node loses that node's work,
        which is bad; never starting loses everything, which is worse.
        """
        if self.remaining() >= required_seconds:
            return True
        return self.elapsed() < 2.0

    def has_room_for_model_turn(self) -> bool:
        """Used inside the tool loop, which cannot predict its own length."""
        return self.remaining() >= 30


class ResearchState(TypedDict, total=False):
    topic: str
    # Optional episode spine chosen by the listener before research starts. When
    # present it constrains scoping and dictates the running order of the written
    # episode; when absent the graph scopes the topic freely.
    subtopics: List[Dict[str, str]]
    phase: str
    suspended: bool
    suspend_reason: str

    restated: str
    layers: Dict[str, str]
    governing_axis: str
    open_questions: List[str]
    evidence_channels: List[str]
    expected_disagreements: List[str]

    round_number: int
    minimum_rounds: int
    maximum_rounds: int
    briefing: str

    findings: List[Dict[str, Any]]
    remaining_questions: List[str]
    gap_reasoning: str
    weakest_link: str

    brainstorm: Dict[str, Any]
    plan: List[Dict[str, str]]

    blocks: List[Dict[str, Any]]
    target_block_count: int

    searches_performed: int
    pages_read: int


def new_research_state(
    topic: str,
    target_block_count: int = DEFAULT_TARGET_BLOCK_COUNT,
    minimum_rounds: int = DEFAULT_MINIMUM_ROUNDS,
    maximum_rounds: int = DEFAULT_MAXIMUM_ROUNDS,
    subtopics: Optional[List[Dict[str, str]]] = None,
) -> ResearchState:
    return {
        "topic": topic,
        "subtopics": [
            {
                "title": str(entry.get("title") or "").strip(),
                "angle": str(entry.get("angle") or "").strip(),
            }
            for entry in (subtopics or [])
            if str(entry.get("title") or "").strip()
        ],
        "phase": "scope",
        "suspended": False,
        "suspend_reason": "",
        "restated": "",
        "layers": {},
        "governing_axis": "",
        "open_questions": [],
        "evidence_channels": [],
        "expected_disagreements": [],
        "round_number": 0,
        "minimum_rounds": max(1, int(minimum_rounds)),
        "maximum_rounds": max(1, int(maximum_rounds)),
        "briefing": "",
        "findings": [],
        "remaining_questions": [],
        "gap_reasoning": "",
        "weakest_link": "",
        "brainstorm": {},
        "plan": [],
        "blocks": [],
        "target_block_count": int(target_block_count),
        "searches_performed": 0,
        "pages_read": 0,
    }


def _runtime(config: RunnableConfig):
    configurable = (config or {}).get("configurable") or {}
    return (
        configurable["vault"],
        configurable["deadline"],
        configurable.get("profile") or LlmProfile(),
    )


def _emit(event: Dict[str, Any]) -> None:
    """Push a progress event to whoever is streaming this run."""
    try:
        writer = get_stream_writer()
    except Exception:  # pragma: no cover - graph invoked outside a stream
        return
    if writer is not None:
        writer(event)


def _work(title: str, reasoning: str) -> None:
    """A beat the library shows: a short job name, then a sentence of thinking."""
    _emit(
        {
            "type": "work",
            "title": title,
            "reasoning": reasoning,
        }
    )


def _suspend(state_phase: str, reason: str) -> Dict[str, Any]:
    _emit({"type": "suspend", "phase": state_phase, "reason": reason})
    return {"suspended": True, "suspend_reason": reason, "phase": state_phase}


def _bullet_list(items: List[str]) -> str:
    if not items:
        return "(none)"
    return "\n".join("- {0}".format(item) for item in items)


def _findings_digest(findings: List[Dict[str, Any]], limit: int = 40) -> str:
    if not findings:
        return "(nothing established yet)"
    lines = []
    for finding in findings[:limit]:
        source_hosts = []
        for source in finding.get("sources") or []:
            url = source.get("url") or ""
            if "//" in url:
                source_hosts.append(url.split("//", 1)[1].split("/", 1)[0])
        attribution = ", ".join(source_hosts[:3]) or "no source"
        lines.append(
            "- [{0}] {1} ({2})".format(
                finding.get("status") or "unverified",
                finding.get("claim") or "",
                attribution,
            )
        )
    return "\n".join(lines)


def _subtopics_text(state: ResearchState) -> str:
    """The chosen spine as a numbered list, or empty when none was chosen."""
    subtopics = state.get("subtopics") or []
    if not subtopics:
        return ""
    return "\n".join(
        "{0}. {1}{2}".format(
            index,
            entry.get("title") or "",
            " — {0}".format(entry["angle"]) if entry.get("angle") else "",
        )
        for index, entry in enumerate(subtopics, 1)
    )


def _json_timeout(deadline: ResearchDeadline) -> float:
    """Bound a JSON call so it fails into a suspend instead of killing the slice."""
    return max(25.0, min(90.0, deadline.remaining() - 20.0))


def _plan_digest(plan: List[Dict[str, str]]) -> str:
    if not plan:
        return "(no plan yet)"
    lines = []
    for item in plan:
        lines.append(
            "- {0} [{1}] {2}{3}".format(
                item.get("id") or "?",
                item.get("status") or "pending",
                item.get("title") or "",
                " — {0}".format(item["angle"]) if item.get("angle") else "",
            )
        )
    return "\n".join(lines)


def _brainstorm_digest(brainstorm: Dict[str, Any]) -> str:
    if not brainstorm:
        return "(none)"
    parts = []
    for key in ("tensions", "must_hunt", "thin_episode", "surprises"):
        values = brainstorm.get(key) or []
        if values:
            parts.append(
                "{0}: {1}".format(
                    key, "; ".join(str(item) for item in values[:10])
                )
            )
    return "\n".join(parts) or "(none)"


def _scope_digest(state: ResearchState) -> str:
    layers = state.get("layers") or {}
    return (
        "Restated: {0}\nGoverning axis: {1}\n"
        "Empirical layer: {2}\nConceptual layer: {3}\nNormative layer: {4}\n"
        "Disagreements expected: {5}"
    ).format(
        state.get("restated") or "",
        state.get("governing_axis") or "",
        layers.get("empirical") or "",
        layers.get("conceptual") or "",
        layers.get("normative") or "",
        "; ".join(state.get("expected_disagreements") or []) or "none named",
    )


def _merge_findings(
    existing: List[Dict[str, Any]], incoming: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Append new findings, skipping ones already held.

    Deduplication is on the normalised claim text. Without it a five-round run
    accumulates the same claim five times, which inflates the checkpoint and
    makes the model think a repeated recollection is corroboration.
    """
    merged = list(existing)
    seen_claims = {
        (finding.get("claim") or "").strip().lower() for finding in merged
    }
    for finding in incoming:
        claim_text = (finding.get("claim") or "").strip()
        if not claim_text or claim_text.lower() in seen_claims:
            continue
        seen_claims.add(claim_text.lower())
        merged.append(
            {
                "claim": claim_text,
                "status": finding.get("status") or "unverified",
                "sources": [
                    {"url": source.get("url"), "title": source.get("title")}
                    for source in (finding.get("sources") or [])
                    if source.get("url")
                ],
                "note": finding.get("note") or "",
            }
        )
    return merged


def scope_node(state: ResearchState, config: RunnableConfig) -> Dict[str, Any]:
    vault, deadline, profile = _runtime(config)
    if not deadline.may_start_node(NODE_TIME_REQUIREMENTS["scope"]):
        return _suspend("scope", "not enough time to scope the question")

    _emit({"type": "phase", "phase": "scope", "message": "Splitting the question apart"})
    _work(
        "Splitting the question apart",
        "I am restating what this episode actually has to settle, and which parts are facts, concepts, or values.",
    )
    scoping_result = chat_json(
        vault,
        messages=[
            {"role": "system", "content": lesson_research_system_prompt()},
            {
                "role": "user",
                "content": scoping_prompt(state["topic"], _subtopics_text(state)),
            },
        ],
        profile=profile,
        max_tokens=2500,
        temperature=0.5,
        required_keys=["open_questions", "governing_axis"],
        required_non_empty=["open_questions"],
        timeout_seconds=_json_timeout(deadline),
    )

    open_questions = [
        str(question)
        for question in (scoping_result.get("open_questions") or [])
        if str(question).strip()
    ]
    _emit(
        {
            "type": "scoped",
            "restated": scoping_result.get("restated") or "",
            "governing_axis": scoping_result.get("governing_axis") or "",
            "open_questions": open_questions,
            "evidence_channels": scoping_result.get("evidence_channels") or [],
        }
    )

    return {
        "restated": scoping_result.get("restated") or "",
        "layers": scoping_result.get("layers") or {},
        "governing_axis": scoping_result.get("governing_axis") or "",
        "open_questions": open_questions,
        "evidence_channels": [
            str(channel) for channel in (scoping_result.get("evidence_channels") or [])
        ],
        "expected_disagreements": [
            str(item) for item in (scoping_result.get("expected_disagreements") or [])
        ],
        "remaining_questions": open_questions,
        "phase": "brainstorm",
        "suspended": False,
    }


def brainstorm_node(state: ResearchState, config: RunnableConfig) -> Dict[str, Any]:
    vault, deadline, profile = _runtime(config)
    if not deadline.may_start_node(NODE_TIME_REQUIREMENTS["brainstorm"]):
        return _suspend("brainstorm", "not enough time to brainstorm")

    _emit(
        {
            "type": "phase",
            "phase": "brainstorm",
            "message": "Pressure-testing the episode before planning it",
        }
    )
    _work(
        "Pressure-testing the episode",
        "I am naming the tensions and the documents this episode is worthless without, before any prose is written.",
    )
    try:
        result = chat_json(
            vault,
            messages=[
                {"role": "system", "content": lesson_research_system_prompt()},
                {
                    "role": "user",
                    "content": brainstorm_prompt(
                        state["topic"],
                        _scope_digest(state),
                        _subtopics_text(state),
                    ),
                },
            ],
            profile=profile,
            max_tokens=2500,
            temperature=0.7,
            required_keys=["tensions", "must_hunt"],
            required_non_empty=["tensions"],
            timeout_seconds=_json_timeout(deadline),
        )
    except (ValueError, ProviderHttpError) as failure:
        _emit({"type": "note", "message": "brainstorm failed: {0}".format(failure)})
        return _suspend("brainstorm", "brainstorm failed: {0}".format(failure))

    brainstorm = {
        "tensions": [str(item) for item in (result.get("tensions") or []) if str(item).strip()],
        "must_hunt": [
            str(item) for item in (result.get("must_hunt") or []) if str(item).strip()
        ],
        "thin_episode": [
            str(item) for item in (result.get("thin_episode") or []) if str(item).strip()
        ],
        "surprises": [
            str(item) for item in (result.get("surprises") or []) if str(item).strip()
        ],
    }
    _emit(
        {
            "type": "brainstorm",
            "tensions": brainstorm["tensions"][:4],
            "must_hunt": brainstorm["must_hunt"][:4],
        }
    )
    return {"brainstorm": brainstorm, "phase": "plan", "suspended": False}


def plan_node(state: ResearchState, config: RunnableConfig) -> Dict[str, Any]:
    vault, deadline, profile = _runtime(config)
    if not deadline.may_start_node(NODE_TIME_REQUIREMENTS["plan"]):
        return _suspend("plan", "not enough time to write the plan")

    existing_subtopics = state.get("subtopics") or []
    _emit({"type": "phase", "phase": "plan", "message": "Writing the episode plan"})
    _work(
        "Deciding what to write, and in what order",
        "I am turning the research agenda into a private writing queue so each turn covers one stretch of a single episode.",
    )

    if existing_subtopics:
        seeded = plan_from_subtopics(existing_subtopics)
    else:
        seeded = []

    try:
        result = chat_json(
            vault,
            messages=[
                {"role": "system", "content": lesson_research_system_prompt()},
                {
                    "role": "user",
                    "content": plan_prompt(
                        state["topic"],
                        _scope_digest(state),
                        _brainstorm_digest(state.get("brainstorm") or {}),
                        _subtopics_text(state),
                        section_count=max(6, len(seeded) or 6),
                    ),
                },
            ],
            profile=profile,
            max_tokens=2500,
            temperature=0.5,
            required_keys=["plan"],
            required_non_empty=["plan"],
            timeout_seconds=_json_timeout(deadline),
        )
        drafted = plan_from_model(result.get("plan") or [])
    except (ValueError, ProviderHttpError) as failure:
        _emit({"type": "note", "message": "plan failed: {0}".format(failure)})
        drafted = []

    plan = drafted or seeded
    if not plan:
        plan = plan_from_model(
            [{"title": state["topic"], "angle": "Cover the question in full."}]
        )

    _emit(
        {
            "type": "plan",
            "plan": plan,
            "count": len(plan),
        }
    )
    return {
        "plan": plan,
        "phase": "research",
        "suspended": False,
    }


def research_node(state: ResearchState, config: RunnableConfig) -> Dict[str, Any]:
    vault, deadline, profile = _runtime(config)
    if not deadline.may_start_node(NODE_TIME_REQUIREMENTS["research"]):
        return _suspend("research", "not enough time for a research round")

    round_number = int(state.get("round_number") or 0) + 1
    questions_to_work = state.get("remaining_questions") or state.get("open_questions") or []

    _emit(
        {
            "type": "phase",
            "phase": "research",
            "message": "Research round {0}".format(round_number),
            "round": round_number,
        }
    )
    _work(
        "Research round {0}".format(round_number),
        "I am working the open questions against primary sources, and I will not write the episode until the findings can carry it.",
    )

    conversation = [
        {"role": "system", "content": lesson_research_system_prompt()},
        {
            "role": "user",
            "content": research_round_prompt(
                topic=state["topic"],
                open_questions_text=_bullet_list([str(q) for q in questions_to_work]),
                findings_digest=_findings_digest(state.get("findings") or []),
                round_number=round_number,
            ),
        },
    ]

    searches_performed = int(state.get("searches_performed") or 0)
    pages_read = int(state.get("pages_read") or 0)
    briefing_text = ""

    for loop_event in run_tool_loop(
        vault,
        messages=conversation,
        profile=profile,
        max_iterations=16,
        max_tokens=2500,
        temperature=0.6,
        deadline=deadline,
    ):
        event_type = loop_event.get("type")
        if event_type == "tool_call":
            arguments = loop_event.get("arguments") or {}
            if loop_event.get("tool") == "exa_search":
                searches_performed += 1
                query = arguments.get("query") or ""
                _emit(
                    {
                        "type": "search",
                        "query": query,
                        "mode": arguments.get("mode") or "auto",
                    }
                )
                _work(
                    "Searching the record",
                    "I am looking for {0}.".format(query or "the next piece of evidence"),
                )
            elif loop_event.get("tool") == "firecrawl_scrape":
                pages_read += 1
                page_url = arguments.get("url") or ""
                _emit({"type": "read", "url": page_url})
                host = ""
                if "//" in page_url:
                    host = page_url.split("//", 1)[1].split("/", 1)[0]
                _work(
                    "Reading a source in full",
                    "I am going through {0} properly, because a search snippet strips the qualifications.".format(
                        host or page_url or "this page"
                    ),
                )
        elif event_type == "tool_result":
            tool_result = loop_event.get("result") or {}
            if tool_result.get("error"):
                _emit({"type": "tool_error", "error": tool_result["error"]})
            elif loop_event.get("tool") == "exa_search":
                _emit(
                    {
                        "type": "search_results",
                        "titles": [
                            result.get("title")
                            for result in (tool_result.get("results") or [])[:5]
                        ],
                    }
                )
        elif event_type == "assistant_text":
            briefing_text = loop_event.get("text") or ""
        elif event_type == "stopped_early":
            _emit(
                {
                    "type": "note",
                    "message": "research round cut short: {0}".format(
                        loop_event.get("reason") or "unknown"
                    ),
                }
            )

    tool_results_exist = any(message.get("role") == "tool" for message in conversation)

    if not briefing_text.strip() and tool_results_exist:
        # The loop stopped before the model volunteered a summary — it hit the
        # iteration cap or the reserve. The searches have already been paid for and
        # their results are sitting in this conversation, so they get written down
        # now. Skipping this step is how a round of real evidence turns into zero
        # findings, which then lets the writing step mark recalled claims
        # "verified" because nothing contradicts them.
        _emit({"type": "phase", "phase": "research", "message": "Writing up what was found"})
        _work(
            "Writing up what was found",
            "The search loop has to stop so I can turn the retrieved sources into claims before the clock runs out.",
        )
        conversation.append(
            {
                "role": "user",
                "content": (
                    "Stop searching now and write up what you have. Give a plain-prose "
                    "briefing of what these searches established: the claims, who "
                    "supports them, where the sources disagree, and what you looked for "
                    "and could not establish. Only state what the results above actually "
                    "support. Do not use bullets or headers."
                ),
            }
        )
        try:
            wrap_up = chat_completion(
                vault,
                messages=conversation,
                profile=profile,
                tools_enabled=False,
                max_tokens=2200,
                temperature=0.4,
                timeout_seconds=max(25.0, deadline.remaining() - 15.0),
            )
            wrap_up_choices = wrap_up.get("choices") or []
            if wrap_up_choices:
                briefing_text = (wrap_up_choices[0].get("message") or {}).get("content") or ""
        except Exception as wrap_up_error:  # noqa: BLE001 - a lost round, not a crash
            _emit(
                {
                    "type": "tool_error",
                    "error": "could not write up round {0}: {1}".format(
                        round_number, str(wrap_up_error)
                    ),
                }
            )

    return {
        "round_number": round_number,
        "briefing": briefing_text,
        "searches_performed": searches_performed,
        "pages_read": pages_read,
        "phase": "distill",
        "suspended": False,
    }


def distill_node(state: ResearchState, config: RunnableConfig) -> Dict[str, Any]:
    vault, deadline, profile = _runtime(config)
    if not deadline.may_start_node(NODE_TIME_REQUIREMENTS["distill"]):
        return _suspend("distill", "not enough time to distill findings")

    briefing_text = (state.get("briefing") or "").strip()
    if not briefing_text:
        return {"phase": "gap_check", "briefing": "", "suspended": False}

    _emit({"type": "phase", "phase": "distill", "message": "Marking claim status"})
    _work(
        "Marking what is actually established",
        "I am sorting each claim into verified, contested, inferred, or still unverified, and dropping anything I cannot source.",
    )
    try:
        distilled = chat_json(
            vault,
            messages=[
                {"role": "system", "content": lesson_research_system_prompt()},
                {"role": "user", "content": distill_prompt(briefing_text)},
            ],
            profile=profile,
            max_tokens=3000,
            temperature=0.3,
            required_keys=["findings"],
            timeout_seconds=_json_timeout(deadline),
        )
    except (ValueError, ProviderHttpError) as failure:
        # Keep the briefing in state and suspend so the next slice can retry.
        # Dropping the briefing here is how a real research round turns into
        # an episode that opens "we could not retrieve the records".
        _emit({"type": "note", "message": "distill failed: {0}".format(failure)})
        return _suspend("distill", "distill failed: {0}".format(failure))

    merged_findings = _merge_findings(
        state.get("findings") or [], distilled.get("findings") or []
    )
    status_tally: Dict[str, int] = {}
    for finding in merged_findings:
        status_name = finding.get("status") or "unverified"
        status_tally[status_name] = status_tally.get(status_name, 0) + 1

    _emit(
        {
            "type": "findings",
            "total": len(merged_findings),
            "by_status": status_tally,
            "newest": [
                finding.get("claim")
                for finding in merged_findings[len(state.get("findings") or []) :][:4]
            ],
        }
    )

    # The briefing is dropped here on purpose: it is the largest transient in the
    # state and it has served its purpose once findings exist.
    return {
        "findings": merged_findings,
        "briefing": "",
        "phase": "revise_plan",
        "suspended": False,
    }


def gap_check_node(state: ResearchState, config: RunnableConfig) -> Dict[str, Any]:
    vault, deadline, profile = _runtime(config)
    if not deadline.may_start_node(NODE_TIME_REQUIREMENTS["gap_check"]):
        return _suspend("gap_check", "not enough time to audit the findings")

    rounds_completed = int(state.get("round_number") or 0)
    minimum_rounds = int(state.get("minimum_rounds") or DEFAULT_MINIMUM_ROUNDS)
    maximum_rounds = int(state.get("maximum_rounds") or DEFAULT_MAXIMUM_ROUNDS)

    _emit({"type": "phase", "phase": "gap_check", "message": "Auditing what is missing"})
    _work(
        "Auditing what is still missing",
        "I am checking whether the findings can carry an episode, or whether another research round is required.",
    )
    try:
        audit = chat_json(
            vault,
            messages=[
                {"role": "system", "content": lesson_research_system_prompt()},
                {
                    "role": "user",
                    "content": gap_check_prompt(
                        topic=state["topic"],
                        findings_digest=_findings_digest(state.get("findings") or []),
                        open_questions_text=_bullet_list(
                            [str(q) for q in (state.get("open_questions") or [])]
                        ),
                        rounds_completed=rounds_completed,
                    ),
                },
            ],
            profile=profile,
            max_tokens=1800,
            temperature=0.3,
            required_keys=["ready"],
            timeout_seconds=_json_timeout(deadline),
        )
    except (ValueError, ProviderHttpError) as failure:
        _emit({"type": "note", "message": "gap check failed: {0}".format(failure)})
        return _suspend("gap_check", "gap check failed: {0}".format(failure))

    model_says_ready = bool(audit.get("ready"))
    remaining_questions = [
        str(question)
        for question in (audit.get("remaining_questions") or [])
        if str(question).strip()
    ]

    # The floor is not negotiable by the model. "Always do deep research" has to
    # mean something structural, or a confident model will declare itself done
    # after one round of cheap searches.
    below_minimum_rounds = rounds_completed < minimum_rounds
    verified_count = sum(
        1
        for finding in (state.get("findings") or [])
        if finding.get("status") in ("verified", "contested")
    )
    too_little_evidence = verified_count < 4

    is_ready = model_says_ready and not below_minimum_rounds and not too_little_evidence
    hit_round_ceiling = rounds_completed >= maximum_rounds

    if is_ready or hit_round_ceiling:
        next_phase = "write_section"
    else:
        next_phase = "research"

    forced_reason = ""
    if below_minimum_rounds and model_says_ready:
        forced_reason = "minimum of {0} rounds not yet met".format(minimum_rounds)
    elif too_little_evidence and model_says_ready:
        forced_reason = "only {0} verified or contested findings so far".format(
            verified_count
        )

    _emit(
        {
            "type": "gap_check",
            "ready": is_ready,
            "next_phase": next_phase,
            "reasoning": audit.get("reasoning") or "",
            "weakest_link": audit.get("weakest_link") or "",
            "forced_another_round": forced_reason,
            "remaining_questions": remaining_questions,
        }
    )

    return {
        "remaining_questions": remaining_questions
        or [str(q) for q in (state.get("open_questions") or [])],
        "gap_reasoning": audit.get("reasoning") or "",
        "weakest_link": audit.get("weakest_link") or "",
        "phase": next_phase,
        "suspended": False,
    }


def revise_plan_node(state: ResearchState, config: RunnableConfig) -> Dict[str, Any]:
    vault, deadline, profile = _runtime(config)
    if not deadline.may_start_node(NODE_TIME_REQUIREMENTS["revise_plan"]):
        return _suspend("revise_plan", "not enough time to revise the plan")

    plan = [dict(item) for item in (state.get("plan") or [])]
    if not plan:
        return {"phase": "gap_check", "suspended": False}

    _emit({"type": "phase", "phase": "revise_plan", "message": "Editing the plan against the findings"})
    _work(
        "Revising the writing queue",
        "I am editing individual lines of the plan against what the sources actually established, not rewriting the whole thing.",
    )
    try:
        result = chat_json(
            vault,
            messages=[
                {"role": "system", "content": lesson_research_system_prompt()},
                {
                    "role": "user",
                    "content": revise_plan_prompt(
                        topic=state["topic"],
                        plan_digest=_plan_digest(plan),
                        findings_digest=_findings_digest(state.get("findings") or []),
                        gap_reasoning=state.get("gap_reasoning") or "",
                    ),
                },
            ],
            profile=profile,
            max_tokens=1800,
            temperature=0.3,
            required_keys=["patches"],
            timeout_seconds=_json_timeout(deadline),
        )
        patches = result.get("patches") or []
    except (ValueError, ProviderHttpError) as failure:
        _emit({"type": "note", "message": "plan revise skipped: {0}".format(failure)})
        patches = []

    if patches:
        plan = apply_plan_patches(plan, patches)
        _emit({"type": "plan", "plan": plan, "count": len(plan)})

    return {"plan": plan, "phase": "gap_check", "suspended": False}


def write_section_node(state: ResearchState, config: RunnableConfig) -> Dict[str, Any]:
    vault, deadline, profile = _runtime(config)
    if not deadline.may_start_node(NODE_TIME_REQUIREMENTS["write_section"]):
        return _suspend("write_section", "not enough time to write the next section")

    plan = [dict(item) for item in (state.get("plan") or [])]
    if not plan:
        plan = plan_from_model(
            [{"title": state["topic"], "angle": "Cover the question in full."}]
        )

    section = next_unwritten(plan)
    if section is None:
        return {"plan": plan, "phase": "done", "suspended": False}

    section_index = next(
        (index for index, item in enumerate(plan, 1) if item.get("id") == section.get("id")),
        1,
    )
    previous_titles = "\n".join(
        "- {0}".format(item.get("title") or "")
        for item in plan
        if item.get("status") == "written"
    )

    _emit(
        {
            "type": "phase",
            "phase": "write_section",
            "message": "Writing: {0}".format(section.get("title") or "the next stretch"),
            "section_id": section.get("id"),
            "section_index": section_index,
            "section_count": len(plan),
        }
    )
    _work(
        "Writing the next stretch",
        "I am appending the next passage of the episode, covering {0}.".format(
            section.get("title") or "what the plan says comes next"
        ),
    )

    findings = state.get("findings") or []
    evidenced_count = sum(
        1 for finding in findings if finding.get("status") in ("verified", "contested")
    )
    target_block_count = int(
        state.get("target_block_count") or DEFAULT_TARGET_BLOCK_COUNT
    )

    writing_instruction = section_writing_prompt(
        topic=state["topic"],
        section_title=section.get("title") or "",
        section_angle=section.get("angle") or "",
        section_index=section_index,
        section_count=len(plan),
        scope_digest=_scope_digest(state),
        findings_digest=_findings_digest(findings, limit=60),
        previous_titles=previous_titles,
        target_block_count=target_block_count,
    )

    if evidenced_count == 0:
        writing_instruction += (
            "\n\nCRITICAL: the research phase established nothing this session. There "
            "are no verified findings and no sources. So: no block may carry status "
            '"verified" or "contested", and no block may cite a source you did not '
            "retrieve. Open by saying plainly that the research did not complete and "
            "what remains unestablished, then give only what you can honestly mark "
            '"unverified" or "inferred". Still write the section — inferred argument '
            "is allowed when labeled — but do not manufacture sources."
        )
        _emit(
            {
                "type": "note",
                "message": "writing with no established evidence — section will be marked unverified",
            }
        )

    try:
        written = chat_json(
            vault,
            messages=[
                {"role": "system", "content": lesson_research_system_prompt()},
                {"role": "user", "content": writing_instruction},
            ],
            profile=profile,
            max_tokens=4500,
            temperature=0.75,
            required_keys=["blocks"],
            required_non_empty=["blocks"],
            timeout_seconds=_json_timeout(deadline),
        )
    except (ValueError, ProviderHttpError) as failure:
        _emit({"type": "note", "message": "section write failed: {0}".format(failure)})
        return _suspend(
            "write_section", "section write failed: {0}".format(failure)
        )

    normalized_blocks = normalize_blocks(
        written.get("blocks") or [], section_id=section.get("id") or ""
    )
    combined_blocks = list(state.get("blocks") or []) + normalized_blocks
    updated_plan = mark_written(plan, section.get("id") or "")

    _emit(
        {
            "type": "blocks_delta",
            "section_id": section.get("id"),
            "count": len(normalized_blocks),
            "blocks": normalized_blocks,
            "written": written_count(updated_plan),
            "total": len(updated_plan),
        }
    )

    still_pending = next_unwritten(updated_plan)
    result = {
        "blocks": combined_blocks,
        "plan": updated_plan,
        "suspended": False,
    }
    if still_pending is None:
        result["phase"] = "done"
        return result
    if deadline.has_room_for(NODE_TIME_REQUIREMENTS["write_section"]):
        result["phase"] = "write_section"
        return result
    result.update(
        _suspend("write_section", "section written; pausing before the next")
    )
    return result


VALID_BLOCK_KINDS = ("heading", "paragraph", "aside", "gap")
VALID_BLOCK_STATUSES = ("verified", "contested", "unverified", "inferred")


def normalize_blocks(
    raw_blocks: List[Dict[str, Any]], section_id: str = ""
) -> List[Dict[str, Any]]:
    """Coerce model output into the block contract the editor relies on.

    The model is sloppy about enums — it has been observed putting a float into
    an array of strings — so nothing from it is trusted structurally. Anything
    unrecognised degrades to a paragraph of unverified status rather than
    breaking the editor.
    """
    normalized: List[Dict[str, Any]] = []
    for block_index, raw_block in enumerate(raw_blocks):
        if not isinstance(raw_block, dict):
            continue
        block_text = str(raw_block.get("text") or "").strip()
        if not block_text:
            continue

        block_kind = str(raw_block.get("kind") or "paragraph").lower()
        if block_kind not in VALID_BLOCK_KINDS:
            block_kind = "paragraph"

        block_status = str(raw_block.get("status") or "unverified").lower()
        if block_status not in VALID_BLOCK_STATUSES:
            block_status = "unverified"

        cleaned_sources = []
        for raw_source in raw_block.get("sources") or []:
            if not isinstance(raw_source, dict):
                continue
            source_url = raw_source.get("url")
            if not source_url:
                continue
            cleaned_sources.append(
                {"url": str(source_url), "title": str(raw_source.get("title") or "")}
            )

        # "verified" and "contested" both assert that a retrieved source backs this
        # block. With no source attached that assertion is empty, so it is demoted
        # rather than displayed. Enforced here because a prompt can be ignored and a
        # status chip in the UI is a promise to the listener.
        if block_status in ("verified", "contested") and not cleaned_sources:
            block_status = "unverified"

        block_record = {
            "id": "blk_{0}_{1}".format(int(time.time() * 1000), block_index),
            "kind": block_kind,
            "text": block_text,
            "sources": cleaned_sources,
            "status": block_status,
            "origin": "lesson",
        }
        if section_id:
            block_record["section_id"] = section_id
        normalized.append(block_record)
    return normalized


def _choose_entry(state: ResearchState) -> str:
    phase = state.get("phase") or "scope"
    if phase == "write":
        return "write_section"
    if phase in (
        "scope",
        "brainstorm",
        "plan",
        "research",
        "distill",
        "revise_plan",
        "gap_check",
        "write_section",
    ):
        return phase
    return "done"


def _route_after(state: ResearchState) -> str:
    if state.get("suspended"):
        return END
    phase = state.get("phase") or "done"
    if phase == "done":
        return END
    return phase


def build_research_graph():
    builder = StateGraph(ResearchState)
    builder.add_node("scope", scope_node)
    builder.add_node("brainstorm", brainstorm_node)
    builder.add_node("plan", plan_node)
    builder.add_node("research", research_node)
    builder.add_node("distill", distill_node)
    builder.add_node("revise_plan", revise_plan_node)
    builder.add_node("gap_check", gap_check_node)
    builder.add_node("write_section", write_section_node)

    entry_targets = {
        "scope": "scope",
        "brainstorm": "brainstorm",
        "plan": "plan",
        "research": "research",
        "distill": "distill",
        "revise_plan": "revise_plan",
        "gap_check": "gap_check",
        "write_section": "write_section",
        "done": END,
    }
    builder.add_conditional_edges(START, _choose_entry, entry_targets)

    onward_targets = {
        "brainstorm": "brainstorm",
        "plan": "plan",
        "research": "research",
        "distill": "distill",
        "revise_plan": "revise_plan",
        "gap_check": "gap_check",
        "write_section": "write_section",
        END: END,
    }
    for node_name in (
        "scope",
        "brainstorm",
        "plan",
        "research",
        "distill",
        "revise_plan",
        "gap_check",
        "write_section",
    ):
        builder.add_conditional_edges(node_name, _route_after, onward_targets)

    return builder.compile()


RESEARCH_GRAPH = build_research_graph()


def run_research_slice(
    vault: SecretVault,
    state: ResearchState,
    budget_seconds: float = 240.0,
    profile: Optional[LlmProfile] = None,
):
    """Advance the research as far as this invocation's clock allows.

    Yields progress events, then one final event: `done` when the lesson exists,
    or `suspended` carrying the state to post back for the next slice.
    """
    deadline = ResearchDeadline(budget_seconds)
    graph_config = {
        "configurable": {
            "vault": vault,
            "deadline": deadline,
            "profile": profile or LlmProfile(),
        },
        "recursion_limit": 80,
    }

    latest_state: Dict[str, Any] = dict(state)
    try:
        for stream_mode, payload in RESEARCH_GRAPH.stream(
            state, config=graph_config, stream_mode=["custom", "values"]
        ):
            if stream_mode == "custom":
                yield payload
            elif stream_mode == "values":
                latest_state = payload
    except Exception as graph_failure:  # noqa: BLE001 - must return a terminal event
        # A raised distill/write failure used to kill the SSE stream with no
        # `done` or `suspended`, which the browser reported as "the stream ended
        # without finishing or suspending". Hand the checkpoint back so the next
        # slice can retry the same phase.
        latest_state = dict(latest_state)
        latest_state["suspended"] = True
        latest_state["suspend_reason"] = "{0}: {1}".format(
            type(graph_failure).__name__, str(graph_failure)
        )
        yield {
            "type": "suspend",
            "phase": latest_state.get("phase") or "scope",
            "reason": latest_state["suspend_reason"],
        }
        yield {
            "type": "suspended",
            "state": latest_state,
            "elapsed_seconds": round(deadline.elapsed(), 1),
        }
        return

    is_finished = (latest_state.get("phase") == "done") and bool(
        latest_state.get("blocks")
    )
    yield {
        "type": "done" if is_finished else "suspended",
        "state": latest_state,
        "elapsed_seconds": round(deadline.elapsed(), 1),
    }


def answer_inline_question(
    vault: SecretVault,
    topic: str,
    question_text: str,
    surrounding_context: str,
    findings: List[Dict[str, Any]],
    budget_seconds: float = 150.0,
    profile: Optional[LlmProfile] = None,
):
    """The fast path: a question asked mid-lesson, answered in a few blocks.

    Deliberately not the full graph. The listener is waiting with a cursor
    blinking, so this is one tool loop and one write, with a tighter iteration
    cap. The standard on fabrication does not relax — only the depth does.
    """
    deadline = ResearchDeadline(budget_seconds)
    profile = profile or LlmProfile()
    conversation = [
        {"role": "system", "content": lesson_research_system_prompt()},
        {
            "role": "user",
            "content": inline_question_prompt(
                topic=topic,
                question_text=question_text,
                surrounding_context=surrounding_context,
                findings_digest=_findings_digest(findings, limit=25),
            ),
        },
    ]

    answer_text = ""
    for loop_event in run_tool_loop(
        vault,
        messages=conversation,
        profile=profile,
        max_iterations=8,
        max_tokens=4000,
        temperature=0.6,
        deadline=deadline,
    ):
        event_type = loop_event.get("type")
        if event_type == "tool_call":
            arguments = loop_event.get("arguments") or {}
            if loop_event.get("tool") == "exa_search":
                yield {
                    "type": "search",
                    "query": arguments.get("query") or "",
                    "mode": arguments.get("mode") or "auto",
                }
            else:
                yield {"type": "read", "url": arguments.get("url") or ""}
        elif event_type == "tool_result" and (loop_event.get("result") or {}).get("error"):
            yield {"type": "tool_error", "error": loop_event["result"]["error"]}
        elif event_type == "assistant_text":
            answer_text = loop_event.get("text") or ""

    # The tool loop answers in prose because forcing JSON while tools are live
    # makes this model drop tool calls. One extra cheap turn converts it.
    blocks: List[Dict[str, Any]] = []
    if answer_text.strip():
        try:
            structured = chat_json(
                vault,
                messages=[
                    {"role": "system", "content": lesson_research_system_prompt()},
                    {
                        "role": "user",
                        "content": (
                            "Convert this answer into lesson blocks, preserving its "
                            "wording as far as possible and keeping it speakable.\n\n"
                            + answer_text
                            + "\n\nReturn one JSON object with key \"blocks\"."
                        ),
                    },
                ],
                profile=profile,
                max_tokens=3500,
                temperature=0.3,
                required_keys=["blocks"],
                required_non_empty=["blocks"],
            )
            blocks = normalize_blocks(structured.get("blocks") or [])
        except ValueError:
            blocks = normalize_blocks(
                [{"kind": "paragraph", "text": answer_text, "status": "unverified"}]
            )

    for block in blocks:
        block["origin"] = "answer"

    yield {
        "type": "done",
        "blocks": blocks,
        "elapsed_seconds": round(deadline.elapsed(), 1),
    }


def propose_subtopics(
    vault: SecretVault,
    topic: str,
    subtopic_count: int,
    profile: Optional[LlmProfile] = None,
) -> List[Dict[str, str]]:
    """Split a subject into an even spine of segments, before research begins.

    Deliberately not a graph node: it runs while the listener is still deciding
    whether to proceed, its result is theirs to accept or discard, and nothing
    should be researched until they do. One cheap call, no tools.
    """
    requested = max(2, min(int(subtopic_count or 5), 12))
    profile = profile or LlmProfile()
    conversation = [
        {"role": "system", "content": lesson_research_system_prompt()},
        {"role": "user", "content": outline_prompt(topic, requested)},
    ]

    best_attempt: List[Dict[str, str]] = []
    # The count is the point of this call — it decides how research effort gets
    # divided — and asking for an exact number does not reliably produce one. A
    # request for three segments was observed returning one, which the non-empty
    # check happily accepted. So the shortfall is named and the model asked again.
    for attempt_index in range(3):
        outline = chat_json(
            vault,
            messages=conversation,
            profile=profile,
            max_tokens=2200,
            temperature=0.6,
            required_keys=["subtopics"],
            required_non_empty=["subtopics"],
        )

        proposed: List[Dict[str, str]] = []
        for entry in outline.get("subtopics") or []:
            if not isinstance(entry, dict):
                continue
            title = str(entry.get("title") or "").strip()
            if not title:
                continue
            proposed.append(
                {"title": title, "angle": str(entry.get("angle") or "").strip()}
            )

        if len(proposed) > len(best_attempt):
            best_attempt = proposed
        if len(proposed) >= requested:
            return proposed[:requested]

        if attempt_index < 2:
            conversation = [
                conversation[0],
                {"role": "user", "content": outline_prompt(topic, requested)},
                {
                    "role": "user",
                    "content": (
                        "That returned {0} subtopic{1}, not {2}. Return exactly {2}, "
                        "each covering a distinct part of the subject, as a JSON "
                        "object with the key \"subtopics\".".format(
                            len(proposed),
                            "" if len(proposed) == 1 else "s",
                            requested,
                        )
                    ),
                },
            ]

    return best_attempt[:requested]
