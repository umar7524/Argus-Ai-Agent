# Security and privacy

ARGUS is a local-first prototype, **not** a hardened multi-user service. Do not expose the Streamlit port to the public internet or use untrusted files without reviewing the security implications.

- Keep `.env`, `credentials.json`, OAuth tokens, `vault/`, and `data/` private. They are excluded by `.gitignore`.
- **Rotate API keys** that have ever been exposed in shared files, commits, screenshots, or logs.
- Research and Vault run with distinct MCP tool sets, but this is application-level separation, not a sandbox.
- When using a remote LLM, text retrieved from Vault documents may be transmitted to the selected inference provider.
- Download URLs are checked against private addresses, but URL redirects/DNS changes require additional hardening before working with untrusted inputs.
- File edits in Vault should be reviewed; current tool permissions do not require a separate approval click for every edit.
- Gmail send and web downloads require user confirmation in the UI. Gmail OAuth scopes include send and readonly.

Please report security issues privately to the repository owner rather than opening a public issue containing credentials or private files.
