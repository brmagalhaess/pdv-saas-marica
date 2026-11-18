"""Brazilian fiscal rules for NFC-e/NF-e: validators, tax profiles and the tax engine.

Pure Python (standard library only) so the cloud, the terminal and the tests
share the exact same rules. See docs/FISCAL.md.
"""
from .engine import SCHEMA, allocate_discount, commercial_gtin, compute_document, compute_item, line_gross
from .readiness import company_issues, has_errors, product_issues, summary
from .schema import FiscalFieldError, clean_fields, legacy_fields, resolve

__all__ = ["SCHEMA", "allocate_discount", "commercial_gtin", "compute_document", "compute_item", "line_gross",
           "company_issues", "has_errors", "product_issues", "summary", "FiscalFieldError", "clean_fields",
           "legacy_fields", "resolve"]
