# Security

The coordinator is a trusted-LAN prototype, not a public multi-tenant service.
Bearer tokens travel over HTTP without TLS. Restrict access to trusted workers.
Never publish tokens, personal paths or private datasets in an issue.
Use isolated environments for untrusted FITS files or coordinator connections.
Report vulnerabilities privately to the repository owner using GitHub private
vulnerability reporting if enabled. Do not include secrets in reports.
