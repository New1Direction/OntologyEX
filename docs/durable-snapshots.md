# Durable GitHub inventory snapshots — experimental single-operator cache

## Purpose

Keep the last successful GitHub source graph across a server process restart.
The browser does not download or store the graph for recovery. This feature
does not create a hosted tenant database, object store, backup or encryption.

## Usage

Create a server-owned parent directory, then start the existing read-only
GitHub control room with an opt-in private snapshot directory:

    mkdir -p -m 700 /srv/ontologyex-private
    python ontology-extraction/scripts/control_room.py \
      --github New1Direction/repo.ing \
      --snapshot-dir /srv/ontologyex-private/inventory \
      --github-refresh-seconds 300

The snapshot directory must be owned by the process user and inaccessible to
other users (0700). The database must be a regular singly-linked file (0600).
Symlinked paths, overly broad permissions and hardlinked DB files are rejected.
On Unix/Linux hosts, run under a dedicated unprivileged process identity.

## Behavior

- First scan builds the same commit-pinned source graph as PR #17.
- Each valid graph is saved in one SQLite transaction with SHA-256 checksum,
  repository ID, SHA-1 Git commit, time and evidence metadata.
- If source content or CI evidence changes, an immutable digest identifies
  that distinct graph. Repeating the same exact graph is idempotent.
- Retains the latest **eight** versions per GitHub numeric repository ID;
  earlier graphs are pruned. This is not an archival guarantee.
- On restart, the server loads only the exact requested repository, verifies
  digest, identity, schema and dangling edges, and marks it STALE_LAST_KNOWN
  until GitHub fetch succeeds again.
- If GitHub is unavailable or persistence fails, the previous snapshot stays
  visible and stale. It never silently becomes a fresh scan.
- A snapshot is not a health signal: all uninstrumented nodes remain UNKNOWN.
- The result includes compact snapshot_history metadata and scanned_at,
  but no raw manifest contents or credentials.

## Security and operational limits

This SQLite cache is **not encrypted by the application**. Use an encrypted
server volume with proper access policy and backup encryption if your
deployment requires protected at-rest storage. Graph names, paths, and
dependencies can be sensitive even without raw source code.

Do not run a shared, multi-tenant SaaS on this cache. No workspace identity
or user authentication is present. Repositories are logically keyed by
GitHub numeric ID, but the actual tenant isolation boundary must be
implemented separately, authenticated and security-tested.

The storage contract accepts only a narrowly defined GitHub source-preview
schema and refuses arbitrary extra fields, dangling references, invented
production health, forged CI scope and invalid evidence/commit identities.
The hash identifies stored bytes; it does not prove semantic truth or source
authenticity beyond the original pinned-blob fetch.

SQLite rollback journal uses a private directory and synchronous commits.
No customer source files or database rows are persisted; only the explicitly
selected graph metadata. Network/CPU/disk budgets are still the existing
prototype constraints, not a hosted queue or distributed scheduler.

Deletion is manual and requires external authorization; the prototype has
no web-based delete API. Before any hosted release, implement user-facing
export/deletion rights, tenant-scoped keys, verifiable backups and restore
drills, durable worker recovery, connector revocation and audit events.

## Tests

    python -m unittest discover -s tests -v

Tests cover repeated scans, retained versions, same-commit CI changes,
stale-on-restart and recovery, an unwritable store, cross-project isolation,
corruption refusal, file permissions, symlink rejection, missing evidence,
extra/credential fields and no fabricated production health.

## Dependency chain

This is a draft on top of #17 (GitHub discovery), which depends on
#14 (local control room) and #11 (source scanner). No production deployment.
