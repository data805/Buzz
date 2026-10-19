# Decisions log

Record every deviation from the brief, and every non-obvious choice, with the reason.

| # | Date | Decision | Reason | Approved by |
|---|------|----------|--------|-------------|
| 1 | 2026-10-15 | The platform is branded "Buzz". The primary logo is black on a transparent background. | Requested by the product owner. The source logo was white on black. | Product owner |
| 2 | 2026-10-19 | Buzz is rebranded as a BongoHive AI Lab product: Neural Slate mark with a gold waveform accent, a geometric wordmark that echoes the BongoHive logotype, and a co-brand lockup (Buzz \| BongoHive AI Lab). The primary UI logo is now `branding/svg/buzz-logo-horizontal-color.svg`; supersedes the black-logo choice in #1. | Buzz is built by the BongoHive AI Lab, so the Lab's sub-brand colours apply. | Product owner |
| 3 | 2026-10-19 | Number rules (Zambian E.164, premium, short-code and international refusal) live in the suppression service, not the dialplan. | They can be unit-tested in Python; the dialplan keeps only hours, channel limit and fail-closed handling. | Pending product-owner review |
| 4 | 2026-10-19 | The inbound disclosure is played by Asterisk, then the call is handed to Dograh through a fresh Local channel. | Dograh's greeting can be interrupted and Dograh rejects inbound channels that are already answered (PLAN B6, B7). | Pending product-owner review |
| 5 | 2026-10-19 | Dial String Template `Local/{number}@buzz-outbound/n`; transfers to `Local/<ext>@buzz-transfer/n` with a 60 s timeout. | Stops Local-channel optimisation and keeps screening inside Dograh's transfer wait (PLAN B8, B9). | Pending product-owner review |
| 6 | 2026-10-19 | `opt_out` tool on every Start and Agent node instead of the Global node. | Global nodes cannot carry tools in 1.49.0 (PLAN B7). | Pending product-owner review |
| 7 | 2026-10-19 | nginx pinned to 1.30.5-alpine instead of 1.28. | 1.28 is no longer maintained. | Pending product-owner review |
| 8 | 2026-10-19 | Backups encrypted with age (public keys on the server, private keys held offline by two custodians); `mc` built from source. | Simple key handling; MinIO no longer ships `mc` (PLAN B13). | Pending product-owner review |
| 9 | 2026-10-19 | Speaches models downloaded in maintenance and loaded by a warm-up service; no `PRELOAD_MODELS`. | Speaches cannot start offline with it (PLAN B1). | Pending product-owner review |
| 10 | 2026-10-19 | Recordings in MinIO reachable from the browser only from staff networks. | Dograh makes the bucket anonymous (PLAN B5). | Pending product-owner review |
