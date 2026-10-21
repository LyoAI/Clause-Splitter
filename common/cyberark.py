"""CyberArk credential vault client (from the PingAn http-server template).

Fetches an encrypted password from the platform and decrypts it (AES-CBC).
Connection details are overridable via CYBERARK_* environment variables.
"""

import hashlib
import json
import os

import requests
from Crypto.Cipher import AES
from binascii import a2b_hex

requests.packages.urllib3.disable_warnings()


class Cyberark:
    def __init__(self):
        self.url = os.environ.get("CYBERARK_URL", "https://dev-ccp.paic.com.cn/pidms/rest/pwd/getPassword")
        self.appid = os.environ.get("CYBERARK_APPID", "App_PIB_CORE__ba56f8")
        self.safe = os.environ.get("CYBERARK_SAFE", "AIM_PIB_CORE")
        self.folder = os.environ.get("CYBERARK_FOLDER", "root")
        self.key = os.environ.get("CYBERARK_KEY", "e9a6a0dba21f0d2d")
        self.encrypt_ver = os.environ.get("CYBERARK_DECODE_VERSION", "2.0")
        self.cbc_iv = "pidms20180327!@#"
        self.signvalue = hashlib.sha256((self.appid + "&" + self.key).encode("utf8")).hexdigest()
        self.postdata = {
            "appid": self.appid,
            "safe": self.safe,
            "folder": self.folder,
            "sign": self.signvalue,
            "encrypt_ver": self.encrypt_ver,
        }

    def decode_password(self, key: str, psw: str) -> str:
        cipher = AES.new(key.encode(), AES.MODE_CBC, self.cbc_iv.encode())
        ret = cipher.decrypt(a2b_hex(psw)).decode("utf-8")
        return ret.replace(ret[-1], "") if ret else ""

    def get_password(self, query_object: str) -> str:
        """Fetch and decrypt the password for the given CyberArk object name."""
        self.postdata["objectname"] = query_object
        resp = requests.post(self.url, json=self.postdata, verify=False)
        content = json.loads(resp.content.decode())
        if str(content.get("code")) != "0":
            raise RuntimeError(f"CyberArk returned code={content.get('code')}, msg={content.get('msg')}")
        body = content["data"]
        return self.decode_password(body["key"], body["psw"])

    def get_password_mongo(self, query_object: str) -> str:
        return self.get_password(query_object)
