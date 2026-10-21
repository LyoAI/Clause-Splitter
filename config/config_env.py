"""Runtime environment identifiers (from the PingAn http-server template)."""

import os
from dotenv import load_dotenv

from clause_splitter.config import REPO_ROOT

load_dotenv(REPO_ROOT / ".env")

RUN_ENV = os.environ.get("RUN_ENV", "dev")
DEPLOYMENT_ENV = os.environ.get("MODEL_ENV", "dev")
CAAS_SERVICE = os.environ.get("caas_service")
