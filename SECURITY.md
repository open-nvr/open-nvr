# Security Policy

## Supported Versions

| Version | Supported          |
| ------- | ------------------ |
| 1.0.x   | :white_check_mark: |
| < 1.0   | :x:                |

## Reporting a Vulnerability

We take the security of OpenNVR seriously. If you have found a vulnerability, please report it responsibly.

**DO NOT** open a public issue for sensitive security vulnerabilities.

### How to Report
Please email the maintainers directly or use GitHub's "Report a vulnerability" feature if enabled.

### Response Timeline
-   We will acknowledge your report within 48 hours.
-   We will provide a timeline for a fix within 1 week.
-   We will coordinate public disclosure after the fix has been released.

## Security Best Practices for Deployment
-   **Change Default Passwords:** Always change the default admin password immediately after installation.
-   **Secure .env:** Ensure your `.env` file is not accessible via the web and permissions are restricted (e.g., `chmod 600`).
-   **HTTPS:** Always use HTTPS in production.
-   **Firewall:** Restrict access to the API and MediaMTX ports to trusted networks only.
