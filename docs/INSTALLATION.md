# Linux host and remote setup

## Install and identify

Use the README's virtual-environment setup. On systems without libusb, install
the distribution's libusb runtime (on Debian/Mint, `libusb-1.0-0`). Connect the
scope's rear USB Device port. The front USB host port is not the control connection.

Review `deploy/70-rigol-ds1202ze.rules`, then install it as root in
`/etc/udev/rules.d/`. Create a `rigol` group and add the intended host account.
Reload udev rules and reconnect the scope. A fresh login may be needed for group
membership. Do not run the MCP server as root or grant every user USB write access.

```sh
.venv/bin/rigol-mcp --list-usb
.venv/bin/rigol-mcp --resource 'YOUR_ACTUAL_VISA_RESOURCE' --identify
```

Use the exact address returned, including the instrument serial. More than one
instrument requires an explicit choice. Only DS1202Z-E is supported. Identity
queries alone do not establish measurement wiring, accuracy or installed options.

## Restricted SSH stdio

The generic launcher and client example contain placeholders; edit all of them
before use. Keep host configuration separate from a public source checkout.

1. Install the server on the host and set the absolute executable/resource in a
   private copy of `deploy/run-scope.sh`. Give that script executable permissions.
   It ignores caller commands and explicitly chooses whether controls are enabled.
2. Generate a dedicated SSH key on each client. Keep its private key there.
3. Add only the public key to the host's `authorized_keys`, with options such as
   `restrict,command="/absolute/path/to/run-scope.sh"` before the key text.
   Verify the host OpenSSH supports these restrictions and permits only the launcher.
4. Independently verify the host key and establish its known-hosts entry before
   noninteractive MCP use. Do not disable host-key checking.
5. Adapt `examples/mcp-ssh.json` for the client account/key, host and launcher.
   Set the MCP client's tool timeout to at least 180 seconds for default captures.

The restricted key is not an administration or file-copy key. Use a separate
authorized administrative channel for installation. No HTTP port or USB-over-IP
service is needed. Do not expose an unauthenticated replacement transport.

## Operation and upgrades

Start with `inspect_signal`; discover advanced operations only when needed.
Completed artifacts are stored under the host account's `~/.cache/rigol-mcp`.
Retention is bounded: archive important evidence before expiry. Different clients
using the same host account can access that account's saved artifacts.

Before upgrading, finish calls and retain the prior wheel/setup/evidence. Test a
candidate in an isolated environment; install a selected wheel and verify a fresh
MCP connection. A package rollback does not restore physical instrument settings.
Remove a restricted public-key entry to revoke that client. Remove its MCP config
separately. No daemon, automatic firmware update or calibration is installed.
