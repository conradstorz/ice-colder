"""Run the cloud portal (identity spec section 8: configuration comes from the environment)."""

import os
import sys

import uvicorn

from ice_cloud.app import create_app
from ice_cloud.settings import CloudSettings, SettingsError

if __name__ == "__main__":
    try:
        settings = CloudSettings.from_env(os.environ)
    except SettingsError as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)
    uvicorn.run(
        create_app(settings),
        host=os.environ.get("ICE_CLOUD_HOST", "127.0.0.1"),
        port=26124,
    )
