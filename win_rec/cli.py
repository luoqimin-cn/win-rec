from __future__ import annotations

import json
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import config
from . import recorder as rec
from . import store, transcribe, summarize, refine
from . import glossary as gloss


def _atomic_write_json(path: Path, data) -> None:
    """Write JSON atomically via tmp file + rename; avoids half-written files on crash."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(path)


def _load_or_transcribe_track(session: store.Session, audio_path: Path,
                              speaker_label: str, partial_name: str,
                              track_label: str) -> list[transcribe.Segment]:
    """Transcribe one audio track, using per-track partial cache if fresher than audio.

    This lets us resume after a crash mid-processing: if mic finished but system crashed,
    a rerun will reuse mic.raw.json partial and only re-run whisper on system.
    """
    partial_path = session.dir / partial_name
    if (partial_path.exists()
            and partial_path.stat().st_mtime > audio_path.stat().st_mtime):
        try:
            raw = json.loads(partial_path.read_text())
            segments = [transcribe.Segment(**d) for d in raw]
            console.print(
                f"  [dim]resuming {track_label} track from cached "
                f"{partial_name} ({len(segments)} segment(s))[/dim]"
            )
            return segments
        except (json.JSONDecodeError, TypeError) as e:
            console.print(
                f"  [yellow]{partial_name} is corrupt ({e}); re-transcribing…[/yellow]"
            )

    console.print(f"  transcribing {track_label} track…")
    segments = transcribe.transcribe_file(audio_path, speaker_label=speaker_label)
    console.print(f"    {len(segments)} segment(s)")
    _atomic_write_json(partial_path, [s.to_dict() for s in segments])
    return segments

app = typer.Typer(add_completion=False, help="win-rec — record, transcribe, summarize meetings")
console = Console()


@app.command()
def start(
    name: str = typer.Option(None, "--name", "-n", help="Optional meeting name."),
) -> None:
    """Start recording (microphone)."""
    try:
        session = rec.start(name=name)
    except rec.RecorderError as e:
        console.print(f"[red]error:[/red] {e}")
        raise typer.Exit(code=1)
    console.print(f"[green]●[/green] recording started → [cyan]{session.session_id}[/cyan]")
    console.print(f"  dir: {session.dir}")
    console.print("  stop with: [bold]win-rec stop[/bold]")


@app.command()
def stop(
    process_now: bool = typer.Option(False, "--process", help="Run transcribe+summarize after stop."),
    do_refine: bool = typer.Option(
        None, "--refine/--no-refine",
        help="Override the LLM refine step (default: on unless AI_REC_REFINE=0).",
    ),
) -> None:
    """Stop the active recording. Optionally run the full pipeline."""
    try:
        session = rec.stop()
    except rec.RecorderError as e:
        console.print(f"[red]error:[/red] {e}")
        raise typer.Exit(code=1)
    console.print(f"[green]■[/green] stopped → [cyan]{session.session_id}[/cyan]")
    meta = session.read_meta()
    if meta.get("duration"):
        console.print(f"  duration: {meta['duration']:.1f}s")
    console.print(f"  file: {session.mic_audio.name}")
    if process_now:
        effective_refine = do_refine if do_refine is not None else config.REFINE_ENABLED
        _process_session(session, refine_enabled=effective_refine)


@app.command()
def process(
    session_ref: str = typer.Argument("latest", help="Session id, or 'latest'."),
    do_refine: bool = typer.Option(
        None, "--refine/--no-refine",
        help="Override the LLM refine step (default: on unless AI_REC_REFINE=0).",
    ),
    no_summary: bool = typer.Option(False, "--no-summary", help="Skip Claude summary step."),
    redo_transcribe: bool = typer.Option(
        False, "--redo-transcribe",
        help="Force re-running Whisper even if transcript.raw.json is already cached.",
    ),
) -> None:
    """Transcribe + summarize a recorded session."""
    try:
        session = store.resolve_session(session_ref)
    except FileNotFoundError as e:
        console.print(f"[red]error:[/red] {e}")
        raise typer.Exit(code=1)
    effective_refine = do_refine if do_refine is not None else config.REFINE_ENABLED
    _process_session(session, skip_summary=no_summary, refine_enabled=effective_refine,
                     redo_transcribe=redo_transcribe)


@app.command(name="list")
def list_cmd() -> None:
    """List recorded sessions."""
    sessions = store.list_sessions()
    if not sessions:
        console.print("[dim]no recordings yet[/dim]")
        return
    table = Table(title="Recordings", show_lines=False)
    table.add_column("session", style="cyan")
    table.add_column("name")
    table.add_column("duration")
    table.add_column("transcript", justify="center")
    table.add_column("summary", justify="center")
    for s in sessions:
        meta = s.read_meta()
        dur = meta.get("duration")
        dur_s = f"{dur:.0f}s" if dur else "—"
        table.add_row(
            s.session_id,
            meta.get("name") or "—",
            dur_s,
            "✓" if s.transcript_md.exists() else "—",
            "✓" if s.summary_md.exists() else "—",
        )
    console.print(table)


@app.command()
def status() -> None:
    """Show whether a recording is currently active + mic state."""
    st = rec.current_state()
    active_session = store.current_session()
    sid = active_session.session_id if active_session else "?"
    rec_state = st["recording"]
    mic_state = st["mic"]

    if rec_state == "recording":
        console.print(f"[green]● recording[/green] → {sid}")
    elif rec_state == "paused":
        console.print(f"[yellow]⏸  paused[/yellow] → {sid}")
    else:
        console.print("[dim]idle[/dim]")
        return

    if mic_state == "paused":
        console.print("  🎤 MIC: [bold yellow]PAUSED[/bold yellow]")
    elif mic_state == "on":
        console.print("  🎤 MIC: [bold green]ON[/bold green]")
    elif mic_state == "off":
        console.print("  🎤 MIC: [bold red]OFF[/bold red]  [dim](turn on: win-rec mic on)[/dim]")


@app.command()
def pause() -> None:
    """Pause the current recording. Segment is finalized; use `win-rec resume` to continue."""
    try:
        session = rec.pause()
    except rec.RecorderError as e:
        console.print(f"[red]error:[/red] {e}")
        raise typer.Exit(code=1)
    console.print(f"[yellow]⏸[/yellow]  paused → {session.session_id}")
    console.print("  resume with: [bold]win-rec resume[/bold]")


@app.command()
def resume() -> None:
    """Resume a paused recording. Starts a new audio segment."""
    try:
        session = rec.resume()
    except rec.RecorderError as e:
        console.print(f"[red]error:[/red] {e}")
        raise typer.Exit(code=1)
    console.print(f"[green]▶[/green]  resumed → {session.session_id}")


def _find_pending_sessions() -> list[store.Session]:
    """Sessions with both audio tracks present but summary.md missing.
    Skips the currently-active recording."""
    active_sid = None
    if rec.ACTIVE_FILE.exists():
        try:
            active_sid = json.loads(rec.ACTIVE_FILE.read_text()).get("session_id")
        except (json.JSONDecodeError, OSError):
            pass
    pending = []
    for s in store.list_sessions():
        if active_sid and s.session_id == active_sid:
            continue
        if not s.mic_audio.exists() or s.mic_audio.stat().st_size == 0:
            continue
        if s.summary_md.exists():
            continue
        pending.append(s)
    return pending


@app.command(name="run-daily")
def run_daily(
    dry_run: bool = typer.Option(False, "--dry-run", help="List pending sessions without processing."),
) -> None:
    """Process any recorded sessions that still lack summary.md.

    Also usable interactively for on-demand batch cleanup.
    """
    pending = _find_pending_sessions()
    if not pending:
        console.print("[dim]no pending sessions[/dim]")
        return
    console.print(f"[blue]found {len(pending)} pending session(s):[/blue]")
    for s in pending:
        console.print(f"  - {s.session_id}  ({s.dir})")
    if dry_run:
        console.print("[dim]--dry-run: not processing[/dim]")
        return

    ok, fail = 0, 0
    for s in pending:
        console.print(f"\n[cyan]═══ {s.session_id} ═══[/cyan]")
        try:
            _process_session(
                s,
                skip_summary=False,
                refine_enabled=config.REFINE_ENABLED,
            )
            ok += 1
        except Exception as e:
            console.print(f"[red]failed:[/red] {e}")
            fail += 1

    console.print(f"\n[bold]run-daily done:[/bold] {ok} ok, {fail} failed, {len(pending)} total")


# ─────────────────────────────────────────────────────────
# `win-rec glossary` — view / open the proper-noun glossary
# ─────────────────────────────────────────────────────────

@app.command()
def glossary(
    edit: bool = typer.Option(False, "--edit", "-e", help="open in $EDITOR (default: notepad)"),
    path: bool = typer.Option(False, "--path", help="print path only"),
) -> None:
    """Show the proper-noun glossary that biases refine LLM name corrections.

    The glossary lives at ~/AI_Rec_Data/glossary.yaml. Add canonical spellings
    of people / companies / products / terms you meet often, and the refine
    step will correct ASR variants to those spellings.

    Apply changes to existing sessions with `win-rec process <session_id>`
    (uses cached transcript.raw.json — only refine + summary re-run).
    """
    gloss.ensure_seed_file()
    if path:
        typer.echo(str(gloss.GLOSSARY_PATH))
        return
    if edit:
        import os as _os
        editor = _os.environ.get("EDITOR", "notepad")
        import subprocess as _sp
        _sp.run([editor, str(gloss.GLOSSARY_PATH)])
        return

    console.print(f"[bold]glossary path[/bold]: {gloss.GLOSSARY_PATH}")
    try:
        flat = gloss.load(strict=True)
    except gloss.GlossaryError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(code=1)
    if not flat:
        console.print("[dim](empty — edit the file to add entries, or run `win-rec glossary --edit`)[/dim]")
        return
    console.print(f"[bold]{len(flat)} entr{'y' if len(flat) == 1 else 'ies'}:[/bold]")
    for canonical, variants in sorted(flat.items()):
        if variants:
            console.print(f"  [cyan]{canonical}[/cyan]  [dim]← { '、'.join(variants) }[/dim]")
        else:
            console.print(f"  [cyan]{canonical}[/cyan]")


# ─────────────────────────────────────────────────────────
# `win-rec delete <session>` — safe deletion via Recycle Bin
# ─────────────────────────────────────────────────────────

_PROTECTED_THRESHOLD_SEC = 180  # sessions with more than 3 minutes of active recording are protected


def _move_to_trash(path: Path) -> None:
    """Move a directory to the Windows Recycle Bin (recoverable)."""
    import send2trash
    send2trash.send2trash(str(path))


@app.command()
def delete(
    session_ref: str = typer.Argument(..., help="session id (or 'latest') to delete"),
    force: bool = typer.Option(False, "--force", help="delete even if session has >3min recording"),
    yes: bool = typer.Option(False, "--yes", "-y", help="skip interactive confirmation (still refuses protected sessions without --force)"),
) -> None:
    """Move a session to the Recycle Bin (recoverable).

    Never rm -rf a recording — this is the only safe delete path. Sessions with
    >3 minutes of active recording require --force to delete.
    """
    # Refuse if the session is currently the active one
    active = store.current_session()
    if active is not None:
        try:
            resolved = store.resolve_session(session_ref)
        except FileNotFoundError as e:
            console.print(f"[red]error:[/red] {e}")
            raise typer.Exit(code=1)
        if resolved.session_id == active.session_id:
            console.print(
                f"[red]refused:[/red] {resolved.session_id} is the currently active session. "
                f"Stop it first: [bold]win-rec stop[/bold]"
            )
            raise typer.Exit(code=1)

    try:
        session = store.resolve_session(session_ref)
    except FileNotFoundError as e:
        console.print(f"[red]error:[/red] {e}")
        raise typer.Exit(code=1)

    active_sec = store.active_recording_seconds(session)
    meta = session.read_meta()
    name = meta.get("name") or "—"

    # Summary of what will be deleted
    console.print(f"[bold]about to delete session[/bold] [cyan]{session.session_id}[/cyan]")
    console.print(f"  name:          {name}")
    console.print(f"  active time:   {active_sec:.0f}s ({active_sec/60:.1f} min)")

    # Count files & size
    total_bytes = sum(f.stat().st_size for f in session.dir.rglob("*") if f.is_file())
    file_count = sum(1 for f in session.dir.rglob("*") if f.is_file())
    console.print(f"  files:         {file_count} ({total_bytes/1e6:.1f} MB)")
    console.print(f"  path:          {session.dir}")

    # Protection check
    if active_sec > _PROTECTED_THRESHOLD_SEC and not force:
        console.print(
            f"[red]refused:[/red] session has {active_sec:.0f}s of recording "
            f"(>{_PROTECTED_THRESHOLD_SEC}s protection threshold). "
            f"Use [bold]--force[/bold] to override."
        )
        raise typer.Exit(code=1)

    # Interactive confirm (unless --yes)
    if not yes:
        typed = typer.prompt(
            f"\nTo confirm, retype the session id ({session.session_id})",
            default="",
            show_default=False,
        )
        if typed.strip() != session.session_id:
            console.print("[yellow]aborted:[/yellow] session id mismatch, nothing deleted")
            raise typer.Exit(code=1)

    # Do the move
    try:
        _move_to_trash(session.dir)
    except Exception as e:
        console.print(f"[red]delete failed:[/red] {e}")
        raise typer.Exit(code=1)
    console.print(f"[green]✓ moved to Recycle Bin[/green] — recoverable via File Explorer")


def _process_session(session: store.Session, *,
                     skip_summary: bool = False,
                     refine_enabled: bool = True,
                     redo_transcribe: bool = False) -> None:
    # Concurrency guard: refuse if another win-rec process is already handling
    # this session. Common trigger: user's stop --process still running when
    # run-daily fires. Two Whisper instances → 2× wall time +
    # potential JSON write races.
    acquired, held_by = store.acquire_process_lock(session)
    if not acquired:
        pid = held_by.get("pid") if held_by else "?"
        started = held_by.get("started_at") if held_by else None
        from datetime import datetime as _dt
        started_str = _dt.fromtimestamp(started).strftime("%Y-%m-%d %H:%M:%S") if started else "?"
        console.print(
            f"[yellow]skipping[/yellow] [cyan]{session.session_id}[/cyan]: "
            f"already being processed by PID {pid} (started {started_str}). "
            f"Wait for it, or `kill {pid}` if stuck, then retry."
        )
        return

    try:
        _process_session_locked(
            session,
            skip_summary=skip_summary,
            refine_enabled=refine_enabled,
            redo_transcribe=redo_transcribe,
        )
    finally:
        store.release_process_lock(session)


def _process_session_locked(session: store.Session, *,
                             skip_summary: bool = False,
                             refine_enabled: bool = True,
                             redo_transcribe: bool = False) -> None:
    console.print(f"[blue]→[/blue] processing [cyan]{session.session_id}[/cyan]")

    raw_json = session.dir / "transcript.raw.json"
    mic_partial = session.dir / "transcript.mic.raw.json"

    if redo_transcribe:
        for p in (raw_json, mic_partial):
            if p.exists():
                p.unlink()

    if raw_json.exists():
        console.print(f"  [dim]reusing cached transcription from {raw_json.name}[/dim]")
        raw_data = json.loads(raw_json.read_text())
        segments = [transcribe.Segment(**d) for d in raw_data]
        console.print(f"    {len(segments)} segment(s) loaded")
    else:
        if not session.mic_audio.exists():
            raise RuntimeError("mic audio missing")
        segments = _load_or_transcribe_track(
            session, session.mic_audio, "我", "transcript.mic.raw.json", "mic",
        )
        _atomic_write_json(raw_json, [s.to_dict() for s in segments])
        if mic_partial.exists():
            mic_partial.unlink()

    if refine_enabled and segments:
        n_batches = (len(segments) + refine.BATCH_SIZE - 1) // refine.BATCH_SIZE
        console.print(
            f"  refining {len(segments)} segments in {n_batches} batch(es) "
            f"(LLM correcting terms + hallucinations)…"
        )
        def _progress(done: int, total: int, error: str | None):
            if error:
                console.print(f"    [yellow]batch {done}/{total} failed:[/yellow] {error}")
            else:
                console.print(f"    batch {done}/{total} ok")
        debug_dir = session.dir / "refine_debug"
        try:
            segments = refine.refine_segments(
                segments, on_progress=_progress, debug_dir=debug_dir,
            )
        except refine.RefineError as e:
            console.print(f"  [yellow]refine partial:[/yellow] {e}")
            if debug_dir.exists() and any(debug_dir.iterdir()):
                console.print(f"  [dim]failed LLM outputs dumped to {debug_dir}[/dim]")
        except Exception as e:
            console.print(f"  [red]refine failed entirely:[/red] {e}")

    session.transcript_json.write_text(
        json.dumps([s.to_dict() for s in segments], indent=2, ensure_ascii=False)
    )
    transcript_md = "\n\n".join(s.to_markdown() for s in segments)
    session.transcript_md.write_text(transcript_md)
    console.print(f"  transcript → {session.transcript_md}")

    if skip_summary:
        return
    console.print("  generating summary…")

    def _on_summary_retry(attempt: int, max_attempts: int, err: Exception):
        console.print(
            f"    [yellow]summary attempt {attempt}/{max_attempts} failed:[/yellow] "
            f"{type(err).__name__}: {err}. Waiting {summarize.RETRY_WAIT_SEC}s then retrying…"
        )

    try:
        summary = summarize.summarize(
            segments, meta=session.read_meta(), on_retry=_on_summary_retry,
        )
        session.summary_md.write_text(summary)
        console.print(f"  summary → {session.summary_md}")
    except Exception as e:
        console.print(
            f"  [yellow]summary failed after {summarize.MAX_ATTEMPTS} attempts:[/yellow] {e}\n"
            f"  transcript is safe at {session.transcript_md}. "
            f"Re-run `win-rec process {session.session_id} --no-summary` to skip, "
            f"or rerun without --no-summary once the LLM is reachable."
        )
        return

    # Auto-scan for new proper nouns (best-effort; never fails the pipeline).
    # Disable with AI_REC_GLOSSARY_AUTO_SCAN=0.
    import os as _os
    if summary and _os.environ.get("AI_REC_GLOSSARY_AUTO_SCAN", "1") != "0":
        try:
            candidates = gloss.scan_summary(summary, session.session_id)
            if candidates:
                console.print(
                    f"  [dim]glossary auto-scan: found {len(candidates)} new candidate(s):[/dim]"
                )
                for name, typ in candidates[:10]:
                    console.print(f"    [dim]+ {name}  ({typ})[/dim]")
                if len(candidates) > 10:
                    console.print(f"    [dim]... and {len(candidates) - 10} more[/dim]")
                written = gloss.append_candidates_to_suspected(
                    candidates, session.session_id,
                )
                if written:
                    console.print(
                        f"  [dim]appended {written} to _suspected_asr_errors in "
                        f"{gloss.GLOSSARY_PATH.name}[/dim]"
                    )
        except Exception as e:
            console.print(f"  [dim yellow]glossary auto-scan skipped: {e}[/dim yellow]")


if __name__ == "__main__":
    app()
