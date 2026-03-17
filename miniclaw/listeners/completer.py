"""Tab-completion for CLI slash commands."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import TYPE_CHECKING

from prompt_toolkit.completion import Completer, Completion, PathCompleter
from prompt_toolkit.document import Document

if TYPE_CHECKING:
    from miniclaw.runtime import Runtime
    from miniclaw.session import Session


class ArgType(Enum):
    NONE = auto()
    ENUM = auto()
    DYNAMIC = auto()
    PATH = auto()
    SUBCOMMAND = auto()
    FREE_TEXT = auto()


@dataclass
class CommandSpec:
    names: list[str]
    meta: str = ""
    arg_type: ArgType = ArgType.NONE
    enum_values: list[str] = field(default_factory=list)
    dynamic_fn: str = ""  # method name on SlashCommandCompleter
    subcommands: dict[str, CommandSpec] = field(default_factory=dict)


class SlashCommandCompleter(Completer):
    """Dynamic completer for MiniClaw slash commands."""

    def __init__(self, runtime: Runtime, session: Session) -> None:
        self.runtime = runtime
        self.session = session
        self._path_completer = PathCompleter(only_directories=True, expanduser=True)
        self._specs = self._build_specs()
        # Build a flat lookup: command name -> spec
        self._lookup: dict[str, CommandSpec] = {}
        for spec in self._specs:
            for name in spec.names:
                self._lookup[name] = spec

    def _build_specs(self) -> list[CommandSpec]:
        plugctx_sub = {
            "init": CommandSpec(names=["init"], meta="scaffold new context"),
            "list": CommandSpec(names=["list"], meta="list all contexts"),
            "status": CommandSpec(names=["status"], meta="show loaded contexts"),
            "load": CommandSpec(
                names=["load"],
                meta="load a context",
                arg_type=ArgType.DYNAMIC,
                dynamic_fn="_plugctx_all_contexts",
            ),
            "unload": CommandSpec(
                names=["unload"],
                meta="unload a context",
                arg_type=ArgType.DYNAMIC,
                dynamic_fn="_plugctx_loaded_contexts",
            ),
            "info": CommandSpec(
                names=["info"],
                meta="show context details",
                arg_type=ArgType.DYNAMIC,
                dynamic_fn="_plugctx_all_contexts",
            ),
        }

        return [
            CommandSpec(names=["help"], meta="show available commands"),
            CommandSpec(names=["reset"], meta="clear conversation history"),
            CommandSpec(names=["sessions"], meta="list saved sessions"),
            CommandSpec(names=["detach"], meta="detach from observed session"),
            CommandSpec(names=["cost"], meta="show usage stats"),
            CommandSpec(names=["pwd"], meta="show working directory"),
            CommandSpec(names=["quit", "exit", "q"], meta="exit the REPL"),
            CommandSpec(
                names=["effort"],
                meta="set thinking effort",
                arg_type=ArgType.ENUM,
                enum_values=["low", "medium", "high"],
            ),
            CommandSpec(
                names=["logging"],
                meta="set log level",
                arg_type=ArgType.ENUM,
                enum_values=["DEBUG", "INFO", "WARNING", "ERROR"],
            ),
            CommandSpec(
                names=["resume"],
                meta="resume a saved session",
                arg_type=ArgType.DYNAMIC,
                dynamic_fn="_session_completions",
            ),
            CommandSpec(
                names=["fork"],
                meta="fork an existing session",
                arg_type=ArgType.DYNAMIC,
                dynamic_fn="_session_completions",
            ),
            CommandSpec(
                names=["attach"],
                meta="attach as observer",
                arg_type=ArgType.DYNAMIC,
                dynamic_fn="_session_completions",
            ),
            CommandSpec(
                names=["model"],
                meta="show or change model",
                arg_type=ArgType.DYNAMIC,
                dynamic_fn="_model_completions",
            ),
            CommandSpec(names=["rename"], meta="rename session", arg_type=ArgType.FREE_TEXT),
            CommandSpec(
                names=["cd"],
                meta="change working directory",
                arg_type=ArgType.PATH,
            ),
            CommandSpec(
                names=["plugctx"],
                meta="manage contexts",
                arg_type=ArgType.SUBCOMMAND,
                subcommands=plugctx_sub,
            ),
        ]

    # ------------------------------------------------------------------
    # Dynamic data helpers
    # ------------------------------------------------------------------

    def _session_completions(self, prefix: str) -> list[Completion]:
        try:
            sessions = self.runtime.list_persisted_sessions()
        except Exception:
            return []
        results = []
        for s in sessions[:30]:
            sid = s.id
            if not sid.startswith(prefix):
                continue
            meta = s.name or ""
            results.append(Completion(sid, start_position=-len(prefix), display_meta=meta))
        return results

    def _model_completions(self, prefix: str) -> list[Completion]:
        defaults = [
            "claude-opus-4-6",
            "claude-sonnet-4-6",
            "claude-haiku-4-5-20251001",
            "gpt-4o",
            "gpt-4o-mini",
        ]
        try:
            current = self.session.agent_config.model
        except Exception:
            current = ""

        seen: set[str] = set()
        results: list[Completion] = []
        for model in defaults:
            if model not in seen and model.startswith(prefix):
                seen.add(model)
                meta = "current" if model == current else ""
                results.append(Completion(model, start_position=-len(prefix), display_meta=meta))

        if current and current not in seen and current.startswith(prefix):
            results.insert(
                0, Completion(current, start_position=-len(prefix), display_meta="current")
            )
        return results

    def _plugctx_all_contexts(self, prefix: str) -> list[Completion]:
        try:
            if self.session.plugctx is None:
                return []
            contexts = self.session.plugctx.list_contexts()
        except Exception:
            return []
        results = []
        for c in contexts:
            path = c["path"]
            if not path.startswith(prefix):
                continue
            meta = "loaded" if c["loaded"] else ""
            results.append(Completion(path, start_position=-len(prefix), display_meta=meta))
        return results

    def _plugctx_loaded_contexts(self, prefix: str) -> list[Completion]:
        try:
            if self.session.plugctx is None:
                return []
            contexts = self.session.plugctx.list_contexts()
        except Exception:
            return []
        results = []
        for c in contexts:
            if not c["loaded"]:
                continue
            path = c["path"]
            if not path.startswith(prefix):
                continue
            results.append(Completion(path, start_position=-len(prefix)))
        return results

    # ------------------------------------------------------------------
    # Main get_completions
    # ------------------------------------------------------------------

    def get_completions(self, document: Document, complete_event):  # noqa: ANN001
        text = document.text_before_cursor
        if not text.startswith("/"):
            return

        # Split into parts: "/cmd", "/cmd arg", "/cmd sub arg"
        body = text[1:]  # strip leading /
        parts = body.split(None, 1)  # at most [cmd, rest]

        if not parts or (len(parts) == 1 and not body.endswith(" ")):
            # Still typing the command name -> complete command names
            prefix = parts[0] if parts else ""
            yield from self._complete_command_names(prefix)
            return

        cmd_name = parts[0].lower()
        spec = self._lookup.get(cmd_name)
        if spec is None:
            return

        rest = parts[1] if len(parts) > 1 else ""
        yield from self._complete_args(spec, rest, document)

    def _complete_command_names(self, prefix: str) -> list[Completion]:
        seen: set[str] = set()
        results: list[Completion] = []
        for spec in self._specs:
            primary = spec.names[0]
            if primary in seen:
                continue
            for name in spec.names:
                if name.startswith(prefix):
                    seen.add(primary)
                    # Complete the full "/name" replacing the typed prefix
                    results.append(
                        Completion(
                            name,
                            start_position=-len(prefix),
                            display_meta=spec.meta,
                        )
                    )
                    break  # one match per spec
        return results

    def _complete_args(
        self, spec: CommandSpec, rest: str, document: Document
    ) -> list[Completion]:
        if spec.arg_type == ArgType.NONE or spec.arg_type == ArgType.FREE_TEXT:
            return []

        if spec.arg_type == ArgType.ENUM:
            prefix = rest.strip()
            return [
                Completion(v, start_position=-len(prefix), display_meta="")
                for v in spec.enum_values
                if v.startswith(prefix)
            ]

        if spec.arg_type == ArgType.DYNAMIC:
            prefix = rest.strip()
            fn = getattr(self, spec.dynamic_fn, None)
            if fn is not None:
                return fn(prefix)
            return []

        if spec.arg_type == ArgType.PATH:
            # Delegate to prompt_toolkit's PathCompleter.
            # Build a sub-document containing just the path portion.
            sub_doc = Document(rest)
            return list(self._path_completer.get_completions(sub_doc, None))

        if spec.arg_type == ArgType.SUBCOMMAND:
            sub_parts = rest.split(None, 1)

            if not sub_parts or (len(sub_parts) == 1 and not rest.endswith(" ")):
                # Complete subcommand name
                sub_prefix = sub_parts[0] if sub_parts else ""
                return [
                    Completion(name, start_position=-len(sub_prefix), display_meta=sub.meta)
                    for name, sub in spec.subcommands.items()
                    if name.startswith(sub_prefix)
                ]

            # Subcommand argument
            sub_name = sub_parts[0].lower()
            sub_spec = spec.subcommands.get(sub_name)
            if sub_spec is None:
                return []
            sub_rest = sub_parts[1] if len(sub_parts) > 1 else ""
            return self._complete_args(sub_spec, sub_rest, document)

        return []
