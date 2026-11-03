# Security and privacy

This is a local review candidate; there is no supported release or published
security-reporting endpoint yet. Report suspected vulnerabilities directly to
the maintainer through the private channel where this checkout was shared. Once
a public repository exists, its owner should enable GitHub private vulnerability
reporting and document that link here. Do not disclose credentials or exploitable
details in a public issue.

Include a minimal reproduction with synthetic data, affected files or revision,
impact, and suggested mitigation. Redact tokens, private domains, user IDs,
addresses, and conversations. Rotate exposed credentials separately; deleting
a file does not erase it from Git history.

Runtime configuration and diagnostics belong in ignored `local/`. Protect that
directory and back up decryption keys separately. The privacy audit is a heuristic,
not proof of anonymity. Before publication, review every eligible file and scan
reachable Git history and the archive.

Remote assistance is optional. Its scope and remaining risks are in the
[Hermes guide](docs/hermes.md). Verify permissions and policies on the actual
cluster before supplying provider or messaging credentials.
