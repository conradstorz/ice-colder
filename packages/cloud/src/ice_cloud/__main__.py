"""Run the cloud portal stub: dev only, no auth (cloud spec sections 8-9)."""

import os
from pathlib import Path

import uvicorn

from ice_cloud.app import create_app

if __name__ == "__main__":
    uvicorn.run(
        create_app(data_dir=Path(os.environ.get("ICE_CLOUD_DATA", "cloud_data"))),
        host=os.environ.get("ICE_CLOUD_HOST", "127.0.0.1"),
        port=26124,
    )
