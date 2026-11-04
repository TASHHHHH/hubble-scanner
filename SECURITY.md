# Security

Remote workers must connect through the TLS coordinator using individual,
revocable credentials. The GUI HTTP listener is restricted to localhost.
Never expose the legacy LAN-only coordinator to the internet.

Telegram credentials are encrypted with Windows current-user DPAPI and remain
on the coordinator. Processes running as that Windows user can decrypt them;
keep that account trusted. Workers never receive the Telegram token.

Do not include tokens, private paths, certificates or datasets in public issues.
Report vulnerabilities privately to the maintainer through GitHub private
vulnerability reporting when enabled.
