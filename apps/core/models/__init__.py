"""
apps/core/models - Django ORM models for every plant's PO/MIR/Stock data,
their reconciliation (*Match) tables, and the auth/audit/correction tables.

Deliberately NOT one shared schema with a `plant` discriminator column.
HRS, RTP-Vapi, and RTP-Achhad's MIR/Stock files have genuinely different
column layouts (Vapi/Achhad's Stock file covers several sub-plants in one
file with extra columns - Billing on Plant, Material Location, HSN Code -
that HRS's file doesn't have at all). Forcing them into one shared table
would mean a pile of always-null columns for whichever plants don't have
that field, and would tempt matching logic that silently assumes a field
exists uniformly across plants when it doesn't. Each plant gets its own
model classes instead - see CLAUDE.md's "Per-plant models, not a shared
schema" section for the full, confirmed-against-real-files reasoning.

Three independent Drive-sourced record types per plant (PurchaseOrder/
POLineItem, MIREntry, StockLot), each synced by its own management command,
plus the reconciliation layer that links them (*POMirMatch/*MirStockMatch)
and a daily snapshot table that replaces the "copy the whole xlsx to Drive
with today's date in the filename" habit with real queryable history.

Because HRS/Achhad/Vapi's Domestic PO, MIR, Stock, and match models are
near-identical triplets by design (same real-world shape, genuinely
different only where a plant's source spreadsheet is genuinely different -
see each section's own header comment), only the FIRST plant's version of
each model family (always HRS) carries a full field-by-field docstring/
comment set below. The other two plants' equivalent classes carry a short
"same shape as HRS<Model> - see that class" docstring and comment only the
fields that are genuinely different for that plant - re-deriving the same
explanation three times would drift out of sync with itself over time.

All three plants (HRS, RTP-Achhad, RTP-Vapi) are now built.

PACKAGE LAYOUT (split 2026-09-23, audit pass)
---------------------------------------------
This was one 3,255-line models.py. It is now one module per plant plus one per shared
concern, each class moved verbatim with the comments above it:

  sync.py         The shared sync job log - the one table every plant's pipeline writes to.
  hrs.py          HRS (Hindustan Rubbers, Silvassa): Domestic and Import POs, MIR, RM Stock, and their matches.
  achhad.py       RTP-Achhad: Domestic and Import POs, MIR, RM Stock (+ its dated daily movement matrix), and matches.
  vapi.py         RTP-Vapi: Domestic and Import POs, MIR, RM Stock, and their matches.
  review.py       Cross-plant human decisions and reference data: corrections, dismissals, manual MIR pins,
  auth.py         Accounts and auth state: PTUser, OTP codes, revoked refresh tokens, trusted devices.
  reports.py      Report dedup log (claim-before-send for the scheduled report emails).
  ledgers.py      Company-wide licence ledgers: RoDTEP scrips/usage and Advance Licences/materials.
  consumption.py  The raw-material consumption ledger: daily rows, excluded events, and observed-day coverage.

Every class is re-exported below, so `from apps.core.models import X` - the only import
style this codebase uses - is unchanged everywhere. Django registers models by app label,
not by module path, and migrations name them `core.<Model>`, so no migration was needed:
`makemigrations --check` reports no changes. This is a split of FILES, not a merge of
MODELS - the per-plant model classes stay separate on purpose (CLAUDE.md, "Per-plant
models, not a shared schema").
"""

from .sync import (  # noqa: F401
    SyncRun,
)
from .hrs import (  # noqa: F401
    HRSDomesticPurchaseOrder,
    HRSDomesticPOLineItem,
    HRSMIREntry,
    HRSRMLot,
    HRSRMSnapshot,
    HRSPOMirMatch,
    HRSMirStockMatch,
    HRSImportPurchaseOrder,
    HRSImportPOLineItem,
    HRSImportPOMirMatch,
)
from .achhad import (  # noqa: F401
    RTPAchhadDomesticPurchaseOrder,
    RTPAchhadDomesticPOLineItem,
    RTPAchhadMIREntry,
    RTPAchhadRMLot,
    RTPAchhadRMSnapshot,
    RTPAchhadRMDailyMovement,
    RTPAchhadPOMirMatch,
    RTPAchhadMirStockMatch,
    RTPAchhadImportPurchaseOrder,
    RTPAchhadImportPOLineItem,
    RTPAchhadImportPOMirMatch,
)
from .vapi import (  # noqa: F401
    RTPVapiDomesticPurchaseOrder,
    RTPVapiDomesticPOLineItem,
    RTPVapiMIREntry,
    RTPVapiRMLot,
    RTPVapiRMSnapshot,
    RTPVapiPOMirMatch,
    RTPVapiMirStockMatch,
    RTPVapiImportPurchaseOrder,
    RTPVapiImportPOLineItem,
    RTPVapiImportPOMirMatch,
)
from .review import (  # noqa: F401
    ImportPOCorrection,
    DomesticPOCorrection,
    MaterialCorrection,
    FlagDismissal,
    ManualMirMatch,
    MaterialCategoryReference,
    DataQualityFlag,
    MatchReview,
)
from .auth import (  # noqa: F401
    PTUser,
    OTPCode,
    RevokedRefreshToken,
    TrustedDevice,
)
from .reports import (  # noqa: F401
    ReportSendLog,
)
from .ledgers import (  # noqa: F401
    RodtepScrollEntry,
    RodtepUsage,
    AdvanceLicense,
    AdvanceLicenseMaterial,
)
from .consumption import (  # noqa: F401
    MaterialConsumptionDaily,
    ConsumptionEvent,
    ConsumptionCoverage,
)

__all__ = [
    "SyncRun",
    "HRSDomesticPurchaseOrder",
    "HRSDomesticPOLineItem",
    "HRSMIREntry",
    "HRSRMLot",
    "HRSRMSnapshot",
    "HRSPOMirMatch",
    "HRSMirStockMatch",
    "RTPAchhadDomesticPurchaseOrder",
    "RTPAchhadDomesticPOLineItem",
    "RTPAchhadMIREntry",
    "RTPAchhadRMLot",
    "RTPAchhadRMSnapshot",
    "RTPAchhadRMDailyMovement",
    "RTPAchhadPOMirMatch",
    "RTPAchhadMirStockMatch",
    "RTPVapiDomesticPurchaseOrder",
    "RTPVapiDomesticPOLineItem",
    "RTPVapiMIREntry",
    "RTPVapiRMLot",
    "RTPVapiRMSnapshot",
    "RTPVapiPOMirMatch",
    "RTPVapiMirStockMatch",
    "HRSImportPurchaseOrder",
    "HRSImportPOLineItem",
    "HRSImportPOMirMatch",
    "RTPAchhadImportPurchaseOrder",
    "RTPAchhadImportPOLineItem",
    "RTPAchhadImportPOMirMatch",
    "RTPVapiImportPurchaseOrder",
    "RTPVapiImportPOLineItem",
    "RTPVapiImportPOMirMatch",
    "ImportPOCorrection",
    "DomesticPOCorrection",
    "MaterialCorrection",
    "FlagDismissal",
    "ManualMirMatch",
    "MaterialCategoryReference",
    "DataQualityFlag",
    "MatchReview",
    "PTUser",
    "OTPCode",
    "RevokedRefreshToken",
    "ReportSendLog",
    "TrustedDevice",
    "RodtepScrollEntry",
    "RodtepUsage",
    "AdvanceLicense",
    "AdvanceLicenseMaterial",
    "MaterialConsumptionDaily",
    "ConsumptionEvent",
    "ConsumptionCoverage",
]
