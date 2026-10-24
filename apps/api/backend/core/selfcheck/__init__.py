"""Block 11 semantic self-check: nested journal, summary, and fake-safe provider."""

from .contracts import POLICY_HASH, SEMANTIC_KEY, SCHEMA_VERSION, SemanticJournal, SemanticVerdict
from .policy import detect_triggers, enabled, parse_verdict, policy_fingerprint, qualify_evidence, reasons_from_verdict
from .prompt import load_prompt
from .provider import ChatSelfCheckProvider, FakeSelfCheckProvider
from .service import apply_parsed, merge_self_check, prepare_journal
from .summary import data_deliverables, summarize

__all__ = [
    "POLICY_HASH",
    "SEMANTIC_KEY",
    "SCHEMA_VERSION",
    "SemanticJournal",
    "SemanticVerdict",
    "ChatSelfCheckProvider",
    "FakeSelfCheckProvider",
    "apply_parsed",
    "data_deliverables",
    "detect_triggers",
    "enabled",
    "load_prompt",
    "merge_self_check",
    "parse_verdict",
    "policy_fingerprint",
    "prepare_journal",
    "qualify_evidence",
    "reasons_from_verdict",
    "summarize",
]
