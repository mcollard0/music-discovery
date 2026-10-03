# Security and private configuration

Keep credentials in a service-readable credential file outside version control.
Copy `config.example.json` to ignored `config.json` and set local paths and settings.
Example values are installation conventions, not an existing deployment.
Protect configuration and credential files with restrictive permissions.
Logs and databases contain music metadata and must remain private.

Before committing, review `git diff --cached` and `git ls-files`; ignored files
already tracked by Git remain tracked. Scan the complete history before publishing
an existing repository. If a credential was committed, revoke it before cleaning
history. Do not submit tokens, logs, database dumps, or library inventories in issues.
Use GitHub's private vulnerability reporting when enabled for security reports.

Root installation commands write system files. Read the installer before running
it. Application execution should use its configured service account. Installation,
scheduling, and starting work are separate actions; check each project's README.
