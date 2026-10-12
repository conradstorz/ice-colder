"""
Masking (brief resolution 4): a SecretStr that is already set must never
round-trip its real value through the browser. Every masked field renders
the fixed `SECRET_MASK` placeholder instead, and `_secret_changed` is the
one helper every masked field's POST handler calls to decide whether the
submitted value is a genuinely new secret (overwrite) or the unchanged
placeholder / a blank left untouched (leave the stored value alone).
Repeating that comparison inline per field is exactly how one field would
end up missing the check, so every page below calls this same function.
"""

# Fixed placeholder rendered for any secret that is already set, real value
# or dummy default alike — never the actual secret text (brief resolution
# 4). A constant length that does not hint at the real secret's length.
SECRET_MASK = "********"


def _secret_placeholder(secret) -> str:
    """The value a masked <input> should render: the fixed mask when
    *secret* is already set (an Optional field that is None renders empty,
    so a first value can be typed)."""
    return SECRET_MASK if secret is not None else ""


def _secret_changed(submitted: str) -> bool:
    """True when *submitted* is a genuinely new secret value rather than
    the unchanged mask this page renders for an already-set secret, or a
    blank the user left untouched. Shared by every masked field on
    Payments, Comms and MQTT (brief resolution 4) so the same rule applies
    everywhere — an empty submission is treated as "leave alone" rather
    than a request to clear the credential, since nothing in this task
    asks for an explicit clear action and silently wiping a credential
    because a field arrived blank would be the destructive mistake this
    rule exists to prevent.
    """
    return submitted != SECRET_MASK and submitted != ""
