# Snowmass production contract fixtures

The executable shadow fixture is generated in a temporary directory by
`scripts/test_snowmass_shadow_gate.py`. It deliberately makes zero network or
paid-model calls and exercises the complete evidence chain: live rights,
environment lock, prepared/revision/translation/render stages, semantic,
structural and visual receipts, package state, and tamper rejection.

Future regression cases belong here when a new document shape is found. A new
shape must first fail closed, then receive an explicit classifier or contract
rule; it must never be accepted by weakening an existing gate.
