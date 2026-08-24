# Trifour fork: iris-vector-graph

**Upstream:** https://github.com/intersystems-community/iris-vector-graph  
**Fork:** https://github.com/cagerber/iris-vector-graph

## Fork policy (story 9-375, 2026-08-24)

- **Name carries fork identity:** IPM modules are `trifour-iris-vector-graph` and
  `trifour-iris-vector-graph-core`. The legacy upstream names (`iris-vector-graph`,
  `iris-vector-graph-core`) are **not used for new installs**; existing environments
  migrate lazily side-by-side (uninstall the old module explicitly per site).
- **Versions are always plain `x.x.x`.** No PEP 440 local segments (`+trifour.n`) —
  that suffix remains valid only on the Python SDK *wheel* distribution
  (`iris-vector-graph` on PyPI/GitLab index), which is a separate artefact from these
  IPM modules.
- **Upstream base is recorded at every merge**, in three places:
  1. this file,
  2. the XML comment at the top of `module.xml` / `module-core.xml`,
  3. the ODS catalog (`tools/deploy/ipm-catalog.yaml`, `upstream_base:` key).

## Current state

| Field | Value |
|-------|-------|
| Upstream base | `v2.7.0` (merge commit `caf9241`) |
| Fork module version | `2.7.1` |
| Renamed modules | `module.xml`, `module-core.xml` |
| Not renamed | `module-vector.xml` (unused by ODS packaging) |

## Upstream merge recipe

```bash
git remote -v                 # origin/cagerber = fork, upstream = intersystems-community
git fetch upstream
git merge upstream/main       # resolve, keep trifour- names + plain versions
# then update: FORK.md table, module.xml/module-core.xml comments + <Version>,
# and ODS tools/deploy/ipm-catalog.yaml upstream_base / version keys.
```
