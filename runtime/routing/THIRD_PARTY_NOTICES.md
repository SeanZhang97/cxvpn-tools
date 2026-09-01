# Third-party notices

## Mihomo

- Project: https://github.com/MetaCubeX/mihomo
- Version: v1.19.30
- License: GNU General Public License v3.0
- License text: https://github.com/MetaCubeX/mihomo/blob/Meta/LICENSE
- Source corresponding to this release:
  https://github.com/MetaCubeX/mihomo/tree/v1.19.30

`mihomo.exe` is distributed unmodified. Mihomo is a separate program launched and
controlled through its documented configuration and REST API.

## Windows Service Wrapper (WinSW)

- Project: https://github.com/winsw/winsw
- Version: v2.12.0
- License: MIT
- License text: https://github.com/winsw/winsw/blob/v2.12.0/LICENSE.txt

`WinSW-x64.exe` is distributed unmodified.

## Rust libraries linked into CXVPNRoutingHost.exe

The first-party service is built from `routing-service/Cargo.lock`. Its runtime
dependencies include `windows-service`, `windows-sys`, `serde`, `serde_json`,
`sha2`, `base64` and their transitive dependencies. These dependencies are
available under MIT or MIT/Apache-2.0 terms; CXVPN selects the MIT option where
dual licensing is offered. Package names and exact versions are preserved in
`routing-service/Cargo.lock` in the source distribution.
