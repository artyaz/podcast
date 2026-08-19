"""Lesson plan: a list of sections the writer walks, one at a time.

The plan is edited like a cursor-shaped patch, never rewritten as a whole.
Each item is addressed by a stable id; a revise pass returns only the lines
that changed. The writer then consumes the first unwritten item, appends
blocks, and checkpoints — so a killed invocation keeps every finished section.
"""

from typing import Any, Dict, List, Optional


def new_plan_item(
    title: str, angle: str = "", index: int = 1, status: str = "pending"
) -> Dict[str, str]:
    return {
        "id": "sec_{0}".format(index),
        "title": str(title or "").strip(),
        "angle": str(angle or "").strip(),
        "status": status if status in ("pending", "written") else "pending",
    }


def plan_from_subtopics(subtopics: List[Dict[str, str]]) -> List[Dict[str, str]]:
    plan = []
    for index, entry in enumerate(subtopics or [], 1):
        title = str(entry.get("title") or "").strip()
        if not title:
            continue
        plan.append(new_plan_item(title, str(entry.get("angle") or ""), index))
    return plan


def plan_from_model(raw_items: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    plan = []
    for index, entry in enumerate(raw_items or [], 1):
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title") or "").strip()
        if not title:
            continue
        plan.append(
            new_plan_item(
                title,
                str(entry.get("angle") or ""),
                index=index,
            )
        )
    return plan


def next_plan_index(plan: List[Dict[str, str]]) -> int:
    highest = 0
    for item in plan:
        item_id = item.get("id") or ""
        if item_id.startswith("sec_"):
            try:
                highest = max(highest, int(item_id.split("_", 1)[1]))
            except ValueError:
                continue
    return highest + 1


def next_unwritten(plan: List[Dict[str, str]]) -> Optional[Dict[str, str]]:
    for item in plan:
        if (item.get("status") or "pending") != "written":
            return item
    return None


def mark_written(plan: List[Dict[str, str]], section_id: str) -> List[Dict[str, str]]:
    updated = []
    for item in plan:
        if item.get("id") == section_id:
            copied = dict(item)
            copied["status"] = "written"
            updated.append(copied)
        else:
            updated.append(dict(item))
    return updated


def apply_plan_patches(
    plan: List[Dict[str, str]], patches: List[Dict[str, Any]]
) -> List[Dict[str, str]]:
    """Apply line-level edits. Unknown ids are ignored; the rest stay put.

    Supported actions:
      replace  — id, optional title, optional angle
      insert   — after_id (empty = prepend), title, optional angle
      delete   — id
    Rewriting the whole plan is not an action, on purpose.
    """
    working = [dict(item) for item in plan]
    by_id = {item["id"]: index for index, item in enumerate(working) if item.get("id")}

    for raw_patch in patches or []:
        if not isinstance(raw_patch, dict):
            continue
        action = str(raw_patch.get("action") or "").strip().lower()
        if action == "replace":
            item_id = str(raw_patch.get("id") or "")
            if item_id not in by_id:
                continue
            target = working[by_id[item_id]]
            if raw_patch.get("title"):
                target["title"] = str(raw_patch["title"]).strip()
            if "angle" in raw_patch and raw_patch.get("angle") is not None:
                target["angle"] = str(raw_patch.get("angle") or "").strip()
        elif action == "delete":
            item_id = str(raw_patch.get("id") or "")
            if item_id not in by_id:
                continue
            del working[by_id[item_id]]
            by_id = {
                item["id"]: index
                for index, item in enumerate(working)
                if item.get("id")
            }
        elif action == "insert":
            title = str(raw_patch.get("title") or "").strip()
            if not title:
                continue
            new_item = new_plan_item(
                title,
                str(raw_patch.get("angle") or ""),
                index=next_plan_index(working),
            )
            after_id = str(raw_patch.get("after_id") or "")
            if after_id and after_id in by_id:
                insert_at = by_id[after_id] + 1
            elif after_id:
                insert_at = len(working)
            else:
                insert_at = 0
            working.insert(insert_at, new_item)
            by_id = {
                item["id"]: index
                for index, item in enumerate(working)
                if item.get("id")
            }

    return [item for item in working if item.get("title")]


def written_count(plan: List[Dict[str, str]]) -> int:
    return sum(1 for item in plan if item.get("status") == "written")
