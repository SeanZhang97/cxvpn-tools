# Third-party notices

## Mihomo

- Project: https://github.com/MetaCubeX/mihomo
- Version: v1.19.30-cxvpn.2 (modified 2026-09-14 and 2026-09-15)
- License: GNU General Public License v3.0
- License text: https://github.com/MetaCubeX/mihomo/blob/Meta/LICENSE
- Source corresponding to this release:
  https://github.com/MetaCubeX/mihomo/tree/v1.19.30

`mihomo.exe` includes CXVPN's Windows VPN route preparation hook and remains
licensed under GPL-3.0. The complete corresponding modified source, including
the license, dependency manifests and build instructions, accompanies the binary
as `mihomo-source.zip`. Checksums and compiler metadata are in `mihomo-build.json`.
The maintained patch and build script are `patches/mihomo/` and `build_mihomo.py`
in the CXVPN source tree. Mihomo remains a separate program; route preparation
uses a bounded local Named Pipe before its interface-bound TCP/UDP operations.

## MetaCubeX meta-rules-dat

- Project: https://github.com/MetaCubeX/meta-rules-dat
- Asset: country-lite.mmdb snapshot downloaded 2026-09-02
- License: GNU General Public License v3.0
- Release: https://github.com/MetaCubeX/meta-rules-dat/releases/tag/latest

The unmodified `country-lite.mmdb` asset is distributed as `Country.mmdb` for
offline `GEOIP,CN` matching. Its SHA-256 is recorded in `README.md`.

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
