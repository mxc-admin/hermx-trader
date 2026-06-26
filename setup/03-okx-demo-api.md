# 03 OKX Demo API

Goal: connect OKX sandbox/demo before any real account.

User provides:

- API key
- API secret
- API passphrase
- sandbox/demo confirmation
- optional trusted IP setup

Agent verifies:

- credentials load from `.env`
- account read works
- target instruments exist
- isolated margin is available
- leverage can be set to 2x

Never:

- put keys in strategy JSON
- put keys in docs
- print keys in logs

