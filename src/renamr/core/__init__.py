"""Local-first core for renamr: catalog, extraction, rules, and safe mutation.

Public usage is documented in ``/tmp/renamr-core-api.md``. Import concrete
symbols from this package rather than reaching into submodules.
"""

from __future__ import annotations

from renamr.benchmark_schema import RulePredicate
from renamr.core.catalog import (
    FactRecord,
    LocationRecord,
    PageMethod,
    PageRecord,
    Presence,
    QualityState,
    RevisionRecord,
)
from renamr.core.db import Database
from renamr.core.errors import (
    CoreError,
    DatabaseCorruptError,
    DatabaseNewerVersionError,
    DestinationOccupiedError,
    PathBoundaryError,
    PreconditionError,
    RuleError,
    StateError,
)
from renamr.core.extract import (
    AssetStatus,
    ExtractionResult,
    ExtractorLimits,
    OCRText,
    detect_media_type,
    extract_document,
    preflight_ocr_assets,
)
from renamr.core.extract_runner import (
    ExtractionRunner,
    InProcessRunner,
    SubprocessRunner,
    build_runner,
)
from renamr.core.features import (
    FeatureExtraction,
    FeatureExtractor,
    FeatureRequest,
    LocalFeatureExtractor,
    facts_to_features,
    feature_cache_key,
    text_extract_key,
)
from renamr.core.history import LegacyUndoPreview, UndoCandidate, UndoOutcome
from renamr.core.inference import LocalInference
from renamr.core.journal import FaultInjector, FaultPoint, ItemState
from renamr.core.lock import WriterLock
from renamr.core.migration import (
    MigrationPreview,
    MigrationResult,
    apply_legacy_migration,
    migration_applied,
    preview_legacy_migration,
)
from renamr.core.mutation import (
    apply,
    history_runs,
    legacy_undo,
    preview_routing,
    recover,
    restore,
    run_items,
    undo,
)
from renamr.core.operations import ApplyResult, RecoveryResult, RunItem
from renamr.core.reconcile import ScanOutcome, ScanStatus
from renamr.core.roots import Root, RootRole
from renamr.core.rules import RoutingDecision, Rule, RuleStatus, render_base_name, select_rule
from renamr.core.settings import (
    DEFAULT_SETTINGS,
    ModelRoles,
    Settings,
    load_settings,
    save_settings,
)
from renamr.core.workflow import (
    activate_rule,
    active_rules,
    create_rule_draft,
    export_text,
    get_pages,
    get_root,
    limits,
    list_roots,
    list_rules,
    mark_tested,
    register_root,
    retry_revision,
    review_items,
    rule_history,
    scan,
)
from renamr.core.workflow import search as search_pages
from renamr.core.workspace import Workspace, open_workspace

__all__ = [
    "DEFAULT_SETTINGS",
    "ApplyResult",
    "AssetStatus",
    "CoreError",
    "Database",
    "DatabaseCorruptError",
    "DatabaseNewerVersionError",
    "DestinationOccupiedError",
    "ExtractionResult",
    "ExtractionRunner",
    "ExtractorLimits",
    "FactRecord",
    "FaultInjector",
    "FaultPoint",
    "FeatureExtraction",
    "FeatureExtractor",
    "FeatureRequest",
    "InProcessRunner",
    "ItemState",
    "LegacyUndoPreview",
    "LocalFeatureExtractor",
    "LocalInference",
    "LocationRecord",
    "MigrationPreview",
    "MigrationResult",
    "ModelRoles",
    "OCRText",
    "PageMethod",
    "PageRecord",
    "PathBoundaryError",
    "PreconditionError",
    "Presence",
    "QualityState",
    "RecoveryResult",
    "RevisionRecord",
    "Root",
    "RootRole",
    "RoutingDecision",
    "Rule",
    "RuleError",
    "RulePredicate",
    "RuleStatus",
    "RunItem",
    "ScanOutcome",
    "ScanStatus",
    "Settings",
    "StateError",
    "SubprocessRunner",
    "UndoCandidate",
    "UndoOutcome",
    "Workspace",
    "WriterLock",
    "activate_rule",
    "active_rules",
    "apply",
    "apply_legacy_migration",
    "build_runner",
    "create_rule_draft",
    "detect_media_type",
    "export_text",
    "extract_document",
    "facts_to_features",
    "feature_cache_key",
    "get_pages",
    "get_root",
    "history_runs",
    "legacy_undo",
    "limits",
    "list_roots",
    "list_rules",
    "load_settings",
    "mark_tested",
    "migration_applied",
    "open_workspace",
    "preflight_ocr_assets",
    "preview_legacy_migration",
    "preview_routing",
    "recover",
    "register_root",
    "render_base_name",
    "restore",
    "retry_revision",
    "review_items",
    "rule_history",
    "run_items",
    "save_settings",
    "scan",
    "search_pages",
    "select_rule",
    "text_extract_key",
    "undo",
]
