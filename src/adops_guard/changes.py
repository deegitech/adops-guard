"""The one path every write takes: plan -> validate -> apply -> read back -> journal.

1. **Plan.** Print what will change, from values read from the API just now.
2. **Validate.** Ask the platform to check the exact request without writing it
   (Google ``validateOnly``, Meta ``execution_options=["validate_only"]``).
   In a dry run the object is read before and after that request: if it
   changed, the command stops (exit 4) and the journal records
   ``dry-run-wrote``. Meta's validate-only request is sent in a dry run only
   with ``--validate``; with ``--apply`` it always goes first.
3. **Stop** unless ``--apply`` was given. This is the default.
4. **Apply** under the single-writer lock. The journal gets an ``intent`` line
   before the request and an ``applied`` or ``error`` line after it.
5. **Read back** the object and compare it with what was sent. A mismatch
   is journaled and exits with code 4.

A write that fails ambiguously (timeout, HTTP 5xx) is never retried
automatically: re-running the same command reads the current value first and
skips the change if it already happened (and then records that the earlier
write is in place, see :func:`settle_noop`).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from adops_guard.context import Context
from adops_guard.errors import AdopsError, AmbiguousWrite, ExitCode, LockBusy, ReadBackMismatch


@dataclass
class Change:
    platform: str
    action: str
    target: str
    description: str
    request: Any
    validate: Callable[[], Any]
    apply: Callable[[], Any]
    read_back: Callable[[], Any]
    expected: Any
    show: Callable[[Any], str] = str
    notes: list[str] = field(default_factory=list)
    on_success: Callable[[], dict[str, Any] | None] | None = None
    validate_in_dry_run: bool = True


def pending_key(platform: str, action: str, target: str) -> str:
    return f"{platform}:{action}:{target}"


def validate_without_writing(
    ctx: Context,
    *,
    platform: str,
    action: str,
    target: str,
    validate: Callable[[], Any],
    read_back: Callable[[], Any],
    show: Callable[[Any], str] = str,
) -> None:
    """Send a validate-only request, and prove by reading the object before and after that it wrote nothing."""
    before = read_back()
    validate()
    after = read_back()
    if after != before:
        ctx.journal().record(
            platform=platform, action=action, target=target, phase="dry-run-wrote", before=before, after=after
        )
        raise ReadBackMismatch(
            f"the object changed during a validate-only request: {show(before)} -> {show(after)}",
            hint="the platform may have applied a request that was only meant to be checked (or someone changed "
            "the object at that moment): check it now, and please report this (see SECURITY.md)",
        )


def execute(ctx: Context, change: Change, *, apply_flag: bool) -> int:
    out = ctx.out
    key = pending_key(change.platform, change.action, change.target)
    out.line(f"PLAN     {change.platform} {change.action}  {change.target}")
    out.line(f"         {change.description}")
    for note in change.notes:
        out.line(f"         {note}")

    pending = (ctx.store().load().get("pending") or {}).get(key) if ctx.settings.state_dir.exists() else None
    if pending:
        out.warn(
            f"an earlier --apply for this target did not finish (run {pending.get('run')}); "
            "the values above were read fresh from the API"
        )

    if apply_flag:
        change.validate()
        out.line("CHECK    validated by the API (validate only); nothing was written")
    elif change.validate_in_dry_run:
        validate_without_writing(
            ctx,
            platform=change.platform,
            action=change.action,
            target=change.target,
            validate=change.validate,
            read_back=change.read_back,
            show=change.show,
        )
        out.line("CHECK    validated by the API (validate only); nothing was written")
    else:
        out.line("CHECK    not sent to the API: add --validate to have the platform check it without writing")
    summary: dict[str, Any] = {
        "platform": change.platform,
        "action": change.action,
        "target": change.target,
        "description": change.description,
    }
    if not apply_flag:
        out.line("DRY RUN  nothing changed. Re-run with --apply to make this change.")
        out.emit({**summary, "applied": False, "validated": change.validate_in_dry_run})
        return ExitCode.OK

    extra: dict[str, Any] | None = None
    with ctx.lock(f"{change.platform} {change.action} {change.target}"):
        journal = ctx.journal()
        store = ctx.store()
        base = {"platform": change.platform, "action": change.action, "target": change.target}
        marker = {"run": ctx.run_id, "expected": change.expected}
        store.update(lambda d: d.setdefault("pending", {}).__setitem__(key, marker))
        journal.record(**base, phase="intent", description=change.description, request=change.request)
        try:
            response = change.apply()
        except AmbiguousWrite as exc:
            journal.record(**base, phase="ambiguous", error=str(exc))
            raise
        except AdopsError as exc:
            journal.record(**base, phase="error", error=str(exc))
            store.update(lambda d: (d.get("pending") or {}).pop(key, None))
            raise
        except BaseException as exc:  # unexpected, or Ctrl-C: the request may or may not have gone out
            journal.record(**base, phase="ambiguous", error=f"{type(exc).__name__}: {exc}")
            raise  # the pending marker stays, so the next run checks and settles it
        journal.record(**base, phase="applied", response=response)
        out.line("APPLY    sent")
        try:
            got = change.read_back()
        except AdopsError as exc:  # the write went through; only the check failed (pending marker stays)
            journal.record(**base, phase="readback-error", error=str(exc))
            exc.hint = "the write was accepted but could not be read back; re-run the command to check it"
            raise
        ok = got == change.expected
        journal.record(**base, phase="verified" if ok else "mismatch", expected=change.expected, got=got)
        store.update(lambda d: (d.get("pending") or {}).pop(key, None))
        if ok:
            out.line(f"VERIFY   read back {change.show(got)}: OK")
            if change.on_success:  # still under the lock: it may update the state file
                extra = change.on_success()

    if not ok:
        raise ReadBackMismatch(
            f"read back {change.show(got)}, expected {change.show(change.expected)}",
            hint="check the object in the platform's UI; the journal has the request and the response",
        )
    out.emit({**summary, "applied": True, "verified": True, "value": got, **(extra or {})})
    return ExitCode.OK


def settle_noop(ctx: Context, platform: str, action: str, target: str, value: Any) -> None:
    """On a NO-OP, resolve the pending marker an earlier ambiguous ``--apply`` left for this target.

    If the value now in place is the value that write was sending, the journal
    gets a ``verified`` line for it; otherwise (it did not take effect, or the
    object changed since) a ``superseded`` line. Either way the marker goes.
    """
    key = pending_key(platform, action, target)
    if not ctx.settings.state_dir.exists():
        return
    pending = (ctx.store().load().get("pending") or {}).get(key)
    if not pending:
        return
    confirmed = "expected" in pending and pending.get("expected") == value
    try:
        with ctx.lock(f"{platform} {action} {target}"):
            ctx.journal().record(
                platform=platform,
                action=action,
                target=target,
                phase="verified" if confirmed else "superseded",
                got=value,
                note=f"found on a later run; the earlier --apply was run {pending.get('run')}",
            )
            ctx.store().update(lambda d: (d.get("pending") or {}).pop(key, None))
    except LockBusy:
        ctx.out.warn("another write holds the lock: the earlier --apply could not be recorded as settled now")
        return
    run = pending.get("run")
    if confirmed:
        ctx.out.line(f"         the earlier --apply (run {run}) is confirmed in place")
    else:
        ctx.out.line(f"         the earlier --apply (run {run}) is not in place, but this command needs no change")
