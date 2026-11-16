#!/usr/bin/env python3
"""Audit pinned dependencies with pip-audit.

The build fails when a known vulnerability already has a fixed release (the
pin must be raised). A vulnerability without any fixed release is reported as
a warning: blocking every build would not make it fixable.
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REQUIREMENTS = ("requirements.txt", "terminal/requirements.txt")


def classify(report):
    """Split a pip-audit JSON report into (fixable, unfixable, skipped) rows."""
    fixable, unfixable, skipped, seen = [], [], [], set()
    for dependency in report.get("dependencies", []):
        name = dependency.get("name", "?")
        if dependency.get("skip_reason"):
            skipped.append({"name": name, "reason": dependency["skip_reason"]})
            continue
        for vulnerability in dependency.get("vulns", []):
            # pip-audit can list the same advisory more than once.
            key = (name, dependency.get("version"), vulnerability.get("id"))
            if key in seen:
                continue
            seen.add(key)
            row = {"name": name, "version": dependency.get("version", "?"),
                   "id": vulnerability.get("id", "?"),
                   "aliases": list(vulnerability.get("aliases") or []),
                   "fix_versions": list(vulnerability.get("fix_versions") or [])}
            (fixable if row["fix_versions"] else unfixable).append(row)
    return fixable, unfixable, skipped


def describe(row):
    names = " / ".join([row["id"]] + row["aliases"])
    fix = ", ".join(row["fix_versions"]) or "sem versão corrigida"
    return f"{row['name']}=={row['version']}: {names} (correção: {fix})"


def annotate(level, message):
    prefix = f"::{level}::" if os.environ.get("GITHUB_ACTIONS") else f"{level.upper()}: "
    print(prefix + message)


def run_pip_audit(requirements):
    command = [sys.executable, "-m", "pip_audit", "--format", "json", "--progress-spinner", "off"]
    for path in requirements:
        command += ["-r", str(path)]
    result = subprocess.run(command, capture_output=True, text=True, cwd=ROOT)
    # Exit status 1 means "vulnerabilities found"; the JSON report tells which.
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise SystemExit("pip-audit não gerou relatório; a auditoria não foi concluída.\n"
                         + (result.stderr or result.stdout)[-2000:])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-r", "--requirement", action="append", type=Path,
                        help="Requirements file (default: root and terminal lists)")
    parser.add_argument("--report", type=Path, help="Read an existing pip-audit JSON report instead of running it")
    args = parser.parse_args(argv)
    if args.report:
        report = json.loads(args.report.read_text())
    else:
        report = run_pip_audit(args.requirement or DEFAULT_REQUIREMENTS)
    fixable, unfixable, skipped = classify(report)
    for row in skipped:
        annotate("warning", f"{row['name']} não pôde ser auditado: {row['reason']}")
    for row in unfixable:
        annotate("warning", "Vulnerabilidade ainda sem correção publicada: " + describe(row))
    for row in fixable:
        annotate("error", "Vulnerabilidade com correção disponível: " + describe(row))
    audited = len(report.get("dependencies", [])) - len(skipped)
    print(f"{audited} dependências auditadas; {len(fixable)} com correção disponível, "
          f"{len(unfixable)} sem correção.")
    return 1 if fixable else 0


if __name__ == "__main__":
    sys.exit(main())
