# Security Policy

## Supported versions

Only the latest released version of TurboSSH on PyPI receives fixes. Please
upgrade (`pip install -U turbossh`) before reporting an issue.

## Reporting a vulnerability

Please report security issues **privately**, not via a public GitHub issue:

- Preferred: open a private advisory at
  <https://github.com/nvnkennedy/turbossh/security/advisories/new>
- Or email: nvnkennedy@gmail.com

Include a description, affected version(s), and steps to reproduce. You'll get an
acknowledgement as soon as possible, and a fix released once verified.

## Notes on TurboSSH's threat model

TurboSSH is a remote-access and lab-automation tool. A few behaviours are
deliberate and documented rather than bugs:

- **`host_key_policy="ignore"`** (the GUI default for lab devices) accepts any
  host key, including a changed one — convenient for re-imaged/DHCP hosts but
  not appropriate for untrusted networks. Use `"auto"` or `"reject"` there.
- **`enable_legacy_algorithms=True`** intentionally re-enables weak KEX/ciphers
  for old embedded/automotive SSH servers.
- **WinRM bootstrap over plain HTTP / `winrm_use_ssl` with cert validation off**
  is a lab convenience; use TLS with validation on real networks.
- **Terminal captures** are written as plaintext under `~/.turbossh/captures/`
  so "Save all output" can cover a whole session; they're removed on clean close
  and swept after a day (see the README).

Passwords are wrapped in `Secret`, masked in logs, stored in the OS keyring (not
plaintext), and are not passed on process command lines.
