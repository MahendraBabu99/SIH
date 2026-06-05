---
artifact_key: lsa.secrets
name: LSA Secrets
category: Credentials
function: lsa.secrets
description: Local Security Authority secret records decrypted from registry hives when keys
  are available.
order: 1440
recommended: false
default_mode: parse_only
---

Sensitive credential material. Parse only when explicitly needed and avoid sending secret values to AI unless case policy allows it. Use metadata and secret names to guide manual review.
