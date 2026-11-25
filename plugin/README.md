# The RomM plugin lives in its own repository

The manifest is not kept here. A copy in this tree drifts from the one people
install, and the published file is the only one that can be installed and
validated against a running server.

- Repository: https://github.com/jkoehler11/rommarcade-plugin
- Manifest: https://raw.githubusercontent.com/jkoehler11/rommarcade-plugin/main/plugin.json

That repository also carries the `rommarcade://` handler for desktop installs
that did not register a scheme handler. Install the plugin in RomM under
**Settings → Plugins → Add plugin**, which needs a server with the plugin shelf
(ROMulus today; stock RomM has none).
