"""Structured diagnostics for ``astrbot-python-subset`` source validation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# Stable diagnostic codes.  These are part of the public contract consumed by
# the LLM Tool adapter, the Dashboard and the validation API; do not rename.
INVALID_SOURCE_ENCODING = "INVALID_SOURCE_ENCODING"
SOURCE_TOO_LARGE = "SOURCE_TOO_LARGE"
SYNTAX_ERROR = "SYNTAX_ERROR"
AST_TOO_LARGE = "AST_TOO_LARGE"
AST_TOO_DEEP = "AST_TOO_DEEP"
NODE_NOT_ALLOWED = "NODE_NOT_ALLOWED"
CONTROL_FLOW_NOT_ALLOWED = "CONTROL_FLOW_NOT_ALLOWED"
ASSIGNMENT_TARGET_NOT_ALLOWED = "ASSIGNMENT_TARGET_NOT_ALLOWED"
NAME_NOT_DEFINED = "NAME_NOT_DEFINED"
NAME_REBIND_NOT_ALLOWED = "NAME_REBIND_NOT_ALLOWED"
MODULE_NOT_ALLOWED = "MODULE_NOT_ALLOWED"
MODULE_MEMBER_NOT_ALLOWED = "MODULE_MEMBER_NOT_ALLOWED"
RELATIVE_IMPORT_NOT_ALLOWED = "RELATIVE_IMPORT_NOT_ALLOWED"
WILDCARD_IMPORT_NOT_ALLOWED = "WILDCARD_IMPORT_NOT_ALLOWED"
IMPORT_FORM_NOT_ALLOWED = "IMPORT_FORM_NOT_ALLOWED"
BUILTIN_NOT_ALLOWED = "BUILTIN_NOT_ALLOWED"
ATTRIBUTE_NOT_ALLOWED = "ATTRIBUTE_NOT_ALLOWED"
CALL_NOT_ALLOWED = "CALL_NOT_ALLOWED"
CALL_SIGNATURE_INVALID = "CALL_SIGNATURE_INVALID"
FUNCTION_LOCATION_NOT_ALLOWED = "FUNCTION_LOCATION_NOT_ALLOWED"
FUNCTION_SIGNATURE_NOT_ALLOWED = "FUNCTION_SIGNATURE_NOT_ALLOWED"
FUNCTION_VALUE_NOT_ALLOWED = "FUNCTION_VALUE_NOT_ALLOWED"
CAPABILITY_VALUE_NOT_ALLOWED = "CAPABILITY_VALUE_NOT_ALLOWED"
ASYNC_CALL_REQUIRES_AWAIT = "ASYNC_CALL_REQUIRES_AWAIT"
AWAIT_TARGET_NOT_ALLOWED = "AWAIT_TARGET_NOT_ALLOWED"
COROUTINE_VALUE_NOT_ALLOWED = "COROUTINE_VALUE_NOT_ALLOWED"
EXCEPTION_TYPE_NOT_ALLOWED = "EXCEPTION_TYPE_NOT_ALLOWED"
DEFAULT_VALUE_NOT_JSON = "DEFAULT_VALUE_NOT_JSON"


@dataclass(frozen=True)
class DiagnosticOccurrence:
    line: int
    column: int
    end_line: int
    end_column: int

    def to_dict(self) -> dict[str, int]:
        return {
            "line": self.line,
            "column": self.column,
            "end_line": self.end_line,
            "end_column": self.end_column,
        }


@dataclass(frozen=True)
class Diagnostic:
    code: str
    severity: str
    message: str
    hint: str | None
    occurrences: list[DiagnosticOccurrence]
    occurrence_count: int
    suppressed_diagnostics: int = 0

    @property
    def sort_key(self) -> tuple[int, int, str]:
        if not self.occurrences:
            return (0, 0, self.code)
        first = self.occurrences[0]
        return (first.line, first.column, self.code)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "hint": self.hint,
            "occurrences": [occ.to_dict() for occ in self.occurrences],
            "occurrence_count": self.occurrence_count,
            "suppressed_diagnostics": self.suppressed_diagnostics,
        }


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    language_version: str
    diagnostics: list[Diagnostic]
    total_diagnostics: int = 0
    truncated: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "total_diagnostics", len(self.diagnostics))

    def to_dict(self) -> dict[str, Any]:
        return {
            "valid": self.valid,
            "language_version": self.language_version,
            "diagnostics": [d.to_dict() for d in self.diagnostics],
            "total_diagnostics": self.total_diagnostics,
            "truncated": self.truncated,
        }

    @classmethod
    def invalid(
        cls,
        diagnostics: list[Diagnostic],
        *,
        language_version: str,
        truncated: bool = False,
    ) -> ValidationResult:
        return cls(
            valid=False,
            language_version=language_version,
            diagnostics=diagnostics,
            truncated=truncated,
        )

    @classmethod
    def ok(cls, language_version: str) -> ValidationResult:
        return cls(valid=True, language_version=language_version, diagnostics=[])


class DiagnosticCollector:
    """Accumulates, groups and orders diagnostics.

    Grouping rule: diagnostics with the same ``code``, canonical subject and
    canonical hint are merged into one group that keeps every occurrence.
    """

    def __init__(self) -> None:
        self._groups: dict[tuple[str, str, str], Diagnostic] = {}
        self._order: list[tuple[str, str, str]] = []
        self._suppressed = 0

    def add(
        self,
        code: str,
        *,
        subject: str,
        hint: str | None,
        occurrence: DiagnosticOccurrence,
        message: str | None = None,
    ) -> None:
        canonical_subject = subject or ""
        canonical_hint = hint or ""
        key = (code, canonical_subject, canonical_hint)
        if key in self._groups:
            existing = self._groups[key]
            occurrences = list(existing.occurrences)
            occurrences.append(occurrence)
            self._groups[key] = Diagnostic(
                code=existing.code,
                severity=existing.severity,
                message=existing.message,
                hint=existing.hint,
                occurrences=occurrences,
                occurrence_count=len(occurrences),
                suppressed_diagnostics=existing.suppressed_diagnostics,
            )
            return
        self._order.append(key)
        resolved_message = message or f"{code}: {canonical_subject}"
        self._groups[key] = Diagnostic(
            code=code,
            severity="error",
            message=resolved_message,
            hint=hint,
            occurrences=[occurrence],
            occurrence_count=1,
        )

    def suppress(self, count: int = 1) -> None:
        self._suppressed += count

    @property
    def suppressed(self) -> int:
        return self._suppressed

    def result(
        self, language_version: str, *, truncated: bool = False
    ) -> ValidationResult:
        diagnostics = [self._groups[key] for key in self._order]
        if self._suppressed:
            diagnostics = [
                Diagnostic(
                    code=d.code,
                    severity=d.severity,
                    message=d.message,
                    hint=d.hint,
                    occurrences=d.occurrences,
                    occurrence_count=d.occurrence_count,
                    suppressed_diagnostics=self._suppressed,
                )
                for d in diagnostics
            ]
        return ValidationResult(
            valid=not diagnostics,
            language_version=language_version,
            diagnostics=diagnostics,
            truncated=truncated,
        )


def utf16_position(
    lines: list[str],
    lineno: int,
    col_offset: int,
) -> tuple[int, int]:
    """Convert CPython AST UTF-8 byte offsets to Monaco 1-based UTF-16 columns.

    CPython ``ast.parse`` reports ``col_offset`` in UTF-8 bytes even for ``str``
    input.  Monaco counts UTF-16 code units, one-based.  This function returns
    the 1-based column (and a 1-based line already provided by the caller).
    """
    if lineno < 1 or lineno > len(lines):
        return (lineno, 1)
    line = lines[lineno - 1]
    byte_consumed = 0
    utf16_units = 0
    for char in line:
        char_bytes = len(char.encode("utf-8"))
        if byte_consumed + char_bytes > col_offset:
            break
        byte_consumed += char_bytes
        if ord(char) > 0xFFFF:
            utf16_units += 2
        else:
            utf16_units += 1
    return (lineno, utf16_units + 1)


def node_occurrence(
    node: Any,
    lines: list[str],
    *,
    fallback_end: tuple[int, int] | None = None,
) -> DiagnosticOccurrence:
    """Build a Monaco-compatible occurrence from an AST node."""
    start_line, start_col = utf16_position(lines, node.lineno, node.col_offset)
    end_lineno = getattr(node, "end_lineno", None)
    end_col_offset = getattr(node, "end_col_offset", None)
    if end_lineno is not None and end_col_offset is not None:
        end_line, end_col = utf16_position(lines, end_lineno, end_col_offset)
        if end_line < start_line or (end_line == start_line and end_col < start_col):
            end_line, end_col = start_line, start_col
    elif fallback_end is not None:
        end_line, end_col = fallback_end
    else:
        end_line, end_col = start_line, start_col
    return DiagnosticOccurrence(
        line=start_line,
        column=start_col,
        end_line=end_line,
        end_column=end_col,
    )


__all__ = [
    "AST_TOO_DEEP",
    "AST_TOO_LARGE",
    "ASYNC_CALL_REQUIRES_AWAIT",
    "ASSIGNMENT_TARGET_NOT_ALLOWED",
    "ATTRIBUTE_NOT_ALLOWED",
    "AWAIT_TARGET_NOT_ALLOWED",
    "BUILTIN_NOT_ALLOWED",
    "CALL_NOT_ALLOWED",
    "CALL_SIGNATURE_INVALID",
    "CAPABILITY_VALUE_NOT_ALLOWED",
    "CONTROL_FLOW_NOT_ALLOWED",
    "COROUTINE_VALUE_NOT_ALLOWED",
    "DEFAULT_VALUE_NOT_JSON",
    "Diagnostic",
    "DiagnosticCollector",
    "DiagnosticOccurrence",
    "EXCEPTION_TYPE_NOT_ALLOWED",
    "FUNCTION_LOCATION_NOT_ALLOWED",
    "FUNCTION_SIGNATURE_NOT_ALLOWED",
    "FUNCTION_VALUE_NOT_ALLOWED",
    "IMPORT_FORM_NOT_ALLOWED",
    "INVALID_SOURCE_ENCODING",
    "MODULE_MEMBER_NOT_ALLOWED",
    "MODULE_NOT_ALLOWED",
    "NAME_NOT_DEFINED",
    "NAME_REBIND_NOT_ALLOWED",
    "NODE_NOT_ALLOWED",
    "RELATIVE_IMPORT_NOT_ALLOWED",
    "SOURCE_TOO_LARGE",
    "SYNTAX_ERROR",
    "ValidationResult",
    "WILDCARD_IMPORT_NOT_ALLOWED",
    "node_occurrence",
    "utf16_position",
]
