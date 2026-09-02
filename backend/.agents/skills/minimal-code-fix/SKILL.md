---
name: minimal-code-fix
description: Implement narrowly scoped production code fixes without changing configuration, architecture, or language and runtime versions.
---

# Minimal Code Fix

Apply this contract to every automated code-change task.

## Required approach

- Locate the smallest existing call chain that proves the failure.
- Fix the root cause with the fewest practical source changes.
- Preserve the current architecture, module boundaries, public contracts, and data flow.
- Reuse existing helpers and patterns; do not introduce a new framework, abstraction layer, service, module, or fallback subsystem.
- Add or adjust only the nearest focused test when a test is needed to prove the fix.
- Keep unrelated formatting, refactoring, renaming, cleanup, and dependency work out of the patch.

## Prohibited changes

- Do not edit build, dependency, runtime, deployment, CI, repository, or application configuration files.
- Do not change Java, Kotlin, Scala, Python, Node.js, Go, or other language/runtime versions.
- Do not change framework, plugin, compiler, build-tool, or dependency versions.
- Do not redesign architecture or move responsibilities between modules.
- Do not modify generated files, lockfiles, infrastructure, deployment assets, or GitHub workflow files.

If the issue cannot be fixed within the allowed source paths, stop without changing a prohibited file. Report the exact source locations inspected and the configuration or architectural change that would require separate human approval.
