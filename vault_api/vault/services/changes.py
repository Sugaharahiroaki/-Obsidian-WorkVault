"""Queue of AI writes for the Windows sync client (pull + acknowledge)."""
from django.utils import timezone

from ..models import PendingChange
from ..security import InvalidPath, normalize_note_path

MAX_PULL = 50
ACK_STATUSES = {PendingChange.APPLIED, PendingChange.CONFLICT, PendingChange.FAILED}


def pending_changes(limit=MAX_PULL):
    """Oldest first, so the client applies them in the order they were made."""
    return [
        {
            "id": change.pk,
            "op": change.op,
            "path": change.path,
            "content": change.content,
            "base_hash": change.base_hash,
            "created_at": timezone.localtime(change.created_at).isoformat(),
        }
        for change in PendingChange.objects.filter(status=PendingChange.PENDING).order_by("id")[:limit]
    ]


def acknowledge(results):
    """Record what the client did with each change.

    ``results``: [{"id", "status": applied|conflict|failed, "conflict_path"?, "detail"?}].
    Re-sending the same acknowledgement is harmless.
    """
    outcome = []
    now = timezone.now()
    for item in results:
        if not isinstance(item, dict) or not isinstance(item.get("id"), int):
            outcome.append({"id": None, "ok": False, "error": "invalid_item"})
            continue
        change_id, status = item["id"], item.get("status")
        if status not in ACK_STATUSES:
            outcome.append({"id": change_id, "ok": False, "error": "invalid_status"})
            continue
        change = PendingChange.objects.filter(pk=change_id).first()
        if change is None:
            outcome.append({"id": change_id, "ok": False, "error": "not_found"})
            continue
        if change.status != PendingChange.PENDING:
            ok = change.status == status
            outcome.append({"id": change_id, "ok": ok, **({} if ok else {"error": "already_acknowledged"})})
            continue
        conflict_path = ""
        if status == PendingChange.CONFLICT:
            try:
                conflict_path = normalize_note_path(item.get("conflict_path"))
            except InvalidPath:
                outcome.append({"id": change_id, "ok": False, "error": "invalid_conflict_path"})
                continue
        detail = item.get("detail")
        change.status = status
        change.applied_at = now
        change.conflict_path = conflict_path
        change.detail = (detail if isinstance(detail, str) else change.detail)[:255]
        change.save(update_fields=["status", "applied_at", "conflict_path", "detail"])
        outcome.append({"id": change_id, "ok": True})
    return outcome
