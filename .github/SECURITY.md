# Security Policy

## Reporting Security Vulnerabilities

**Please do not report security vulnerabilities through public GitHub issues.**

If you discover a security vulnerability, please report it by emailing [vtmocanu@gmail.com](mailto:vtmocanu@gmail.com).

Please include the following information in your report:

- Description of the vulnerability
- Steps to reproduce the issue
- Affected versions
- Potential impact
- Any suggested fixes (if available)

## Response Timeline

We will acknowledge your report within **3 business days** and provide a detailed response within **7 business days** indicating the next steps in handling your report.

We will keep you informed of the progress towards a fix and may ask for additional information or guidance.

## Scope

This repository contains agent skill definitions (Markdown). The most relevant risks are skills that instruct an agent to run unsafe commands, leak secrets, or reference malicious resources. Reports about such content are in scope.

## Supported Versions

| Version | Supported |
| ------- | --------- |
| 0.x     | Yes       |

## Security Best Practices

When using these skills, we recommend:

- Review a skill's instructions before letting an agent act on them.
- Run agents with least-privilege credentials.
- Keep your local copy up to date with the latest release.
