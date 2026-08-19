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

    scope -> research -> distill -> gap_check -> research (again) or write -> done

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
from .llm import DEFAULT_CHAT_MODEL, chat_completion, chat_json, run_tool_loop
from .prompts import (
    distill_prompt,
    gap_check_prompt,
    inline_question_prompt,
    lesson_research_system_prompt,
    lesson_writing_prompt,
    research_round_prompt,
    scoping_prompt,
)

# How long each node needs, worst case, to be worth starting. These are wall
# clock seconds and they are deliberately generous: suspending early is free,
# whereas being killed mid-node loses the whole node's work and its spend.
NODE_TIME_REQUIREMENTS = {
    "scope": 45,
    "research": 100,
    "distill": 45,
    "gap_check": 35,
    # Writing an episode is the longest single model turn in the graph — it emits
    # thousands of tokens in one call. A measured run started it with time that
    # looked sufficient and still crossed 300s of wall clock and got killed. The
    # requirement is therefore set close to a whole pass, so that in practice
    # writing suspends and claims a fresh invocation to itself rather than
    # squeezing into whatever is left over after research.
    "write": 170,
}

DEFAULT_MINIMUM_ROUNDS = 2
DEFAULT_MAXIMUM_ROUNDS = 5
DEFAULT_TARGET_BLOCK_COUNT = 12


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

    blocks: List[Dict[str, Any]]
    target_block_count: int

    searches_performed: int
    pages_read: int


def new_research_state(
    topic: str,
    target_block_count: int = DEFAULT_TARGET_BLOCK_COUNT,
    minimum_rounds: int = DEFAULT_MINIMUM_ROUNDS,
    maximum_rounds: int = DEFAULT_MAXIMUM_ROUNDS,
) -> ResearchState:
    return {
        "topic": topic,
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
        configurable.get("model_identifier") or DEFAULT_CHAT_MODEL,
    )


def _emit(event: Dict[str, Any]) -> None:
    """Push a progress event to whoever is streaming this run."""
    try:
        writer = get_stream_writer()
    except Exception:  # pragma: no cover - graph invoked outside a stream
        return
    if writer is not None:
        writer(event)


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
    vault, deadline, model_identifier = _runtime(config)
    if not deadline.may_start_node(NODE_TIME_REQUIREMENTS["scope"]):
        return _suspend("scope", "not enough time to scope the question")

    _emit({"type": "phase", "phase": "scope", "message": "Splitting the question apart"})
    scoping_result = chat_json(
        vault,
        messages=[
            {"role": "system", "content": lesson_research_system_prompt()},
            {"role": "user", "content": scoping_prompt(state["topic"])},
        ],
        model_identifier=model_identifier,
        max_tokens=2500,
        temperature=0.5,
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
        "phase": "research",
        "suspended": False,
    }


def research_node(state: ResearchState, config: RunnableConfig) -> Dict[str, Any]:
    vault, deadline, model_identifier = _runtime(config)
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
        model_identifier=model_identifier,
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
                _emit(
                    {
                        "type": "search",
                        "query": arguments.get("query") or "",
                        "mode": arguments.get("mode") or "auto",
                    }
                )
            elif loop_event.get("tool") == "firecrawl_scrape":
                pages_read += 1
                _emit({"type": "read", "url": arguments.get("url") or ""})
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
                model_identifier=model_identifier,
                tools_enabled=False,
                max_tokens=2200,
                temperature=0.4,
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
    vault, deadline, model_identifier = _runtime(config)
    if not deadline.may_start_node(NODE_TIME_REQUIREMENTS["distill"]):
        return _suspend("distill", "not enough time to distill findings")

    briefing_text = (state.get("briefing") or "").strip()
    if not briefing_text:
        return {"phase": "gap_check", "briefing": "", "suspended": False}

    _emit({"type": "phase", "phase": "distill", "message": "Marking claim status"})
    distilled = chat_json(
        vault,
        messages=[
            {"role": "system", "content": lesson_research_system_prompt()},
            {"role": "user", "content": distill_prompt(briefing_text)},
        ],
        model_identifier=model_identifier,
        max_tokens=4000,
        temperature=0.3,
    )

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
        "phase": "gap_check",
        "suspended": False,
    }


def gap_check_node(state: ResearchState, config: RunnableConfig) -> Dict[str, Any]:
    vault, deadline, model_identifier = _runtime(config)
    if not deadline.may_start_node(NODE_TIME_REQUIREMENTS["gap_check"]):
        return _suspend("gap_check", "not enough time to audit the findings")

    rounds_completed = int(state.get("round_number") or 0)
    minimum_rounds = int(state.get("minimum_rounds") or DEFAULT_MINIMUM_ROUNDS)
    maximum_rounds = int(state.get("maximum_rounds") or DEFAULT_MAXIMUM_ROUNDS)

    _emit({"type": "phase", "phase": "gap_check", "message": "Auditing what is missing"})
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
        model_identifier=model_identifier,
        max_tokens=1800,
        temperature=0.3,
    )

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
        next_phase = "write"
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


def write_node(state: ResearchState, config: RunnableConfig) -> Dict[str, Any]:
    vault, deadline, model_identifier = _runtime(config)
    if not deadline.may_start_node(NODE_TIME_REQUIREMENTS["write"]):
        return _suspend("write", "not enough time to write the lesson")

    _emit({"type": "phase", "phase": "write", "message": "Writing the episode"})

    findings = state.get("findings") or []
    evidenced_count = sum(
        1 for finding in findings if finding.get("status") in ("verified", "contested")
    )

    writing_instruction = lesson_writing_prompt(
        topic=state["topic"],
        scope_digest=_scope_digest(state),
        findings_digest=_findings_digest(findings, limit=60),
        target_block_count=int(
            state.get("target_block_count") or DEFAULT_TARGET_BLOCK_COUNT
        ),
    )

    if evidenced_count == 0:
        # The round ceiling can route here with nothing established — a failed run,
        # a provider outage, every search coming back empty. Left alone the model
        # writes a confident episode from memory and marks it "verified", because
        # no finding contradicts it. That is the one error the listener cannot
        # catch, so the instruction is made explicit rather than hoped for.
        writing_instruction += (
            "\n\nCRITICAL: the research phase established nothing this session. There "
            "are no verified findings and no sources. So: no block may carry status "
            '"verified" or "contested", and no block may cite a source you did not '
            "retrieve. Open by saying plainly that the research did not complete and "
            "what remains unestablished, then give only what you can honestly mark "
            '"unverified" or "inferred". A short, honest episode is the correct output '
            "here. Do not manufacture a complete one."
        )
        _emit(
            {
                "type": "note",
                "message": "writing with no established evidence — episode will be marked unverified",
            }
        )

    written = chat_json(
        vault,
        messages=[
            {"role": "system", "content": lesson_research_system_prompt()},
            {"role": "user", "content": writing_instruction},
        ],
        model_identifier=model_identifier,
        max_tokens=9000,
        temperature=0.75,
    )

    normalized_blocks = normalize_blocks(written.get("blocks") or [])
    _emit({"type": "blocks", "count": len(normalized_blocks), "blocks": normalized_blocks})

    return {"blocks": normalized_blocks, "phase": "done", "suspended": False}


VALID_BLOCK_KINDS = ("heading", "paragraph", "aside", "gap")
VALID_BLOCK_STATUSES = ("verified", "contested", "unverified", "inferred")


def normalize_blocks(raw_blocks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
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

        normalized.append(
            {
                "id": "blk_{0}_{1}".format(int(time.time() * 1000), block_index),
                "kind": block_kind,
                "text": block_text,
                "sources": cleaned_sources,
                "status": block_status,
                "origin": "lesson",
            }
        )
    return normalized


def _choose_entry(state: ResearchState) -> str:
    phase = state.get("phase") or "scope"
    if phase in ("scope", "research", "distill", "gap_check", "write"):
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
    builder.add_node("research", research_node)
    builder.add_node("distill", distill_node)
    builder.add_node("gap_check", gap_check_node)
    builder.add_node("write", write_node)

    entry_targets = {
        "scope": "scope",
        "research": "research",
        "distill": "distill",
        "gap_check": "gap_check",
        "write": "write",
        "done": END,
    }
    builder.add_conditional_edges(START, _choose_entry, entry_targets)

    onward_targets = {
        "research": "research",
        "distill": "distill",
        "gap_check": "gap_check",
        "write": "write",
        END: END,
    }
    for node_name in ("scope", "research", "distill", "gap_check", "write"):
        builder.add_conditional_edges(node_name, _route_after, onward_targets)

    return builder.compile()


RESEARCH_GRAPH = build_research_graph()


def run_research_slice(
    vault: SecretVault,
    state: ResearchState,
    budget_seconds: float = 240.0,
    model_identifier: str = DEFAULT_CHAT_MODEL,
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
            "model_identifier": model_identifier,
        },
        "recursion_limit": 80,
    }

    latest_state: Dict[str, Any] = dict(state)
    for stream_mode, payload in RESEARCH_GRAPH.stream(
        state, config=graph_config, stream_mode=["custom", "values"]
    ):
        if stream_mode == "custom":
            yield payload
        elif stream_mode == "values":
            latest_state = payload

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
    model_identifier: str = DEFAULT_CHAT_MODEL,
):
    """The fast path: a question asked mid-lesson, answered in a few blocks.

    Deliberately not the full graph. The listener is waiting with a cursor
    blinking, so this is one tool loop and one write, with a tighter iteration
    cap. The standard on fabrication does not relax — only the depth does.
    """
    deadline = ResearchDeadline(budget_seconds)
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
        model_identifier=model_identifier,
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
                model_identifier=model_identifier,
                max_tokens=3500,
                temperature=0.3,
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
