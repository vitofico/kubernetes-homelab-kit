# Security and privacy

Report suspected vulnerabilities through
[GitHub private vulnerability reporting](https://github.com/vitofico/kubernetes-homelab-kit/security/advisories/new).
Do not disclose credentials or exploitable details in a public issue.

This project is an early-stage template with no tagged stable release yet.
Security fixes are developed on the default branch; no support window or
response-time guarantee has been established.

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
