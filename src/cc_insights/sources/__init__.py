"""Source adapters. Each log-producing tool gets exactly one module here."""

from cc_insights.sources.base import EventKind, RawEvent, SourceAdapter

__all__ = ["EventKind", "RawEvent", "SourceAdapter"]
