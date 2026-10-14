"""Authentication: deliberately absent (cloud spec section 8).

The stub is for development only and must not be deployed anywhere reachable
from beyond localhost. There is no authentication: anyone who can reach the
port can read every machine's report and push reports for any machine_id; a
pushed bundle's static files are served from the portal's origin; the
selection cookie is not a credential. Uploaded bundle templates also
receive the full template context, including `request` (and so
`request.app.state`: the store, the registry, the clock); that is an
escalation path once auth exists, so sandboxed bundles must get a restricted
context (no `request`) before auth ships. Auth and mTLS (layer-contracts spec
section 4) are required before any deployment beyond localhost.
"""
