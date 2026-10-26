"""MongoDB connection settings (password can come from CyberArk)."""

import os
from dataclasses import dataclass

MONGO_PASSWORD_OBJ = os.environ.get("OBJ_MONGO_PASSWORD_SAAS", "MONGO_NG4T5E57_SAAS")


@dataclass(frozen=True)
class ConfigMongo:
    host: str
    port: int
    user: str
    database: str
    replicaset: str
    password: str  # resolved lazily; empty string = use MONGO_PASSWORD / CyberArk

    def uri(self, password: str) -> str:
        auth = f"{self.user}:{password}@" if self.user else ""
        rs = f"?replicaSet={self.replicaset}" if self.replicaset else ""
        return f"mongodb://{auth}{self.host}:{self.port}/{self.database}{rs}"


CONFIG_MONGO = ConfigMongo(
    host=os.environ.get("MONGO_HOST", "ng4t5e57-dds.dbstg.paic.com.cn"),
    port=int(os.environ.get("MONGO_PORT", "14444")),
    user=os.environ.get("MONGO_USER_SAAS", "temple_saas"),
    database=os.environ.get("MONGO_DBNAME_SAAS", "temple_saas"),
    replicaset=os.environ.get("MONGO_REPLICASET", "ng4t5e57"),
    password=os.environ.get("MONGO_PASSWORD", ""),
)
