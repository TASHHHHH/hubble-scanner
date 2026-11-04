# Release checklist

- Run the Windows test suite, offline demo and cluster smoke test.
- Verify the distributed setup instructions against the release version.
- Exclude credentials, certificates, caches, model weights and local workspaces.
- Record changes and known limitations in the release notes.
- Build the source archive from tracked files and publish its SHA-256 checksum.

This project currently distributes Python source. Installation instructions are
in README.md; distributed deployment instructions are in DISTRIBUTED_SCAN.md.
