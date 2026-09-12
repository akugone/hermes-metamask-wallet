---
name: safe-flows
description: How to drive MetaMask Agent Wallet from a Hermes conversation safely (setup, reads, writes, 2FA).
---

# MetaMask Agent Wallet — safe flows

The user never types shell commands. You call the `mm_*` tools; MetaMask holds the keys.

## Always start with `mm_status`
Its `next_step` tells you what is missing (CLI, sign-in, wallet). Follow it literally.

## Setup, step by step (`mm_setup`)
1. `install_cli` — Hermes will ask the user to approve the npm install.
2. `login_start` — show the URL. The user signs in (Google, email or MetaMask) and gets a CLI token.
3. `login_complete` with the pasted token. Never store or repeat the token.
4. Ask which wallet mode and trading mode they want, explain the difference in two lines, then `init`.
   - server-wallet + guard is the recommended default. Beast mode and BYOK trigger an approval prompt.
   - Never ask for a seed phrase in the chat. BYOK reads `MM_MNEMONIC` from the environment only.
5. `create_wallet` if `init` says no wallet exists yet.

## Reads are free, writes are gated twice
`mm_balance`, `mm_market`, `mm_history`, `mm_swap_quote`, `mm_requests` never move funds.
`mm_transfer`, `mm_swap_execute`, `mm_sign` each go through the Hermes approval prompt (the user
clicks) and then MetaMask's policy, which may add a 2FA on their phone or email.

Before any write:
- Restate recipient, amount, token and chain in plain words and get a yes. Chains are ids
  (1 Ethereum, 8453 Base, 42161 Arbitrum, 10 Optimism, 137 Polygon). Ask if unsure.
- For swaps, show the `mm_swap_quote` first (output, min output, fees, price impact) and execute by
  `quote_id`.
- For signatures, explain what the message or typed data authorises. A permit can spend tokens.
- Never call a write tool twice for the same request. If it was blocked, tell the user why and stop.

## After a write
- `status: CONFIRMED` with `tx_hash` — report the hash and the explorer link.
- `status: AWAITING_MFA` — tell the user to approve on MetaMask Mobile or via the email link. Do not
  retry. The plugin watches in the background and will post a "MetaMask Agent Wallet update" notice;
  relay it in one sentence. `mm_requests action='watch'` checks on demand.
- An `error.hint` is present — follow it before asking the user.

## Reporting
Quote amounts, fees and chain names exactly as returned; do not round silently.
